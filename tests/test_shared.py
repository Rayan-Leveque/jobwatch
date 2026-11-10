from __future__ import annotations

import http.client
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

from jobwatch.auth import accept_invite, create_invite
from jobwatch.db import connect, init_db
from jobwatch.serve import make_handler
from jobwatch.shared import Instance, SharedError, register_instance
from jobwatch.shared import make_handler as make_shared_handler


def _instance(
    db_path: Path,
    slug: str,
    email: str,
    company: str,
    document: bytes,
    *,
    accept: bool = True,
    trust_proxy: bool = False,
):
    db_path.parent.mkdir()
    conn = connect(db_path)
    init_db(conn)
    invite = create_invite(conn, slug, email)
    if accept:
        accept_invite(conn, invite, "mot de passe secret", workspace_slug=slug)
    conn.execute("INSERT INTO source (type, name) VALUES ('test', 'test')")
    company_id = conn.execute("INSERT INTO company (name) VALUES (?)", (company,)).lastrowid
    offer_id = conn.execute(
        "INSERT INTO offer (source_id, company_id, title, url) VALUES (1, ?, 'Poste', ?)",
        (company_id, f"https://example.com/{slug}"),
    ).lastrowid
    conn.execute(
        "INSERT INTO search (name, include_json, exclude_json, locations_json) "
        "VALUES ('test', '[]', '[]', '[]')"
    )
    conn.execute("INSERT INTO match (search_id, offer_id) VALUES (1, ?)", (offer_id,))
    path = db_path.parent / "document.pdf"
    path.write_bytes(document)
    conn.execute(
        "INSERT INTO document_library (type, label, file_path) VALUES ('cv', 'CV', ?)",
        (str(path),),
    )
    conn.commit()
    conn.close()
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(
            db_path, workspace_slug=slug, secure_cookie=False, trust_proxy=trust_proxy
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, invite


def _request(port: int, method: str, path: str, body: bytes | None = None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    conn.close()
    return result


def _login(port: int, email: str) -> str:
    body = urlencode({"email": email, "password": "mot de passe secret"}).encode()
    status, headers, _payload = _request(
        port,
        "POST",
        "/login",
        body,
        {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": f"http://127.0.0.1:{port}",
        },
    )
    assert status == 303
    return headers["Set-Cookie"].split(";", 1)[0]


def test_shared_host_routes_each_account_to_its_private_database(tmp_path: Path) -> None:
    alice, alice_thread, _alice_invite = _instance(
        tmp_path / "alice" / "jobwatch.db",
        "alice",
        "alice@example.com",
        "Société Alice",
        b"CV Alice",
    )
    bob, bob_thread, _bob_invite = _instance(
        tmp_path / "bob" / "jobwatch.db",
        "bob",
        "bob@example.com",
        "Société Bob",
        b"CV Bob",
    )
    charlie, charlie_thread, charlie_invite = _instance(
        tmp_path / "charlie" / "jobwatch.db",
        "charlie",
        "charlie@example.com",
        "Société Charlie",
        b"CV Charlie",
        accept=False,
    )
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "instances": [
                    {"slug": "alice", "email": "alice@example.com", "port": alice.server_port},
                    {"slug": "bob", "email": "bob@example.com", "port": bob.server_port},
                    {
                        "slug": "charlie",
                        "email": "charlie@example.com",
                        "port": charlie.server_port,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    shared = ThreadingHTTPServer(("127.0.0.1", 0), make_shared_handler(registry))
    shared_thread = threading.Thread(target=shared.serve_forever, daemon=True)
    shared_thread.start()
    port = shared.server_port
    try:
        status, headers, _body = _request(port, "GET", "/")
        assert status == 303
        assert headers["Location"] == "/login"

        alice_cookie = _login(port, "alice@example.com")
        bob_cookie = _login(port, "bob@example.com")
        assert alice_cookie.startswith("id=alice.")
        assert bob_cookie.startswith("id=bob.")
        unknown = urlencode({"email": "unknown@example.com", "password": "incorrect"}).encode()
        status, _headers, body = _request(
            port,
            "POST",
            "/login",
            unknown,
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": f"http://127.0.0.1:{port}",
            },
        )
        assert status == 401
        assert b"Email ou mot de passe incorrect" in body

        status, _headers, body = _request(port, "GET", "/", headers={"Cookie": alice_cookie})
        assert status == 200
        assert b"Soci\xc3\xa9t\xc3\xa9 Alice" in body
        assert b"Soci\xc3\xa9t\xc3\xa9 Bob" not in body
        marker = b'name="csrf-token" content="'
        csrf = body.split(marker, 1)[1].split(b'"', 1)[0].decode()
        status, _headers, _body = _request(
            port,
            "POST",
            "/match/1/later",
            b"{}",
            {
                "Content-Type": "application/json",
                "Cookie": alice_cookie,
                "Origin": f"http://127.0.0.1:{port}",
                "X-CSRF-Token": csrf,
            },
        )
        assert status == 200
        status, _headers, body = _request(
            port, "GET", "/documents/1", headers={"Cookie": alice_cookie}
        )
        assert status == 200
        assert body == b"CV Alice"
        alice_conn = connect(tmp_path / "alice" / "jobwatch.db")
        bob_conn = connect(tmp_path / "bob" / "jobwatch.db")
        assert alice_conn.execute("SELECT state FROM match").fetchone()[0] == "later"
        assert bob_conn.execute("SELECT state FROM match").fetchone()[0] == "new"
        alice_conn.close()
        bob_conn.close()

        status, _headers, body = _request(port, "GET", "/", headers={"Cookie": bob_cookie})
        assert status == 200
        assert b"Soci\xc3\xa9t\xc3\xa9 Bob" in body
        assert b"Soci\xc3\xa9t\xc3\xa9 Alice" not in body
        status, _headers, body = _request(
            port, "GET", "/documents/1", headers={"Cookie": bob_cookie}
        )
        assert status == 200
        assert body == b"CV Bob"

        alice_token = alice_cookie.split(".", 1)[1]
        status, headers, _body = _request(
            port, "GET", "/", headers={"Cookie": f"id=bob.{alice_token}"}
        )
        assert status == 303
        assert headers["Location"] == "/login"

        status, _headers, body = _request(port, "GET", f"/invite/{charlie_invite}")
        assert status == 200
        assert b"Cr\xc3\xa9er votre compte" in body
        invitation_body = urlencode(
            {
                "password": "mot de passe charlie",
                "password_confirmation": "mot de passe charlie",
            }
        ).encode()
        status, headers, _body = _request(
            port,
            "POST",
            f"/invite/{charlie_invite}",
            invitation_body,
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": f"http://127.0.0.1:{port}",
            },
        )
        assert status == 303
        assert headers["Set-Cookie"].startswith("id=charlie.")
        assert "HttpOnly" in headers["Set-Cookie"]
        assert "SameSite=Strict" in headers["Set-Cookie"]

        status, _headers, body = _request(port, "GET", "/healthz")
        assert status == 200
        assert json.loads(body) == {"status": "ok"}
    finally:
        shared.shutdown()
        shared.server_close()
        shared_thread.join(timeout=5)
        for server, thread in (
            (alice, alice_thread),
            (bob, bob_thread),
            (charlie, charlie_thread),
        ):
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def test_login_page_served_without_any_backend(tmp_path: Path) -> None:
    """La page de connexion commune doit rester disponible même si tout est arrêté."""
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps({"instances": [{"slug": "zoe", "email": "zoe@example.com", "port": 1}]}),
        encoding="utf-8",
    )
    shared = ThreadingHTTPServer(("127.0.0.1", 0), make_shared_handler(registry))
    thread = threading.Thread(target=shared.serve_forever, daemon=True)
    thread.start()
    port = shared.server_port
    try:
        status, _headers, body = _request(port, "GET", "/login")
        assert status == 200
        assert b"Connexion" in body
        assert b"Email" in body
        body = urlencode({"email": "zoe@example.com", "password": "incorrect"}).encode()
        status, _headers, body = _request(
            port,
            "POST",
            "/login",
            body,
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": f"http://127.0.0.1:{port}",
            },
        )
        assert status == 503
        assert b"service indisponible" in body
    finally:
        shared.shutdown()
        shared.server_close()
        thread.join(timeout=5)


