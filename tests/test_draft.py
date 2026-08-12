"""Tests for jobwatch.draft : génération de lettre de motivation et routes du dashboard."""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from jobwatch import draft, serve
from jobwatch.config import ConfigError, DraftConfig, load_config
from jobwatch.db import connect, init_db
from jobwatch.draft import (
    BODY_END_MARKER,
    BODY_START_MARKER,
    DraftError,
    apply_body_edit,
    compile_latex,
    escape_latex_body,
    extract_body,
    extract_latex,
    french_date,
    get_body_edit,
    render_pngs,
    run_job,
    splice_body,
    unescape_latex_body,
)
from jobwatch.serve import make_handler, render_page, render_swipe_page

HAS_TEX = shutil.which("lualatex") is not None and shutil.which("pdftoppm") is not None

MINIMAL_TEX = (
    "\\documentclass{article}\n"
    "\\begin{document}\n"
    "Madame, Monsieur, je souhaite rejoindre votre équipe.\n"
    "\\end{document}\n"
)

MINIMAL_TEX_WITH_BODY = (
    "\\documentclass{article}\n"
    "\\begin{document}\n"
    "Jean Dupont\n\n"
    f"{BODY_START_MARKER}\n"
    "Madame, Monsieur,\n\n"
    "Je souhaite rejoindre votre équipe.\n"
    f"{BODY_END_MARKER}\n\n"
    "Jean Dupont\n"
    "\\end{document}\n"
)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "jobwatch.db"
    conn = connect(path)
    init_db(conn)
    conn.close()
    return path


def _conn(db_path: Path) -> sqlite3.Connection:
    return connect(db_path)


def _seed_match(
    conn: sqlite3.Connection,
    title: str = "AI Engineer",
    company: str = "Acme",
    url: str = "https://example.com/offer",
    with_content: bool = True,
    state: str = "new",
    fit: str | None = None,
) -> int:
    conn.execute("INSERT OR IGNORE INTO source (type, name) VALUES ('test', 'test')")
    source_id = conn.execute("SELECT id FROM source WHERE name = 'test'").fetchone()["id"]
    conn.execute("INSERT OR IGNORE INTO company (name) VALUES (?)", (company,))
    company_id = conn.execute(
        "SELECT id FROM company WHERE name = ?", (company,)
    ).fetchone()["id"]
    cur = conn.execute(
        "INSERT INTO offer (source_id, company_id, title, url) VALUES (?, ?, ?, ?)",
        (source_id, company_id, title, url),
    )
    offer_id = int(cur.lastrowid)
    if with_content:
        conn.execute(
            "INSERT INTO offer_content (offer_id, markdown, fetch_method, status) "
            "VALUES (?, 'Poste IA à Paris, stack Python.', 'http', 'ok')",
            (offer_id,),
        )
    conn.execute(
        "INSERT OR IGNORE INTO search (name, include_json, exclude_json, locations_json) "
        "VALUES ('test', '[]', '[]', '[]')"
    )
    search_id = conn.execute("SELECT id FROM search WHERE name = 'test'").fetchone()["id"]
    cur = conn.execute(
        "INSERT INTO match (search_id, offer_id, state, fit) VALUES (?, ?, ?, ?)",
        (search_id, offer_id, state, fit),
    )
    conn.commit()
    return int(cur.lastrowid)


def _seed_cv(conn: sqlite3.Connection, tmp_path: Path) -> int:
    cv_path = tmp_path / "cv.txt"
    cv_path.write_text("Rayan Leveque, ingénieur IA, Python, RAG.", encoding="utf-8")
    cur = conn.execute(
        "INSERT INTO document_library (type, label, file_path) VALUES ('cv', 'CV IA', ?)",
        (str(cv_path),),
    )
    conn.commit()
    return int(cur.lastrowid)


def _seed_job(
    conn: sqlite3.Connection, match_id: int, cv_id: int, instruction: str | None = None
) -> int:
    cur = conn.execute(
        "INSERT INTO draft_job (match_id, track, cv_library_id, instruction) "
        "VALUES (?, 'engineer', ?, ?)",
        (match_id, cv_id, instruction),
    )
    conn.commit()
    return int(cur.lastrowid)


def _seed_ok_job(
    conn: sqlite3.Connection,
    db_path: Path,
    match_id: int,
    cv_id: int,
    tex_text: str = MINIMAL_TEX_WITH_BODY,
) -> int:
    """Insère un job 'ok' avec un .tex persisté, comme le laisserait une génération réussie."""
    from jobwatch.library import documents_dir

    target_dir = documents_dir(db_path)
    target_dir.mkdir(parents=True, exist_ok=True)
    tex_path = target_dir / f"draft_{match_id}.tex"
    tex_path.write_text(tex_text, encoding="utf-8")
    cur = conn.execute(
        "INSERT INTO draft_job (match_id, track, cv_library_id, status, tex_path) "
        "VALUES (?, 'engineer', ?, 'ok', ?)",
        (match_id, cv_id, str(tex_path)),
    )
    conn.commit()
    return int(cur.lastrowid)


def _config(tmp_path: Path) -> DraftConfig:
    example = tmp_path / "exemple.tex"
    example.write_text(MINIMAL_TEX, encoding="utf-8")
    return DraftConfig(
        opencode_bin="opencode",
        model="opencode-go/test",
        examples={"engineer": [example]},
    )


# ---------------------------------------------------------------- config


