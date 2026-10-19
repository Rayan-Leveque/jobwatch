"""Candidature assistée : `jw prepare`, agent d'envoi et lecture des codes email.

Les binaires `claude` et `playwright-cli` sont remplacés par de vrais scripts
exécutables : le parcours sous-processus, dossier de travail et base est réel,
seul le modèle est simulé.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from email.message import EmailMessage
from pathlib import Path

import pytest
from click.testing import CliRunner
from test_draft import _seed_cv, _seed_match

from jobwatch import prepare, serve
from jobwatch.cli import cli
from jobwatch.config import AutoApplyConfig, ConfigError, ImapConfig, load_config
from jobwatch.db import connect, init_db
from jobwatch.mailcode import format_messages, parse_message
from jobwatch.submit import accounts_path, queue_submission, run_submission

IMAP_SECRET = "imap-secret-never-shown"


def _seed_profile(conn, cv_id: int) -> None:
    workspace_id = conn.execute(
        "INSERT INTO workspace (slug, name) VALUES ('rayan', 'Rayan')"
    ).lastrowid
    account_id = conn.execute("INSERT INTO account (email) VALUES ('r@example.com')").lastrowid
    conn.execute(
        "INSERT INTO candidate_profile (account_id, workspace_id, cv_library_id, "
        "reusable_details, completed_at) VALUES (?, ?, ?, ?, datetime('now'))",
        (account_id, workspace_id, cv_id, "Email : rayan@example.com, téléphone 06 00 00 00 00"),
    )
    conn.commit()


def _write_config(tmp_path: Path, db_path: Path, limit: int = 2) -> Path:
    example = tmp_path / "exemple.tex"
    example.write_text("\\documentclass{article}\\begin{document}x\\end{document}\n")
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""\
db: {db_path}
searches: []
sources:
  linkedin:
    from_profile: true
notify: {{}}
draft:
  runner: codex
  model: test-model
  examples:
    engineer: [{example}]
autoapply:
  daily_limit: {limit}
  browser_profile: {tmp_path / "profile"}
  imap:
    host: imap.example.com
    user: candidat@example.com
    password: {IMAP_SECRET}
""",
        encoding="utf-8",
    )
    return path


def _fake_letter(db_path: Path):
    """Remplace draft.run_job : un PDF et un .tex à corps balisé, comme une vraie génération."""

    def run_job(path: Path, _config, job_id: int) -> None:
        conn = connect(path)
        job = conn.execute("SELECT match_id FROM draft_job WHERE id = ?", (job_id,)).fetchone()
        pdf = db_path.parent / f"draft_{job['match_id']}.pdf"
        tex = pdf.with_suffix(".tex")
        pdf.write_bytes(b"%PDF-1.4 lettre")
        tex.write_text(
            "% JOBWATCH:BODY_START\nJe souhaite rejoindre votre équipe.\n% JOBWATCH:BODY_END\n",
            encoding="utf-8",
        )
        conn.execute(
            "UPDATE draft_job SET status = 'ok', pdf_path = ?, tex_path = ? WHERE id = ?",
            (str(pdf), str(tex), job_id),
        )
        conn.commit()
        conn.close()

    return run_job


@pytest.fixture()
def instance(tmp_path: Path):
    db_path = tmp_path / "data" / "jobwatch.db"
    db_path.parent.mkdir()
    conn = connect(db_path)
    init_db(conn)
    cv_pdf = tmp_path / "import_0a1b2c_CV_Candidat.pdf"
    cv_pdf.write_bytes(b"%PDF-1.4 cv")
    cv_id = _seed_cv(conn, tmp_path)
    _seed_profile(conn, cv_id)
    conn.close()
    return db_path, _write_config(tmp_path, db_path), cv_id


