"""Tests for jobwatch.serve : rendu HTML et serveur HTTP."""

from __future__ import annotations

import base64
import json
import re
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
from jobwatch.serve import _markdown_to_html, make_handler, render_page


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


def test_search_haystack_ignores_the_document_library_menus(
    conn: sqlite3.Connection,
) -> None:
    """Régression : chercher une société ne doit pas matcher via les <option>.

    Les menus « Candidater » et « Générer LM » listent toute la bibliothèque.
    Le filtre lisait le textContent de la carte, donc une lettre nommée
    « LM Wavestone - … » faisait ressortir toutes les cartes portant un
    formulaire au lieu des seules offres Wavestone.
    """
    _seed_offer(conn, company="Valeo", title="Ingénieur IA", location="Créteil", state="later")
    _seed_offer(conn, company="Wavestone", title="Consultant IA", location="Paris", state="later")
    _seed_library(conn, "cover_letter", "LM Wavestone - Consultant IA", "/tmp/lm.pdf")

    page = render_page(conn)

    # Le nom de la lettre est bien présent dans la page (les menus le listent)...
    assert "LM Wavestone - Consultant IA" in page
    # ...mais il ne doit apparaître dans aucune zone cherchable.
    haystacks = re.findall(r'data-search="([^"]*)"', page)
    assert haystacks, "les cartes doivent exposer data-search"
    valeo = [h for h in haystacks if "valeo" in h]
    assert valeo, "la carte Valeo doit être cherchable"
    assert all("wavestone" not in h for h in valeo)

    wavestone = [h for h in haystacks if "wavestone" in h]
    assert len(wavestone) == 1
    assert "consultant ia" in wavestone[0]
    assert "paris" in wavestone[0]


def test_search_haystack_is_accent_and_case_folded(conn: sqlite3.Connection) -> None:
    """La comparaison côté JS est un simple includes : le serveur normalise."""
    _seed_offer(conn, company="Éloïse & Co", title="Développeur IA", location="Lyon")

    haystacks = re.findall(r'data-search="([^"]*)"', render_page(conn))

    assert any("eloise" in h and "developpeur" in h for h in haystacks)


def _seed_library(
    conn: sqlite3.Connection, doc_type: str, label: str, file_path: str
) -> int:
    cur = conn.execute(
        "INSERT INTO document_library (type, label, file_path) VALUES (?, ?, ?)",
        (doc_type, label, file_path),
    )
    conn.commit()
    return int(cur.lastrowid)


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


def _add_summary(conn: sqlite3.Connection, offer_id: int, *bullets: str) -> int:
    summary_id = int(
        conn.execute("INSERT INTO offer_summary (offer_id) VALUES (?)", (offer_id,)).lastrowid
    )
    conn.executemany(
        "INSERT INTO summary_bullet (summary_id, position, text) VALUES (?, ?, ?)",
        ((summary_id, position, bullet) for position, bullet in enumerate(bullets)),
    )
    conn.commit()
    return summary_id


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


def test_tracks_split_matches_exclusively(conn: sqlite3.Connection) -> None:
    _seed_offer(conn, company="NewPO", title="Chef de projet IA", state="new")
    _seed_offer(conn, company="SeenPO", title="Chef(fe) de projets IA — innovation", state="seen")
    _seed_offer(conn, company="LaterPO", title="Product Owner Data & IA", state="later")
    _seed_offer(conn, company="EngCo", title="AI Engineer", state="new")

    engineer = render_page(conn)
    project = render_page(conn, "project")
    assert "EngCo" in _section_html(engineer, "new")
    assert "EngCo" not in project
    assert "NewPO" in _section_html(project, "new")
    assert "SeenPO" in _section_html(project, "seen")
    assert "LaterPO" in _section_html(project, "later")
    for company in ("NewPO", "SeenPO", "LaterPO"):
        assert company not in engineer


def test_track_title_variants_and_case(conn: sqlite3.Connection) -> None:
    _seed_offer(conn, company="ProduitCo", title="CHEF DE PRODUIT Applicatif IA", state="new")
    _seed_offer(conn, company="PMCo", title="Product manager IA (H/F)", state="seen")

    project = render_page(conn, "project")
    assert "ProduitCo" in _section_html(project, "new")
    assert "PMCo" in _section_html(project, "seen")