def test_register_instance_is_atomic_under_concurrency(tmp_path: Path) -> None:
    registry = tmp_path / "shared.json"

    def add(index: int) -> None:
        register_instance(
            registry,
            Instance(f"user{index}", f"user{index}@example.com", 9100 + index),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(add, range(2)))
    entries = json.loads(registry.read_text(encoding="utf-8"))["instances"]
    assert {item["slug"] for item in entries} == {"user0", "user1"}
    try:
        register_instance(registry, Instance("user0", "autre@example.com", 9200))
    except SharedError:
        pass
    else:
        raise AssertionError("doublon de slug accepté")
    entries = json.loads(registry.read_text(encoding="utf-8"))["instances"]
    assert len(entries) == 2


def test_invite_discovery_skips_dead_instance(tmp_path: Path) -> None:
    charlie, charlie_thread, charlie_invite = _instance(
        tmp_path / "charlie" / "jobwatch.db",
        "charlie",
        "charlie@example.com",
        "Société Charlie",
        b"CV Charlie",
        accept=False,
    )
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "instances": [
                    {"slug": "zoe", "email": "zoe@example.com", "port": 1},
                    {
                        "slug": "charlie",
                        "email": "charlie@example.com",
                        "port": charlie.server_port,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    shared = ThreadingHTTPServer(("127.0.0.1", 0), make_shared_handler(registry))
    shared_thread = threading.Thread(target=shared.serve_forever, daemon=True)
    shared_thread.start()
    try:
        started = time.monotonic()
        status, _headers, body = _request(shared.server_port, "GET", f"/invite/{charlie_invite}")
        assert status == 200
        assert b"Cr\xc3\xa9er votre compte" in body
        assert time.monotonic() - started < 10
    finally:
        shared.shutdown()
        shared.server_close()
        shared_thread.join(timeout=5)
        charlie.shutdown()
        charlie.server_close()
        charlie_thread.join(timeout=5)


def test_login_throttle_follows_the_visitor_address_behind_the_gateway(tmp_path: Path) -> None:
    """Cinq échecs depuis une adresse ne doivent pas bloquer le vrai propriétaire."""
    alice, alice_thread, _invite = _instance(
        tmp_path / "alice" / "jobwatch.db",
        "alice",
        "alice@example.com",
        "Société Alice",
        b"CV Alice",
        trust_proxy=True,
    )
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {"instances": [{"slug": "alice", "email": "alice@example.com", "port": alice.server_port}]}
        ),
        encoding="utf-8",
    )
    shared = ThreadingHTTPServer(("127.0.0.1", 0), make_shared_handler(registry))
    shared_thread = threading.Thread(target=shared.serve_forever, daemon=True)
    shared_thread.start()

    def attempt(password: str, address: str) -> int:
        body = urlencode({"email": "alice@example.com", "password": password}).encode()
        status, _headers, _payload = _request(
            shared.server_port,
            "POST",
            "/login",
            body,
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": f"http://127.0.0.1:{shared.server_port}",
                "Host": f"127.0.0.1:{shared.server_port}",
                "X-Real-IP": address,
            },
        )
        return status

    try:
        for _ in range(5):
            assert attempt("mauvais mot de passe", "203.0.113.7") != 303
        assert attempt("mot de passe secret", "203.0.113.7") == 429
        assert attempt("mot de passe secret", "198.51.100.4") == 303
    finally:
        for server, thread in ((shared, shared_thread), (alice, alice_thread)):
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
