"""Tests navigateur réels (Playwright/Chromium) des actions du dashboard.

Ces tests vérifient le parcours client complet : clic réel sur un bouton
d'action, mise à jour du DOM sans rechargement de page. Le test « transitions
sautées » reproduit le bug observé sur iPhone : quand le système saute la
transition CSS sans exposer prefers-reduced-motion (économie d'énergie),
transitionend ne se déclenche jamais ; l'ancien code attendait cet événement
et la carte restait figée sans toast d'annulation.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from test_serve import _add_content, _add_summary, _apply, _seed_offer

import jobwatch.serve as serve_module
from jobwatch.auth import create_invite
from jobwatch.config import DraftConfig
from jobwatch.db import connect, init_db
from jobwatch.serve import make_handler

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect, sync_playwright

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


@pytest.fixture()
def swipe_batch_dashboard(tmp_path: Path, monkeypatch):
    """Une offre, un CV et une génération neutralisée pour tester la sortie du swipe."""
    db_path = tmp_path / "jw.db"
    conn = connect(db_path)
    init_db(conn)
    _seed_offer(conn, company="BatchCo", title="Batch Role", state="new")
    cv_path = tmp_path / "cv.pdf"
    cv_path.write_bytes(b"%PDF-1.4 test")
    conn.execute(
        "INSERT INTO document_library (type, label, file_path) VALUES ('cv', 'CV test', ?)",
        (str(cv_path),),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(serve_module, "_spawn_draft_job", lambda *_args: None)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(db_path, DraftConfig(model="test-model")),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", db_path
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture()
def protected_dashboard(tmp_path: Path):
    """Instance nommée protégée, avec invitation propriétaire à consommer."""
    db_path = tmp_path / "jw.db"
    conn = connect(db_path)
    init_db(conn)
    _seed_offer(conn, company="AuthCo", title="Auth Role", state="new")
    invite = create_invite(conn, "alice", "alice@example.com")
    conn.close()
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(db_path, workspace_slug="alice", secure_cookie=False),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", invite
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture()
def onboarding_instance(tmp_path: Path):
    """Instance protégée avec le wizard d'onboarding activé."""
    db_path = tmp_path / "jw.db"
    conn = connect(db_path)
    init_db(conn)
    invite = create_invite(conn, "alice", "alice@example.com")
    conn.close()
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(
            db_path,
            workspace_slug="alice",
            secure_cookie=False,
            onboarding_config=DraftConfig(model="test-model"),
            onboarding_enabled=True,
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", invite
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _open_page(browser, url: str, dismiss_popup: bool = True):
    page = browser.new_page()
    page.goto(url)
    if dismiss_popup:
        popup = page.locator("#swipe-popup")
        if popup.count() and popup.is_visible():
            page.locator(".swipe-popup-later").click()
            popup.wait_for(state="hidden")
    # sentinelle : disparaît si la page est rechargée
    page.evaluate("window.__jw_no_reload = true")
    return page


def _assert_not_reloaded(page) -> None:
    assert page.evaluate("window.__jw_no_reload") is True


def _card(page, company: str):
    return page.locator(f'.row:has(.company:text-is("{company}"))')


def _sign_in_to_onboarding(page, url: str, invite: str) -> None:
    page.goto(f"{url}/invite/{invite}")
    password = "une très longue phrase secrète"
    page.get_by_label("Mot de passe", exact=True).fill(password)
    page.get_by_label("Confirmation", exact=True).fill(password)
    page.get_by_role("button", name="Créer mon compte").click()
    page.wait_for_load_state("networkidle")
    if page.url.endswith("/login"):
        page.get_by_label("Email", exact=True).fill("alice@example.com")
        page.get_by_label("Mot de passe", exact=True).fill(password)
        page.get_by_role("button", name="Se connecter").click()
        page.wait_for_load_state("networkidle")
    page.goto(f"{url}/onboarding")
    page.locator("#choice-step").wait_for(state="visible")


def _assert_balanced_action_panel(
    page, panel_selector: str, action_selector: str, reference_selector: str
) -> None:
    gaps = page.evaluate(
        """({panelSelector, actionSelector, referenceSelector}) => {
          const panel = document.querySelector(panelSelector);
          const action = document.querySelector(actionSelector);
          const reference = document.querySelector(referenceSelector);
          const actionRect = action.getBoundingClientRect();
          const referenceRect = reference.getBoundingClientRect();
          const panelRect = panel.getBoundingClientRect();
          return {
            top: actionRect.top - referenceRect.bottom,
            bottom: panelRect.bottom - actionRect.bottom,
          };
        }""",
        {
            "panelSelector": panel_selector,
            "actionSelector": action_selector,
            "referenceSelector": reference_selector,
        },
    )
    assert abs(gaps["top"] - gaps["bottom"]) <= 1.5, gaps


def _assert_no_horizontal_overflow(page) -> None:
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth + 1"
    )