def test_track_pages_have_full_sections_and_tabs(conn: sqlite3.Connection) -> None:
    _seed_offer(conn, company="HighPO", title="Chef de projet IA", state="new", fit="high")

    engineer = render_page(conn)
    project = render_page(conn, "project")
    assert "HighPO" in _section_html(project, "priority")
    assert "HighPO" not in engineer
    assert 'class="card-action action-later"' in _section_html(project, "priority")
    for page, active in ((engineer, ">Ingénieur IA<"), (project, ">Chef de projet / PO<")):
        assert 'href="/"' in page and 'href="/po"' in page
        assert f'aria-current="page"{active}' in page


def test_tracks_split_applied_and_discarded(conn: sqlite3.Connection) -> None:
    match_id, offer_id = _seed_offer(conn, company="AppPO", title="Chef de projet IA", state="new")
    _apply(conn, match_id, offer_id)
    eng_match, eng_offer = _seed_offer(conn, company="AppEng", title="AI Engineer", state="new")
    _apply(conn, eng_match, eng_offer)
    trash_id, _ = _seed_offer(conn, company="TrashPO", title="Product Owner IA", state="discarded")
    conn.execute("UPDATE match SET discarded_at = datetime('now') WHERE id = ?", (trash_id,))
    conn.commit()

    engineer = render_page(conn)
    project = render_page(conn, "project")
    assert "AppPO" in _section_html(project, "applied")
    assert "TrashPO" in _section_html(project, "discarded")
    assert "AppPO" not in engineer
    assert "TrashPO" not in engineer
    assert "AppEng" in _section_html(engineer, "applied")
    assert "AppEng" not in project


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

    assert 'class="row row-new"' in section
    assert 'class="reader-tab summary-toggle"' in section
    assert 'aria-expanded="false"' in section
    assert 'aria-controls="summary-match-' in section
    assert 'aria-label="Afficher le résumé de AI &lt;Engineer&gt; &quot;Senior&quot;"' in section
    assert 'class="reader-tabs"' in section
    assert 'class="summary-panel"' in section
    assert "hidden" in section
    assert "En bref" in section
    assert "Premier &lt;script&gt;alert(1)&lt;/script&gt;" in section
    assert "<script>alert(1)</script>" not in section
    assert section.index("Premier") < section.index("Deuxième &amp; dernier")


@pytest.mark.parametrize("fit", ["high", "medium", "low", None])
def test_summary_is_rendered_for_any_fit(
    conn: sqlite3.Connection, fit: str | None
) -> None:
    """Depuis les résumés structurés systématiques, En bref s'affiche quel que soit le fit."""
    _match_id, offer_id = _seed_offer(conn, company=f"Co-{fit}", fit=fit)
    _add_summary(conn, offer_id, "Fait visible")

    page = render_page(conn)

    assert "Fait visible" in page
    assert "En bref" in page
    assert '<button class="reader-tab summary-toggle"' in page


def test_summary_fields_render_labeled_lines(conn: sqlite3.Connection) -> None:
    _match_id, offer_id = _seed_offer(conn, company="FieldsCo")
    summary_id = _add_summary(conn, offer_id, "Puce mission")
    conn.executemany(
        "INSERT INTO summary_field (summary_id, key, value) VALUES (?, ?, ?)",
        [
            (summary_id, "experience", "3-5 ans"),
            (summary_id, "salary", "non précisé"),
            (summary_id, "remote", "hybride 2 jours"),
            (summary_id, "stack", "Python, RAG"),
        ],
    )
    conn.commit()

    page = render_page(conn)

    assert "Expérience souhaitée" in page
    assert "3-5 ans" in page
    assert "hybride 2 jours" in page
    # Champ « non précisé » : affiché mais atténué
    assert 'summary-field sf-empty' in page
    # L'ordre d'affichage suit FIELD_LABELS : expérience avant stack
    assert page.index("Expérience souhaitée") < page.index("Stack")


def test_high_without_summary_has_no_empty_summary_space(conn: sqlite3.Connection) -> None:
    _seed_offer(conn, company="HighCo", fit="high")

    page = render_page(conn)

    assert "En bref" not in page
    assert '<div class="summary-panel"' not in page
    assert 'class="reader-tab summary-toggle"' not in page


def test_high_application_renders_its_summary(conn: sqlite3.Connection) -> None:
    match_id, offer_id = _seed_offer(conn, company="AppliedHigh", fit="high")
    _add_summary(conn, offer_id, "Fait candidature")
    _apply(conn, match_id, offer_id)

    page = render_page(conn)
    applied = _section_html(page, "applied")

    assert "Fait candidature" in applied
    assert 'aria-controls="summary-application-' in applied


