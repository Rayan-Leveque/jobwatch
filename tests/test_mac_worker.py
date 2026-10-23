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
    "apply_email": None, "blocker": None,
    # Offres connexes vues sur le site : une valide, une sans URL exacte écartée.
    "related_offers": [
        {"title": "AI Engineer junior", "url": "https://ats.example.com/jobs/42",
         "company": "Acme", "location": "Paris"},
        {"title": "Data Scientist", "url": "voir la page carrières", "company": "Acme",
         "location": None},
    ],
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
    # Un second dossier en file part dans le même passage, sans attendre launchd.
    second_match = _prepared(db_path, cv_id, url="https://example.com/offer-2")
    second_job = queue_submission(conn, second_match)
    conn.execute("UPDATE search SET include_json = '[\"AI\"]'")
    conn.commit()
    conn.close()

    done = _run_worker(_worker_env(tmp_path, config_path))
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"job {job_id} : submitted" in done.stdout
    assert f"job {second_job} : submitted" in done.stdout

    conn = connect(db_path)
    job = conn.execute("SELECT * FROM submission_job WHERE id = ?", (job_id,)).fetchone()
    applied = conn.execute("SELECT 1 FROM application WHERE match_id = ?", (match_id,)).fetchone()
    related = conn.execute(
        "SELECT o.url, COUNT(m.id) FROM offer o LEFT JOIN match m ON m.offer_id = o.id "
        "WHERE o.title IN ('AI Engineer junior', 'Data Scientist') GROUP BY o.id"
    ).fetchall()
    conn.close()
    assert job["status"] == "submitted"
    # L'offre connexe entre en base et dans le matching, prête à être notée.
    assert [tuple(row) for row in related] == [("https://ats.example.com/jobs/42", 1)]
    assert applied is not None
    assert Path(job["proof_path"]).read_bytes() == b"png-mac"

    mac_dir = tmp_path / "mac-work" / str(job_id)
    received = json.loads((mac_dir / "files.json").read_text())
    assert "dossier.md" in received and "Lettre_motivation_Acme.pdf" in received
    assert not any(name.startswith(".mot_de_passe") for name in received)
    # Le faux `jw` du Mac refuse toute autre commande que mail-code.
    assert (mac_dir / "shim_rc.txt").read_text() != "0"
    # Chrome en arrière-plan sur le profil dédié, fermé après l'agent : le Chrome habituel
    # n'est pas touché et aucune fenêtre n'apparaît.
    calls = (tmp_path / "browser-calls.txt").read_text().splitlines()
    assert calls[:2] == [
        f"open https://example.com/offer --browser=chrome --profile={tmp_path / 'mac-profile'}",
        "close",
    ]
    argv = json.loads((tmp_path / "argv.json").read_text())
    assert "Bash(playwright-cli cookie*)" in argv

    # L'icône de barre des menus montre l'envoi et le compteur.
    status = json.loads((_support_dir(tmp_path) / "status.json").read_text())
    assert status["current"] is None and status["sent"] == 2
    assert status["history"][0]["status"] == "submitted"
    assert status["history"][0]["label"].startswith("Acme - ")
    menu = _run_menubar(tmp_path).splitlines()
    assert menu[0] == "2 | sfimage=paperplane"
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
         "Acorus | sfimage=paperplane.fill length=22"),
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


def test_captcha_reopens_the_dossier_in_a_visible_window(
    instance, tmp_path: Path  # noqa: F811
) -> None:
    db_path, config_path, cv_id = instance
    _worker_config(config_path)
    match_id = _prepared(db_path, cv_id)
    conn = connect(db_path)
    job_id = queue_submission(conn, match_id)
    conn.close()
    env = _worker_env(tmp_path, config_path)
    blocked = {**SUBMITTED, "status": "needs_human", "reason": "Captcha.", "blocker": "captcha"}
    # Arrêt sur captcha en arrière-plan ; avec le candidat devant l'écran, l'envoi passe.
    env["CLAUDE_BIN"] = str(_fake_bin(
        tmp_path / "mac-bin", "fake-claude-captcha",
        "import json, sys\n"
        "assisted = 'Reprise assistée' in sys.argv[sys.argv.index('-p') + 1]\n"
        f"result = json.loads({json.dumps(json.dumps(SUBMITTED))}) if assisted "
        f"else json.loads({json.dumps(json.dumps(blocked))})\n"
        "print(json.dumps({'structured_output': result}))\n",
    ))

    done = _run_worker(env)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "captcha demandé, reprise en fenêtre visible" in done.stdout
    assert f"job {job_id} : submitted" in done.stdout
    opens = [
        call for call in (tmp_path / "browser-calls.txt").read_text().splitlines()
        if call.startswith("open ")
    ]
    assert len(opens) == 2
    assert not opens[0].endswith("--headed") and opens[1].endswith("--headed")


