from __future__ import annotations

import json

from test_serve_auth import _request, _start_server

from jobwatch.auth import accept_invite, create_invite, create_session, resolve_session
from jobwatch.db import connect, init_db


def test_manual_onboarding_has_preset_and_individual_location(tmp_path):
    db = tmp_path / "jobwatch.db"
    conn = connect(db)
    init_db(conn)
    invite = create_invite(conn, "alice", "alice@example.com")
    accept_invite(conn, invite, "une phrase de passe privée", workspace_slug="alice")
    token, session = create_session(conn, "alice@example.com", "une phrase de passe privée", "alice")
    conn.close()
    server, thread = _start_server(db, workspace_slug="alice", onboarding_enabled=True)
    try:
        headers = {"Cookie": f"id={token}", "X-CSRF-Token": session.csrf_token}
        status, _, body = _request(server.server_address[1], "GET", "/onboarding", headers=headers)
        assert status == 200
        assert 'id="choose-po-moa"' in body
        assert 'id="locations"' in body
        assert 'id="choose-cv" type="button" hidden' in body
        status, _, body = _request(
            server.server_address[1], "POST", "/onboarding/complete", headers=headers,
            body=json.dumps({"intents": [{"label": "MOA", "keywords": ["MOA"]}],
                             "locations": ["Lyon"], "include_remote": False}).encode(),
        )
        assert status == 200, body
        conn = connect(db)
        assert json.loads(conn.execute("SELECT locations_json FROM search").fetchone()[0]) == ["Lyon"]
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_login_rejects_large_body_before_reading(tmp_path):
    db = tmp_path / "jobwatch.db"
    conn = connect(db)
    init_db(conn)
    create_invite(conn, "alice", "alice@example.com")
    conn.close()
    server, thread = _start_server(db, workspace_slug="alice")
    try:
        status, _, _ = _request(server.server_address[1], "POST", "/login", body=b"x" * 20000)
        assert status == 413
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_account_recovery_revokes_previous_session(tmp_path):
    conn = connect(tmp_path / "jobwatch.db")
    init_db(conn)
    invite = create_invite(conn, "alice", "alice@example.com")
    accept_invite(conn, invite, "une phrase de passe privée", workspace_slug="alice")
    token, _ = create_session(conn, "alice@example.com", "une phrase de passe privée", "alice")
    recovery = create_invite(conn, "alice", "alice@example.com")
    accept_invite(conn, recovery, "une nouvelle phrase privée", workspace_slug="alice")
    assert resolve_session(conn, token) is None
    conn.close()
