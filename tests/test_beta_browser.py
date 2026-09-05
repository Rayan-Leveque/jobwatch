from __future__ import annotations

import json
from itertools import pairwise

import pytest
from playwright.sync_api import expect
from test_serve_auth import _start_server
from test_serve_browser import _assert_no_horizontal_overflow, browser  # noqa: F401

from jobwatch.auth import create_invite
from jobwatch.collectors.base import RawOffer, store_offers
from jobwatch.db import connect, init_db


def _assert_readable_seniority(page):
    labels = page.locator(".range-labels > span").evaluate_all("""elements => elements.map(el => {
        const range = document.createRange();
        range.selectNodeContents(el);
        const lines = [...range.getClientRects()];
        const rect = range.getBoundingClientRect();
        return {text: el.textContent, lines: lines.length, left: rect.left, right: rect.right};
    })""")
    assert len(labels) == 6
    assert all(label["lines"] == 1 for label in labels), labels
    assert all(right["left"] - left["right"] >= 4 for left, right in pairwise(labels)), labels
    rail = page.locator(".range-rail").bounding_box()
    assert abs((labels[0]["left"] + labels[0]["right"]) / 2 - rail["x"]) < 1
    assert abs((labels[-1]["left"] + labels[-1]["right"]) / 2 - rail["x"] - rail["width"]) < 1
    for slider in page.locator(".range-input").all():
        assert slider.evaluate("el => getComputedStyle(el).borderTopWidth") == "0px"


@pytest.mark.parametrize("width,height", [(320, 740), (390, 844), (1280, 900)])
def test_beta_preset_location_and_tracking(browser, tmp_path, width, height):  # noqa: F811
    db = tmp_path / "jobwatch.db"
    conn = connect(db)
    init_db(conn)
    invite = create_invite(conn, "alice", "alice@example.com")
    store_offers(conn, "test", "test", [
        RawOffer("Product Owner", "https://example.com/lyon", "Lyon Test", "test", "Lyon"),
        RawOffer("Business Analyst", "https://example.com/paris", "Paris Test", "test", "Paris"),
    ])
    conn.close()
    server, thread = _start_server(db, workspace_slug="alice", secure_cookie=False,
                                   onboarding_enabled=True)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    page = browser.new_page(viewport={"width": width, "height": height})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(f"{url}/invite/{invite}")
        page.screenshot(path=str(tmp_path / f"invite-{width}.png"), full_page=True)
        page.locator('input[name="password"]').fill("une phrase privée pour la bêta")
        page.locator('input[name="password_confirmation"]').fill("une phrase privée pour la bêta")
        page.locator('button[type="submit"]').click()
        page.wait_for_url(f"{url}/onboarding")
        assert not page.locator("#choose-cv").is_visible()
        page.screenshot(path=str(tmp_path / f"choice-{width}.png"), full_page=True)
        page.locator("#choose-po-moa").click()
        assert page.locator(".intent-label").count() == 2
        page.locator("#locations").fill("Lyon")
        _assert_readable_seniority(page)
        assert page.locator("body").evaluate(
            "el => getComputedStyle(el).backgroundColor"
        ) == "rgb(243, 241, 235)"
        location_box = page.locator("#locations").locator("..").bounding_box()
        note = page.locator(".analysis-note").bounding_box()
        assert note["y"] - (location_box["y"] + location_box["height"]) >= 16
        _assert_no_horizontal_overflow(page)
        page.screenshot(path=str(tmp_path / f"onboarding-{width}.png"), full_page=True)
        page.locator("#confirm").click()
        page.wait_for_url(f"{url}/options?welcome=1")
        assert not page.get_by_role("tab", name="Lettres", exact=True).is_visible()
        _assert_readable_seniority(page)
        page.screenshot(path=str(tmp_path / f"options-{width}.png"), full_page=True)
        page.goto(f"{url}/")
        if page.locator("#swipe-popup").is_visible():
            page.locator(".swipe-popup-later").click()
        expect(page.locator('.company:text-is("Lyon Test")')).to_be_visible()
        assert page.locator('.company:text-is("Paris Test")').count() == 0
        page.locator(".action-later").first.click()
        page.locator(".undo-toast").wait_for(state="visible")
        conn = connect(db)
        assert conn.execute("SELECT state FROM match").fetchone()[0] == "later"
        locations = conn.execute("SELECT locations_json FROM search").fetchall()
        assert all(json.loads(row[0]) == ["Lyon"] for row in locations)
        conn.close()
        page.goto(f"{url}/onboarding?edit=1")
        assert page.locator("#locations").input_value() == "Lyon"
        page.locator(".intent-label").first.fill("Mon objectif PO")
        page.locator("#confirm").click()
        page.wait_for_url(f"{url}/")
        conn = connect(db)
        assert conn.execute("SELECT state FROM match").fetchone()[0] == "later"
        conn.close()
        _assert_no_horizontal_overflow(page)
        assert errors == []
    finally:
        page.close()
        server.shutdown()
        server.server_close()
        thread.join(5)
