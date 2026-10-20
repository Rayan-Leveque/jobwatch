"""Poste d'envoi Mac : le vrai worker Node dialogue avec le vrai `jw`.

SSH est remplacé par une exécution locale (`JOBWATCH_SSH=""`) ; seuls `claude`
et `playwright-cli` sont simulés par des scripts qui notent leurs appels.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_autoapply import _fake_bin, _prepared, instance  # noqa: F401  (fixture)

from jobwatch import serve
from jobwatch.db import connect
from jobwatch.submit import queue_submission

MAC_WORKER = Path(__file__).parent.parent / "ops" / "mac-worker"
WORKER = MAC_WORKER / "jobwatch-worker.mjs"
MENUBAR = MAC_WORKER / "jobwatch-menubar.mjs"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node absent")

SUBMITTED = {
    "status": "submitted", "reason": "Confirmation affichée.",
    "final_url": "https://ats.example.com/merci", "account_created_on": None,
}


def _worker_env(
    tmp_path: Path, config_path: Path, open_ok: bool = True, open_stdout: str = ""
) -> dict[str, str]:
    bins = tmp_path / "mac-bin"
    bins.mkdir(exist_ok=True)
    calls = tmp_path / "browser-calls.txt"
    browser = _fake_bin(
        bins, "fake-playwright-cli",
        "import sys\n"
        f"open({str(calls)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"print({open_stdout!r} if sys.argv[1] == 'open' else '')\n"
        f"sys.exit(0 if {open_ok!r} or sys.argv[1] != 'open' else 1)\n",
    )
    claude = _fake_bin(
        bins, "fake-claude",
        "import json, os, pathlib, subprocess, sys\n"
        f"pathlib.Path({str(tmp_path / 'argv.json')!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "pathlib.Path('files.json').write_text(json.dumps(sorted(os.listdir('.'))))\n"
        "rc = subprocess.run(['jw', 'list'], capture_output=True).returncode\n"
        "pathlib.Path('shim_rc.txt').write_text(str(rc))\n"
        "pathlib.Path('preuve.png').write_bytes(b'png-mac')\n"
        f"print(json.dumps({{'structured_output': json.loads({json.dumps(SUBMITTED)!r})}}))\n",
    )
    return {
        "PATH": "/usr/bin:/bin",
        "JOBWATCH_SSH": "",
        # Même forme qu'en production : instance nommée avant la sous-commande.
        "JOBWATCH_JW": f"{sys.executable} -m jobwatch.cli --instance mac",
        "XDG_CONFIG_HOME": str(_instance_home(tmp_path, config_path)),
        "CLAUDE_BIN": str(claude),
        "PLAYWRIGHT_CLI": str(browser),
        "JOBWATCH_WORK_ROOT": str(tmp_path / "mac-work"),
        "JOBWATCH_PROFILE": str(tmp_path / "mac-profile"),
        "HOME": str(tmp_path),
    }


def _instance_home(tmp_path: Path, config_path: Path) -> Path:
    home = tmp_path / "xdg"
    target = home / "jobwatch" / "instances" / "mac"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(config_path, target / "config.yaml")
    return home


def _run_worker(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [NODE, str(WORKER)], env=env, capture_output=True, text=True, timeout=120, check=False
    )


def _worker_config(config_path: Path) -> Path:
    config_path.write_text(config_path.read_text() + "  runner: worker\n", encoding="utf-8")
    return config_path


def test_worker_claims_dossier_runs_agent_and_records_application(
    instance, tmp_path: Path  # noqa: F811
) -> None:
    db_path, config_path, cv_id = instance
    _worker_config(config_path)
    match_id = _prepared(db_path, cv_id)
    conn = connect(db_path)
    job_id = queue_submission(conn, match_id)
    conn.close()

    done = _run_worker(_worker_env(tmp_path, config_path))
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"job {job_id} : submitted" in done.stdout

    conn = connect(db_path)
    job = conn.execute("SELECT * FROM submission_job WHERE id = ?", (job_id,)).fetchone()
    applied = conn.execute("SELECT 1 FROM application WHERE match_id = ?", (match_id,)).fetchone()
    conn.close()
    assert job["status"] == "submitted"
    assert applied is not None
    assert Path(job["proof_path"]).read_bytes() == b"png-mac"

    mac_dir = tmp_path / "mac-work" / str(job_id)
    received = json.loads((mac_dir / "files.json").read_text())
    assert "dossier.md" in received and "Lettre_motivation_Acme.pdf" in received
    assert not any(name.startswith(".mot_de_passe") for name in received)
    # Le faux `jw` du Mac refuse toute autre commande que mail-code.
    assert (mac_dir / "shim_rc.txt").read_text() != "0"
    # Fenêtre visible sur le profil dédié, fermée après l'agent : le Chrome habituel n'est pas touché.
    calls = (tmp_path / "browser-calls.txt").read_text().splitlines()
    assert calls == [
        (
            f"open https://example.com/offer --browser=chrome --profile={tmp_path / 'mac-profile'}"
            " --headed"
        ),
        "close",
    ]
    argv = json.loads((tmp_path / "argv.json").read_text())
    assert "Bash(playwright-cli cookie*)" in argv

    # L'icône de barre des menus montre l'envoi et le compteur.
    status = json.loads((_support_dir(tmp_path) / "status.json").read_text())
    assert status["current"] is None and status["sent"] == 1
    assert status["history"][0]["status"] == "submitted"
    assert status["history"][0]["label"].startswith("Acme - ")
    menu = _run_menubar(tmp_path).splitlines()
    assert menu[0] == "1 | sfimage=paperplane"
    assert any(line.startswith("✓ Acme - ") for line in menu)


def _support_dir(home: Path) -> Path:
    return home / "Library" / "Application Support" / "jobwatch-worker"


def _run_menubar(home: Path) -> str:
    return subprocess.run(
        [NODE, str(MENUBAR)], env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=30, check=True,
    ).stdout


@pytest.mark.parametrize(
    ("status", "first_line"),
    [
        (None, "| sfimage=exclamationmark.triangle.fill sfcolor=#d33"),
        ({"current": None, "error": "serveur muet", "sent": 3, "minutes_ago": 1},
         "| sfimage=exclamationmark.triangle.fill sfcolor=#d33"),
        # Mac éteint ou launchd arrêté : plus aucun passage.
        ({"current": None, "error": None, "sent": 3, "minutes_ago": 30},
         "| sfimage=exclamationmark.triangle.fill sfcolor=#d33"),
        ({"current": {"label": "Acorus - AI Product Manager", "timeout": 1800},
          "error": None, "sent": 3, "minutes_ago": 1},
         "Acorus | sfimage=paperplane.fill length=18"),
    ],
)
def test_menubar_shows_worker_state(tmp_path: Path, status, first_line) -> None:
    if status is not None:
        from datetime import UTC, datetime, timedelta

        moment = (datetime.now(UTC) - timedelta(minutes=status.pop("minutes_ago"))).isoformat()
        if status["current"]:
            status["current"]["started"] = moment
        _support_dir(tmp_path).mkdir(parents=True)
        (_support_dir(tmp_path) / "status.json").write_text(
            json.dumps({**status, "updated": moment, "history": []})
        )
    assert _run_menubar(tmp_path).splitlines()[0] == first_line


def test_worker_without_queued_dossier_launches_no_browser(
    instance, tmp_path: Path  # noqa: F811
) -> None:
    _db_path, config_path, _cv_id = instance
    _worker_config(config_path)
    done = _run_worker(_worker_env(tmp_path, config_path))
    assert done.returncode == 0, done.stdout + done.stderr
    assert not (tmp_path / "browser-calls.txt").exists()


@pytest.fixture
def offer_site() -> Iterator[ThreadingHTTPServer]:
    """Site d'offre local qui répond le code HTTP posé dans `server.status`."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(self.server.status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


