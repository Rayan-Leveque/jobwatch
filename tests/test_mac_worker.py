"""Poste d'envoi Mac : le vrai worker Node dialogue avec le vrai `jw`.

SSH est remplacé par une exécution locale (`JOBWATCH_SSH=""`) ; seuls `claude`
et `playwright-cli` sont simulés par des scripts qui notent leurs appels.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_autoapply import _fake_bin, _prepared, instance  # noqa: F401  (fixture)

from jobwatch import serve
from jobwatch.db import connect
from jobwatch.submit import queue_submission

WORKER = Path(__file__).parent.parent / "ops" / "mac-worker" / "jobwatch-worker.mjs"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node absent")

SUBMITTED = {
    "status": "submitted", "reason": "Confirmation affichée.",
    "final_url": "https://ats.example.com/merci", "account_created_on": None,
}


def _worker_env(tmp_path: Path, config_path: Path, attach_ok: bool = True) -> dict[str, str]:
    bins = tmp_path / "mac-bin"
    bins.mkdir(exist_ok=True)
    calls = tmp_path / "browser-calls.txt"
    browser = _fake_bin(
        bins, "fake-playwright-cli",
        "import sys\n"
        f"open({str(calls)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"sys.exit(0 if {attach_ok!r} or sys.argv[1] != 'attach' else 1)\n",
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
    calls = (tmp_path / "browser-calls.txt").read_text().splitlines()
    assert calls[0] == "attach --extension=chrome"
    assert calls[1] == "tab-new https://example.com/offer"
    assert calls[-1] == "detach"
    argv = json.loads((tmp_path / "argv.json").read_text())
    assert "Bash(playwright-cli cookie*)" in argv


def test_worker_leaves_queue_untouched_when_chrome_is_unreachable(
    instance, tmp_path: Path  # noqa: F811
) -> None:
    db_path, config_path, cv_id = instance
    _worker_config(config_path)
    match_id = _prepared(db_path, cv_id)
    conn = connect(db_path)
    queue_submission(conn, match_id)
    conn.close()

    done = _run_worker(_worker_env(tmp_path, config_path, attach_ok=False))
    assert done.returncode == 0
    assert "Chrome injoignable" in done.stdout
    conn = connect(db_path)
    status = conn.execute(
        "SELECT status FROM submission_job WHERE match_id = ?", (match_id,)
    ).fetchone()[0]
    conn.close()
    assert status == "queued"


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
