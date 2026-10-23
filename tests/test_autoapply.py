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
from typing import ClassVar

import httpx
import pytest
from click.testing import CliRunner
from test_draft import _seed_cv, _seed_match

from jobwatch import cli as cli_module
from jobwatch import prepare, serve
from jobwatch.cli import cli
from jobwatch.config import AutoApplyConfig, ConfigError, ImapConfig, load_config
from jobwatch.db import connect, init_db
from jobwatch.mailcode import format_messages, parse_message
from jobwatch.prepare import offer_status
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


@pytest.fixture(autouse=True)
def _offers_online(monkeypatch) -> dict[str, int]:
    """Pas de réseau en test : chaque offre répond 200, sauf statut posé par le test."""
    statuses: dict[str, int] = {}
    monkeypatch.setattr(prepare, "offer_status", lambda url, _client: statuses.get(url, 200))
    return statuses


def test_prepare_skips_withdrawn_offers_without_using_the_daily_limit(
    instance, monkeypatch, _offers_online
) -> None:
    db_path, config_path, _cv_id = instance
    conn = connect(db_path)
    live = [
        _seed_match(conn, company=f"Vivante{i}", url=f"https://example.com/v{i}", fit="high")
        for i in range(2)
    ]
    gone_now = _seed_match(conn, company="Retirée", url="https://example.com/r", fit="high")
    gone_known = _seed_match(conn, company="Connue", url="https://example.com/k", fit="high")
    conn.execute(
        "UPDATE offer_content SET status = 'failed', failure_reason = 'http_404' "
        "WHERE offer_id = (SELECT offer_id FROM match WHERE id = ?)",
        (gone_known,),
    )
    conn.commit()
    conn.close()
    _offers_online["https://example.com/r"] = 410
    monkeypatch.setattr(prepare.draft, "run_job", _fake_letter(db_path))

    result = CliRunner().invoke(cli, ["prepare", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    assert "2 candidature(s) à valider" in result.output
    assert "offre retirée, écartée : Retirée" in result.output

    conn = connect(db_path)
    prepared = {row[0] for row in conn.execute("SELECT match_id FROM submission_job")}
    states = dict(conn.execute("SELECT id, state FROM match").fetchall())
    conn.close()
    assert prepared == set(live)
    assert states[gone_now] == states[gone_known] == "discarded"


def test_prepare_runs_alone_and_survives_server_restart(instance, monkeypatch) -> None:
    import fcntl

    db_path, config_path, _cv_id = instance
    conn = connect(db_path)
    match_id = _seed_match(conn, company="Seule", url="https://example.com/s", fit="high")
    killed = conn.execute(
        "INSERT INTO draft_job (match_id, track, status, owner) "
        "VALUES (?, 'engineer', 'running', 'prepare')", (match_id,),
    ).lastrowid
    from_ui = conn.execute(
        "INSERT INTO draft_job (match_id, track, status) VALUES (?, 'engineer', 'running')",
        (match_id,),
    ).lastrowid
    conn.commit()
    conn.close()
    # Le dashboard redémarre (déploiement) : seule la lettre lancée par lui échoue.
    serve._fail_interrupted_draft_jobs(db_path)
    monkeypatch.setattr(prepare.draft, "run_job", _fake_letter(db_path))

    with open(db_path.with_name(db_path.name + ".prepare.lock"), "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        busy = CliRunner().invoke(cli, ["prepare", "--config", str(config_path)])
    assert busy.exit_code == 0 and "préparation déjà en cours" in busy.output
    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM submission_job").fetchone()[0] == 0
    assert conn.execute(
        "SELECT status FROM draft_job WHERE id = ?", (killed,)
    ).fetchone()[0] == "running"
    conn.close()

    done = CliRunner().invoke(cli, ["prepare", "--config", str(config_path)])
    assert "1 candidature(s) à valider" in done.output
    conn = connect(db_path)
    statuses = dict(conn.execute("SELECT id, error FROM draft_job WHERE id IN (?, ?)",
                                 (killed, from_ui)).fetchall())
    conn.close()
    # Une préparation tuée laisse sa lettre « en cours » : la suivante la clôt.
    assert statuses == {killed: "préparation interrompue",
                        from_ui: "interrompu par un redémarrage du serveur"}


def test_prepare_stops_when_every_letter_fails(instance, monkeypatch) -> None:
    db_path, config_path, _cv_id = instance
    config_path.write_text(
        config_path.read_text().replace("daily_limit: 2", "daily_limit: 50"), encoding="utf-8"
    )
    conn = connect(db_path)
    for i in range(6):
        _seed_match(conn, company=f"Co{i}", url=f"https://example.com/{i}", fit="high")
    legacy = _seed_match(conn, company="Ancienne", url="jobwatch:ecb9b443", fit="high")
    conn.close()

    def broken(path: Path, _config, job_id: int) -> None:
        # Panne commune : le binaire du rédacteur est introuvable (PATH du timer).
        conn = connect(path)
        conn.execute(
            "UPDATE draft_job SET status = 'failed', "
            "error = 'appel codex échoué : No such file or directory' WHERE id = ?", (job_id,),
        )
        conn.commit()
        conn.close()

    monkeypatch.setattr(prepare.draft, "run_job", broken)
    alerts: list[str] = []
    monkeypatch.setattr(cli_module, "notify", lambda _c, title, body, _cl: alerts.append(title))
    result = CliRunner().invoke(cli, ["prepare", "--config", str(config_path)])
    assert result.exit_code == 1
    assert "préparation arrêtée : 3 lettres d'affilée en échec" in result.output
    assert alerts == ["jobwatch : préparation arrêtée"]
    conn = connect(db_path)
    attempted = conn.execute("SELECT COUNT(*) FROM submission_job").fetchone()[0]
    legacy_state = conn.execute("SELECT state FROM match WHERE id = ?", (legacy,)).fetchone()[0]
    conn.close()
    assert attempted == 3
    assert legacy_state == "discarded"


def test_prepare_keeps_offers_for_later_when_codex_quota_is_used_up(
    instance, monkeypatch
) -> None:
    db_path, config_path, _cv_id = instance
    conn = connect(db_path)
    match_id = _seed_match(conn, company="Co", url="https://example.com/1", fit="high")
    conn.close()

    def quota(path: Path, _config, job_id: int) -> None:
        conn = connect(path)
        conn.execute(
            "UPDATE draft_job SET status = 'failed', "
            "error = 'quota codex épuisé sur tous les comptes' WHERE id = ?", (job_id,),
        )
        conn.commit()
        conn.close()

    monkeypatch.setattr(prepare.draft, "run_job", quota)
    alerts: list[str] = []
    monkeypatch.setattr(cli_module, "notify", lambda _c, title, body, _cl: alerts.append(title))
    result = CliRunner().invoke(cli, ["prepare", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    assert "quota codex épuisé" in result.output
    assert alerts == []
    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM submission_job").fetchone()[0] == 0
    # Quota revenu : l'offre est reprise.
    monkeypatch.setattr(prepare.draft, "run_job", _fake_letter(db_path))
    result = CliRunner().invoke(cli, ["prepare", "--config", str(config_path)])
    status = conn.execute(
        "SELECT status FROM submission_job WHERE match_id = ?", (match_id,)
    ).fetchone()
    conn.close()
    assert result.exit_code == 0, result.output
    assert status is not None and status[0] != "failed"


def test_offer_status_follows_redirects_to_the_final_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/offre":
            return httpx.Response(301, headers={"Location": "/expiree"})
        return httpx.Response(410)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert offer_status("https://ats.example.com/offre", client) == 410
    with httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200))) as client:
        assert offer_status("https://ats.example.com/offre", client) == 200


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
    # Gardée au swipe : passe après la Priorité haute, donc hors limite ici.
    _seed_match(conn, company="Swipée", url="https://example.com/s", state="later")
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


def test_prepare_takes_swiped_matches_after_priority_ones(instance, monkeypatch) -> None:
    db_path, config_path, _cv_id = instance
    conn = connect(db_path)
    _seed_match(conn, company="Préparée", url="https://example.com/p", fit="high")
    swiped = _seed_match(conn, company="ManuelleCo", url="https://example.com/l", state="later")
    # Ancienne lettre du swipe : un nouveau dossier la réécrit au format court.
    conn.execute(
        "INSERT INTO draft_job (match_id, track, status) VALUES (?, 'engineer', 'ok')", (swiped,)
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(prepare.draft, "run_job", _fake_letter(db_path))
    result = CliRunner().invoke(cli, ["prepare", "--config", str(config_path)])
    assert "2 candidature(s) à valider" in result.output
    assert result.output.index("Préparée") < result.output.index("ManuelleCo")

    conn = connect(db_path)
    page = serve.render_page(conn, "all", draft_enabled=True)
    letters = conn.execute(
        "SELECT COUNT(*) FROM draft_job WHERE match_id = ?", (swiped,)
    ).fetchone()[0]
    conn.close()
    assert letters == 2
    review = page.split('data-section="review"')[1].split("</details>")[0]
    assert "Préparée" in review and "ManuelleCo" in review and "Envoyer la candidature" in review
    assert "ManuelleCo" not in page.split('data-section="later"')[1].split("</details>")[0]


def test_prepare_takes_medium_fit_when_configured(instance, monkeypatch) -> None:
    db_path, config_path, _cv_id = instance
    conn = connect(db_path)
    _seed_match(conn, company="MoyenneCo", url="https://example.com/m", fit="medium")
    _seed_match(conn, company="HauteCo", url="https://example.com/h", fit="high")
    conn.close()
    monkeypatch.setattr(prepare.draft, "run_job", _fake_letter(db_path))
    # Par défaut, seules les offres « haute » partent.
    result = CliRunner().invoke(cli, ["prepare", "--config", str(config_path)])
    assert "HauteCo" in result.output and "MoyenneCo" not in result.output

    config_path.write_text(
        config_path.read_text().replace("autoapply:\n", "autoapply:\n  fits: [high, medium]\n"),
        encoding="utf-8",
    )
    result = CliRunner().invoke(cli, ["prepare", "--config", str(config_path)])
    assert "MoyenneCo" in result.output, result.output


def test_prepare_caps_automatic_applications_per_company(instance, monkeypatch) -> None:
    db_path, config_path, _cv_id = instance
    config_path.write_text(
        config_path.read_text().replace("daily_limit: 2", "daily_limit: 50")
        .replace("autoapply:\n", "autoapply:\n  max_per_company: 2\n"),
        encoding="utf-8",
    )
    conn = connect(db_path)
    # Entités d'un même groupe : le plafond les compte ensemble.
    for i, company in enumerate(["Devoteam", "Devoteam Innovative Tech France", "Devoteam M Cloud"]):
        _seed_match(conn, title=f"AI Engineer {i}", company=company,
                    url=f"https://example.com/d{i}", fit="high")
    _seed_match(conn, title="Data Scientist", company="Devotion", url="https://example.com/o",
                fit="high")
    swiped = _seed_match(conn, title="PO IA", company="Devoteam", url="https://example.com/s",
                         state="later")
    conn.close()
    monkeypatch.setattr(prepare.draft, "run_job", _fake_letter(db_path))
    result = CliRunner().invoke(cli, ["prepare", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    conn = connect(db_path)
    prepared = conn.execute(
        "SELECT c.name FROM submission_job sj JOIN match m ON m.id = sj.match_id "
        "JOIN offer o ON o.id = m.offer_id JOIN company c ON c.id = o.company_id "
        "ORDER BY sj.id"
    ).fetchall()
    swiped_job = conn.execute(
        "SELECT 1 FROM submission_job WHERE match_id = ?", (swiped,)
    ).fetchone()
    conn.close()
    names = [row[0] for row in prepared]
    assert sum(name.startswith("Devoteam") for name in names) == 3  # 2 auto + 1 choisie à la main
    assert "Devotion" in names  # autre entreprise, malgré le préfixe commun
    assert swiped_job is not None


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


def _prepared(db_path: Path, cv_id: int, url: str = "https://example.com/offer") -> int:
    conn = connect(db_path)
    match_id = _seed_match(conn, state="later", fit="high", url=url)
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


class _FakeSMTP:
    """Remplace smtplib.SMTP_SSL : garde la connexion et les messages envoyés."""

    sent: ClassVar[list] = []
    logins: ClassVar[list] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        pass

    def login(self, user: str, password: str) -> None:
        _FakeSMTP.logins.append((self.host, user, password))

    def send_message(self, message) -> None:
        _FakeSMTP.sent.append(message)


MAIL_ONLY = {
    "status": "needs_human", "reason": "L'offre indique « Postuler par mail ».",
    "final_url": "https://emploi.example.gouv.fr/offre", "account_created_on": None,
    "apply_email": "recrutement@mairie.example.fr",
}


def _mail_only_submission(instance, tmp_path: Path, monkeypatch, smtp_host: str | None):
    from jobwatch import mailapply

    db_path, _config_path, cv_id = instance
    match_id = _prepared(db_path, cv_id)
    conn = connect(db_path)
    conn.execute(
        "UPDATE candidate_profile SET reusable_details = ?",
        ("Nom : Rayan Leveque\nEmail : rayan@example.com\nTéléphone : 06 00 00 00 00",),
    )
    conn.commit()
    job_id = queue_submission(conn, match_id)
    conn.close()
    config = _agent(tmp_path, MAIL_ONLY)
    config.imap.smtp_host = smtp_host
    _FakeSMTP.sent, _FakeSMTP.logins = [], []
    monkeypatch.setattr(mailapply.smtplib, "SMTP_SSL", _FakeSMTP)
    run_submission(db_path, config, job_id)
    conn = connect(db_path)
    job = conn.execute("SELECT * FROM submission_job WHERE id = ?", (job_id,)).fetchone()
    applied = conn.execute("SELECT 1 FROM application WHERE match_id = ?", (match_id,)).fetchone()
    conn.close()
    return job, applied


def test_letter_file_name_keeps_accented_letters() -> None:
    from jobwatch.submit import _file_stem

    assert _file_stem("Civils de la Défense - Ministère des Armées") == (
        "Civils_de_la_Defense_Ministere_des_Armees"
    )


def test_mail_only_offer_is_sent_by_email_with_cv_and_letter(
    instance, tmp_path: Path, monkeypatch
) -> None:
    job, applied = _mail_only_submission(instance, tmp_path, monkeypatch, "smtp.example.com")
    assert job["status"] == "submitted", job["reason"]
    assert applied is not None
    assert job["final_url"] == "mailto:recrutement@mairie.example.fr"
    assert _FakeSMTP.logins == [("smtp.example.com", "candidat@example.com", IMAP_SECRET)]
    (message,) = _FakeSMTP.sent
    assert message["To"] == "recrutement@mairie.example.fr"
    assert message["From"] == "Rayan Leveque <candidat@example.com>"
    assert message["Subject"] == "Candidature - AI Engineer"
    names = sorted(part.get_filename() for part in message.iter_attachments())
    assert names == ["Lettre_motivation_Acme.pdf", "cv.txt"]
    body = message.get_body(("plain",)).get_content()
    assert "poste de AI Engineer" in body and "Rayan Leveque\n06 00 00 00 00" in body


def test_mail_only_offer_without_smtp_hands_back_the_address(
    instance, tmp_path: Path, monkeypatch
) -> None:
    job, applied = _mail_only_submission(instance, tmp_path, monkeypatch, None)
    assert job["status"] == "needs_human"
    assert applied is None and _FakeSMTP.sent == []
    assert "recrutement@mairie.example.fr" in job["reason"]
    assert "smtp_host" in job["reason"]


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
    serve._interrupt_submission_jobs(db_path, None)
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


def test_auto_send_queues_prepared_dossiers_but_not_failed_letters(
    instance, monkeypatch
) -> None:
    db_path, config_path, _cv_id = instance
    config_path.write_text(
        config_path.read_text() + "  runner: worker\n  auto_send: true\n", encoding="utf-8"
    )
    conn = connect(db_path)
    good = _seed_match(conn, company="Bonne", url="https://example.com/g", fit="high")
    bad = _seed_match(conn, company="Ratée", url="https://example.com/b", fit="high")
    conn.close()
    letter = _fake_letter(db_path)

    def run_job(path: Path, config, job_id: int) -> None:
        check = connect(path)
        match_id = check.execute(
            "SELECT match_id FROM draft_job WHERE id = ?", (job_id,)
        ).fetchone()[0]
        check.close()
        if match_id == bad:
            fail = connect(path)
            fail.execute(
                "UPDATE draft_job SET status = 'failed', error = 'quota' WHERE id = ?", (job_id,)
            )
            fail.commit()
            fail.close()
            return
        letter(path, config, job_id)

    monkeypatch.setattr(prepare.draft, "run_job", run_job)
    result = CliRunner().invoke(cli, ["prepare", "--config", str(config_path)])
    assert result.exit_code == 0, result.output

    conn = connect(db_path)
    statuses = dict(conn.execute("SELECT match_id, status FROM submission_job").fetchall())
    conn.close()
    assert statuses == {good: "queued", bad: "failed"}


def test_auto_send_requires_a_worker(tmp_path: Path, instance) -> None:
    _db_path, config_path, _cv_id = instance
    config_path.write_text(config_path.read_text() + "  auto_send: true\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="runner: worker"):
        load_config(config_path)
