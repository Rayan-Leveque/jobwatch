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
import gzip
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from importlib import resources
from pathlib import Path

import httpx

from jobwatch.config import DRAFT_TRACKS, DraftConfig
from jobwatch.db import connect
from jobwatch.enrich import _extract_text, _fetch_and_extract
from jobwatch.library import documents_dir, ensure_private_directory, protect_private_file
from jobwatch.llm_runner import LLMRunnerError, run_codex, run_opencode
from jobwatch.profile import draft_profile_context

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

BODY_START_MARKER = "% JOBWATCH:BODY_START"
BODY_END_MARKER = "% JOBWATCH:BODY_END"

_BODY_MARKERS_INSTRUCTION = (
    "Encadre le corps de la lettre - du paragraphe d'ouverture au paragraphe de "
    "clôture inclus, hors date, en-tête destinataire/société et bloc de signature - "
    "par deux lignes de commentaire LaTeX seules sur leur ligne : "
    f"`{BODY_START_MARKER}` juste avant, `{BODY_END_MARKER}` juste après. Le texte "
    "entre ces deux marqueurs doit être du texte brut (paragraphes séparés par une "
    "ligne vide), sans aucune commande LaTeX de mise en forme (pas de \\textbf, "
    "\\emph, \\begin{{itemize}}...) ; seuls les caractères spéciaux LaTeX "
    "(% & _ # $ {{ }} ^ ~ \\) y sont échappés normalement."
)

PROMPT = (
    "Rédige une lettre de motivation en français pour l'offre d'emploi décrite dans la "
    "section OFFRE du document fourni, au nom du candidat décrit dans la section CV. "
    "Les sections EXEMPLE contiennent de vraies lettres du candidat : imite fidèlement "
    "leur format LaTeX (préambule, mise en page, formules d'ouverture et de clôture, "
    "signature) et leur ton. Réponds uniquement avec le document LaTeX complet, de "
    "\\documentclass à \\end{{document}}, sans texte autour. Contraintes strictes : "
    "une seule page ; aucune image ni \\includegraphics ; date : {date} ; "
    "La section PROFIL PERSONNEL, lorsqu'elle existe, contient des préférences et des faits "
    "facultatifs fournis par le candidat : utilise les éléments pertinents sans les forcer. "
    "N'invente aucun fait absent du CV et du PROFIL PERSONNEL. Si cette section est absente, "
    "rédige sobrement à partir du seul CV. " + _BODY_MARKERS_INSTRUCTION
)

REGENERATE_PROMPT_SUFFIX = (
    " La section LETTRE PRÉCÉDENTE contient la version à retravailler ; consigne du "
    "candidat : {instruction}"
)

REPAIR_PROMPT = (
    "Le document LaTeX de la section LETTRE contient des erreurs : la compilation "
    "lualatex échoue avec le log de la section ERREUR. Corrige le document et réponds "
    "uniquement avec le document LaTeX complet corrigé, de \\documentclass à "
    "\\end{document}, sans texte autour. Si les marqueurs "
    f"{BODY_START_MARKER} / {BODY_END_MARKER} sont présents, conserve-les à leur "
    "position autour du corps de la lettre."
)

_LATEX_FENCE_RE = re.compile(r"```(?:latex|tex)?\s*\n(.*?)```", re.DOTALL)

_LATEX_ESCAPE_MAP = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "$": r"\$",
    "&": r"\&",
    "#": r"\#",
    "^": r"\textasciicircum{}",
    "_": r"\_",
    "~": r"\textasciitilde{}",
    "%": r"\%",
}
_LATEX_UNESCAPE_MAP = {tex: char for char, tex in _LATEX_ESCAPE_MAP.items()}
_LATEX_UNESCAPE_RE = re.compile(
    "|".join(re.escape(tex) for tex in sorted(_LATEX_UNESCAPE_MAP, key=len, reverse=True))
)

_BODY_RE = re.compile(
    re.escape(BODY_START_MARKER) + r"\n(.*?)\n" + re.escape(BODY_END_MARKER), re.DOTALL
)