def test_worker_runs_codex_in_its_sandbox(instance, tmp_path: Path) -> None:  # noqa: F811
    db_path, config_path, cv_id = instance
    config_path.write_text(
        config_path.read_text() + "  runner: worker\n  agent: codex\n  model: gpt-6.1-sol\n",
        encoding="utf-8",
    )
    match_id = _prepared(db_path, cv_id)
    conn = connect(db_path)
    job_id = queue_submission(conn, match_id)
    conn.close()
    env = _worker_env(tmp_path, config_path)
    # Socket de la session, tel que playwright-cli le pose dans le dossier temporaire.
    sockets = tmp_path / "tmp" / "pw-test" / "cli"
    sockets.mkdir(parents=True)
    (sockets / "abc-jobwatch-mac.sock").touch()
    env["TMPDIR"] = str(tmp_path / "tmp")
    # Le compte principal a épuisé son quota : le compte de secours prend le relais.
    env["JOBWATCH_CODEX_FALLBACK_HOMES"] = str(tmp_path / "codex-spare")
    env["CODEX_BIN"] = str(_fake_bin(
        tmp_path / "mac-bin", "fake-codex",
        "import json, os, pathlib, subprocess, sys\n"
        "auth = os.readlink(os.path.join(os.environ['CODEX_HOME'], 'auth.json'))\n"
        f"open({str(tmp_path / 'accounts.txt')!r}, 'a').write(auth + '\\n')\n"
        "if 'codex-spare' not in auth:\n"
        "    print(\"ERROR: You've hit your usage limit.\", file=sys.stderr); sys.exit(1)\n"
        f"pathlib.Path({str(tmp_path / 'argv.json')!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "run = lambda *a: subprocess.run(['playwright-cli', *a], capture_output=True).returncode\n"
        "pathlib.Path('filtre.json').write_text(json.dumps([\n"
        "    run('cookie-list'), run('state-save', 'x'), run('upload', '/etc/passwd'),\n"
        "    run('upload', '../dossier.md'), run('upload', 'cv.txt')]))\n"
        "pathlib.Path('home.txt').write_text(os.environ['CODEX_HOME'])\n"
        f"pathlib.Path('agent_result.json').write_text({json.dumps(json.dumps(SUBMITTED))})\n"
        "print('journal codex')\n",
    ))

    done = _run_worker(env)
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"job {job_id} : submitted" in done.stdout
    assert (tmp_path / "accounts.txt").read_text().split() == [
        str(tmp_path / ".codex" / "auth.json"), str(tmp_path / "codex-spare" / "auth.json"),
    ]
    argv = json.loads((tmp_path / "argv.json").read_text())
    assert argv[:2] == ["exec", "--skip-git-repo-check"] and "gpt-6.1-sol" in argv
    mac_dir = tmp_path / "mac-work" / str(job_id)
    # Cookies, export de session et upload hors du dossier refusés ; le CV passe.
    assert json.loads((mac_dir / "filtre.json").read_text()) == [1, 1, 1, 1, 0]
    assert (tmp_path / "browser-calls.txt").read_text().splitlines()[-2] == "upload cv.txt"
    home = Path((mac_dir / "home.txt").read_text())
    config = (home / "config.toml").read_text()
    assert '":workspace_roots" = "write"' in config
    assert f'"{sockets.resolve()}" = "allow"' in config
    assert str(tmp_path / "Library" / "Application Support") + '/jobwatch-worker/mailcode"' in config
    rules = (home / "rules" / "jobwatch.rules").read_text()
    assert '"curl"' in rules and str(tmp_path / "mac-bin" / "fake-playwright-cli") in rules