@pytest.mark.parametrize(
    ("site_status", "open_ok", "open_stdout", "job_status", "reason"),
    [
        (200, False, "", "failed", "Chrome ne s'ouvre pas"),
        # Chrome échoue sur une offre retirée (ERR_HTTP_RESPONSE_CODE_FAILURE) :
        # ce n'est pas une panne du Mac, l'humain décide d'écarter l'offre.
        (410, False, "", "needs_human", "offre expirée (HTTP 410)"),
        # En fenêtre visible, Chrome affiche la page 410 : l'agent n'est pas lancé.
        (200, True, "### Page\n- HTTP status: 410\n", "needs_human", "offre expirée (HTTP 410)"),
    ],
)
def test_worker_hands_back_dossier_when_offer_page_does_not_open(
    instance, tmp_path: Path, offer_site,  # noqa: F811
    site_status, open_ok, open_stdout, job_status, reason,
) -> None:
    db_path, config_path, cv_id = instance
    _worker_config(config_path)
    match_id = _prepared(db_path, cv_id)
    offer_site.status = site_status
    conn = connect(db_path)
    conn.execute(
        "UPDATE offer SET url = ? WHERE id = (SELECT offer_id FROM match WHERE id = ?)",
        (f"http://127.0.0.1:{offer_site.server_port}/offre", match_id),
    )
    conn.commit()
    queue_submission(conn, match_id)
    conn.close()

    done = _run_worker(_worker_env(tmp_path, config_path, open_ok, open_stdout))
    assert done.returncode == 0, done.stdout + done.stderr
    conn = connect(db_path)
    job = conn.execute(
        "SELECT status, reason FROM submission_job WHERE match_id = ?", (match_id,)
    ).fetchone()
    conn.close()
    assert not (tmp_path / "argv.json").exists()
    assert job["status"] == job_status
    assert reason in job["reason"]
    # Le navigateur est ouvert même quand la page échoue : il ne doit pas rester.
    assert (tmp_path / "browser-calls.txt").read_text().splitlines()[-1] == "close"