def test_config_empty_draft_block_is_inert(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"db: {tmp_path / 'db.sqlite'}\n"
        "searches:\n  - name: test\n    include: ['AI']\n"
        "draft: {}\n"
    )
    assert load_config(config_file).draft is None


def test_config_parses_draft_block(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"db: {tmp_path / 'db.sqlite'}\n"
        "searches:\n  - name: test\n    include: ['AI']\n"
        "draft:\n"
        "  opencode_bin: /usr/bin/opencode\n"
        "  model: opencode-go/gpt-5.6-luna\n"
        "  examples:\n"
        "    engineer: ['~/lettres/a.tex']\n"
        "    project: ['~/lettres/b.tex']\n"
    )
    config = load_config(config_file).draft
    assert config is not None
    assert config.model == "opencode-go/gpt-5.6-luna"
    assert config.examples["engineer"] == [Path("~/lettres/a.tex").expanduser()]
    assert config.examples["project"] == [Path("~/lettres/b.tex").expanduser()]


def test_config_rejects_unknown_track(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"db: {tmp_path / 'db.sqlite'}\n"
        "searches:\n  - name: test\n    include: ['AI']\n"
        "draft:\n"
        "  opencode_bin: opencode\n"
        "  model: m\n"
        "  examples:\n"
        "    designer: ['a.tex']\n"
    )
    with pytest.raises(ConfigError, match="piste"):
        load_config(config_file)


# ---------------------------------------------------------------- helpers purs


def test_french_date() -> None:
    import datetime

    assert french_date(datetime.date(2026, 8, 9)) == "9 août 2026"


def test_extract_latex_from_fenced_block() -> None:
    text = f"Voici la lettre :\n```latex\n{MINIMAL_TEX}```\nBonne chance !"
    assert extract_latex(text) == MINIMAL_TEX


def test_extract_latex_from_raw_response() -> None:
    assert extract_latex(f"\n{MINIMAL_TEX}\n") == MINIMAL_TEX


def test_extract_latex_rejects_incomplete_document() -> None:
    with pytest.raises(DraftError, match="LaTeX"):
        extract_latex("Je ne peux pas rédiger cette lettre.")


# ---------------------------------------------------------------- éditeur : marqueurs, échappement


def test_escape_latex_body_escapes_every_special_character() -> None:
    raw = "100% de $succès & aucun_doute #1 {vraiment} ~top~ \\o/"
    escaped = escape_latex_body(raw)
    assert escaped == (
        "100\\% de \\$succès \\& aucun\\_doute \\#1 \\{vraiment\\} "
        "\\textasciitilde{}top\\textasciitilde{} \\textbackslash{}o/"
    )
    assert unescape_latex_body(escaped) == raw


def test_unescape_latex_body_reverses_escape_for_prose() -> None:
    raw = "R&D chez Acme, budget de 20% et variable_name conservée."
    assert unescape_latex_body(escape_latex_body(raw)) == raw


def test_extract_body_returns_none_without_markers() -> None:
    assert extract_body(MINIMAL_TEX) is None


def test_extract_body_returns_unescaped_prose_between_markers() -> None:
    tex = (
        "\\documentclass{article}\\begin{document}\n"
        f"{BODY_START_MARKER}\nBonjour \\& bienvenue, 50\\%.\n{BODY_END_MARKER}\n"
        "\\end{document}\n"
    )
    assert extract_body(tex) == "Bonjour & bienvenue, 50%."


def test_splice_body_raises_without_markers() -> None:
    with pytest.raises(DraftError, match="modifiable"):
        splice_body(MINIMAL_TEX, "nouveau texte")


def test_splice_body_replaces_only_between_markers_and_escapes() -> None:
    spliced = splice_body(MINIMAL_TEX_WITH_BODY, "Nouveau texte avec 50% et R&D.")
    assert "Jean Dupont" in spliced
    assert "Nouveau texte avec 50\\% et R\\&D." in spliced
    assert extract_body(spliced) == "Nouveau texte avec 50% et R&D."


# ---------------------------------------------------------------- résolution des lettres exemples


def test_example_texts_prefers_config_examples(db_path: Path, tmp_path: Path) -> None:
    conn = _conn(db_path)
    library_tex = tmp_path / "library.tex"
    library_tex.write_text("EXEMPLE BIBLIOTHEQUE", encoding="utf-8")
    conn.execute(
        "INSERT INTO document_library (type, label, file_path) VALUES "
        "('letter_example', 'style', ?)",
        (str(library_tex),),
    )
    conn.commit()
    texts = draft._example_texts(conn, _config(tmp_path), "engineer")
    conn.close()
    assert texts == [MINIMAL_TEX]


def test_example_texts_falls_back_to_library_when_config_empty(
    db_path: Path, tmp_path: Path
) -> None:
    conn = _conn(db_path)
    library_tex = tmp_path / "library.tex"
    library_tex.write_text("EXEMPLE BIBLIOTHEQUE", encoding="utf-8")
    conn.execute(
        "INSERT INTO document_library (type, label, file_path) VALUES "
        "('letter_example', 'style', ?)",
        (str(library_tex),),
    )
    conn.commit()
    config = DraftConfig(model="m", opencode_bin="opencode")
    texts = draft._example_texts(conn, config, "engineer")
    conn.close()
    assert texts == ["EXEMPLE BIBLIOTHEQUE"]


def test_example_texts_falls_back_to_generic_default(db_path: Path) -> None:
    conn = _conn(db_path)
    config = DraftConfig(model="m", opencode_bin="opencode")
    texts = draft._example_texts(conn, config, "engineer")
    conn.close()
    assert len(texts) == 1
    assert BODY_START_MARKER in texts[0]