def test_summary_reader_preserves_external_link_interaction(
    conn: sqlite3.Connection,
) -> None:
    _match_id, offer_id = _seed_offer(conn, company="HighCo", fit="high")
    _add_summary(conn, offer_id, "Fait")

    page = render_page(conn)

    assert 'class="reader-tab summary-toggle"' in page
    assert 'class="card-reader"' in page
    assert 'target="_blank"' in page
    assert ".meta a { position:relative; z-index:3" in page
    assert "pointer-events:auto" in page


def test_content_panel_renders_escaped_and_collapsible(conn: sqlite3.Connection) -> None:
    _match_id, offer_id = _seed_offer(conn, company="ContentCo", title="AI Engineer", fit=None)
    _add_content(conn, offer_id, "Poste <script>alert(1)</script>\n\nDeuxième paragraphe.")

    page = render_page(conn)
    section = _section_html(page, "new")

    assert 'class="reader-tab offer-toggle"' in section
    assert 'aria-expanded="false"' in section
    assert 'aria-controls="content-match-' in section
    assert ">Annonce</button>" in section
    assert 'class="content-panel"' in section
    assert "hidden" in section
    assert "Poste &lt;script&gt;alert(1)&lt;/script&gt;" in section
    assert "<script>alert(1)</script>" not in section
    assert "Deuxième paragraphe." in section


def test_content_panel_absent_without_offer_content(conn: sqlite3.Connection) -> None:
    _seed_offer(conn, company="NoContentCo")

    page = render_page(conn)

    assert '<button class="reader-tab offer-toggle"' not in page
    assert '<div class="content-panel"' not in page


def test_content_panel_absent_when_fetch_failed(conn: sqlite3.Connection) -> None:
    _match_id, offer_id = _seed_offer(conn, company="FailedCo")
    _add_content(conn, offer_id, "peu importe", status="failed")

    page = render_page(conn)

    assert '<button class="reader-tab offer-toggle"' not in page
    assert '<div class="content-panel"' not in page


def test_markdown_to_html_renders_heading_as_styled_paragraph() -> None:
    """Pas de vraie balise <h1>-<h6> : ça casserait la hiérarchie de titres de la carte."""
    assert _markdown_to_html("## Missions") == '<p class="md-heading">Missions</p>'


def test_markdown_to_html_ignores_hash_without_space() -> None:
    assert _markdown_to_html("#recrutement2026") == "<p>#recrutement2026</p>"


def test_markdown_to_html_renders_bold_and_italic() -> None:
    assert _markdown_to_html("**Stack**: *Python*") == "<p><strong>Stack</strong>: <em>Python</em></p>"


def test_markdown_to_html_leaves_lone_asterisk_untouched() -> None:
    """Un astérisque isolé (ex: note de salaire '40-50k*') ne doit pas être avalé."""
    assert _markdown_to_html("Salaire 40-50k*") == "<p>Salaire 40-50k*</p>"


def test_markdown_to_html_renders_flat_unordered_list() -> None:
    html_out = _markdown_to_html("- Python\n- SQL")
    assert html_out == "<ul><li>Python</li><li>SQL</li></ul>"


def test_markdown_to_html_renders_ordered_list() -> None:
    html_out = _markdown_to_html("1. Entretien RH\n2. Entretien technique")
    assert html_out == "<ol><li>Entretien RH</li><li>Entretien technique</li></ol>"


def test_markdown_to_html_switches_list_type_without_blank_line() -> None:
    html_out = _markdown_to_html("- Python\n1. Entretien RH")
    assert html_out == "<ul><li>Python</li></ul><ol><li>Entretien RH</li></ol>"


def test_markdown_to_html_renders_safe_link() -> None:
    html_out = _markdown_to_html("Voir [le site](https://example.com/careers)")
    assert (
        '<a href="https://example.com/careers" target="_blank" '
        'rel="noopener noreferrer">le site</a>' in html_out
    )


def test_markdown_to_html_degrades_unsafe_link_scheme_to_label_only() -> None:
    """Ni lien ni syntaxe brute : le contenu réel regorge de liens relatifs/ancre
    de navigation scrapés (ex. '#main-content'), les laisser en littéral donnerait
    une impression de rendu cassé sur une bonne partie des annonces."""
    html_out = _markdown_to_html("[cliquer](javascript:malicious)")
    assert "<a " not in html_out
    assert "javascript:" not in html_out
    assert html_out == "<p>cliquer</p>"


def test_markdown_to_html_degrades_relative_nav_link_to_label_only() -> None:
    html_out = _markdown_to_html("[Skip to main content](#main-content)")
    assert "<a " not in html_out
    assert html_out == "<p>Skip to main content</p>"