def test_worker_failure_sends_an_alert(instance, monkeypatch) -> None:  # noqa: F811
    from click.testing import CliRunner

    from jobwatch import cli as cli_module

    db_path, config_path, cv_id = instance
    _worker_config(config_path)
    match_id = _prepared(db_path, cv_id)
    conn = connect(db_path)
    job_id = queue_submission(conn, match_id)
    conn.close()
    alerts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        cli_module, "notify", lambda _config, title, body, _client: alerts.append((title, body))
    )
    runner = CliRunner()
    runner.invoke(cli_module.cli, ["submission-claim", "--config", str(config_path)])
    done = runner.invoke(
        cli_module.cli, ["submission-finish", str(job_id), "--config", str(config_path)],
        input=json.dumps({"error": "ENOSPC: no space left on device"}),
    )
    assert done.output.startswith("failed")
    assert alerts == [("jobwatch : poste d'envoi en panne",
                       "poste d'envoi : ENOSPC: no space left on device")]


def test_worker_timeout_hands_dossier_back(instance, tmp_path: Path) -> None:  # noqa: F811
    db_path, config_path, cv_id = instance
    _worker_config(config_path)
    match_id = _prepared(db_path, cv_id)
    conn = connect(db_path)
    job_id = queue_submission(conn, match_id)
    conn.close()
    claim = subprocess.run(
        [sys.executable, "-m", "jobwatch.cli", "submission-claim", "--config", str(config_path)],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(claim.stdout)["job_id"] == job_id
    finish = subprocess.run(
        [sys.executable, "-m", "jobwatch.cli", "submission-finish", str(job_id),
         "--config", str(config_path)],
        input=json.dumps({"timeout": True}), capture_output=True, text=True, check=True,
    )
    assert finish.stdout.startswith("needs_human")


def test_server_restart_keeps_remote_submission_running(instance) -> None:  # noqa: F811
    db_path, _config_path, cv_id = instance
    match_id = _prepared(db_path, cv_id)
    conn = connect(db_path)
    conn.execute("UPDATE submission_job SET status = 'running' WHERE match_id = ?", (match_id,))
    conn.commit()
    conn.close()

    from jobwatch.config import AutoApplyConfig

    serve._interrupt_submission_jobs(
        db_path, AutoApplyConfig(browser_profile=db_path.parent / "p", runner="worker")
    )
    conn = connect(db_path)
    status = conn.execute(
        "SELECT status FROM submission_job WHERE match_id = ?", (match_id,)
    ).fetchone()[0]
    conn.close()
    assert status == "running"