def _assert_offer_link_in_header(card) -> None:
    link = card.get_by_role("link", name="Voir l’offre externe")
    expect(link).to_have_count(1)
    expect(link).to_have_attribute("target", "_blank")
    expect(link).to_have_attribute("rel", "noopener noreferrer")
    assert link.evaluate("el => el.parentElement.classList.contains('card-badges')")
    expect(card.locator(".meta a")).to_have_count(0)
    assert not card.locator(".meta").inner_text().rstrip().endswith("·")
    dimensions = link.evaluate(
        "el => ({width: el.getBoundingClientRect().width, "
        "height: el.getBoundingClientRect().height})"
    )
    assert dimensions["width"] == dimensions["height"]
    assert dimensions["width"] <= 36


def test_offer_link_is_a_header_icon_on_match_application_and_swipe_cards(
    browser, dashboard
) -> None:
    url, db_path = dashboard
    conn = connect(db_path)
    match_id, offer_id = _seed_offer(
        conn, company="AppliedCo", title="Applied Role", state="new"
    )
    _apply(conn, match_id, offer_id)
    conn.close()

    page = _open_page(browser, url)
    _assert_offer_link_in_header(_card(page, "NewCo"))
    page.locator(".section-applied > summary").click()
    _assert_offer_link_in_header(_card(page, "AppliedCo"))

    page.goto(f"{url}/swipe")
    _assert_offer_link_in_header(page.locator(".swipe-card.top"))
    page.close()


def test_onboarding_actions_are_balanced_on_both_paths(browser, onboarding_instance) -> None:
    url, invite = onboarding_instance
    page = browser.new_page()
    _sign_in_to_onboarding(page, url, invite)

    for width, height in ((390, 844), (1280, 900)):
        page.set_viewport_size({"width": width, "height": height})
        page.goto(f"{url}/onboarding")
        page.get_by_role("button", name=re.compile(r"^Créer mes catégories")).click()
        page.locator("#intent-step .panel").wait_for(state="visible")
        _assert_balanced_action_panel(
            page, "#intent-step .action-panel", "#intent-step .actions", "#intent-list"
        )
        _assert_no_horizontal_overflow(page)

        page.locator("#back-to-choice-intents").click()
        page.locator("#choice-step").wait_for(state="visible")
        page.get_by_role("button", name=re.compile(r"^Importer mes CV")).click()
        page.locator("#upload-step .panel").wait_for(state="visible")
        _assert_balanced_action_panel(
            page, "#upload-step .action-panel", "#analyze", "#drop-zone"
        )
        heights = page.evaluate(
            """() => ({
              back: document.querySelector('#back-to-choice-upload').getBoundingClientRect().height,
              action: document.querySelector('#analyze').getBoundingClientRect().height,
            })"""
        )
        assert abs(heights["back"] - heights["action"]) <= 1.5, heights
        _assert_no_horizontal_overflow(page)

        page.locator("#back-to-choice-upload").click()
        page.locator("#choice-step").wait_for(state="visible")
    page.close()


def test_onboarding_keeps_invalid_file_error_visible(browser, onboarding_instance) -> None:
    url, invite = onboarding_instance
    page = browser.new_page()
    _sign_in_to_onboarding(page, url, invite)
    page.get_by_role("button", name=re.compile(r"^Importer mes CV")).click()

    page.locator("#cv-file").set_input_files(
        {"name": "notes.txt", "mimeType": "text/plain", "buffer": b"pas un CV"}
    )

    assert "n’est pas un fichier PDF" in page.locator("#upload-status").inner_text()
    assert page.locator("#upload-status").get_attribute("class") == "status error"
    assert page.locator("#analyze").is_disabled()
    page.close()