def test_markdown_to_html_renders_titled_link() -> None:
    html_out = _markdown_to_html('[Wavestone](https://www.wavestone.com/ "Wavestone")')
    assert (
        '<a href="https://www.wavestone.com/" target="_blank" '
        'rel="noopener noreferrer">Wavestone</a>' in html_out
    )


def test_markdown_to_html_renders_link_with_parens_in_url() -> None:
    """Motif réel (choisirleservicepublic.gouv.fr) : une URL peut contenir des parenthèses."""
    html_out = _markdown_to_html("[Fiche](https://example.gouv.fr/metiers/ingenieur(e)/)")
    assert (
        '<a href="https://example.gouv.fr/metiers/ingenieur(e)/" target="_blank" '
        'rel="noopener noreferrer">Fiche</a>' in html_out
    )


def test_markdown_to_html_degrades_mailto_with_raw_spaces_to_label_only() -> None:
    """Motif réel (partage par email) : espaces bruts non encodés dans l'URL,
    et un titre optionnel en fin de parenthèse ne doit pas être avalé par l'URL."""
    html_out = _markdown_to_html(
        '[Partager par email](mailto:?subject=Une offre &body=Voir ici "Partager par email")'
    )
    assert "<a " not in html_out
    assert html_out == "<p>Partager par email</p>"


def test_markdown_to_html_renders_clickable_logo_as_plain_link() -> None:
    """Motif réel des offres scrapées : logo cliquable [![alt](image)](lien)."""
    html_out = _markdown_to_html(
        "[![Wavestone logo](https://c.example.com/logo.png)](https://www.wavestone.com/)"
    )
    assert (
        '<a href="https://www.wavestone.com/" target="_blank" '
        'rel="noopener noreferrer">Wavestone logo</a>' in html_out
    )
    assert "![" not in html_out


def test_markdown_to_html_drops_standalone_image() -> None:
    html_out = _markdown_to_html("![Decorative banner](https://example.com/banner.png)")
    assert html_out == "<p>Decorative banner</p>"


def test_markdown_to_html_renders_underlined_setext_heading() -> None:
    """markdownify produit ce style par défaut pour les h1/h2 (pas de #) ; une
    ligne de séparation visuelle marque la coupure de section sous le titre."""
    html_out = _markdown_to_html("Missions\n========\n\nTexte.")
    assert html_out == '<p class="md-heading">Missions</p><hr><p>Texte.</p>'


def test_markdown_to_html_drops_bare_heading_marker() -> None:
    """Motif réel (offre Valeo) : '#### ' sans texte (logo d'entreprise réduit
    à rien par markdownify) ne doit pas laisser '####' apparaître littéralement."""
    html_out = _markdown_to_html("#### \n\nValeo")
    assert "#" not in html_out
    assert html_out == "<p>Valeo</p>"


def test_markdown_to_html_renders_horizontal_rule() -> None:
    """Une ligne de --- seule (pas de texte juste avant) devient une vraie
    séparation visuelle, comme sur Obsidian, au lieu de tirets littéraux."""
    html_out = _markdown_to_html("Texte 1.\n\n---\n\nTexte 2.")
    assert html_out == "<p>Texte 1.</p><hr><p>Texte 2.</p>"


def test_markdown_to_html_renders_long_horizontal_rule() -> None:
    html_out = _markdown_to_html("Texte 1.\n\n----------------------------\n\nTexte 2.")
    assert html_out == "<p>Texte 1.</p><hr><p>Texte 2.</p>"


def test_markdown_to_html_horizontal_rule_does_not_break_setext_heading() -> None:
    """--- juste après une ligne de texte (sans ligne blanche) reste un titre
    souligné suivi de sa ligne, pas un --- littéral ni un titre sans ligne."""
    html_out = _markdown_to_html("Titre\n---\n\nTexte.")
    assert html_out == '<p class="md-heading">Titre</p><hr><p>Texte.</p>'


def test_markdown_to_html_drops_bare_heading_marker() -> None:
    """Motif réel (offre Valeo) : '#### ' sans texte (logo d'entreprise réduit
    à rien par markdownify) ne doit pas laisser '####' apparaître littéralement."""
    html_out = _markdown_to_html("#### \n\nValeo")
    assert "#" not in html_out
    assert html_out == "<p>Valeo</p>"


def test_markdown_to_html_renders_horizontal_rule() -> None:
    """Une ligne de --- seule (pas de texte juste avant) devient une vraie
    séparation visuelle, comme sur Obsidian, au lieu de tirets littéraux."""
    html_out = _markdown_to_html("Texte 1.\n\n---\n\nTexte 2.")
    assert html_out == "<p>Texte 1.</p><hr><p>Texte 2.</p>"