_DEFAULT_EXAMPLE_FILE = "default_letter_example.tex"


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
        extracted, fetch_method, html = _fetch_and_extract(url, client)
    if extracted is None:
        return None
    html_gz = gzip.compress(html.encode("utf-8")) if html else None
    if row is None:
        conn.execute(
            "INSERT INTO offer_content (offer_id, markdown, fetch_method, extract_method, "
            "html_gz, status, fetch_attempts) VALUES (?, ?, ?, ?, ?, 'ok', 1)",
            (offer_id, extracted.markdown, fetch_method, extracted.method, html_gz),
        )
    else:
        conn.execute(
            "UPDATE offer_content SET markdown = ?, fetch_method = ?, extract_method = ?, "
            "html_gz = ?, status = 'ok', fetch_attempts = fetch_attempts + 1, "
            "failure_reason = NULL, fetched_at = datetime('now') WHERE offer_id = ?",
            (extracted.markdown, fetch_method, extracted.method, html_gz, offer_id),
        )
    conn.commit()
    return extracted.markdown


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


def _default_example_text() -> str:
    return resources.files("jobwatch").joinpath(_DEFAULT_EXAMPLE_FILE).read_text(
        encoding="utf-8"
    )


def _library_example_texts(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT file_path FROM document_library WHERE type = 'letter_example' "
        "ORDER BY uploaded_at DESC, id DESC"
    ).fetchall()
    texts = []
    for row in rows:
        path = Path(str(row["file_path"]))
        try:
            texts.append(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            raise DraftError(f"lettre exemple illisible : {path} ({exc})") from exc
    return texts


def _example_texts(conn: sqlite3.Connection, config: DraftConfig, track: str) -> list[str]:
    """Résout les lettres exemples : config.examples, puis bibliothèque, puis modèle générique."""
    paths = config.examples.get(track)
    if not paths and track == "all":
        paths = list(
            dict.fromkeys(
                path for key in DRAFT_TRACKS for path in config.examples.get(key, [])
            )
        )
    if paths:
        texts = []
        for path in paths:
            try:
                texts.append(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as exc:
                raise DraftError(f"lettre exemple illisible : {path} ({exc})") from exc
        return texts

    library_texts = _library_example_texts(conn)
    if library_texts:
        return library_texts

    return [_default_example_text()]


def _build_bundle(
    offer_text: str,
    cv_text: str,
    examples: list[str],
    previous_tex: str | None,
    profile_context: str | None = None,
) -> str:
    """Assemble le fichier joint unique passé au LLM, sectionné et sans ambiguïté."""
    parts = [f"# OFFRE\n\n{offer_text}", f"# CV\n\n{cv_text}"]
    if profile_context is not None:
        parts.append(f"# PROFIL PERSONNEL\n\n{profile_context}")
    parts.extend(
        f"# EXEMPLE {index}\n\n{text}" for index, text in enumerate(examples, start=1)
    )
    if previous_tex is not None:
        parts.append(f"# LETTRE PRÉCÉDENTE\n\n{previous_tex}")
    return "\n\n".join(parts)


CODEX_PREAMBLE = (
    "Le bloc <stdin> ci-dessous contient le document fourni (sections OFFRE, CV, "
    "EXEMPLE...). N'exécute aucune commande et ne lis aucun fichier : réponds "
    "directement. "
)
CODEX_TIMEOUT_SECONDS = 600


def _call_llm(config: DraftConfig, prompt: str, attachment: str) -> str:
    if config.runner == "codex":
        return _call_codex(config, prompt, attachment)
    return _call_opencode(config, prompt, attachment)


def _call_codex(config: DraftConfig, prompt: str, attachment: str) -> str:
    try:
        text = run_codex(binary=config.codex_bin, model=config.model,
                         prompt=CODEX_PREAMBLE + prompt, attachment=attachment,
                         timeout=CODEX_TIMEOUT_SECONDS, variant=config.variant)
    except LLMRunnerError as exc:
        raise DraftError(str(exc)) from exc
    if not text.strip():
        raise DraftError("réponse vide du modèle")
    return text


def _call_opencode(config: DraftConfig, prompt: str, attachment: str) -> str:
    try:
        stdout = run_opencode(binary=config.opencode_bin, model=config.model,
                              prompt=prompt, attachment=attachment,
                              timeout=LLM_TIMEOUT_SECONDS, attachment_name="bundle.md")
    except LLMRunnerError as exc:
        raise DraftError(str(exc)) from exc
    text = _extract_text(stdout)
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


def escape_latex_body(text: str) -> str:
    """Échappe les caractères spéciaux LaTeX d'un texte brut, hors accents (natifs en UTF-8)."""
    return "".join(_LATEX_ESCAPE_MAP.get(char, char) for char in text)


def unescape_latex_body(text: str) -> str:
    """Inverse escape_latex_body pour afficher le corps d'une lettre en texte brut."""
    return _LATEX_UNESCAPE_RE.sub(lambda m: _LATEX_UNESCAPE_MAP[m.group(0)], text)


def extract_body(tex: str) -> str | None:
    """Renvoie le corps éditable (texte brut) d'un .tex, ou None si les marqueurs sont absents."""
    match = _BODY_RE.search(tex)
    if match is None:
        return None
    return unescape_latex_body(match.group(1))


def splice_body(tex: str, body_text: str) -> str:
    """Remplace le corps entre les marqueurs par body_text (échappé), le reste inchangé."""
    match = _BODY_RE.search(tex)
    if match is None:
        raise DraftError(
            "cette lettre ne contient pas de section modifiable (générée avant l'éditeur)"
        )
    escaped = escape_latex_body(body_text)
    return tex[: match.start(1)] + escaped + tex[match.end(1) :]


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


def _pdf_page_count(pdf_path: Path) -> int:
    """Renvoie le nombre de pages du PDF via pdfinfo (même paquet poppler que pdftoppm)."""
    try:
        completed = subprocess.run(
            ["pdfinfo", str(pdf_path)],
            capture_output=True,
            text=True,
            timeout=COMPILE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DraftError(f"pdfinfo a échoué : {exc}") from exc
    if completed.returncode != 0:
        raise DraftError(f"pdfinfo a quitté avec le code {completed.returncode}")
    for line in completed.stdout.splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":", 1)[1].strip())
    raise DraftError("pdfinfo n'a pas indiqué le nombre de pages")


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
    examples = _example_texts(conn, config, track)
    personal_context = draft_profile_context(conn)

    instruction = str(job["instruction"]).strip() if job["instruction"] else ""
    previous_tex = _previous_tex(conn, match_id) if instruction else None
    today = datetime.datetime.now(tz=datetime.UTC).astimezone().date()
    prompt = PROMPT.format(date=french_date(today))
    if previous_tex is not None:
        prompt += REGENERATE_PROMPT_SUFFIX.format(instruction=instruction)
    elif instruction:
        prompt += f" Consigne du candidat : {instruction}"
    bundle = _build_bundle(
        offer_text, cv_text, examples, previous_tex, profile_context=personal_context
    )

    target_dir = documents_dir(db_path)
    with tempfile.TemporaryDirectory() as tmp_dir:
        work_dir = Path(tmp_dir)
        tex, pdf_path = _generate_tex(config, prompt, bundle, work_dir)
        ensure_private_directory(target_dir)
        stem = f"draft_{match_id}"
        final_tex = target_dir / f"{stem}.tex"
        final_pdf = target_dir / f"{stem}.pdf"
        final_tex.write_text(tex, encoding="utf-8")
        shutil.copyfile(pdf_path, final_pdf)
        pages = render_pngs(final_pdf, target_dir, stem)
        protect_private_file(final_tex)
        protect_private_file(final_pdf)
        for page_path in target_dir.glob(f"{stem}-*.png"):
            protect_private_file(page_path)

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


def _latest_ok_job(conn: sqlite3.Connection, match_id: int) -> sqlite3.Row:
    job = conn.execute(
        "SELECT * FROM draft_job WHERE match_id = ? AND status = 'ok' AND tex_path IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (match_id,),
    ).fetchone()
    if job is None:
        raise DraftError("aucune lettre générée pour ce match")
    return job


def get_body_edit(conn: sqlite3.Connection, match_id: int) -> str:
    """Renvoie le corps éditable en texte brut de la dernière lettre générée avec succès."""
    job = _latest_ok_job(conn, match_id)
    path = Path(str(job["tex_path"]))
    if not path.exists():
        raise DraftError("fichier source .tex introuvable")
    tex = path.read_text(encoding="utf-8", errors="replace")
    body = extract_body(tex)
    if body is None:
        raise DraftError(
            "cette lettre ne contient pas de section modifiable (générée avant l'éditeur)"
        )
    return body


def _commit_letter_files(
    target_dir: Path, stem: str, tex: str, pdf_path: Path, png_paths: list[Path]
) -> tuple[Path, Path]:
    """Publie tex/pdf/pngs vers target_dir par rename atomique une fois chaque nouvelle
    version entièrement écrite, pour qu'un échec d'écriture en cours de route (disque
    plein, permission) ne laisse jamais la lettre live partiellement écrasée."""
    final_tex = target_dir / f"{stem}.tex"
    final_pdf = target_dir / f"{stem}.pdf"
    staged: list[tuple[Path, Path]] = []
    try:
        staged_tex = target_dir / f".{stem}.tex.new"
        staged_tex.write_text(tex, encoding="utf-8")
        staged.append((staged_tex, final_tex))

        staged_pdf = target_dir / f".{stem}.pdf.new"
        shutil.copyfile(pdf_path, staged_pdf)
        staged.append((staged_pdf, final_pdf))

        for png in png_paths:
            staged_png = target_dir / f".{png.name}.new"
            shutil.copyfile(png, staged_png)
            staged.append((staged_png, target_dir / png.name))

        kept_names = {dest.name for _, dest in staged}
        for src, dest in staged:
            protect_private_file(src)
            os.replace(src, dest)
        for stale in target_dir.glob(f"{stem}-*.png"):
            if stale.name not in kept_names:
                stale.unlink()
    finally:
        for src, _ in staged:
            src.unlink(missing_ok=True)
    return final_tex, final_pdf


def apply_body_edit(conn: sqlite3.Connection, db_path: Path, match_id: int, body_text: str) -> int:
    """Recompile la lettre existante avec un corps édité à la main ; renvoie l'id du nouveau job.

    Aucun appel LLM : une compilation échouée, ou un rendu dépassant une page, lève
    DraftError sans rien persister (ni fichier, ni ligne draft_job), pour laisser
    l'utilisateur corriger son texte plutôt que de retomber sur la boucle de réparation
    du modèle (réservée aux brouillons générés) ou d'écraser la lettre précédente. Un
    job 'running' est réservé le temps de l'opération pour bloquer une régénération
    concurrente sur le même match, comme le fait déjà le flux de régénération pour un
    nouveau hand-edit.
    """
    job = _latest_ok_job(conn, match_id)
    tex_path = Path(str(job["tex_path"]))
    if not tex_path.exists():
        raise DraftError("fichier source .tex introuvable")
    tex = tex_path.read_text(encoding="utf-8", errors="replace")
    new_tex = splice_body(tex, body_text)

    match = _load_match(conn, match_id)

    running = conn.execute(
        "SELECT id FROM draft_job WHERE match_id = ? AND status IN ('running', 'queued')",
        (match_id,),
    ).fetchone()
    if running is not None:
        raise DraftError("une génération est déjà en cours")

    cur = conn.execute(
        "INSERT INTO draft_job (match_id, track, cv_library_id, status) "
        "VALUES (?, ?, ?, 'running')",
        (match_id, job["track"], job["cv_library_id"]),
    )
    placeholder_id = int(cur.lastrowid)
    conn.commit()

    target_dir = documents_dir(db_path)
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            result = compile_latex(new_tex, work_dir)
            if not isinstance(result, Path):
                raise DraftError(f"compilation LaTeX en échec : {result[-500:]}")
            pages = _pdf_page_count(result)
            if pages > 1:
                raise DraftError(
                    "la lettre modifiée dépasse une page : raccourcissez le texte et réessayez"
                )
            stem = f"draft_{match_id}"
            work_pdf = work_dir / f"{stem}.pdf"
            shutil.copyfile(result, work_pdf)
            render_pngs(work_pdf, work_dir, stem)
            new_pngs = sorted(work_dir.glob(f"{stem}-*.png"))

            ensure_private_directory(target_dir)
            final_tex, final_pdf = _commit_letter_files(
                target_dir, stem, new_tex, work_pdf, new_pngs
            )
    except DraftError:
        conn.execute("DELETE FROM draft_job WHERE id = ?", (placeholder_id,))
        conn.commit()
        raise
    except Exception as exc:
        conn.execute("DELETE FROM draft_job WHERE id = ?", (placeholder_id,))
        conn.commit()
        log.exception(
            "draft: échec inattendu de l'édition manuelle pour le match %d", match_id
        )
        raise DraftError(f"erreur interne : {exc}") from exc

    library_id = _upsert_library_entry(conn, match_id, _label_for(match), final_pdf)
    _finish_job(
        conn,
        placeholder_id,
        status="ok",
        tex_path=str(final_tex),
        pdf_path=str(final_pdf),
        png_pages=pages,
        library_id=library_id,
    )
    return placeholder_id