def test_manual_onboarding_reaches_unified_dashboard(browser, onboarding_instance) -> None:
    url, invite = onboarding_instance
    page = browser.new_page()
    _sign_in_to_onboarding(page, url, invite)
    page.get_by_role("button", name=re.compile(r"^Créer mes catégories")).click()
    page.locator(".intent-label").fill("Ingénierie IA")
    page.locator(".keywords").fill("AI Engineer, LLM Engineer")

    page.locator("#confirm").click()

    page.wait_for_url(f"{url}/")
    assert page.get_by_role("link", name=re.compile("Modifier mes catégories")).is_visible()
    assert page.locator(".track-tabs").count() == 0
    page.close()


def test_card_reader_keeps_only_one_panel_open(browser, dashboard) -> None:
    url, db_path = dashboard
    conn = connect(db_path)
    offer_id = int(
        conn.execute(
            "SELECT o.id FROM offer o JOIN company c ON c.id = o.company_id "
            "WHERE c.name = 'NewCo'"
        ).fetchone()["id"]
    )
    _add_summary(conn, offer_id, "Résumé visible")
    _add_content(conn, offer_id, "Annonce visible")
    conn.close()
    page = _open_page(browser, url)
    card = _card(page, "NewCo")
    summary = card.locator(".summary-panel")
    content = card.locator(".content-panel")

    card.locator(".summary-toggle").click()
    assert summary.is_visible()
    assert not content.is_visible()
    card.locator(".offer-toggle").click()
    assert not summary.is_visible()
    assert content.is_visible()
    assert card.locator('.reader-tab[aria-expanded="true"]').all_inner_texts() == ["Annonce"]
    card.locator(".offer-toggle").click()
    assert not content.is_visible()
    tab_tops = card.locator(".reader-tab").evaluate_all(
        "tabs => tabs.map(tab => Math.round(tab.getBoundingClientRect().top))"
    )
    assert len(set(tab_tops)) == 1
    _assert_no_horizontal_overflow(page)
    page.close()


def test_offer_markdown_renders_visually_not_as_raw_syntax(browser, dashboard) -> None:
    """L'onglet Annonce affiche du HTML rendu, pas la syntaxe Markdown brute."""
    url, db_path = dashboard
    conn = connect(db_path)
    offer_id = int(
        conn.execute(
            "SELECT o.id FROM offer o JOIN company c ON c.id = o.company_id "
            "WHERE c.name = 'NewCo'"
        ).fetchone()["id"]
    )
    _add_content(
        conn,
        offer_id,
        "## Missions\n\n**Stack** : Python\n\n- Développement\n- Code review",
    )
    conn.close()
    page = _open_page(browser, url)
    card = _card(page, "NewCo")
    content = card.locator(".content-panel")

    card.locator(".offer-toggle").click()
    assert content.is_visible()
    assert content.locator(".md-heading", has_text="Missions").is_visible()
    assert content.locator("strong", has_text="Stack").is_visible()
    assert content.locator("li", has_text="Développement").is_visible()
    assert content.locator("li", has_text="Code review").is_visible()
    assert "##" not in content.inner_text()
    assert "**" not in content.inner_text()
    assert "- Développement" not in content.inner_text()
    _assert_no_horizontal_overflow(page)
    page.close()


def test_swipe_card_scroll_hides_scrollbar_chrome(browser, dashboard) -> None:
    """La zone défilante de la carte swipe ne réserve plus de gouttière pour une
    scrollbar visible (piste/pouce masqués), une fois le contenu déplié en
    overflow réel, mais le défilement reste fonctionnel."""
    url, db_path = dashboard
    conn = connect(db_path)
    offer_id = int(
        conn.execute(
            "SELECT o.id FROM offer o JOIN company c ON c.id = o.company_id "
            "WHERE c.name = 'NewCo'"
        ).fetchone()["id"]
    )
    long_content = "\n\n".join(f"## Section {i}\n\nTexte de section {i}." for i in range(30))
    _add_content(conn, offer_id, long_content)
    conn.close()

    page = browser.new_page()
    page.goto(f"{url}/swipe")
    popup = page.locator("#swipe-popup")
    if popup.count() and popup.is_visible():
        page.locator(".swipe-popup-later").click()
    card = page.locator(".swipe-card.top")
    card.locator(".swipe-content-toggle").click()

    scroll = card.locator(".swipe-card-scroll")
    assert scroll.evaluate("el => el.scrollHeight > el.clientHeight")

    computed_width = scroll.evaluate("el => getComputedStyle(el).scrollbarWidth")
    assert computed_width == "none", computed_width

    widths = scroll.evaluate("el => ({offset: el.offsetWidth, client: el.clientWidth})")
    assert widths["offset"] == widths["client"], widths

    assert scroll.evaluate("el => el.scrollTop") == 0
    scroll.evaluate("el => { el.scrollTop = 100 }")
    assert scroll.evaluate("el => el.scrollTop") == 100
    page.close()