def test_exhausted_codex_accounts_keep_the_dossier_queued(
    instance, tmp_path: Path  # noqa: F811
) -> None:
    db_path, config_path, cv_id = instance
    config_path.write_text(
        config_path.read_text() + "  runner: worker\n  agent: codex\n  model: gpt-6.1-sol\n",
        encoding="utf-8",
    )
    match_id = _prepared(db_path, cv_id)
    conn = connect(db_path)
    job_id = queue_submission(conn, match_id)
    conn.close()
    env = _worker_env(tmp_path, config_path)
    sockets = tmp_path / "tmp" / "pw-test" / "cli"
    sockets.mkdir(parents=True)
    (sockets / "abc-jobwatch-mac.sock").touch()
    env["TMPDIR"] = str(tmp_path / "tmp")
    env["JOBWATCH_CODEX_FALLBACK_HOMES"] = str(tmp_path / "codex-spare")
    env["CODEX_BIN"] = str(_fake_bin(
        tmp_path / "mac-bin", "fake-codex",
        "import sys\n"
        f"open({str(tmp_path / 'codex-calls.txt')!r}, 'a').write('x\\n')\n"
        "print(\"ERROR: You've hit your usage limit. Try again at 2:43 AM.\", file=sys.stderr)\n"
        "sys.exit(1)\n",
    ))

    done = _run_worker(env)
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"job {job_id} : queued" in done.stdout
    conn = connect(db_path)
    status = conn.execute("SELECT status FROM submission_job WHERE id = ?", (job_id,)).fetchone()[0]
    conn.close()
    assert status == "queued"
    assert len((tmp_path / "codex-calls.txt").read_text().split()) == 2  # un essai par compte

    # Passage suivant avant la reprise : aucun Chrome, aucun appel Codex.
    (tmp_path / "browser-calls.txt").unlink()
    done = _run_worker(env)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not (tmp_path / "browser-calls.txt").exists()
    assert len((tmp_path / "codex-calls.txt").read_text().split()) == 2
    menu = _run_menubar(tmp_path).splitlines()
    assert menu[0].endswith("sfimage=hourglass")
    assert "Quota Codex épuisé, reprise vers 02:43" in menu


@pytest.mark.parametrize(
    ("command", "ok"),
    [
        ("mail-code --minutes 15", True),
        ("mail-code --minutes 15; rm -rf ~", False),
        ("mail-code --minutes 1 2", False),
        ("list", False),
        (None, False),
    ],
)
def test_mail_code_key_runs_nothing_else(tmp_path: Path, command, ok) -> None:
    jw = _fake_bin(tmp_path, "jw", "import sys\nprint(' '.join(sys.argv[1:]))\n")
    env = {"PATH": "/usr/bin:/bin"}
    if command is not None:
        env["SSH_ORIGINAL_COMMAND"] = command
    done = subprocess.run(
        [str(MAC_WORKER / "mail-code-only.sh"), str(jw), "--instance", "rayan"],
        env=env, capture_output=True, text=True, check=False,
    )
    assert (done.returncode == 0) is ok
    assert done.stdout == ("--instance rayan mail-code --minutes 15\n" if ok else "")


def test_two_lanes_share_the_queue_without_double_sending(
    instance, tmp_path: Path  # noqa: F811
) -> None:
    db_path, config_path, cv_id = instance
    _worker_config(config_path)
    jobs = []
    for i in range(4):
        match_id = _prepared(db_path, cv_id, url=f"https://example.com/offer-{i}")
        conn = connect(db_path)
        jobs.append(queue_submission(conn, match_id))
        conn.close()
    env = _worker_env(tmp_path, config_path)
    # Profil déjà cloné pour la voie 2 (sinon elle attend sans réclamer).
    (tmp_path / "mac-profile").mkdir()
    lanes = [
        subprocess.Popen(
            [NODE, str(WORKER)], env={**env, "JOBWATCH_LANE": lane},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        for lane in ("1", "2")
    ]
    outputs = [lane.communicate(timeout=120)[0] for lane in lanes]
    assert all(lane.returncode == 0 for lane in lanes), outputs

    sent = [job for job in jobs for out in outputs if f"job {job} : submitted" in out]
    assert sorted(sent) == sorted(jobs)  # chaque dossier une fois, et une seule
    support = _support_dir(tmp_path)
    assert (support / "status.json").exists() and (support / "status-2.json").exists()
    menu = _run_menubar(tmp_path).splitlines()
    assert menu[0] == "4 | sfimage=paperplane"
    assert sum(line.startswith("✓ ") for line in menu) == 4


def test_paused_worker_leaves_the_queue_alone(instance, tmp_path: Path) -> None:  # noqa: F811
    db_path, config_path, cv_id = instance
    _worker_config(config_path)
    match_id = _prepared(db_path, cv_id)
    conn = connect(db_path)
    queue_submission(conn, match_id)
    conn.close()
    _support_dir(tmp_path).mkdir(parents=True)
    (_support_dir(tmp_path) / "pause").touch()

    done = _run_worker(_worker_env(tmp_path, config_path))
    assert done.returncode == 0, done.stdout + done.stderr
    conn = connect(db_path)
    status = conn.execute(
        "SELECT status FROM submission_job WHERE match_id = ?", (match_id,)
    ).fetchone()[0]
    conn.close()
    assert status == "queued" and not (tmp_path / "browser-calls.txt").exists()
    menu = _run_menubar(tmp_path).splitlines()
    assert menu[0].endswith("sfimage=pause.circle") and "En pause" in menu
    assert any(line.startswith("Reprendre les envois") for line in menu)


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