def test_markdown_to_html_renders_long_horizontal_rule() -> None:
    html_out = _markdown_to_html("Texte 1.\n\n----------------------------\n\nTexte 2.")
    assert html_out == "<p>Texte 1.</p><hr><p>Texte 2.</p>"


def test_markdown_to_html_horizontal_rule_does_not_break_setext_heading() -> None:
    """--- juste après une ligne de texte (sans ligne blanche) reste un titre
    souligné, pas une séparation : l'ambiguïté est déjà résolue en amont."""
    html_out = _markdown_to_html("Titre\n---\n\nTexte.")
    assert html_out == '<p class="md-heading">Titre</p><p>Texte.</p>'


def test_markdown_to_html_escapes_html_inside_formatting() -> None:
    html_out = _markdown_to_html("**<script>alert(1)</script>**")
    assert "<script>" not in html_out
    assert "<strong>&lt;script&gt;alert(1)&lt;/script&gt;</strong>" in html_out


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

    assert "offer-toggle" in page
    assert "Texte complet peu importe le fit." in page


def test_content_panel_renders_for_application(conn: sqlite3.Connection) -> None:
    match_id, offer_id = _seed_offer(conn, company="AppliedContentCo")
    _add_content(conn, offer_id, "Annonce complete pour une candidature.")
    _apply(conn, match_id, offer_id)

    page = render_page(conn)
    applied = _section_html(page, "applied")

    assert "offer-toggle" in applied
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