# ---------------------------------------------------------------- compilation réelle


@pytest.mark.skipif(not HAS_TEX, reason="lualatex/pdftoppm absents")
def test_compile_latex_and_render_pngs(tmp_path: Path) -> None:
    pdf_path = compile_latex(MINIMAL_TEX, tmp_path)
    assert isinstance(pdf_path, Path) and pdf_path.exists()
    pages = render_pngs(pdf_path, tmp_path, "draft_1")
    assert pages == 1
    assert (tmp_path / "draft_1-1.png").exists()


@pytest.mark.skipif(not HAS_TEX, reason="lualatex/pdftoppm absents")
def test_compile_latex_returns_log_on_error(tmp_path: Path) -> None:
    result = compile_latex("\\documentclass{article}\\begin{document}\\boom\\end{document}", tmp_path)
    assert isinstance(result, str)
    assert "boom" in result


# ---------------------------------------------------------------- run_job


def test_draft_bundle_uses_optional_profile_without_inventing_missing_context() -> None:
    personalized = draft._build_bundle(
        "Offre réelle",
        "CV réel",
        ["Exemple"],
        None,
        profile_context=(
            "## Motivations\n\nConstruire des produits utiles.\n\n"
            "## Ton préféré\n\nDirect et chaleureux."
        ),
    )
    without_profile = draft._build_bundle("Offre réelle", "CV réel", ["Exemple"], None)

    assert "# PROFIL PERSONNEL" in personalized
    assert "Construire des produits utiles" in personalized
    assert "# PROFIL PERSONNEL" not in without_profile
    prompt = draft.PROMPT.format(date="12 août 2026")
    assert "N'invente aucun fait absent du CV et du PROFIL PERSONNEL" in prompt
    assert "Si cette section est absente" in prompt