def test_config_requires_draft_block(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"db: {tmp_path / 'jw.db'}\nsearches: []\nsources:\n  linkedin:\n    from_profile: true\n"
        f"autoapply:\n  browser_profile: {tmp_path / 'p'}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="draft"):
        load_config(path)


def test_prepare_cli_respects_daily_limit_and_runs_once_per_day(
    instance, monkeypatch
) -> None:
    db_path, config_path, _cv_id = instance
    conn = connect(db_path)
    high = [
        _seed_match(conn, company=f"Co{i}", url=f"https://example.com/{i}", fit="high")
        for i in range(3)
    ]
    medium = _seed_match(conn, company="Moyen", url="https://example.com/m", fit="medium")
    conn.close()
    monkeypatch.setattr(prepare.draft, "run_job", _fake_letter(db_path))

    runner = CliRunner()
    first = runner.invoke(cli, ["prepare", "--config", str(config_path)])
    assert first.exit_code == 0, first.output
    assert "2 candidature(s) à valider, 0 échec(s)" in first.output
    second = runner.invoke(cli, ["prepare", "--config", str(config_path)])
    assert "0 candidature(s) à valider" in second.output

    conn = connect(db_path)
    jobs = conn.execute("SELECT match_id, status FROM submission_job").fetchall()
    states = dict(conn.execute("SELECT id, state FROM match").fetchall())
    conn.close()
    assert [row["status"] for row in jobs] == ["prepared", "prepared"]
    prepared_ids = {row["match_id"] for row in jobs}
    assert prepared_ids < set(high)
    assert all(states[i] == "later" for i in prepared_ids)
    assert states[medium] == "new"


def test_prepared_dossier_moves_from_later_to_review_section(instance, monkeypatch) -> None:
    db_path, config_path, _cv_id = instance
    conn = connect(db_path)
    _seed_match(conn, company="Préparée", url="https://example.com/p", fit="high")
    _seed_match(conn, company="ManuelleCo", url="https://example.com/l", state="later")
    conn.close()
    monkeypatch.setattr(prepare.draft, "run_job", _fake_letter(db_path))
    CliRunner().invoke(cli, ["prepare", "--config", str(config_path)])

    conn = connect(db_path)
    page = serve.render_page(conn, "all", draft_enabled=True)
    conn.close()
    review = page.split('data-section="review"')[1].split("</details>")[0]
    later = page.split('data-section="later"')[1].split("</details>")[0]
    assert "Préparée" in review and "Envoyer la candidature" in review
    assert "ManuelleCo" in later and "Préparée" not in later


def _fake_bin(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _agent(tmp_path: Path, result: dict) -> AutoApplyConfig:
    """Faux claude : note ses arguments, dépose la preuve, rend l'enveloppe JSON."""
    bins = tmp_path / "bin"
    bins.mkdir(exist_ok=True)
    browser = _fake_bin(bins, "fake-browser", "import sys\nprint(' '.join(sys.argv[1:]))\n")
    claude = _fake_bin(
        bins,
        "fake-claude",
        "import json, os, pathlib, sys\n"
        f"pathlib.Path({str(tmp_path / 'argv.json')!r}).write_text(json.dumps(sys.argv[1:]))\n"
        f"pathlib.Path({str(tmp_path / 'env.json')!r}).write_text(json.dumps(dict(os.environ)))\n"
        "pathlib.Path('preuve.png').write_bytes(b'png')\n"
        f"print(json.dumps({{'structured_output': json.loads({json.dumps(result)!r})}}))\n",
    )
    return AutoApplyConfig(
        browser_profile=tmp_path / "profile",
        claude_bin=str(claude),
        browser_bin=str(browser),
        imap=ImapConfig(host="imap.example.com", user="candidat@example.com", password=IMAP_SECRET),
        config_path=tmp_path / "config.yaml",
    )


def _prepared(db_path: Path, cv_id: int) -> int:
    conn = connect(db_path)
    match_id = _seed_match(conn, state="later", fit="high")
    pdf = db_path.parent / "lettre.pdf"
    pdf.write_bytes(b"%PDF-1.4 lettre")
    draft_id = conn.execute(
        "INSERT INTO draft_job (match_id, track, cv_library_id, status, pdf_path) "
        "VALUES (?, 'engineer', ?, 'ok', ?)",
        (match_id, cv_id, str(pdf)),
    ).lastrowid
    conn.execute(
        "INSERT INTO submission_job (match_id, cv_library_id, draft_job_id) VALUES (?, ?, ?)",
        (match_id, cv_id, draft_id),
    )
    conn.commit()
    conn.close()
    return match_id


def test_submitted_dossier_records_application_with_proof(instance, tmp_path: Path) -> None:
    db_path, _config_path, cv_id = instance
    match_id = _prepared(db_path, cv_id)
    config = _agent(
        tmp_path,
        {"status": "submitted", "reason": "Confirmation affichée.",
         "final_url": "https://ats.example.com/merci", "account_created_on": None},
    )
    conn = connect(db_path)
    job_id = queue_submission(conn, match_id)
    conn.close()

    run_submission(db_path, config, job_id)

    conn = connect(db_path)
    job = conn.execute("SELECT * FROM submission_job WHERE id = ?", (job_id,)).fetchone()
    application = conn.execute(
        "SELECT id FROM application WHERE match_id = ?", (match_id,)
    ).fetchone()
    documents = dict(
        conn.execute(
            "SELECT type, path FROM document WHERE application_id = ?", (application["id"],)
        ).fetchall()
    )
    state = conn.execute("SELECT state FROM match WHERE id = ?", (match_id,)).fetchone()[0]
    conn.close()
    assert job["status"] == "submitted", job["reason"]
    assert job["final_url"] == "https://ats.example.com/merci"
    assert Path(job["proof_path"]).read_bytes() == b"png"
    assert state == "applied"
    work_dir = Path(job["proof_path"]).parent
    assert Path(documents["cover_letter"]) == work_dir / "Lettre_motivation_Acme.pdf"
    assert Path(documents["cv"]).parent == work_dir


def test_agent_sees_neither_imap_password_nor_browser_storage(instance, tmp_path: Path) -> None:
    db_path, _config_path, cv_id = instance
    match_id = _prepared(db_path, cv_id)
    config = _agent(
        tmp_path,
        {"status": "needs_human", "reason": "Captcha.", "final_url": "", "account_created_on": None},
    )
    conn = connect(db_path)
    job_id = queue_submission(conn, match_id)
    conn.close()
    run_submission(db_path, config, job_id)

    argv = json.loads((tmp_path / "argv.json").read_text())
    denied = argv[argv.index("--disallowedTools") + 1 : argv.index("--setting-sources")]
    assert f"Bash({config.browser_bin} cookie*)" in denied
    assert f"Bash({config.browser_bin} upload /*)" in denied
    assert "--strict-mcp-config" in argv
    work_dir = db_path.parent / "documents" / "submissions" / str(job_id)
    for path in work_dir.iterdir():
        assert IMAP_SECRET.encode() not in path.read_bytes(), path.name
    assert IMAP_SECRET not in " ".join(argv)
    env = json.loads((tmp_path / "env.json").read_text())
    assert env["JOBWATCH_MAILCODE_CONFIG"] == str(config.config_path)


def test_needs_human_keeps_dossier_and_saves_created_account(instance, tmp_path: Path) -> None:
    db_path, _config_path, cv_id = instance
    match_id = _prepared(db_path, cv_id)
    config = _agent(
        tmp_path,
        {"status": "needs_human", "reason": "Question sur le salaire actuel.",
         "final_url": "https://x.wd3.myworkdayjobs.com/apply",
         "account_created_on": "x.wd3.myworkdayjobs.com"},
    )
    conn = connect(db_path)
    run_submission(db_path, config, queue_submission(conn, match_id))
    job = conn.execute("SELECT * FROM submission_job WHERE match_id = ?", (match_id,)).fetchone()
    applied = conn.execute("SELECT 1 FROM application WHERE match_id = ?", (match_id,)).fetchone()
    assert job["status"] == "needs_human"
    assert job["reason"] == "Question sur le salaire actuel."
    assert applied is None
    # Le dossier se relance une fois le blocage levé.
    assert queue_submission(conn, match_id) == job["id"]
    conn.close()

    accounts = json.loads(accounts_path(db_path).read_text())
    assert accounts["x.wd3.myworkdayjobs.com"]["email"] == "candidat@example.com"
    assert len(accounts["x.wd3.myworkdayjobs.com"]["password"]) >= 16
    assert oct(os.stat(accounts_path(db_path)).st_mode & 0o777) == "0o600"


def test_server_restart_hands_interrupted_submission_back(instance) -> None:
    db_path, _config_path, cv_id = instance
    match_id = _prepared(db_path, cv_id)
    conn = connect(db_path)
    conn.execute("UPDATE submission_job SET status = 'running' WHERE match_id = ?", (match_id,))
    conn.commit()
    serve._interrupt_submission_jobs(db_path)
    job = conn.execute("SELECT * FROM submission_job WHERE match_id = ?", (match_id,)).fetchone()
    conn.close()
    assert job["status"] == "needs_human"
    assert "vérifiez le site" in job["reason"]


def test_mail_code_keeps_verification_link_and_code_from_html_email() -> None:
    message = EmailMessage()
    message["From"] = "Workday <no-reply@myworkday.com>"
    message["Subject"] = "Vérifiez votre adresse"
    message["Date"] = "Mon, 19 Oct 2026 16:30:00 +0000"
    message.set_content("")
    message.add_alternative(
        '<p>Votre code : <b>482913</b></p><a href="https://x.myworkday.com/verify?t=abc">Vérifier</a>',
        subtype="html",
    )
    message.get_payload()[0].set_payload("")  # partie texte vide : repli sur le HTML
    summary = parse_message(message.as_bytes())
    assert summary is not None
    assert "482913" in summary.text
    assert "https://x.myworkday.com/verify?t=abc" in summary.text
    assert "Objet : Vérifiez votre adresse" in format_messages([summary])
