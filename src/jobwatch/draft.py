"""Génération assistée d'une lettre de motivation : bouton « Générer LM » du dashboard.

Pour un match donné, assemble le texte de l'offre (récupéré à la demande via la
mécanique d'enrich s'il manque), le texte du CV choisi dans la bibliothèque et
les lettres exemples .tex de la piste métier, puis demande à un LLM (OpenCode)
d'écrire le document LaTeX complet. Le .tex est compilé avec lualatex (avec une
boucle de réparation LLM en cas d'erreur), rendu en PNG par pdftoppm pour
l'aperçu mobile, et enregistré dans la bibliothèque de documents comme lettre
de motivation prête pour le formulaire Candidater.

Chaque génération est un job persistant en base (table draft_job) exécuté dans
un thread du serveur : le dashboard sonde son état et survit au verrouillage de
l'iPhone comme au rechargement de la page.
"""

from __future__ import annotations

import datetime
import logging
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from pathlib import Path

import httpx

from jobwatch.config import DraftConfig
from jobwatch.db import connect
from jobwatch.enrich import _extract_text, _fetch_and_convert
from jobwatch.library import documents_dir

log = logging.getLogger(__name__)

LLM_TIMEOUT_SECONDS = 300
# Deux générations simultanées au plus : un batch post-tri de 15 lettres ne doit
# pas lancer 15 sous-processus opencode + lualatex d'un coup sur le VPS.
MAX_CONCURRENT_JOBS = 2
_job_slots = threading.Semaphore(MAX_CONCURRENT_JOBS)
COMPILE_TIMEOUT_SECONDS = 120
REPAIR_ATTEMPTS = 2
LOG_TAIL_CHARS = 3000
PNG_DPI = 150
MAX_LABEL_LENGTH = 80

WARNING_NO_CONTENT = (
    "Lettre générée sans le texte complet de l'offre (page irrécupérable) : "
    "seuls le titre, la société et le résumé ont été fournis au modèle."
)

_MONTHS_FR = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)

PROMPT = (
    "Rédige une lettre de motivation en français pour l'offre d'emploi décrite dans la "
    "section OFFRE du fichier joint, au nom du candidat décrit dans la section CV. "
    "Les sections EXEMPLE contiennent de vraies lettres du candidat : imite fidèlement "
    "leur format LaTeX (préambule, mise en page, formules d'ouverture et de clôture, "
    "signature) et leur ton. Réponds uniquement avec le document LaTeX complet, de "
    "\\documentclass à \\end{{document}}, sans texte autour. Contraintes strictes : "
    "une seule page ; aucune image ni \\includegraphics ; date : {date} ; "
    "n'invente aucun fait absent du CV."
)

REGENERATE_PROMPT_SUFFIX = (
    " La section LETTRE PRÉCÉDENTE contient la version à retravailler ; consigne du "
    "candidat : {instruction}"
)

REPAIR_PROMPT = (
    "Le document LaTeX de la section LETTRE contient des erreurs : la compilation "
    "lualatex échoue avec le log de la section ERREUR. Corrige le document et réponds "
    "uniquement avec le document LaTeX complet corrigé, de \\documentclass à "
    "\\end{document}, sans texte autour."
)

_LATEX_FENCE_RE = re.compile(r"```(?:latex|tex)?\s*\n(.*?)```", re.DOTALL)


class DraftError(Exception):
    """Échec attendu de la génération ; le message est stocké dans draft_job.error."""


def french_date(today: datetime.date) -> str:
    return f"{today.day} {_MONTHS_FR[today.month - 1]} {today.year}"


def _load_match(conn: sqlite3.Connection, match_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT m.id AS id, o.id AS offer_id, o.title AS title, o.url AS url, "
        "       o.location AS location, c.name AS company "
        "FROM match m "
        "JOIN offer o ON o.id = m.offer_id "
        "LEFT JOIN company c ON c.id = o.company_id "
        "WHERE m.id = ?",
        (match_id,),
    ).fetchone()
    if row is None:
        raise DraftError(f"aucun match avec l'id {match_id}")
    return row