@pytest.mark.skipif(not HAS_TEX, reason="lualatex/pdftoppm absents")
def test_run_job_success_writes_files_and_library(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    conn = _conn(db_path)
    match_id = _seed_match(conn)
    cv_id = _seed_cv(conn, tmp_path)
    job_id = _seed_job(conn, match_id, cv_id)
    conn.close()

    monkeypatch.setattr(draft, "_call_llm", lambda config, prompt, bundle: MINIMAL_TEX)
    run_job(db_path, _config(tmp_path), job_id)

    conn = _conn(db_path)
    job = conn.execute("SELECT * FROM draft_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "ok", job["error"]
    assert job["warning"] is None
    assert job["png_pages"] == 1
    assert Path(job["pdf_path"]).exists()
    assert Path(job["tex_path"]).read_text() == MINIMAL_TEX
    entry = conn.execute(
        "SELECT * FROM document_library WHERE id = ?", (job["library_id"],)
    ).fetchone()
    assert entry["type"] == "cover_letter"
    assert entry["label"] == "LM Acme - AI Engineer"
    assert entry["file_path"] == job["pdf_path"]
    conn.close()


@pytest.mark.skipif(not HAS_TEX, reason="lualatex/pdftoppm absents")
def test_run_job_regeneration_reuses_library_entry(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    conn = _conn(db_path)
    match_id = _seed_match(conn)
    cv_id = _seed_cv(conn, tmp_path)
    first = _seed_job(conn, match_id, cv_id)
    conn.close()

    seen_bundles: list[str] = []

    def fake_llm(config, prompt, bundle):
        seen_bundles.append(bundle)
        return MINIMAL_TEX

    monkeypatch.setattr(draft, "_call_llm", fake_llm)
    run_job(db_path, _config(tmp_path), first)

    conn = _conn(db_path)
    second = _seed_job(conn, match_id, cv_id, instruction="plus court")
    conn.close()
    run_job(db_path, _config(tmp_path), second)

    conn = _conn(db_path)
    library_ids = [
        row["library_id"]
        for row in conn.execute(
            "SELECT library_id FROM draft_job WHERE match_id = ? ORDER BY id", (match_id,)
        )
    ]
    assert library_ids[0] == library_ids[1]
    assert conn.execute("SELECT COUNT(*) AS n FROM document_library").fetchone()["n"] == 2
    conn.close()
    assert "LETTRE PRÉCÉDENTE" in seen_bundles[1]


@pytest.mark.skipif(not HAS_TEX, reason="lualatex/pdftoppm absents")
def test_run_job_without_offer_content_falls_back_with_warning(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    conn = _conn(db_path)
    match_id = _seed_match(conn, with_content=False)
    cv_id = _seed_cv(conn, tmp_path)
    job_id = _seed_job(conn, match_id, cv_id)
    conn.close()

    monkeypatch.setattr(draft, "_fetch_and_extract", lambda url, client: (None, None, None))
    monkeypatch.setattr(draft, "_call_llm", lambda config, prompt, bundle: MINIMAL_TEX)
    run_job(db_path, _config(tmp_path), job_id)

    conn = _conn(db_path)
    job = conn.execute("SELECT * FROM draft_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "ok", job["error"]
    assert job["warning"] == draft.WARNING_NO_CONTENT
    conn.close()


def test_run_job_failure_lands_in_error(db_path: Path, tmp_path: Path, monkeypatch) -> None:
    conn = _conn(db_path)
    match_id = _seed_match(conn)
    cv_id = _seed_cv(conn, tmp_path)
    job_id = _seed_job(conn, match_id, cv_id)
    conn.close()

    def boom(config, prompt, bundle):
        raise DraftError("réponse vide du modèle")

    monkeypatch.setattr(draft, "_call_llm", boom)
    run_job(db_path, _config(tmp_path), job_id)

    conn = _conn(db_path)
    job = conn.execute("SELECT * FROM draft_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "failed"
    assert "réponse vide" in job["error"]
    conn.close()


# ---------------------------------------------------------------- corps éditable


def test_get_body_edit_returns_unescaped_prose(db_path: Path, tmp_path: Path) -> None:
    conn = _conn(db_path)
    match_id = _seed_match(conn)
    cv_id = _seed_cv(conn, tmp_path)
    _seed_ok_job(conn, db_path, match_id, cv_id)
    body = get_body_edit(conn, match_id)
    conn.close()
    assert body == "Madame, Monsieur,\n\nJe souhaite rejoindre votre équipe."


def test_get_body_edit_raises_without_markers(db_path: Path, tmp_path: Path) -> None:
    conn = _conn(db_path)
    match_id = _seed_match(conn)
    cv_id = _seed_cv(conn, tmp_path)
    _seed_ok_job(conn, db_path, match_id, cv_id, tex_text=MINIMAL_TEX)
    with pytest.raises(DraftError, match="modifiable"):
        get_body_edit(conn, match_id)
    conn.close()


def test_get_body_edit_raises_without_generated_letter(db_path: Path) -> None:
    conn = _conn(db_path)
    match_id = _seed_match(conn)
    with pytest.raises(DraftError, match="aucune lettre"):
        get_body_edit(conn, match_id)
    conn.close()


@pytest.mark.skipif(not HAS_TEX, reason="lualatex/pdftoppm absents")
def test_apply_body_edit_recompiles_and_creates_new_job(db_path: Path, tmp_path: Path) -> None:
    conn = _conn(db_path)
    match_id = _seed_match(conn)
    cv_id = _seed_cv(conn, tmp_path)
    first_job = _seed_ok_job(conn, db_path, match_id, cv_id)

    new_job_id = apply_body_edit(conn, db_path, match_id, "Nouveau texte de lettre à 100%.")

    assert new_job_id != first_job
    job = conn.execute("SELECT * FROM draft_job WHERE id = ?", (new_job_id,)).fetchone()
    assert job["status"] == "ok"
    assert Path(job["pdf_path"]).exists()
    assert "Nouveau texte de lettre" in Path(job["tex_path"]).read_text(encoding="utf-8")
    assert job["library_id"] is not None
    conn.close()


@pytest.mark.skipif(not HAS_TEX, reason="lualatex/pdftoppm absents")
def test_apply_body_edit_failure_does_not_persist(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    conn = _conn(db_path)
    match_id = _seed_match(conn)
    cv_id = _seed_cv(conn, tmp_path)
    _seed_ok_job(conn, db_path, match_id, cv_id)
    monkeypatch.setattr(
        draft, "compile_latex", lambda tex, work_dir: "erreur de compilation simulée"
    )

    with pytest.raises(DraftError, match="compilation"):
        apply_body_edit(conn, db_path, match_id, "texte qui casse la compilation")

    count = conn.execute(
        "SELECT COUNT(*) AS n FROM draft_job WHERE match_id = ?", (match_id,)
    ).fetchone()["n"]
    conn.close()
    assert count == 1  # seulement le job initial : rien n'a été ajouté après l'échec


@pytest.mark.skipif(not HAS_TEX, reason="lualatex/pdftoppm absents")
def test_apply_body_edit_rejects_page_overflow(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    conn = _conn(db_path)
    match_id = _seed_match(conn)
    cv_id = _seed_cv(conn, tmp_path)
    _seed_ok_job(conn, db_path, match_id, cv_id)
    monkeypatch.setattr(draft, "_pdf_page_count", lambda pdf_path: 2)

    with pytest.raises(DraftError, match="une page"):
        apply_body_edit(conn, db_path, match_id, "Texte qui déborderait sur deux pages.")

    count = conn.execute(
        "SELECT COUNT(*) AS n FROM draft_job WHERE match_id = ?", (match_id,)
    ).fetchone()["n"]
    conn.close()
    assert count == 1  # rien n'a été ajouté après le rejet pour dépassement d'une page


@pytest.mark.skipif(not HAS_TEX, reason="lualatex/pdftoppm absents")
def test_apply_body_edit_rejects_when_generation_running(
    db_path: Path, tmp_path: Path
) -> None:
    conn = _conn(db_path)
    match_id = _seed_match(conn)
    cv_id = _seed_cv(conn, tmp_path)
    _seed_ok_job(conn, db_path, match_id, cv_id)
    _seed_job(conn, match_id, cv_id)  # job laissé 'running' (statut par défaut du schéma)

    with pytest.raises(DraftError, match="déjà en cours"):
        apply_body_edit(conn, db_path, match_id, "Nouveau texte pendant une génération.")

    count = conn.execute(
        "SELECT COUNT(*) AS n FROM draft_job WHERE match_id = ?", (match_id,)
    ).fetchone()["n"]
    conn.close()
    assert count == 2  # le job 'ok' initial et le job 'running' : rien de plus


@pytest.mark.skipif(not HAS_TEX, reason="lualatex/pdftoppm absents")
def test_apply_body_edit_unexpected_failure_leaves_live_files_untouched(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    conn = _conn(db_path)
    match_id = _seed_match(conn)
    cv_id = _seed_cv(conn, tmp_path)
    _seed_ok_job(conn, db_path, match_id, cv_id)

    first_job_id = apply_body_edit(conn, db_path, match_id, "Premier texte valide à 100%.")
    first_job = conn.execute("SELECT * FROM draft_job WHERE id = ?", (first_job_id,)).fetchone()
    target_dir = Path(first_job["tex_path"]).parent
    original_tex = Path(first_job["tex_path"]).read_text(encoding="utf-8")
    original_pdf_bytes = Path(first_job["pdf_path"]).read_bytes()

    real_copyfile = shutil.copyfile
    calls = {"n": 0}

    def flaky_copyfile(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:  # échoue lors de la copie du pdf mis en scène dans target_dir
            raise OSError("disque plein simulé")
        return real_copyfile(src, dst)

    monkeypatch.setattr(draft.shutil, "copyfile", flaky_copyfile)

    with pytest.raises(DraftError, match="erreur interne"):
        apply_body_edit(conn, db_path, match_id, "Second texte qui échoue à la publication.")

    assert Path(first_job["tex_path"]).read_text(encoding="utf-8") == original_tex
    assert Path(first_job["pdf_path"]).read_bytes() == original_pdf_bytes
    assert not [p for p in target_dir.iterdir() if p.name.endswith(".new")]

    count = conn.execute(
        "SELECT COUNT(*) AS n FROM draft_job WHERE match_id = ?", (match_id,)
    ).fetchone()["n"]
    conn.close()
    assert count == 2  # le job initial et le premier edit réussi : rien de plus après l'échec


# ---------------------------------------------------------------- rendu HTML


def test_render_page_hides_letter_reader_when_draft_disabled(db_path: Path) -> None:
    conn = _conn(db_path)
    _seed_match(conn)
    assert 'class="reader-tab letter-toggle' not in render_page(conn)
    enabled = render_page(conn, draft_enabled=True)
    assert 'class="reader-tab letter-toggle letter-empty"' in enabled
    assert ">Lettre</span></button>" in enabled
    conn.close()


def test_render_page_shows_running_state(db_path: Path, tmp_path: Path) -> None:
    conn = _conn(db_path)
    match_id = _seed_match(conn)
    cv_id = _seed_cv(conn, tmp_path)
    _seed_job(conn, match_id, cv_id)
    page = render_page(conn, draft_enabled=True)
    conn.close()
    assert 'data-status="running"' in page
    assert "Génération de la lettre en cours" in page


def test_render_page_shows_completed_letter_in_reader(db_path: Path, tmp_path: Path) -> None:
    conn = _conn(db_path)
    match_id = _seed_match(conn)
    cv_id = _seed_cv(conn, tmp_path)
    job_id = _seed_job(conn, match_id, cv_id)
    conn.execute(
        "UPDATE draft_job SET status = 'ok', tex_path = 'letter.tex', "
        "pdf_path = 'letter.pdf', png_pages = 1 WHERE id = ?",
        (job_id,),
    )
    conn.commit()

    page = render_page(conn, draft_enabled=True)
    conn.close()

    assert 'class="reader-tab letter-toggle letter-ok"' in page
    assert "Lettre · prête" in page
    assert ">Régénérer la lettre</button>" in page
    assert f'/match/{match_id}/letter/1.png' in page


# ---------------------------------------------------------------- serveur HTTP


def _start_server(
    db_path: Path, draft_config: DraftConfig | None
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db_path, draft_config))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _request(
    port: int, path: str, payload: dict | None = None
) -> tuple[int, str]:
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"} if data else {}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_http_draft_post_requires_config(db_path: Path) -> None:
    conn = _conn(db_path)
    match_id = _seed_match(conn)
    conn.close()
    server, thread = _start_server(db_path, None)
    try:
        status, body = _request(
            server.server_address[1],
            f"/match/{match_id}/draft",
            {"cv_library_id": 1, "track": "engineer"},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert status == 503
    assert "draft" in body


def test_http_draft_post_creates_job_and_rejects_concurrent(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    conn = _conn(db_path)
    match_id = _seed_match(conn)
    cv_id = _seed_cv(conn, tmp_path)
    conn.close()

    spawned: list[int] = []
    monkeypatch.setattr(serve, "_spawn_draft_job", lambda db, cfg, job_id: spawned.append(job_id))
    server, thread = _start_server(db_path, _config(tmp_path))
    try:
        port = server.server_address[1]
        status, body = _request(
            port, f"/match/{match_id}/draft",
            {"cv_library_id": cv_id, "instruction": "", "track": "engineer"},
        )
        assert status == 202, body
        assert spawned == [json.loads(body)["job_id"]]

        status, body = _request(
            port, f"/match/{match_id}/draft",
            {"cv_library_id": cv_id, "track": "engineer"},
        )
        assert status == 409

        status, body = _request(port, f"/match/{match_id}/draft/status")
        assert status == 200
        payload = json.loads(body)
        assert payload["status"] == "queued"
        assert "draft-spinner" in payload["html"]

        status, _ = _request(port, "/match/999/draft/status")
        assert status == 404
        status, _ = _request(port, f"/match/{match_id}/letter.pdf")
        assert status == 404
        status, _ = _request(port, f"/match/{match_id}/letter/1.png")
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_fail_interrupted_draft_jobs_unblocks_match(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    conn = _conn(db_path)
    match_id = _seed_match(conn)
    cv_id = _seed_cv(conn, tmp_path)
    running_id = _seed_job(conn, match_id, cv_id)
    cur = conn.execute(
        "INSERT INTO draft_job (match_id, track, cv_library_id, status) "
        "VALUES (?, 'engineer', ?, 'queued')",
        (match_id, cv_id),
    )
    queued_id = int(cur.lastrowid)
    cur = conn.execute(
        "INSERT INTO draft_job (match_id, track, cv_library_id, status) "
        "VALUES (?, 'engineer', ?, 'ok')",
        (match_id, cv_id),
    )
    ok_id = int(cur.lastrowid)
    conn.commit()
    conn.close()

    serve._fail_interrupted_draft_jobs(db_path)

    conn = _conn(db_path)
    rows = {row["id"]: row for row in conn.execute("SELECT * FROM draft_job")}
    conn.close()
    for job_id in (running_id, queued_id):
        assert rows[job_id]["status"] == "failed"
        assert "redémarrage" in rows[job_id]["error"]
        assert rows[job_id]["finished_at"] is not None
    assert rows[ok_id]["status"] == "ok"
    assert rows[ok_id]["finished_at"] is None

    spawned: list[int] = []
    monkeypatch.setattr(serve, "_spawn_draft_job", lambda db, cfg, job_id: spawned.append(job_id))
    server, thread = _start_server(db_path, _config(tmp_path))
    try:
        status, body = _request(
            server.server_address[1],
            f"/match/{match_id}/draft",
            {"cv_library_id": cv_id, "track": "engineer"},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert status == 202, body
    assert spawned == [json.loads(body)["job_id"]]


def test_http_draft_post_validates_fields(db_path: Path, tmp_path: Path) -> None:
    conn = _conn(db_path)
    match_id = _seed_match(conn)
    conn.close()
    server, thread = _start_server(db_path, _config(tmp_path))
    try:
        port = server.server_address[1]
        status, _ = _request(
            port, f"/match/{match_id}/draft", {"cv_library_id": "x", "track": "engineer"}
        )
        assert status == 400
        status, _ = _request(
            port, f"/match/{match_id}/draft", {"cv_library_id": 1, "track": "designer"}
        )
        assert status == 400
        status, _ = _request(
            port, "/match/999/draft", {"cv_library_id": 1, "track": "engineer"}
        )
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.skipif(not HAS_TEX, reason="lualatex/pdftoppm absents")
def test_http_letter_files_served_after_job(db_path: Path, tmp_path: Path, monkeypatch) -> None:
    conn = _conn(db_path)
    match_id = _seed_match(conn)
    cv_id = _seed_cv(conn, tmp_path)
    job_id = _seed_job(conn, match_id, cv_id)
    conn.close()
    monkeypatch.setattr(draft, "_call_llm", lambda config, prompt, bundle: MINIMAL_TEX)
    run_job(db_path, _config(tmp_path), job_id)

    server, thread = _start_server(db_path, _config(tmp_path))
    try:
        port = server.server_address[1]
        status, body = _request(port, f"/match/{match_id}/draft/status")
        assert status == 200
        payload = json.loads(body)
        assert payload["status"] == "ok"
        assert f"/match/{match_id}/letter/1.png" in payload["html"]
        assert payload["library_id"] == 2
        assert payload["library_label"] == "LM Acme - AI Engineer"

        url = f"http://127.0.0.1:{port}/match/{match_id}/letter.pdf"
        with urllib.request.urlopen(url, timeout=5) as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "application/pdf"
            assert resp.read(5) == b"%PDF-"
        url = f"http://127.0.0.1:{port}/match/{match_id}/letter/1.png"
        with urllib.request.urlopen(url, timeout=5) as resp:
            assert resp.status == 200
            assert resp.read(8) == b"\x89PNG\r\n\x1a\n"
        status, body = _request(port, f"/match/{match_id}/letter.tex")
        assert status == 200
        assert "documentclass" in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.skipif(not HAS_TEX, reason="lualatex/pdftoppm absents")
def test_http_letter_body_get_and_post(db_path: Path, tmp_path: Path) -> None:
    conn = _conn(db_path)
    match_id = _seed_match(conn)
    cv_id = _seed_cv(conn, tmp_path)
    _seed_ok_job(conn, db_path, match_id, cv_id)
    conn.close()

    server, thread = _start_server(db_path, _config(tmp_path))
    try:
        port = server.server_address[1]
        status, body = _request(port, f"/match/{match_id}/letter/body")
        assert status == 200
        assert json.loads(body)["body"] == "Madame, Monsieur,\n\nJe souhaite rejoindre votre équipe."

        status, body = _request(
            port, f"/match/{match_id}/letter/body", {"body": "Texte édité à la main."}
        )
        assert status == 200, body
        payload = json.loads(body)
        assert payload["status"] == "ok"
        assert "letter-page" in payload["html"]

        status, body = _request(port, f"/match/{match_id}/letter/body")
        assert json.loads(body)["body"] == "Texte édité à la main."
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_letter_body_post_requires_config(db_path: Path, tmp_path: Path) -> None:
    conn = _conn(db_path)
    match_id = _seed_match(conn)
    cv_id = _seed_cv(conn, tmp_path)
    _seed_ok_job(conn, db_path, match_id, cv_id)
    conn.close()
    server, thread = _start_server(db_path, None)
    try:
        status, _ = _request(
            server.server_address[1], f"/match/{match_id}/letter/body", {"body": "x"}
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert status == 503


def test_http_letter_body_get_404_without_letter(db_path: Path) -> None:
    conn = _conn(db_path)
    match_id = _seed_match(conn)
    conn.close()
    server, thread = _start_server(db_path, None)
    try:
        status, _ = _request(server.server_address[1], f"/match/{match_id}/letter/body")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert status == 404


def test_http_letter_body_post_validates_body(db_path: Path, tmp_path: Path) -> None:
    conn = _conn(db_path)
    match_id = _seed_match(conn)
    cv_id = _seed_cv(conn, tmp_path)
    _seed_ok_job(conn, db_path, match_id, cv_id)
    conn.close()
    server, thread = _start_server(db_path, _config(tmp_path))
    try:
        status, _ = _request(
            server.server_address[1], f"/match/{match_id}/letter/body", {"body": "   "}
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert status == 400


# ---------------------------------------------------------------- swipe & batch


def test_render_swipe_page_orders_high_fit_first(db_path: Path, tmp_path: Path) -> None:
    conn = _conn(db_path)
    _seed_match(conn, title="Offre banale", url="https://example.com/a")
    _seed_match(conn, title="Offre top", url="https://example.com/b", fit="high")
    _seed_match(conn, title="Offre triée", url="https://example.com/c", state="later")
    _seed_cv(conn, tmp_path)
    page = render_swipe_page(conn, draft_enabled=True)
    conn.close()
    assert page.count("swipe-card") >= 2
    assert page.index("Offre top") < page.index("Offre banale")
    assert "Offre triée" not in page
    assert 'id="batch-count">1<' in page  # la 'later' sans lettre est éligible


def test_render_swipe_page_without_draft_config(db_path: Path) -> None:
    conn = _conn(db_path)
    _seed_match(conn)
    page = render_swipe_page(conn)
    conn.close()
    assert "non configurée" in page
    assert 'id="batch-btn"' not in page


def test_swipe_done_offers_a_way_out(db_path: Path, tmp_path: Path) -> None:
    """La fin du tri propose un retour explicite ; l'avancement part dans la pastille."""
    conn = _conn(db_path)
    _seed_match(conn)
    _seed_cv(conn, tmp_path)
    page = render_swipe_page(conn, draft_enabled=True)
    conn.close()
    assert 'class="card-action done-back" href="/"' in page
    assert 'data-back-href="/"' in page
    assert "window.location.assign(backHref)" in page
    assert "Signaler un bug" in page
    assert 'id="batch-badge"' in page
    assert 'id="batch-progress"' not in page


def test_batch_badge_follows_to_the_dashboard(db_path: Path, tmp_path: Path) -> None:
    """Le tableau de bord affiche le même badge d'avancement, piste comprise."""
    conn = _conn(db_path)
    _seed_match(conn)
    page = render_page(conn, track="project", draft_enabled=True)
    plain = render_page(conn)
    conn.close()
    assert 'id="batch-badge"' in page
    assert 'data-track="project"' in page
    assert 'id="batch-badge"' not in plain


def test_http_swipe_routes(db_path: Path, tmp_path: Path) -> None:
    server, thread = _start_server(db_path, _config(tmp_path))
    try:
        port = server.server_address[1]
        for path in ("/swipe", "/po/swipe"):
            status, body = _request(port, path)
            assert status == 200
            assert "swipe-stage" in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_batch_creates_queued_jobs_for_eligible_only(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    conn = _conn(db_path)
    eligible = _seed_match(conn, url="https://example.com/a", state="later")
    failed_retry = _seed_match(conn, url="https://example.com/b", state="later")
    already_ok = _seed_match(conn, url="https://example.com/c", state="later")
    _seed_match(conn, url="https://example.com/d", state="new")
    cv_id = _seed_cv(conn, tmp_path)
    conn.execute(
        "INSERT INTO draft_job (match_id, track, cv_library_id, status) "
        "VALUES (?, 'engineer', ?, 'failed')",
        (failed_retry, cv_id),
    )
    conn.execute(
        "INSERT INTO draft_job (match_id, track, cv_library_id, status) "
        "VALUES (?, 'engineer', ?, 'ok')",
        (already_ok, cv_id),
    )
    conn.commit()
    conn.close()

    spawned: list[int] = []
    monkeypatch.setattr(serve, "_spawn_draft_job", lambda db, cfg, job_id: spawned.append(job_id))
    server, thread = _start_server(db_path, _config(tmp_path))
    try:
        port = server.server_address[1]
        status, body = _request(
            port, "/draft/batch", {"track": "engineer", "cv_library_id": cv_id}
        )
        assert status == 202
        assert json.loads(body)["count"] == 2
        assert len(spawned) == 2

        conn = _conn(db_path)
        queued = {
            int(row["match_id"])
            for row in conn.execute("SELECT match_id FROM draft_job WHERE status = 'queued'")
        }
        conn.close()
        assert queued == {eligible, failed_retry}

        # Relancer ne double pas les jobs : tout est déjà en file
        status, body = _request(
            port, "/draft/batch", {"track": "engineer", "cv_library_id": cv_id}
        )
        assert status == 202
        assert json.loads(body)["count"] == 0

        status, body = _request(port, "/draft/batch/status?track=engineer")
        assert status == 200
        counts = json.loads(body)
        assert counts["queued"] == 2
        assert counts["ok"] == 1

        status, _ = _request(port, "/draft/batch/status?track=nope")
        assert status == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_batch_requires_config(db_path: Path) -> None:
    server, thread = _start_server(db_path, None)
    try:
        status, _ = _request(
            server.server_address[1], "/draft/batch",
            {"track": "engineer", "cv_library_id": 1},
        )
        assert status == 503
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.skipif(not HAS_TEX, reason="lualatex/pdftoppm absents")
def test_run_job_processes_queued_job(db_path: Path, tmp_path: Path, monkeypatch) -> None:
    conn = _conn(db_path)
    match_id = _seed_match(conn)
    cv_id = _seed_cv(conn, tmp_path)
    cur = conn.execute(
        "INSERT INTO draft_job (match_id, track, cv_library_id, status) "
        "VALUES (?, 'engineer', ?, 'queued')",
        (match_id, cv_id),
    )
    job_id = int(cur.lastrowid)
    conn.commit()
    conn.close()

    monkeypatch.setattr(draft, "_call_llm", lambda config, prompt, bundle: MINIMAL_TEX)
    run_job(db_path, _config(tmp_path), job_id)

    conn = _conn(db_path)
    job = conn.execute("SELECT status FROM draft_job WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    assert job["status"] == "ok"


def test_render_page_keeps_swipe_button_and_only_prompts_for_new_offers(db_path: Path) -> None:
    conn = _conn(db_path)
    page = render_page(conn)
    assert 'class="swipe-fab" href="/swipe"' in page
    assert 'aria-label="Ouvrir le tri des offres"' in page
    assert 'id="swipe-popup"' not in page
    _seed_match(conn)
    page = render_page(conn)
    assert 'class="swipe-fab" href="/swipe"' in page
    assert 'id="swipe-popup"' in page
    assert "C'est le moment de swiper." in page
    assert 'swipe-fab-count">1<' in page
    conn.close()


def test_config_parses_codex_draft_runner(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"db: {tmp_path / 'db.sqlite'}\n"
        "searches:\n  - name: test\n    include: ['AI']\n"
        "draft:\n"
        "  runner: codex\n"
        "  model: gpt-5.6-luna\n"
        "  variant: max\n"
    )
    config = load_config(config_file).draft
    assert config is not None
    assert config.runner == "codex"
    assert config.codex_bin == "codex"
    assert config.variant == "max"

    # Le runner opencode exige toujours opencode_bin explicitement.
    config_file.write_text(
        f"db: {tmp_path / 'db.sqlite'}\n"
        "searches:\n  - name: test\n    include: ['AI']\n"
        "draft:\n"
        "  model: m\n"
    )
    with pytest.raises(ConfigError, match="opencode_bin"):
        load_config(config_file)

    config_file.write_text(
        f"db: {tmp_path / 'db.sqlite'}\n"
        "searches:\n  - name: test\n    include: ['AI']\n"
        "draft:\n"
        "  runner: pi\n"
        "  model: m\n"
    )
    with pytest.raises(ConfigError, match="draft.runner"):
        load_config(config_file)


def test_call_llm_codex_builds_command_and_reads_output(monkeypatch) -> None:
    import subprocess as sp

    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        captured["input"] = kwargs.get("input")
        out_path = Path(command[command.index("-o") + 1])
        out_path.write_text(MINIMAL_TEX, encoding="utf-8")
        return sp.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("jobwatch.llm_runner.subprocess.run", fake_run)
    config = DraftConfig(model="gpt-5.6-luna", runner="codex", variant="max")
    text = draft._call_llm(config, "Rédige la lettre.", "# OFFRE\n\ncontenu")

    assert text == MINIMAL_TEX
    command = captured["command"]
    assert command[:2] == ["codex", "exec"]
    assert command[command.index("--model") + 1] == "gpt-5.6-luna"
    assert "model_reasoning_effort=max" in command
    assert command[command.index("-s") + 1] == "read-only"
    assert "--ignore-user-config" in command
    disabled = {command[index + 1] for index, item in enumerate(command) if item == "--disable"}
    assert disabled == {"shell_tool", "code_mode_host", "apps", "plugins"}
    assert captured["input"] == "# OFFRE\n\ncontenu"
    assert command[-1].endswith("Rédige la lettre.")


def test_call_llm_codex_failure_raises_drafterror(monkeypatch) -> None:
    import subprocess as sp

    monkeypatch.setattr(
        "jobwatch.llm_runner.subprocess.run",
        lambda command, **kwargs: sp.CompletedProcess(command, 1, stdout="", stderr="boom"),
    )
    config = DraftConfig(model="m", runner="codex")
    with pytest.raises(DraftError, match="codex"):
        draft._call_llm(config, "prompt", "bundle")


def test_call_opencode_denies_every_tool_by_name(monkeypatch) -> None:
    """opencode.json et OPENCODE_PERMISSION sont les contrats lus par opencode."""
    import subprocess as sp

    from jobwatch.enrich import OPENCODE_TOOLS

    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        captured["file"] = json.loads(
            (Path(kwargs["cwd"]) / "opencode.json").read_text(encoding="utf-8")
        )
        captured["env"] = json.loads(kwargs["env"]["OPENCODE_PERMISSION"])
        event = json.dumps({"type": "text", "part": {"text": MINIMAL_TEX}})
        return sp.CompletedProcess(command, 0, stdout=event, stderr="")

    monkeypatch.setattr("jobwatch.llm_runner.subprocess.run", fake_run)
    config = DraftConfig(model="m", runner="opencode")
    text = draft._call_llm(config, "Rédige la lettre.", "# OFFRE\n\ncontenu")

    assert text.strip() == MINIMAL_TEX.strip()
    assert "--pure" in captured["command"]
    expected = {"*": "deny", **{tool: "deny" for tool in OPENCODE_TOOLS}}
    assert captured["file"]["permission"] == expected
    assert captured["env"] == expected