def test_invite_then_protected_action_in_browser(browser, protected_dashboard) -> None:
    url, invite = protected_dashboard
    page = browser.new_page()
    page.goto(f"{url}/invite/{invite}")
    password = "une très longue phrase secrète"
    page.get_by_label("Mot de passe", exact=True).fill(password)
    page.get_by_label("Confirmation", exact=True).fill(password)
    page.get_by_role("button", name="Créer mon compte").click()
    page.wait_for_url(f"{url}/")
    popup = page.locator("#swipe-popup")
    if popup.is_visible():
        page.locator(".swipe-popup-later").click()
    card = _card(page, "AuthCo")
    card.locator(".action-later").click()
    page.locator(".undo-toast").wait_for(state="visible", timeout=5000)
    assert card.count() == 0
    page.close()


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


def test_swipe_popup_shows_once_per_session(browser, dashboard) -> None:
    url, _db_path = dashboard
    page = _open_page(browser, url, dismiss_popup=False)
    popup = page.locator("#swipe-popup")
    popup.wait_for(state="visible")
    assert "C'est le moment de swiper." in popup.inner_text()
    page.locator(".swipe-popup-later").click()
    popup.wait_for(state="hidden")
    # Rechargement dans la même session : le popup ne revient pas,
    # le bouton badge de la barre du haut reste disponible.
    page.reload()
    page.wait_for_selector(".swipe-fab")
    assert not page.locator("#swipe-popup").is_visible()
    page.close()


def test_bug_report_form_is_for_regular_users(browser, dashboard) -> None:
    url, db_path = dashboard
    page = _open_page(browser, url)

    page.get_by_role("button", name="Signaler un bug").click()
    dialog = page.get_by_role("dialog", name="Signaler un bug")
    expect(dialog).to_be_visible()
    dialog.get_by_label("Que s'est-il passé ?").fill(
        "Le résumé de la première offre ne s'affiche pas."
    )
    dialog.get_by_role("button", name="Envoyer").click()

    expect(dialog.locator("#bug-report-status")).to_contain_text(
        "le signalement a bien été envoyé"
    )
    conn = connect(db_path)
    report = conn.execute("SELECT message, page, user_agent FROM bug_report").fetchone()
    conn.close()
    assert report["message"] == "Le résumé de la première offre ne s'affiche pas."
    assert report["page"] == "/"
    assert "Chrome" in report["user_agent"]
    page.close()


def test_batch_generation_returns_to_dashboard(browser, swipe_batch_dashboard) -> None:
    url, db_path = swipe_batch_dashboard
    page = browser.new_page()
    page.goto(f"{url}/swipe")

    with page.expect_response(re.compile(r"/match/\d+/later$")) as later_response:
        page.locator("#swipe-yes").click()
    assert later_response.value.ok
    expect(page.locator("#swipe-done")).to_be_visible()

    with page.expect_response(re.compile(r"/draft/batch$")) as batch_response:
        page.locator("#batch-btn").click()
    assert batch_response.value.status == 202
    page.wait_for_url(f"{url}/")

    conn = connect(db_path)
    job = conn.execute("SELECT status FROM draft_job").fetchone()
    conn.close()
    assert job["status"] == "queued"
    assert page.locator(".hero").is_visible()
    page.close()


def test_onboarding_refuses_to_drop_an_incomplete_category(browser, onboarding_instance) -> None:
    url, invite = onboarding_instance
    page = browser.new_page()
    _sign_in_to_onboarding(page, url, invite)
    page.get_by_role("button", name=re.compile(r"^Créer mes catégories")).click()
    page.locator(".intent-label").fill("Ingénierie IA")
    page.locator(".keywords").fill("AI Engineer")
    page.locator("#add-intent").click()
    page.locator(".intent").nth(1).locator(".intent-label").fill("Produit")

    page.locator("#confirm").click()

    status = page.locator("#intent-status")
    assert "nom et au moins un mot-clé" in status.inner_text()
    assert page.url.endswith("/onboarding")

    page.locator(".intent").nth(1).locator(".keywords").fill("Product Owner")
    page.locator("#confirm").click()

    page.wait_for_url(f"{url}/")
    page.close()
