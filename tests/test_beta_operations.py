from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest
import yaml
from click.testing import CliRunner
from test_serve_auth import _request, _start_server

from jobwatch.auth import accept_invite, create_invite, create_session
from jobwatch.backup import BackupError, create_backup, restore_backup
from jobwatch.cli import cli
from jobwatch.collectors.linkedin import LinkedInCollector
from jobwatch.config import load_config
from jobwatch.db import connect
from jobwatch.library import save_upload
from jobwatch.onboarding import complete_profile


def _instance(tmp_path, monkeypatch, slug):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    result = CliRunner().invoke(cli, ["--instance", slug, "init", "--beta"])
    assert result.exit_code == 0, result.output
    path = tmp_path / "config" / "jobwatch" / "instances" / slug / "config.yaml"
    config = load_config(path)
    conn = connect(config.db)
    invite = create_invite(conn, slug, f"{slug}@example.com")
    owner = accept_invite(conn, invite, "une phrase de passe privée", workspace_slug=slug)
    workspace = conn.execute("SELECT id FROM workspace").fetchone()[0]
    return path, config, conn, owner, workspace


def test_collect_uses_profile_location_and_deduplicates(tmp_path, monkeypatch):
    _path, _config, conn, owner, workspace = _instance(tmp_path, monkeypatch, "alice")
    calls = []

    def response(request):
        calls.append(dict(request.url.params))
        return httpx.Response(200, text='''<li data-entity-urn="urn:li:jobPosting:123">
          <h3 class="base-search-card__title">Business Analyst</h3>
          <h4 class="base-search-card__subtitle">Entreprise test</h4>
          <span class="job-search-card__location">Lyon</span></li>''')

    client = httpx.Client(transport=httpx.MockTransport(response))
    monkeypatch.setattr(LinkedInCollector, "_request_client", lambda self: client)
    runner = CliRunner()
    args = ["--instance", "alice", "run"]
    assert runner.invoke(cli, args).exit_code == 0
    assert calls == []  # Aucun réseau avant confirmation du profil.
    complete_profile(conn, owner, workspace, [],
                     [{"label": "MOA", "keywords": ["Business Analyst"]}],
                     locations=["Lyon"], include_remote=True, cover_letters_enabled=False)
    first = runner.invoke(cli, args)
    assert first.exit_code == 0, first.output
    assert calls[0]["keywords"] == "Business Analyst"
    assert calls[0]["location"] == "Lyon"
    assert calls[1]["location"] == "France" and calls[1]["f_WT"] == "2"
    second = runner.invoke(cli, args)
    assert second.exit_code == 0
    assert "0 nouvelles offres" in second.output
    assert conn.execute("SELECT COUNT(*) FROM match").fetchone()[0] == 1
    client.close()
    conn.close()


def test_backup_restore_documents_and_cookie_isolation(tmp_path, monkeypatch):
    path, config, conn, _owner, _workspace = _instance(tmp_path, monkeypatch, "alice")
    content = b"%PDF-1.4 document prive"
    entry = save_upload(conn, config.db, "cv", "Mon CV", "cv.pdf", base64.b64encode(content).decode())
    token, _ = create_session(conn, "alice@example.com", "une phrase de passe privée", "alice")
    conn.close()
    backup = tmp_path / "backup"
    create_backup(path, backup)
    restored_path = restore_backup(backup, tmp_path / "restored")
    restored = load_config(restored_path)
    restored_conn = connect(restored.db)
    document = restored_conn.execute("SELECT file_path FROM document_library").fetchone()[0]
    assert str(tmp_path / "restored") in document
    assert restored_conn.execute("SELECT COUNT(*) FROM web_session").fetchone()[0] == 0
    restored_conn.close()
    server, thread = _start_server(restored.db, workspace_slug="alice")
    try:
        status, _, _ = _request(server.server_address[1], "GET", f"/documents/{entry['id']}",
                                headers={"Cookie": f"id={token}"})
        assert status == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    assert Path(document).read_bytes() == content
    with pytest.raises(FileExistsError):
        restore_backup(backup, tmp_path / "restored")
    (backup / "config.yaml").write_text("altération")
    with pytest.raises(BackupError, match="altérée"):
        restore_backup(backup, tmp_path / "tampered")
    assert not (tmp_path / "tampered").exists()


def test_separate_instances_reject_each_others_session(tmp_path, monkeypatch):
    _, _, alice, _, _ = _instance(tmp_path, monkeypatch, "alice")
    token, _ = create_session(alice, "alice@example.com", "une phrase de passe privée", "alice")
    alice.close()
    _, bob_config, bob, _, _ = _instance(tmp_path, monkeypatch, "bob")
    save_upload(bob, bob_config.db, "cv", "CV Bob", "cv.pdf",
                base64.b64encode(b"%PDF-1.4 bob").decode())
    bob.close()
    server, thread = _start_server(bob_config.db, workspace_slug="bob")
    try:
        for route in ("/documents/1", "/match/1/draft/status"):
            status, _, _ = _request(server.server_address[1], "GET", route,
                                    headers={"Cookie": f"id={token}"})
            assert status == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_failed_collection_is_visible_to_supervisor(tmp_path, monkeypatch):
    _, _, conn, owner, workspace = _instance(tmp_path, monkeypatch, "alice")
    complete_profile(conn, owner, workspace, [], [{"label": "MOA", "keywords": ["MOA"]}])
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(429)))
    monkeypatch.setattr(LinkedInCollector, "_request_client", lambda self: client)
    result = CliRunner().invoke(cli, ["--instance", "alice", "run"])
    assert result.exit_code == 1
    assert "collecte incomplète" in result.output
    assert conn.execute("SELECT value FROM instance_setting WHERE key='last_collection'").fetchone() is None
    client.close()
    conn.close()


def test_beta_config_disables_all_ai(tmp_path, monkeypatch):
    path, config, conn, _, _ = _instance(tmp_path, monkeypatch, "alice")
    assert config.draft is config.enrich is config.research is None
    assert config.sources.linkedin.from_profile
    assert yaml.safe_load(path.read_text())["searches"] == []
    conn.close()