def _offer_markdown(conn: sqlite3.Connection, offer_id: int, url: str) -> str | None:
    """Renvoie le texte de l'offre, en le récupérant à la demande s'il manque en base."""
    row = conn.execute(
        "SELECT markdown, status FROM offer_content WHERE offer_id = ?", (offer_id,)
    ).fetchone()
    if row is not None and row["status"] == "ok" and row["markdown"]:
        return str(row["markdown"])
    with httpx.Client(timeout=30.0) as client:
        markdown, fetch_method = _fetch_and_convert(url, client)
    if markdown is None:
        return None
    if row is None:
        conn.execute(
            "INSERT INTO offer_content (offer_id, markdown, fetch_method, status) "
            "VALUES (?, ?, ?, 'ok')",
            (offer_id, markdown, fetch_method),
        )
    else:
        conn.execute(
            "UPDATE offer_content SET markdown = ?, fetch_method = ?, status = 'ok', "
            "fetched_at = datetime('now') WHERE offer_id = ?",
            (markdown, fetch_method, offer_id),
        )
    conn.commit()
    return markdown


def _offer_fallback(conn: sqlite3.Connection, match: sqlite3.Row) -> str:
    """Contexte minimal (titre, société, lieu, résumé) quand la page est irrécupérable."""
    lines = [
        f"Titre du poste : {match['title']}",
        f"Société : {match['company'] or 'inconnue'}",
    ]
    if match["location"]:
        lines.append(f"Lieu : {match['location']}")
    bullets = conn.execute(
        "SELECT sb.text AS text FROM offer_summary os "
        "JOIN summary_bullet sb ON sb.summary_id = os.id "
        "WHERE os.offer_id = ? ORDER BY sb.position",
        (int(match["offer_id"]),),
    ).fetchall()
    if bullets:
        lines.append("Résumé de l'offre :")
        lines.extend(f"- {row['text']}" for row in bullets)
    return "\n".join(lines)