def test_http_po_route_serves_project_track(tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    connection = connect(db_path)
    init_db(connection)
    _seed_offer(connection, company="EngCo", title="AI Engineer")
    _seed_offer(connection, company="POCo", title="Product Owner IA")
    connection.close()
    server, thread = _start_server(db_path)
    try:
        port = server.server_address[1]
        status, _, body = _get(port, "/po")
        assert status == 200
        assert "POCo" in body
        assert "EngCo" not in body
        status, _, root_body = _get(port, "/")
        assert status == 200
        assert "EngCo" in root_body
        assert "POCo" not in root_body
        status, _, _ = _get(port, "/autre")
        assert status == 404
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


def test_database_error_returns_500_text(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "jw.db"
    connection = connect(db_path)
    init_db(connection)
    connection.close()

    def _boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("boom")

    monkeypatch.setattr("jobwatch.serve.render_page", _boom)
    server, thread = _start_server(db_path)
    try:
        port = server.server_address[1]
        status, headers, body = _get(port, "/")
        assert status == 500
        assert headers["Content-Type"] == "text/plain; charset=utf-8"
        assert body == "erreur base de données : boom\n"
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


def test_post_restore_accepts_later_state(tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    connection = connect(db_path)
    init_db(connection)
    match_id, _ = _seed_offer(connection, company="Acme", title="Role", state="later")
    connection.close()
    server, thread = _start_server(db_path)
    try:
        port = server.server_address[1]
        _post(port, f"/match/{match_id}/discard")
        status, _headers, _body = _post(
            port,
            f"/match/{match_id}/restore",
            body=json.dumps({"state": "later"}).encode("utf-8"),
            content_type="application/json",
        )
        assert status == 200

        check = connect(db_path)
        row = check.execute("SELECT * FROM match WHERE id = ?", (match_id,)).fetchone()
        check.close()
        assert row["state"] == "later"
        assert row["discarded_at"] is None
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


def _json_post(port: int, path: str, payload: dict) -> tuple[int, dict[str, str], str]:
    return _post(
        port, path, body=json.dumps(payload).encode("utf-8"), content_type="application/json"
    )


def _apply_state(db_path: Path, match_id: int) -> dict[str, object]:
    conn = connect(db_path)
    match = conn.execute("SELECT state FROM match WHERE id = ?", (match_id,)).fetchone()
    application = conn.execute(
        "SELECT id, note FROM application WHERE match_id = ?", (match_id,)
    ).fetchone()
    documents = []
    events = []
    if application is not None:
        documents = conn.execute(
            "SELECT type, path FROM document WHERE application_id = ? ORDER BY type",
            (application["id"],),
        ).fetchall()
        events = conn.execute(
            "SELECT type, comment FROM event WHERE application_id = ? ORDER BY id",
            (application["id"],),
        ).fetchall()
    conn.close()
    return {
        "state": match["state"],
        "application": application,
        "documents": [(row["type"], row["path"]) for row in documents],
        "events": [(row["type"], row["comment"]) for row in events],
    }


def test_post_apply_with_both_paths_creates_application_and_documents(tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    connection = connect(db_path)
    init_db(connection)
    match_id, _ = _seed_offer(connection, company="Acme", title="Role", state="later")
    cv_id = _seed_library(connection, "cv", "CV Acme", "cv/acme.pdf")
    cover_letter_id = _seed_library(connection, "cover_letter", "LM Acme", "lm/acme.md")
    connection.close()
    server, thread = _start_server(db_path)
    try:
        port = server.server_address[1]
        status, headers, body = _json_post(
            port,
            f"/match/{match_id}/apply",
            {"cv_library_id": cv_id, "cover_letter_library_id": cover_letter_id},
        )
        assert status == 200
        assert "application/json" in headers["Content-Type"]
        assert json.loads(body) == {"ok": True}

        result = _apply_state(db_path, match_id)
        assert result["state"] == "applied"
        assert result["application"] is not None
        assert result["documents"] == [("cover_letter", "lm/acme.md"), ("cv", "cv/acme.pdf")]
        assert result["events"] == [("applied", None)]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_post_apply_with_single_path_creates_one_document(tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    connection = connect(db_path)
    init_db(connection)
    match_id, _ = _seed_offer(connection, company="Acme", title="Role", state="later")
    cv_id = _seed_library(connection, "cv", "CV seul", "cv/seul.pdf")
    connection.close()
    server, thread = _start_server(db_path)
    try:
        port = server.server_address[1]
        status, _headers, _body = _json_post(
            port,
            f"/match/{match_id}/apply",
            {"cv_library_id": cv_id, "cover_letter_library_id": ""},
        )
        assert status == 200
        result = _apply_state(db_path, match_id)
        assert result["state"] == "applied"
        assert result["documents"] == [("cv", "cv/seul.pdf")]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_post_apply_without_paths_creates_application_only(tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    connection = connect(db_path)
    init_db(connection)
    match_id, _ = _seed_offer(connection, company="Acme", title="Role", state="later")
    connection.close()
    server, thread = _start_server(db_path)
    try:
        port = server.server_address[1]
        # corps absent et champs blancs : mêmes effets, zéro document
        status, _headers, _body = _post(port, f"/match/{match_id}/apply")
        assert status == 200
        result = _apply_state(db_path, match_id)
        assert result["state"] == "applied"
        assert result["application"] is not None
        assert result["documents"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_post_apply_blank_library_ids_create_no_document(tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    connection = connect(db_path)
    init_db(connection)
    match_id, _ = _seed_offer(connection, company="Acme", title="Role", state="later")
    connection.close()
    server, thread = _start_server(db_path)
    try:
        port = server.server_address[1]
        status, _headers, _body = _json_post(
            port,
            f"/match/{match_id}/apply",
            {"cv_library_id": "", "cover_letter_library_id": None},
        )
        assert status == 200
        assert _apply_state(db_path, match_id)["documents"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_post_apply_reachable_from_any_non_terminal_state(tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    connection = connect(db_path)
    init_db(connection)
    new_id, _ = _seed_offer(connection, company="NewCo", title="Role", state="new")
    seen_id, _ = _seed_offer(connection, company="SeenCo", title="Role", state="seen")
    connection.close()
    server, thread = _start_server(db_path)
    try:
        port = server.server_address[1]
        for match_id in (new_id, seen_id):
            status, _headers, _body = _post(port, f"/match/{match_id}/apply")
            assert status == 200
            assert _apply_state(db_path, match_id)["state"] == "applied"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_post_apply_rejected_for_discarded_match(tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    connection = connect(db_path)
    init_db(connection)
    match_id, _ = _seed_offer(connection, company="Acme", title="Role", state="discarded")
    connection.close()
    server, thread = _start_server(db_path)
    try:
        port = server.server_address[1]
        status, _headers, body = _post(port, f"/match/{match_id}/apply")
        assert status == 409
        assert "écarté" in json.loads(body)["error"]
        assert _apply_state(db_path, match_id)["application"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_post_apply_rejected_when_already_applied(tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    connection = connect(db_path)
    init_db(connection)
    match_id, _ = _seed_offer(connection, company="Acme", title="Role", state="later")
    connection.close()
    server, thread = _start_server(db_path)
    try:
        port = server.server_address[1]
        assert _post(port, f"/match/{match_id}/apply")[0] == 200
        status, _headers, body = _post(port, f"/match/{match_id}/apply")
        assert status == 409
        assert "déjà été postulé" in json.loads(body)["error"]

        conn = connect(db_path)
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM application WHERE match_id = ?", (match_id,)
        ).fetchone()["n"]
        conn.close()
        assert count == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_post_apply_rejects_non_integer_library_id(tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    connection = connect(db_path)
    init_db(connection)
    match_id, _ = _seed_offer(connection, company="Acme", title="Role", state="later")
    connection.close()
    server, thread = _start_server(db_path)
    try:
        port = server.server_address[1]
        status, _headers, _body = _json_post(
            port, f"/match/{match_id}/apply", {"cv_library_id": "not-an-id"}
        )
        assert status == 400
        assert _apply_state(db_path, match_id)["application"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_post_apply_missing_match_returns_404(tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    connection = connect(db_path)
    init_db(connection)
    connection.close()
    server, thread = _start_server(db_path)
    try:
        status, _headers, _body = _post(server.server_address[1], "/match/999999/apply")
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_cli_apply_and_http_apply_share_the_same_effects(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Le même parcours en base pour `jw apply` et POST /match/<id>/apply."""
    db_path = tmp_path / "jw.db"
    config = _write_config(tmp_path, db_path)
    connection = connect(db_path)
    init_db(connection)
    cli_id, _ = _seed_offer(connection, company="CliCo", title="Role", state="later")
    http_id, _ = _seed_offer(connection, company="HttpCo", title="Role", state="later")
    connection.close()

    result = runner.invoke(cli, ["apply", str(cli_id), "--config", str(config)])
    assert result.exit_code == 0, result.output

    server, thread = _start_server(db_path)
    try:
        assert _post(server.server_address[1], f"/match/{http_id}/apply")[0] == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    via_cli = _apply_state(db_path, cli_id)
    via_http = _apply_state(db_path, http_id)
    assert via_cli["state"] == via_http["state"] == "applied"
    assert via_cli["events"] == via_http["events"] == [("applied", None)]
    assert via_cli["documents"] == via_http["documents"] == []


def test_apply_button_and_form_rendered_for_actionable_sections_only(
    conn: sqlite3.Connection,
) -> None:
    _seed_offer(conn, company="NewCo", title="New Role", state="new")
    _seed_offer(conn, company="LaterCo", title="Later Role", state="later")
    trash_id, _ = _seed_offer(conn, company="TrashCo", title="Trash Role", state="discarded")
    conn.execute("UPDATE match SET discarded_at = datetime('now') WHERE id = ?", (trash_id,))
    app_match_id, app_offer_id = _seed_offer(conn, company="AppCo", title="App Role", state="new")
    _apply(conn, app_match_id, app_offer_id)
    conn.commit()

    page = render_page(conn)
    for key in ("new", "later"):
        section = _section_html(page, key)
        assert 'class="card-action action-apply"' in section
        assert 'class="apply-form"' in section
        assert 'name="cv_library_id"' in section
        assert 'name="cover_letter_library_id"' in section
        assert 'class="doc-icon-btn doc-upload-btn"' in section
    for key in ("discarded", "applied"):
        section = _section_html(page, key)
        assert 'class="card-action action-apply"' not in section
        assert 'class="apply-form"' not in section


def test_post_documents_uploads_and_returns_library_entry(tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    connection = connect(db_path)
    init_db(connection)
    connection.close()
    server, thread = _start_server(db_path)
    try:
        port = server.server_address[1]
        pdf = b"%PDF-1.4\ncontenu pdf"
        content = base64.b64encode(pdf).decode("ascii")
        status, headers, body = _json_post(
            port,
            "/documents",
            {"filename": "mon cv.pdf", "type": "cv", "label": "CV principal", "content_base64": content},
        )
        assert status == 201
        assert "application/json" in headers["Content-Type"]
        payload = json.loads(body)
        assert payload["label"] == "CV principal"
        assert payload["type"] == "cv"

        conn = connect(db_path)
        row = conn.execute(
            "SELECT type, label, file_path FROM document_library WHERE id = ?", (payload["id"],)
        ).fetchone()
        conn.close()
        assert row["type"] == "cv"
        assert row["label"] == "CV principal"
        file_path = Path(row["file_path"])
        assert file_path.parent == db_path.parent / "documents"
        assert file_path.read_bytes() == pdf
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_post_documents_sanitizes_traversal_filename(tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    connection = connect(db_path)
    init_db(connection)
    connection.close()
    server, thread = _start_server(db_path)
    try:
        port = server.server_address[1]
        content = base64.b64encode(b"%PDF-1.4\nmalicious").decode("ascii")
        status, _headers, body = _json_post(
            port,
            "/documents",
            {
                "filename": "../../../etc/passwd",
                "type": "cv",
                "label": "",
                "content_base64": content,
            },
        )
        assert status == 201
        payload = json.loads(body)

        conn = connect(db_path)
        row = conn.execute(
            "SELECT file_path FROM document_library WHERE id = ?", (payload["id"],)
        ).fetchone()
        conn.close()
        file_path = Path(row["file_path"])
        assert file_path.parent == db_path.parent / "documents"
        assert file_path.name.endswith("_passwd")
        assert not (db_path.parent / "etc" / "passwd").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_post_documents_rejects_invalid_type(tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    connection = connect(db_path)
    init_db(connection)
    connection.close()
    server, thread = _start_server(db_path)
    try:
        port = server.server_address[1]
        content = base64.b64encode(b"x").decode("ascii")
        status, _headers, body = _json_post(
            port,
            "/documents",
            {"filename": "a.pdf", "type": "resume", "label": "", "content_base64": content},
        )
        assert status == 400
        assert "error" in json.loads(body)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_post_documents_rejects_non_pdf_cv(tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    connection = connect(db_path)
    init_db(connection)
    connection.close()
    server, thread = _start_server(db_path)
    try:
        content = base64.b64encode(b"not a pdf").decode("ascii")
        status, _headers, body = _json_post(
            server.server_address[1],
            "/documents",
            {"filename": "cv.pdf", "type": "cv", "label": "", "content_base64": content},
        )
        assert status == 400
        assert "fichier PDF" in json.loads(body)["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_post_documents_rejects_missing_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    connection = connect(db_path)
    init_db(connection)
    connection.close()
    server, thread = _start_server(db_path)
    try:
        port = server.server_address[1]
        status, _headers, body = _json_post(port, "/documents", {"filename": "a.pdf"})
        assert status == 400
        assert "error" in json.loads(body)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_uploaded_document_appears_in_dropdown_on_next_render(tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    connection = connect(db_path)
    init_db(connection)
    _seed_offer(connection, company="Acme", title="Role", state="later")
    connection.close()
    server, thread = _start_server(db_path)
    try:
        port = server.server_address[1]
        content = base64.b64encode(b"%PDF-1.4\nx").decode("ascii")
        status, _headers, body = _json_post(
            port,
            "/documents",
            {"filename": "cv.pdf", "type": "cv", "label": "Mon CV", "content_base64": content},
        )
        assert status == 201
        library_id = json.loads(body)["id"]

        _, _headers, page_body = _get(port, "/")
        assert f'<option value="{library_id}">Mon CV</option>' in page_body
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


def test_document_field_has_icon_buttons(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _seed_offer(conn)
    page = render_page(conn)
    assert 'doc-preview-btn' in page
    assert 'doc-upload-btn' in page
    assert 'title="Prévisualiser"' in page
    assert 'title="Uploader"' in page
    assert "Uploader</button>" not in page


def test_http_document_preview_serves_library_file(tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    connection = connect(db_path)
    init_db(connection)
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    text = tmp_path / "lettre.tex"
    text.write_text("\\documentclass{article}")
    connection.execute(
        "INSERT INTO document_library (type, label, file_path) VALUES ('cv', 'CV', ?)",
        (str(pdf),),
    )
    connection.execute(
        "INSERT INTO document_library (type, label, file_path) VALUES "
        "('cover_letter', 'LM', ?)",
        (str(text),),
    )
    connection.commit()
    connection.close()
    server, thread = _start_server(db_path)
    try:
        port = server.server_address[1]
        status, headers, body = _get(port, "/documents/1")
        assert status == 200
        assert headers["Content-Type"] == "application/pdf"
        assert body.startswith("%PDF-1.4")
        status, headers, body = _get(port, "/documents/2")
        assert status == 200
        assert headers["Content-Type"] == "text/plain; charset=utf-8"
        status, _, _ = _get(port, "/documents/99")
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_matches_of_a_config_deactivated_search_stay_visible(conn: sqlite3.Connection) -> None:
    _seed_offer(conn, company="Bridge", title="Data Engineer", search="suivi-importe")
    conn.execute("UPDATE search SET active = 0 WHERE name = 'suivi-importe'")
    conn.commit()

    page = render_page(conn, "engineer")

    assert "Bridge" in page


def test_matches_of_an_archived_category_are_hidden(conn: sqlite3.Connection) -> None:
    _seed_offer(conn, company="Ancienne", title="Data Engineer", search="Data")
    conn.execute(
        "UPDATE search SET active = 0, archived_at = datetime('now') WHERE name = 'Data'"
    )
    conn.commit()

    page = render_page(conn, "engineer")

    assert "Ancienne" not in page
