"""Tests navigateur réels (Playwright/Chromium) des actions du dashboard.

Ces tests vérifient le parcours client complet : clic réel sur un bouton
d'action, mise à jour du DOM sans rechargement de page. Le test « transitions
sautées » reproduit le bug observé sur iPhone : quand le système saute la
transition CSS sans exposer prefers-reduced-motion (économie d'énergie),
transitionend ne se déclenche jamais ; l'ancien code attendait cet événement
et la carte restait figée sans toast d'annulation.
"""

from __future__ import annotations

import sqlite3
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from test_serve import _seed_offer

from jobwatch.db import connect, init_db
from jobwatch.serve import make_handler

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

NO_TRANSITIONS_CSS = "*, *::before, *::after { transition: none !important }"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            instance = p.chromium.launch()
        except PlaywrightError as exc:  # pragma: no cover - environnement sans Chromium
            pytest.skip(f"chromium indisponible : {exc}")
        yield instance
        instance.close()


@pytest.fixture()
def dashboard(tmp_path: Path):
    """Base seedée + serveur HTTP réel ; renvoie (url, db_path)."""
    db_path = tmp_path / "jw.db"
    conn = connect(db_path)
    init_db(conn)
    _seed_offer(conn, company="NewCo", title="New Role", state="new")
    _seed_offer(conn, company="LaterCo", title="Later Role", state="later")
    conn.close()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", db_path
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _open_page(browser, url: str):
    page = browser.new_page()
    page.goto(url)
    # sentinelle : disparaît si la page est rechargée
    page.evaluate("window.__jw_no_reload = true")
    return page


def _assert_not_reloaded(page) -> None:
    assert page.evaluate("window.__jw_no_reload") is True


def _card(page, company: str):
    return page.locator(f'.row:has(.company:text-is("{company}"))')


def test_later_click_removes_card_and_shows_undo_without_reload(browser, dashboard) -> None:
    url, _db_path = dashboard
    page = _open_page(browser, url)
    card = _card(page, "NewCo")
    card.locator(".action-later").click()
    page.locator(".undo-toast").wait_for(state="visible", timeout=5000)
    assert card.count() == 0
    _assert_not_reloaded(page)
    page.close()


def test_discard_click_removes_card_and_shows_undo_without_reload(browser, dashboard) -> None:
    url, _db_path = dashboard
    page = _open_page(browser, url)
    card = _card(page, "NewCo")
    card.locator(".action-discard").click()
    page.locator(".undo-toast").wait_for(state="visible", timeout=5000)
    assert card.count() == 0
    _assert_not_reloaded(page)
    page.close()


def test_later_click_updates_dom_even_when_transitions_are_skipped(browser, dashboard) -> None:
    """Régression iPhone : transition sautée sans prefers-reduced-motion."""
    url, _db_path = dashboard
    page = _open_page(browser, url)
    page.add_style_tag(content=NO_TRANSITIONS_CSS)
    assert (
        page.evaluate("window.matchMedia('(prefers-reduced-motion: reduce)').matches") is False
    )
    card = _card(page, "NewCo")
    card.locator(".action-later").click()
    page.locator(".undo-toast").wait_for(state="visible", timeout=5000)
    assert card.count() == 0
    _assert_not_reloaded(page)
    page.close()


def test_undo_restores_previous_state(browser, dashboard) -> None:
    url, db_path = dashboard
    page = _open_page(browser, url)
    card = _card(page, "NewCo")
    card.locator(".action-later").click()
    page.locator(".undo-toast .undo-btn").wait_for(state="visible", timeout=5000)
    with page.expect_navigation():
        page.locator(".undo-toast .undo-btn").click()
    conn = connect(db_path)
    state = conn.execute(
        "SELECT m.state AS state FROM match m JOIN offer o ON o.id = m.offer_id "
        "JOIN company c ON c.id = o.company_id WHERE c.name = 'NewCo'"
    ).fetchone()["state"]
    conn.close()
    assert state == "new"
    page.close()


def test_apply_form_submits_and_removes_card_without_reload(browser, dashboard, tmp_path: Path) -> None:
    url, db_path = dashboard
    cv_src = tmp_path / "moncv.pdf"
    cv_src.write_bytes(b"%PDF-1.4 cv content")
    letter_src = tmp_path / "lettre.md"
    letter_src.write_bytes(b"# Lettre de motivation")

    page = _open_page(browser, url)
    page.locator('[data-section="later"] > summary').click()
    card = _card(page, "LaterCo")
    card.locator(".action-apply").click()
    form = card.locator(".apply-form")
    form.wait_for(state="visible", timeout=5000)

    cv_field = form.locator('.doc-field[data-doc-type="cv"]')
    cv_field.locator(".doc-file-input").set_input_files(str(cv_src))
    cv_field.locator(".doc-label-prompt").wait_for(state="visible", timeout=5000)
    cv_field.locator(".doc-label-input").fill("CV LaterCo")
    cv_field.locator(".doc-label-confirm").click()
    page.wait_for_function(
        "el => el.value !== ''", arg=cv_field.locator('[name="cv_library_id"]').element_handle()
    )

    letter_field = form.locator('.doc-field[data-doc-type="cover_letter"]')
    letter_field.locator(".doc-file-input").set_input_files(str(letter_src))
    letter_field.locator(".doc-label-prompt").wait_for(state="visible", timeout=5000)
    letter_field.locator(".doc-label-confirm").click()
    page.wait_for_function(
        "el => el.value !== ''",
        arg=letter_field.locator('[name="cover_letter_library_id"]').element_handle(),
    )

    form.locator(".apply-submit").click()
    page.locator(".undo-toast").wait_for(state="visible", timeout=5000)
    assert card.count() == 0
    assert page.locator(".undo-toast .undo-btn").count() == 0
    _assert_not_reloaded(page)

    conn = connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT a.id AS id, m.state AS state FROM application a "
        "JOIN match m ON m.id = a.match_id "
        "JOIN offer o ON o.id = a.offer_id JOIN company c ON c.id = o.company_id "
        "WHERE c.name = 'LaterCo'"
    ).fetchone()
    documents = conn.execute(
        "SELECT type, path FROM document WHERE application_id = ? ORDER BY type",
        (row["id"],),
    ).fetchall()
    conn.close()
    assert row["state"] == "applied"
    assert [d["type"] for d in documents] == ["cover_letter", "cv"]
    assert Path(documents[1]["path"]).read_bytes() == b"%PDF-1.4 cv content"
    assert Path(documents[0]["path"]).read_bytes() == b"# Lettre de motivation"
    page.close()


def test_card_action_buttons_render_in_order_later_apply_discard(browser, dashboard) -> None:
    url, _db_path = dashboard
    page = _open_page(browser, url)
    buttons = _card(page, "NewCo").locator(".card-actions .card-action")
    assert buttons.all_inner_texts() == ["Plus tard", "Candidater", "Écarter"]
    page.close()
