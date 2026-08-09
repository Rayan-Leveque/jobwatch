from __future__ import annotations

import http.client
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

from jobwatch.auth import create_invite
from jobwatch.db import connect, init_db
from jobwatch.serve import make_handler


def _start_server(
    db_path: Path, *, workspace_slug: str | None = None, secure_cookie: bool = True
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(
            db_path,
            workspace_slug=workspace_slug,
            secure_cookie=secure_cookie,
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _request(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    result = response.status, dict(response.getheaders()), response.read().decode("utf-8")
    conn.close()
    return result


def _form_headers(port: int) -> dict[str, str]:
    return {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": f"http://127.0.0.1:{port}",
    }


def test_legacy_instance_remains_accessible_without_account(tmp_path: Path) -> None:
    db_path = tmp_path / "jobwatch.db"
    conn = connect(db_path)
    init_db(conn)
    conn.close()
    server, thread = _start_server(db_path)
    try:
        status, headers, body = _request(server.server_address[1], "GET", "/")
        assert status == 200
        assert headers["Cache-Control"] == "no-store"
        assert "Nouveaux matchs" in body
        assert 'name="csrf-token"' not in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_invite_session_csrf_and_logout_end_to_end(tmp_path: Path) -> None:
    db_path = tmp_path / "jobwatch.db"
    conn = connect(db_path)
    init_db(conn)
    invite = create_invite(conn, "alice", "alice@example.com")
    conn.close()
    server, thread = _start_server(
        db_path, workspace_slug="alice", secure_cookie=False
    )
    port = server.server_address[1]
    try:
        status, headers, _body = _request(port, "GET", "/")
        assert status == 303
        assert headers["Location"] == "/login"

        status, _headers, body = _request(port, "GET", f"/invite/{invite}")
        assert status == 200
        assert "Créer votre compte" in body

        password = "une très longue phrase secrète"
        encoded = urlencode(
            {"password": password, "password_confirmation": password}
        ).encode()
        status, headers, _body = _request(
            port,
            "POST",
            f"/invite/{invite}",
            body=encoded,
            headers=_form_headers(port),
        )
        assert status == 303
        assert headers["Location"] == "/"
        cookie_header = headers["Set-Cookie"]
        assert "HttpOnly" in cookie_header
        assert "SameSite=Strict" in cookie_header
        assert "Secure" not in cookie_header
        cookie = cookie_header.split(";", 1)[0]

        status, _headers, body = _request(
            port, "GET", "/", headers={"Cookie": cookie}
        )
        assert status == 200
        marker = 'name="csrf-token" content="'
        csrf = body.split(marker, 1)[1].split('"', 1)[0]

        status, _headers, body = _request(
            port,
            "POST",
            "/documents",
            body=b"{}",
            headers={"Content-Type": "application/json", "Cookie": cookie},
        )
        assert status == 403
        assert "CSRF" in body

        status, _headers, _body = _request(
            port,
            "POST",
            "/documents",
            body=b"{}",
            headers={
                "Content-Type": "application/json",
                "Cookie": cookie,
                "X-CSRF-Token": csrf,
            },
        )
        assert status == 400

        status, headers, _body = _request(
            port,
            "POST",
            "/logout",
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 303
        assert headers["Location"] == "/login"
        assert "Max-Age=0" in headers["Set-Cookie"]

        status, headers, _body = _request(
            port, "GET", "/", headers={"Cookie": cookie}
        )
        assert status == 303
        assert headers["Location"] == "/login"

        login_body = urlencode(
            {"email": "alice@example.com", "password": password}
        ).encode()
        status, headers, _body = _request(
            port,
            "POST",
            "/login",
            body=login_body,
            headers=_form_headers(port),
        )
        assert status == 303
        assert headers["Location"] == "/"
        assert "HttpOnly" in headers["Set-Cookie"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_login_cookie_is_secure_by_default(tmp_path: Path) -> None:
    db_path = tmp_path / "jobwatch.db"
    conn = connect(db_path)
    init_db(conn)
    invite = create_invite(conn, "alice", "alice@example.com")
    conn.close()
    server, thread = _start_server(db_path, workspace_slug="alice")
    port = server.server_address[1]
    try:
        password = "une très longue phrase secrète"
        body = urlencode(
            {"password": password, "password_confirmation": password}
        ).encode()
        status, headers, _body = _request(
            port,
            "POST",
            f"/invite/{invite}",
            body=body,
            headers=_form_headers(port),
        )
        assert status == 303
        assert "Secure" in headers["Set-Cookie"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
