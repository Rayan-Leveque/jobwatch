"""Tests for jobwatch.serve : rendu HTML et serveur HTTP."""

from __future__ import annotations

import json
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
    fit: str | None = None,
    deadline: str | None = None,
    collected_at: str = "2026-08-01 10:00:00",
) -> tuple[int, int]:
    if url is None:
        url = f"https://example.com/{company}-{title}".replace(" ", "-")
    conn.execute("INSERT OR IGNORE INTO source (type, name) VALUES ('test', 'test')")
    source_id = conn.execute("SELECT id FROM source WHERE name = 'test'").fetchone()["id"]
    conn.execute("INSERT OR IGNORE INTO company (name) VALUES (?)", (company,))
    company_id = conn.execute("SELECT id FROM company WHERE name = ?", (company,)).fetchone()["id"]
    conn.execute(
        "INSERT INTO offer (source_id, company_id, title, url, platform, location, "
        "contract, deadline, collected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source_id, company_id, title, url, platform, location, contract, deadline, collected_at),
    )
    offer_id = conn.execute("SELECT id FROM offer WHERE url = ?", (url,)).fetchone()["id"]
    conn.execute(
        "INSERT OR IGNORE INTO search (name, include_json, exclude_json, locations_json, active) "
        "VALUES (?, '[]', '[]', '[]', 1)",
        (search,),
    )
    search_id = conn.execute("SELECT id FROM search WHERE name = ?", (search,)).fetchone()["id"]
    conn.execute(
        "INSERT INTO match (search_id, offer_id, state, fit) VALUES (?, ?, ?, ?)",
        (search_id, offer_id, state, fit),
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


def _add_summary(conn: sqlite3.Connection, offer_id: int, *bullets: str) -> None:
    summary_id = int(
        conn.execute("INSERT INTO offer_summary (offer_id) VALUES (?)", (offer_id,)).lastrowid
    )
    conn.executemany(
        "INSERT INTO summary_bullet (summary_id, position, text) VALUES (?, ?, ?)",
        ((summary_id, position, bullet) for position, bullet in enumerate(bullets)),
    )
    conn.commit()


def _add_content(
    conn: sqlite3.Connection, offer_id: int, markdown: str, status: str = "ok"
) -> None:
    conn.execute(
        "INSERT INTO offer_content (offer_id, markdown, fetch_method, status) "
        "VALUES (?, ?, 'http', ?)",
        (offer_id, markdown if status == "ok" else None, status),
    )
    conn.commit()


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
    for label in ("Priorité haute", "Nouveaux matchs", "Vus", "Candidatures"):
        assert label in page
    assert "Aucune offre prioritaire pour l'instant." in page
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


def test_placement_of_later_and_recently_discarded(conn: sqlite3.Connection) -> None:
    _seed_offer(conn, company="LaterCo", title="Later Role", state="later")
    trash_id, _ = _seed_offer(conn, company="TrashCo", title="Trash Role", state="discarded")
    conn.execute(
        "UPDATE match SET discarded_at = datetime('now', '-1 days') WHERE id = ?", (trash_id,)
    )
    conn.commit()

    page = render_page(conn)
    assert "LaterCo" in _section_html(page, "later")
    assert "LaterCo" not in _section_html(page, "new")
    assert "LaterCo" not in _section_html(page, "seen")
    assert "LaterCo" not in _section_html(page, "priority")
    assert "LaterCo" not in _section_html(page, "applied")
    assert "LaterCo" not in _section_html(page, "discarded")
    assert "TrashCo" in _section_html(page, "discarded")
    assert "TrashCo" not in _section_html(page, "new")
    assert "TrashCo" not in _section_html(page, "seen")
    assert "TrashCo" not in _section_html(page, "later")
    assert "TrashCo" not in _section_html(page, "applied")


def test_discarded_matches_older_than_30_days_are_hidden_but_row_kept(
    conn: sqlite3.Connection,
) -> None:
    inside_id, _ = _seed_offer(conn, company="InsideCo", title="Inside", state="discarded")
    conn.execute(
        "UPDATE match SET discarded_at = datetime('now', '-29 days') WHERE id = ?", (inside_id,)
    )
    outside_id, _ = _seed_offer(conn, company="OutsideCo", title="Outside", state="discarded")
    conn.execute(
        "UPDATE match SET discarded_at = datetime('now', '-31 days') WHERE id = ?", (outside_id,)
    )
    conn.commit()

    page = render_page(conn)
    assert "InsideCo" in _section_html(page, "discarded")
    assert "OutsideCo" not in page

    row = conn.execute("SELECT state, discarded_at FROM match WHERE id = ?", (outside_id,)).fetchone()
    assert row["state"] == "discarded"
    assert row["discarded_at"] is not None


def test_action_buttons_rendered_for_actionable_sections_only(conn: sqlite3.Connection) -> None:
    _seed_offer(conn, company="NewCo", title="New Role", state="new")
    _seed_offer(conn, company="LaterCo", title="Later Role", state="later")
    trash_id, _ = _seed_offer(conn, company="TrashCo", title="Trash Role", state="discarded")
    conn.execute("UPDATE match SET discarded_at = datetime('now') WHERE id = ?", (trash_id,))
    app_match_id, app_offer_id = _seed_offer(conn, company="AppCo", title="App Role", state="new")
    _apply(conn, app_match_id, app_offer_id)
    conn.commit()

    page = render_page(conn)
    assert 'class="card-action action-later"' in _section_html(page, "new")
    assert 'class="card-action action-later"' in _section_html(page, "later")
    assert 'class="card-action action-later"' not in _section_html(page, "discarded")
    assert 'class="card-action action-later"' not in _section_html(page, "applied")
    assert 'class="card-action action-discard"' in _section_html(page, "new")


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


def test_fit_pills_rendered_high_medium_low(conn: sqlite3.Connection) -> None:
    _seed_offer(conn, company="HighCo", title="Role", fit="high")
    _seed_offer(conn, company="MidCo", title="Role", fit="medium")
    _seed_offer(conn, company="LowCo", title="Role", fit="low")
    page = render_page(conn)
    assert '<span class="pill fit high">high</span>' in page
    assert '<span class="pill fit medium">medium</span>' in page
    assert '<span class="pill fit low">low</span>' in page


def test_high_summary_renders_escaped_ordered_accessible_collapsible_block(
    conn: sqlite3.Connection,
) -> None:
    _match_id, offer_id = _seed_offer(
        conn, company="HighCo", title='AI <Engineer> "Senior"', fit="high"
    )
    _add_summary(conn, offer_id, "Premier <script>alert(1)</script>", "Deuxième & dernier")

    page = render_page(conn)
    section = _section_html(page, "priority")

    assert 'class="row row-new has-summary"' in section
    assert 'class="card-toggle"' in section
    assert 'aria-expanded="false"' in section
    assert 'aria-controls="summary-match-' in section
    assert 'aria-label="Afficher le résumé de AI &lt;Engineer&gt; &quot;Senior&quot;"' in section
    assert 'class="summary-chevron"' in section
    assert 'class="summary-panel"' in section
    assert "hidden" in section
    assert "En bref" in section
    assert "Premier &lt;script&gt;alert(1)&lt;/script&gt;" in section
    assert "<script>alert(1)</script>" not in section
    assert section.index("Premier") < section.index("Deuxième &amp; dernier")


@pytest.mark.parametrize("fit", ["medium", "low", None])
def test_summary_is_not_rendered_for_non_high_fit(
    conn: sqlite3.Connection, fit: str | None
) -> None:
    _match_id, offer_id = _seed_offer(conn, company=f"Co-{fit}", fit=fit)
    _add_summary(conn, offer_id, "Fait masqué")

    page = render_page(conn)

    assert "Fait masqué" not in page
    assert "En bref" not in page
    assert '<button class="card-toggle"' not in page


def test_high_without_summary_has_no_empty_summary_space(conn: sqlite3.Connection) -> None:
    _seed_offer(conn, company="HighCo", fit="high")

    page = render_page(conn)

    assert "En bref" not in page
    assert '<div class="summary-panel"' not in page
    assert 'class="row row-new has-summary"' not in page


def test_high_application_renders_its_summary(conn: sqlite3.Connection) -> None:
    match_id, offer_id = _seed_offer(conn, company="AppliedHigh", fit="high")
    _add_summary(conn, offer_id, "Fait candidature")
    _apply(conn, match_id, offer_id)

    page = render_page(conn)
    applied = _section_html(page, "applied")

    assert "Fait candidature" in applied
    assert 'aria-controls="summary-application-' in applied


def test_summary_toggle_script_preserves_external_link_interaction(
    conn: sqlite3.Connection,
) -> None:
    _match_id, offer_id = _seed_offer(conn, company="HighCo", fit="high")
    _add_summary(conn, offer_id, "Fait")

    page = render_page(conn)

    assert "button.addEventListener('click'" in page
    assert "button.setAttribute('aria-expanded'" in page
    assert "panel.hidden = expanded" in page
    assert 'target="_blank"' in page
    assert ".meta a { position:relative; z-index:3" in page
    assert "pointer-events:auto" in page


def test_content_panel_renders_escaped_and_collapsible(conn: sqlite3.Connection) -> None:
    _match_id, offer_id = _seed_offer(conn, company="ContentCo", title="AI Engineer", fit=None)
    _add_content(conn, offer_id, "Poste <script>alert(1)</script>\n\nDeuxième paragraphe.")

    page = render_page(conn)
    section = _section_html(page, "new")

    assert 'class="content-toggle"' in section
    assert 'aria-expanded="false"' in section
    assert 'aria-controls="content-match-' in section
    assert "Annonce complète" in section
    assert 'class="content-panel"' in section
    assert "hidden" in section
    assert "Poste &lt;script&gt;alert(1)&lt;/script&gt;" in section
    assert "<script>alert(1)</script>" not in section
    assert "Deuxième paragraphe." in section


def test_content_panel_absent_without_offer_content(conn: sqlite3.Connection) -> None:
    _seed_offer(conn, company="NoContentCo")

    page = render_page(conn)

    assert '<button class="content-toggle"' not in page
    assert '<div class="content-panel"' not in page


def test_content_panel_absent_when_fetch_failed(conn: sqlite3.Connection) -> None:
    _match_id, offer_id = _seed_offer(conn, company="FailedCo")
    _add_content(conn, offer_id, "peu importe", status="failed")

    page = render_page(conn)

    assert '<button class="content-toggle"' not in page
    assert '<div class="content-panel"' not in page


def test_content_panel_independent_from_summary_panel(conn: sqlite3.Connection) -> None:
    """Le panneau En bref existant reste inchangé, que l'annonce complète existe ou non."""
    _match_id, offer_id = _seed_offer(conn, company="BothCo", fit="high")
    _add_summary(conn, offer_id, "Fait résumé")
    _add_content(conn, offer_id, "Texte complet de annonce.")

    page = render_page(conn)
    section = _section_html(page, "priority")

    assert 'class="summary-panel"' in section
    assert "En bref" in section
    assert "Fait résumé" in section
    assert 'class="content-panel"' in section
    assert "Texte complet de annonce." in section
    # les deux panneaux ont des id distincts
    assert 'aria-controls="summary-match-' in section
    assert 'aria-controls="content-match-' in section


def test_content_panel_rendered_for_low_fit_and_no_summary(conn: sqlite3.Connection) -> None:
    """Contrairement à En bref, le panneau annonce complète n'est pas conditionné au fit."""
    _match_id, offer_id = _seed_offer(conn, company="LowFitCo", fit="low")
    _add_content(conn, offer_id, "Texte complet peu importe le fit.")

    page = render_page(conn)

    assert "content-toggle" in page
    assert "Texte complet peu importe le fit." in page


def test_content_panel_renders_for_application(conn: sqlite3.Connection) -> None:
    match_id, offer_id = _seed_offer(conn, company="AppliedContentCo")
    _add_content(conn, offer_id, "Annonce complete pour une candidature.")
    _apply(conn, match_id, offer_id)

    page = render_page(conn)
    applied = _section_html(page, "applied")

    assert "content-toggle" in applied
    assert 'aria-controls="content-application-' in applied
    assert "Annonce complete pour une candidature." in applied


def test_no_fit_pill_when_fit_null(conn: sqlite3.Connection) -> None:
    _seed_offer(conn, company="Acme", title="Role", fit=None)
    page = render_page(conn)
    assert "pill fit" not in page


def test_invalid_fit_is_not_rendered(conn: sqlite3.Connection) -> None:
    _seed_offer(conn, company="Acme", title="Role", fit='high\" onclick=\"alert(1)')
    page = render_page(conn)
    assert "pill fit" not in page
    assert "onclick" not in page


def test_deadline_displayed_in_meta(conn: sqlite3.Connection) -> None:
    _seed_offer(conn, company="Acme", title="Role", deadline="2026-09-01")
    page = render_page(conn)
    assert "échéance 1 sept." in _section_html(page, "new")


def test_deadline_escaped(conn: sqlite3.Connection) -> None:
    _seed_offer(conn, company="Acme", title="Role", deadline='"><script>alert(1)</script>')
    page = render_page(conn)
    section = _section_html(page, "new")
    assert "&lt;script&gt;" in section
    assert "<script>alert(1)" not in section


def test_match_order_fit_then_collected_desc(conn: sqlite3.Connection) -> None:
    _seed_offer(conn, company="LoCo", title="L", fit="low", collected_at="2026-08-05 10:00:00")
    _match_id, high_offer_id = _seed_offer(
        conn, company="HiCo", title="H", fit="high", collected_at="2026-08-01 10:00:00"
    )
    _add_summary(conn, high_offer_id, "Résumé high")
    _seed_offer(conn, company="NoneCo", title="N", fit=None, collected_at="2026-08-04 10:00:00")
    _seed_offer(conn, company="MidCo", title="M", fit="medium", collected_at="2026-08-03 10:00:00")
    page = render_page(conn)
    section = _section_html(page, "new")
    indexes = [section.index(name) for name in ("MidCo", "LoCo", "NoneCo")]
    assert indexes == sorted(indexes)
    assert "HiCo" not in section
    assert "HiCo" in _section_html(page, "priority")


def test_priority_section_precedes_others_without_duplicates_and_sorts_globally(
    conn: sqlite3.Connection,
) -> None:
    _match_id, older_id = _seed_offer(
        conn,
        company="OlderHigh",
        state="new",
        fit="high",
        collected_at="2026-08-01 10:00:00",
    )
    _match_id, newer_id = _seed_offer(
        conn,
        company="NewerHigh",
        state="seen",
        fit="high",
        collected_at="2026-08-05 10:00:00",
    )
    _add_summary(conn, older_id, "Résumé ancien")
    _add_summary(conn, newer_id, "Résumé récent")
    _seed_offer(conn, company="MediumNew", state="new", fit="medium")

    page = render_page(conn)
    priority = _section_html(page, "priority")

    section_positions = [
        page.index(f'data-section="{key}"')
        for key in ("priority", "new", "seen", "applied")
    ]
    assert section_positions == sorted(section_positions)
    assert '<details class="section section-priority" open' in page
    assert priority.index("NewerHigh") < priority.index("OlderHigh")
    assert "Résumé récent" in priority
    assert "Résumé ancien" in priority
    assert "OlderHigh" not in _section_html(page, "new")
    assert "NewerHigh" not in _section_html(page, "seen")
    assert page.count('class="company">OlderHigh</div>') == 1
    assert page.count('class="company">NewerHigh</div>') == 1
    assert '<span class="stat-value">2</span><span class="stat-label">Nouveaux matchs</span>' in page
    assert '<span class="stat-value">1</span><span class="stat-label">Vus</span>' in page
    assert '<span class="count">2</span>' in priority


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


def _post(
    port: int, path: str, body: bytes | None = None, content_type: str | None = None
) -> tuple[int, dict[str, str], str]:
    url = f"http://127.0.0.1:{port}{path}"
    headers = {"Content-Type": content_type} if content_type else {}
    req = urllib.request.Request(url, data=body or b"", method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8")


def test_post_later_updates_match_state(tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    connection = connect(db_path)
    init_db(connection)
    match_id, _ = _seed_offer(connection, company="Acme", title="Role", state="new")
    connection.close()
    server, thread = _start_server(db_path)
    try:
        port = server.server_address[1]
        status, headers, body = _post(port, f"/match/{match_id}/later")
        assert status == 200
        assert "application/json" in headers["Content-Type"]
        assert json.loads(body) == {"ok": True}

        check = connect(db_path)
        row = check.execute("SELECT state FROM match WHERE id = ?", (match_id,)).fetchone()
        check.close()
        assert row["state"] == "later"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_post_discard_matches_cli_discard_db_state(tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    connection = connect(db_path)
    init_db(connection)
    match_id, _ = _seed_offer(connection, company="Acme", title="Role", state="new")
    connection.close()
    server, thread = _start_server(db_path)
    try:
        port = server.server_address[1]
        status, _headers, _body = _post(port, f"/match/{match_id}/discard")
        assert status == 200

        check = connect(db_path)
        row = check.execute(
            "SELECT state, discarded_at FROM match WHERE id = ?", (match_id,)
        ).fetchone()
        check.close()
        assert row["state"] == "discarded"
        assert row["discarded_at"] is not None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_post_restore_reverts_state_and_preserves_other_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    connection = connect(db_path)
    init_db(connection)
    match_id, _ = _seed_offer(
        connection, company="Acme", title="Role", state="seen", fit="high", deadline="2026-09-01"
    )
    connection.close()
    server, thread = _start_server(db_path)
    try:
        port = server.server_address[1]
        _post(port, f"/match/{match_id}/discard")
        status, _headers, _body = _post(
            port,
            f"/match/{match_id}/restore",
            body=json.dumps({"state": "seen"}).encode("utf-8"),
            content_type="application/json",
        )
        assert status == 200

        check = connect(db_path)
        row = check.execute("SELECT * FROM match WHERE id = ?", (match_id,)).fetchone()
        check.close()
        assert row["state"] == "seen"
        assert row["discarded_at"] is None
        assert row["fit"] == "high"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_post_restore_rejects_disallowed_target_state(tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    connection = connect(db_path)
    init_db(connection)
    match_id, _ = _seed_offer(connection, company="Acme", title="Role", state="new")
    connection.close()
    server, thread = _start_server(db_path)
    try:
        port = server.server_address[1]
        status, _headers, _body = _post(
            port,
            f"/match/{match_id}/restore",
            body=json.dumps({"state": "discarded"}).encode("utf-8"),
            content_type="application/json",
        )
        assert status == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_post_invalid_match_id_rejected_safely(tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    connection = connect(db_path)
    init_db(connection)
    connection.close()
    server, thread = _start_server(db_path)
    try:
        port = server.server_address[1]
        for path in ("/match/abc/later", "/match/1.5/discard", "/match/-1/later", "/match//later"):
            status, _headers, _body = _post(port, path)
            assert status == 404

        status, _headers, _body = _post(port, "/match/999999/later")
        assert status == 404
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