def _cv_text(conn: sqlite3.Connection, cv_library_id: int) -> str:
    row = conn.execute(
        "SELECT file_path FROM document_library WHERE id = ? AND type = 'cv'",
        (cv_library_id,),
    ).fetchone()
    if row is None:
        raise DraftError(f"aucun CV avec l'id de bibliothèque {cv_library_id}")
    path = Path(str(row["file_path"]))
    if not path.exists():
        raise DraftError(f"fichier CV introuvable : {path}")
    if path.suffix.lower() == ".pdf":
        try:
            completed = subprocess.run(
                ["pdftotext", "-layout", str(path), "-"],
                capture_output=True, text=True, timeout=60, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DraftError(f"pdftotext a échoué sur {path.name} : {exc}") from exc
        if completed.returncode != 0:
            raise DraftError(f"pdftotext a échoué sur {path.name} (code {completed.returncode})")
        text = completed.stdout.strip()
    else:
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            raise DraftError(f"lecture du CV impossible : {exc}") from exc
    if not text:
        raise DraftError(f"texte du CV vide après extraction : {path.name}")
    return text


def _example_texts(config: DraftConfig, track: str) -> list[str]:
    texts = []
    for path in config.examples.get(track, []):
        try:
            texts.append(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise DraftError(f"lettre exemple illisible : {path} ({exc})") from exc
    return texts


def _build_bundle(
    offer_text: str,
    cv_text: str,
    examples: list[str],
    previous_tex: str | None,
) -> str:
    """Assemble le fichier joint unique passé au LLM, sectionné et sans ambiguïté."""
    parts = [f"# OFFRE\n\n{offer_text}", f"# CV\n\n{cv_text}"]
    parts.extend(
        f"# EXEMPLE {index}\n\n{text}" for index, text in enumerate(examples, start=1)
    )
    if previous_tex is not None:
        parts.append(f"# LETTRE PRÉCÉDENTE\n\n{previous_tex}")
    return "\n\n".join(parts)


def _call_llm(config: DraftConfig, prompt: str, attachment: str) -> str:
    # Pièce jointe via --file et non en argument : le noyau limite chaque
    # argument à ~128 Ko (MAX_ARG_STRLEN), un bundle offre+CV+exemples dépasse.
    with tempfile.TemporaryDirectory() as tmp_dir:
        bundle_path = Path(tmp_dir) / "bundle.md"
        bundle_path.write_text(attachment, encoding="utf-8")
        try:
            completed = subprocess.run(
                [
                    config.opencode_bin,
                    "run",
                    "--model",
                    config.model,
                    "--format",
                    "json",
                    f"--file={bundle_path}",
                    "--",
                    prompt,
                ],
                capture_output=True,
                text=True,
                timeout=LLM_TIMEOUT_SECONDS,
                check=False,
                cwd=tmp_dir,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DraftError(f"appel OpenCode échoué : {exc}") from exc
    if completed.returncode != 0:
        raise DraftError(f"OpenCode a quitté avec le code {completed.returncode}")
    text = _extract_text(completed.stdout)
    if not text.strip():
        raise DraftError("réponse vide du modèle")
    return text


def extract_latex(text: str) -> str:
    """Extrait le document LaTeX d'une réponse LLM (bloc ```latex``` ou texte brut)."""
    for match in _LATEX_FENCE_RE.finditer(text):
        if "\\documentclass" in match.group(1):
            text = match.group(1)
            break
    start = text.find("\\documentclass")
    end = text.rfind("\\end{document}")
    if start == -1 or end == -1:
        raise DraftError("la réponse du modèle ne contient pas de document LaTeX complet")
    return text[start : end + len("\\end{document}")].strip() + "\n"


def compile_latex(tex: str, work_dir: Path) -> Path | str:
    """Compile le .tex dans work_dir ; renvoie le chemin du PDF, ou le log d'erreur."""
    tex_path = work_dir / "lettre.tex"
    tex_path.write_text(tex, encoding="utf-8")
    try:
        completed = subprocess.run(
            ["lualatex", "-interaction=nonstopmode", "-halt-on-error", tex_path.name],
            capture_output=True,
            text=True,
            timeout=COMPILE_TIMEOUT_SECONDS,
            check=False,
            cwd=work_dir,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DraftError(f"lualatex n'a pas pu être lancé : {exc}") from exc
    pdf_path = work_dir / "lettre.pdf"
    if completed.returncode == 0 and pdf_path.exists():
        return pdf_path
    log_path = work_dir / "lettre.log"
    log_text = ""
    if log_path.exists():
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
    return (log_text or completed.stdout or "échec lualatex sans log")[-LOG_TAIL_CHARS:]


def render_pngs(pdf_path: Path, target_dir: Path, stem: str) -> int:
    """Rend chaque page du PDF en PNG <stem>-<n>.png ; renvoie le nombre de pages."""
    for stale in target_dir.glob(f"{stem}-*.png"):
        stale.unlink()
    try:
        completed = subprocess.run(
            ["pdftoppm", "-png", "-r", str(PNG_DPI), str(pdf_path), str(target_dir / stem)],
            capture_output=True,
            text=True,
            timeout=COMPILE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DraftError(f"pdftoppm a échoué : {exc}") from exc
    if completed.returncode != 0:
        raise DraftError(f"pdftoppm a quitté avec le code {completed.returncode}")
    pages = sorted(target_dir.glob(f"{stem}-*.png"))
    if not pages:
        raise DraftError("pdftoppm n'a produit aucune page")
    # pdftoppm nomme <stem>-1.png ... ou <stem>-01.png selon le nombre de pages :
    # renomme vers un schéma stable <stem>-<n>.png sans zéro initial.
    for index, path in enumerate(pages, start=1):
        stable = target_dir / f"{stem}-{index}.png"
        if path != stable:
            path.rename(stable)
    return len(pages)


def _generate_tex(
    config: DraftConfig, prompt: str, bundle: str, work_dir: Path
) -> tuple[str, Path]:
    """Boucle génération + réparation : renvoie (tex, chemin du PDF compilé)."""
    tex = extract_latex(_call_llm(config, prompt, bundle))
    for attempt in range(REPAIR_ATTEMPTS + 1):
        result = compile_latex(tex, work_dir)
        if isinstance(result, Path):
            return tex, result
        if attempt == REPAIR_ATTEMPTS:
            raise DraftError(
                f"compilation LaTeX en échec après {REPAIR_ATTEMPTS} réparation(s) : "
                f"{result[-500:]}"
            )
        log.warning("draft: compilation échouée, tentative de réparation %d", attempt + 1)
        repair_bundle = f"# LETTRE\n\n{tex}\n\n# ERREUR\n\n{result}"
        tex = extract_latex(_call_llm(config, REPAIR_PROMPT, repair_bundle))
    raise AssertionError("unreachable")


def _upsert_library_entry(
    conn: sqlite3.Connection, match_id: int, label: str, pdf_path: Path
) -> int:
    """Réutilise l'entrée cover_letter du brouillon précédent de ce match, sinon en crée une."""
    row = conn.execute(
        "SELECT library_id FROM draft_job "
        "WHERE match_id = ? AND library_id IS NOT NULL ORDER BY id DESC LIMIT 1",
        (match_id,),
    ).fetchone()
    if row is not None:
        library_id = int(row["library_id"])
        conn.execute(
            "UPDATE document_library SET label = ?, file_path = ?, "
            "uploaded_at = datetime('now') WHERE id = ?",
            (label, str(pdf_path), library_id),
        )
        return library_id
    cur = conn.execute(
        "INSERT INTO document_library (type, label, file_path) VALUES ('cover_letter', ?, ?)",
        (label, str(pdf_path)),
    )
    return int(cur.lastrowid)


def _label_for(match: sqlite3.Row) -> str:
    label = f"LM {match['company'] or 'Société inconnue'} - {match['title']}"
    if len(label) > MAX_LABEL_LENGTH:
        label = label[: MAX_LABEL_LENGTH - 1] + "…"
    return label


def _previous_tex(conn: sqlite3.Connection, match_id: int) -> str | None:
    row = conn.execute(
        "SELECT tex_path FROM draft_job "
        "WHERE match_id = ? AND status = 'ok' AND tex_path IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (match_id,),
    ).fetchone()
    if row is None:
        return None
    path = Path(str(row["tex_path"]))
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def _finish_job(conn: sqlite3.Connection, job_id: int, **fields: object) -> None:
    assignments = ", ".join(f"{name} = ?" for name in fields)
    conn.execute(
        f"UPDATE draft_job SET {assignments}, finished_at = datetime('now') WHERE id = ?",
        (*fields.values(), job_id),
    )
    conn.commit()


def run_job(db_path: Path, config: DraftConfig, job_id: int) -> None:
    """Exécute un job de génération de bout en bout ; toute erreur finit dans draft_job.error.

    Conçu pour tourner dans un thread du serveur : ouvre sa propre connexion
    SQLite et ne lève jamais (le résultat, succès ou échec, est en base).
    """
    try:
        conn = connect(db_path)
    except sqlite3.Error:
        log.exception("draft: connexion SQLite impossible pour le job %d", job_id)
        return
    try:
        with _job_slots:
            try:
                conn.execute(
                    "UPDATE draft_job SET status = 'running' WHERE id = ? AND status = 'queued'",
                    (job_id,),
                )
                conn.commit()
                job = conn.execute(
                    "SELECT * FROM draft_job WHERE id = ?", (job_id,)
                ).fetchone()
                if job is None:
                    raise DraftError(f"job {job_id} introuvable")
                _run_job_inner(conn, db_path, config, job)
            except DraftError as exc:
                _finish_job(conn, job_id, status="failed", error=str(exc))
            except Exception as exc:  # un bug ne doit jamais laisser un job bloqué en 'running'
                log.exception("draft: échec inattendu du job %d", job_id)
                _finish_job(conn, job_id, status="failed", error=f"erreur interne : {exc}")
    finally:
        conn.close()


def _run_job_inner(
    conn: sqlite3.Connection, db_path: Path, config: DraftConfig, job: sqlite3.Row
) -> None:
    job_id = int(job["id"])
    match_id = int(job["match_id"])
    track = str(job["track"])
    if job["cv_library_id"] is None:
        raise DraftError("aucun CV sélectionné")
    match = _load_match(conn, match_id)

    warning = None
    offer_text = _offer_markdown(conn, int(match["offer_id"]), str(match["url"]))
    if offer_text is None:
        offer_text = _offer_fallback(conn, match)
        warning = WARNING_NO_CONTENT

    cv_text = _cv_text(conn, int(job["cv_library_id"]))
    examples = _example_texts(config, track)

    instruction = str(job["instruction"]).strip() if job["instruction"] else ""
    previous_tex = _previous_tex(conn, match_id) if instruction else None
    today = datetime.datetime.now(tz=datetime.UTC).astimezone().date()
    prompt = PROMPT.format(date=french_date(today))
    if previous_tex is not None:
        prompt += REGENERATE_PROMPT_SUFFIX.format(instruction=instruction)
    elif instruction:
        prompt += f" Consigne du candidat : {instruction}"
    bundle = _build_bundle(offer_text, cv_text, examples, previous_tex)

    target_dir = documents_dir(db_path)
    with tempfile.TemporaryDirectory() as tmp_dir:
        work_dir = Path(tmp_dir)
        tex, pdf_path = _generate_tex(config, prompt, bundle, work_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        stem = f"draft_{match_id}"
        final_tex = target_dir / f"{stem}.tex"
        final_pdf = target_dir / f"{stem}.pdf"
        final_tex.write_text(tex, encoding="utf-8")
        shutil.copyfile(pdf_path, final_pdf)
        pages = render_pngs(final_pdf, target_dir, stem)

    library_id = _upsert_library_entry(conn, match_id, _label_for(match), final_pdf)
    _finish_job(
        conn,
        job_id,
        status="ok",
        warning=warning,
        tex_path=str(final_tex),
        pdf_path=str(final_pdf),
        png_pages=pages,
        library_id=library_id,
    )
