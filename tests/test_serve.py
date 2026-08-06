"""Tests for jobwatch.serve : rendu HTML et serveur HTTP."""

from __future__ import annotations

import socket
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from click.testing import CliRunner

from jobwatch.cli import cli
from jobwatch.db import connect, init_db
from jobwatch.serve import make_handler, render_page


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = connect(":memory:")
    init_db(connection)
    yield connection
    connection.close()


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _write_config(tmp_path: Path, db_path: Path) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""db: {db_path}
searches:
  - name: ai-paris
    include: ["AI engineer", "LLM"]
sources: {{}}
notify: {{}}
"""
    )
    return config


def _seed_offer(
    conn: sqlite3.Connection,
    company: str = "Acme",
    title: str = "AI Engineer",
    url: str | None = None,
    location: str = "Paris",
    contract: str = "permanent",
    platform: str = "Test",
    search: str = "ai-paris",
    state: str = "new",
) -> tuple[int, int]:
    if url is None:
        url = f"https://example.com/{company}-{title}".replace(" ", "-")
    conn.execute("INSERT OR IGNORE INTO source (type, name) VALUES ('test', 'test')")
    source_id = conn.execute("SELECT id FROM source WHERE name = 'test'").fetchone()["id"]
    conn.execute("INSERT OR IGNORE INTO company (name) VALUES (?)", (company,))
    company_id = conn.execute("SELECT id FROM company WHERE name = ?", (company,)).fetchone()["id"]
    conn.execute(
        "INSERT INTO offer (source_id, company_id, title, url, platform, location, contract) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (source_id, company_id, title, url, platform, location, contract),
    )
    offer_id = conn.execute("SELECT id FROM offer WHERE url = ?", (url,)).fetchone()["id"]
    conn.execute(
        "INSERT OR IGNORE INTO search (name, include_json, exclude_json, locations_json, active) "
        "VALUES (?, '[]', '[]', '[]', 1)",
        (search,),
    )
    search_id = conn.execute("SELECT id FROM search WHERE name = ?", (search,)).fetchone()["id"]
    conn.execute(
        "INSERT INTO match (search_id, offer_id, state) VALUES (?, ?, ?)",
        (search_id, offer_id, state),
    )
    match_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    return match_id, offer_id


def _apply(conn: sqlite3.Connection, match_id: int, offer_id: int, note: str = "note") -> int:
    cur = conn.execute(
        "INSERT INTO application (match_id, offer_id, note) VALUES (?, ?, ?)",
        (match_id, offer_id, note),
    )
    application_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO event (application_id, type, comment) VALUES (?, 'applied', ?)",
        (application_id, note),
    )
    conn.execute("UPDATE match SET state = 'applied' WHERE id = ?", (match_id,))
    conn.commit()
    return application_id


def _section_html(page: str, key: str) -> str:
    marker = f'data-section="{key}"'
    start = page.index(marker)
    end = page.find("<details", start + len(marker))
    if end == -1:
        end = len(page)
    return page[start:end]


def test_empty_database_renders(conn: sqlite3.Connection) -> None:
    page = render_page(conn)
    assert page.startswith("<!DOCTYPE html>")
    for label in ("Nouveaux matchs", "Vus", "Candidatures"):
        assert label in page
    assert "Aucun nouveau match pour l'instant." in page
    assert "Aucun match parcouru pour l'instant." in page
    assert "Aucune candidature pour l'instant." in page
    assert "0 offres" in page
    assert "jobwatch.db" not in page
    assert '<article class="row' not in page


def test_placement_of_new_seen_applied_and_discarded(conn: sqlite3.Connection) -> None:
    _seed_offer(conn, company="NewCo", title="New Role", state="new")
    _seed_offer(conn, company="SeenCo", title="Seen Role", state="seen")
    _seed_offer(conn, company="DropCo", title="Drop Role", state="discarded")
    match_id, offer_id = _seed_offer(conn, company="AppCo", title="App Role", state="new")
    _apply(conn, match_id, offer_id)

    page = render_page(conn)
    assert "NewCo" in _section_html(page, "new")
    assert "SeenCo" in _section_html(page, "seen")
    assert "AppCo" in _section_html(page, "applied")
    assert "AppCo" not in _section_html(page, "new")
    assert "AppCo" not in _section_html(page, "seen")
    assert "DropCo" not in page
    assert "via ai-paris" in _section_html(page, "new")


def test_match_with_application_excluded_from_new(conn: sqlite3.Connection) -> None:
    match_id, offer_id = _seed_offer(conn, company="StaleCo", title="Stale", state="new")
    cur = conn.execute(
        "INSERT INTO application (match_id, offer_id) VALUES (?, ?)", (match_id, offer_id)
    )
    application_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO event (application_id, type) VALUES (?, 'applied')", (application_id,)
    )
    conn.commit()

    page = render_page(conn)
    assert "StaleCo" not in _section_html(page, "new")
    assert "StaleCo" in _section_html(page, "applied")


def test_application_status_uses_latest_event(conn: sqlite3.Connection) -> None:
    match_id, offer_id = _seed_offer(conn, company="Acme", title="Engineer")
    cur = conn.execute(
        "INSERT INTO application (match_id, offer_id) VALUES (?, ?)", (match_id, offer_id)
    )
    application_id = int(cur.lastrowid)
    conn.execute("UPDATE match SET state = 'applied' WHERE id = ?", (match_id,))
    events = [
        ("applied", "2026-01-05 10:00:00"),
        ("follow_up", "2026-06-01 09:00:00"),
        ("interview", "2026-06-01 09:00:00"),
        ("rejected", "2026-06-01 09:00:00"),
    ]
    for etype, at in events:
        conn.execute(
            "INSERT INTO event (application_id, type, at) VALUES (?, ?, ?)",
            (application_id, etype, at),
        )
    conn.commit()

    page = render_page(conn)
    applied = _section_html(page, "applied")
    assert "Refus" in applied
    assert "Entretien" not in applied

    conn.execute(
        "INSERT INTO event (application_id, type, at) VALUES (?, 'offer', '2026-07-01 08:00:00')",
        (application_id,),
    )
    conn.commit()
    page = render_page(conn)
    applied = _section_html(page, "applied")
    assert "Offre reçue" in applied
    assert "Refus" not in applied


def test_application_without_event_shows_unknown_status(conn: sqlite3.Connection) -> None:
    match_id, offer_id = _seed_offer(conn, company="Acme", title="Engineer")
    conn.execute(
        "INSERT INTO application (match_id, offer_id) VALUES (?, ?)", (match_id, offer_id)
    )
    conn.execute("UPDATE match SET state = 'applied' WHERE id = ?", (match_id,))
    conn.commit()

    page = render_page(conn)
    assert "Statut inconnu" in _section_html(page, "applied")


def test_values_escaped_and_unsafe_schemes_not_linked(conn: sqlite3.Connection) -> None:
    _seed_offer(
        conn,
        company='"><script>alert(1)</script>',
        title="Tom & Jerry <3",
        url="javascript:alert(1)",
        state="new",
    )
    page = render_page(conn)
    assert "&lt;script&gt;" in page
    assert "<script>alert(1)" not in page
    assert 'href="javascript:' not in page
    assert "Café &amp; Compagnie" not in page


def test_safe_url_rendered_with_target_and_rel(conn: sqlite3.Connection) -> None:
    _seed_offer(conn, company="Acme", title="Role", url="https://example.com/job?a=1&b=2")
    page = render_page(conn)
    assert 'href="https://example.com/job?a=1&amp;b=2"' in page
    assert 'target="_blank"' in page
    assert 'rel="noopener noreferrer"' in page


def test_null_optional_fields_render(conn: sqlite3.Connection) -> None:
    _seed_offer(conn, company="Acme", title="Role", location=None, contract=None, platform=None)
    page = render_page(conn)
    assert "Acme" in page
    assert "Role" in page


def test_offer_without_company_remains_visible(conn: sqlite3.Connection) -> None:
    match_id, offer_id = _seed_offer(conn, company="Temporary", title="Offre sans société")
    conn.execute("UPDATE offer SET company_id = NULL WHERE id = ?", (offer_id,))
    conn.commit()

    page = render_page(conn)
    assert "Offre sans société" in _section_html(page, "new")
    assert "Société inconnue" in _section_html(page, "new")

    _apply(conn, match_id, offer_id)
    page = render_page(conn)
    assert "Offre sans société" in _section_html(page, "applied")
    assert "Société inconnue" in _section_html(page, "applied")
    assert "via ai-paris" in _section_html(page, "applied")


def test_unicode_text_renders(conn: sqlite3.Connection) -> None:
    _seed_offer(conn, company="Café & Compagnie", title="Ingénieur DevOps - Paris")
    page = render_page(conn)
    assert "Café &amp; Compagnie" in page
    assert "Ingénieur DevOps - Paris" in page


def _start_server(db_path: Path) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _get(port: int, path: str) -> tuple[int, dict[str, str], str]:
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8")


def test_http_root_serves_page(tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    connection = connect(db_path)
    init_db(connection)
    _seed_offer(connection, company="Acme", title="Role")
    connection.close()
    server, thread = _start_server(db_path)
    try:
        status, headers, body = _get(server.server_address[1], "/")
        assert status == 200
        assert headers["Content-Type"] == "text/html; charset=utf-8"
        assert int(headers["Content-Length"]) == len(body.encode("utf-8"))
        assert headers["Cache-Control"] == "no-store"
        assert body.startswith("<!DOCTYPE html>")
        assert "Nouveaux matchs" in body
        assert "Acme" in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_missing_route_returns_plain_404(tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    connection = connect(db_path)
    init_db(connection)
    connection.close()
    server, thread = _start_server(db_path)
    try:
        for path in ("/missing", "/favicon.ico"):
            status, headers, body = _get(server.server_address[1], path)
            assert status == 404
            assert headers["Content-Type"] == "text/plain; charset=utf-8"
            assert "404" in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_serve_help_shows_defaults(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["serve", "--help"])
    assert result.exit_code == 0, result.output
    assert "--config" in result.output
    assert "--host" in result.output
    assert "127.0.0.1" in result.output
    assert "--port" in result.output
    assert "8000" in result.output


def test_serve_port_in_use_fails_cleanly(runner: CliRunner, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    config = _write_config(tmp_path, db_path)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        result = runner.invoke(cli, ["serve", "--config", str(config), "--port", str(port)])
        assert result.exit_code == 1
        assert "erreur :" in result.output
        assert "impossible d'écouter" in result.output
    finally:
        sock.close()


def test_serve_prints_url_and_stops_on_ctrl_c(
    runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "jw.db"
    config = _write_config(tmp_path, db_path)
    captured: dict[str, object] = {}

    class FakeServer:
        def __init__(self, addr, handler) -> None:
            captured["addr"] = addr
            self.server_address = ("127.0.0.1", 8123)

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr("jobwatch.serve.ThreadingHTTPServer", FakeServer)
    result = runner.invoke(
        cli,
        ["serve", "--config", str(config), "--host", "127.0.0.1", "--port", "8123"],
    )
    assert result.exit_code == 0, result.output
    assert "tableau de bord jobwatch : http://127.0.0.1:8123" in result.output
    assert "arrêt du serveur" in result.output
    assert captured["addr"] == ("127.0.0.1", 8123)
    assert captured["closed"] is True
