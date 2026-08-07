"""Tests for jobwatch.enrich : fetch/convert/résumé automatique des offres."""

from __future__ import annotations

import sqlite3

import httpx
import pytest

from jobwatch.config import EnrichConfig
from jobwatch.db import connect, init_db
from jobwatch.enrich import EnrichError, enrich


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = connect(":memory:")
    init_db(connection)
    yield connection
    connection.close()


def _seed_offer(
    conn: sqlite3.Connection,
    source_type: str = "france_travail",
    source_name: str = "france_travail",
    url: str = "https://example.com/offer-1",
    title: str = "AI Engineer",
) -> int:
    conn.execute("INSERT OR IGNORE INTO source (type, name) VALUES (?, ?)", (source_type, source_name))
    source_id = conn.execute(
        "SELECT id FROM source WHERE name = ?", (source_name,)
    ).fetchone()["id"]
    conn.execute("INSERT OR IGNORE INTO company (name) VALUES ('Acme')")
    company_id = conn.execute("SELECT id FROM company WHERE name = 'Acme'").fetchone()["id"]
    cur = conn.execute(
        "INSERT INTO offer (source_id, company_id, title, url) VALUES (?, ?, ?, ?)",
        (source_id, company_id, title, url),
    )
    return int(cur.lastrowid)


def _config() -> EnrichConfig:
    return EnrichConfig(opencode_bin="opencode", model="opencode/deepseek-v4-flash-free")


def _no_sleep(_seconds: float) -> None:
    return None


LONG_HTML = "<html><body><p>" + ("Ingénieur IA Paris. " * 30) + "</p></body></html>"
SHORT_HTML = "<html><body><p>trop court</p></body></html>"


def _http_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_enrich_without_config_raises() -> None:
    conn = connect(":memory:")
    init_db(conn)
    with pytest.raises(EnrichError):
        enrich(conn, None)


def test_enrich_ignores_bridge_offers(conn: sqlite3.Connection) -> None:
    """Les offres du bridge ai-job-search (type 'web'/'import') ne sont jamais enrichies."""
    _seed_offer(conn, source_type="web", source_name="linkedin", url="https://linkedin.com/x")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no fetch expected for bridge offers")

    result = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)
    assert result.fetched_ok == 0
    assert result.fetched_failed == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM offer_content").fetchone()["n"] == 0


def test_enrich_stores_content_and_summary(conn: sqlite3.Connection, monkeypatch) -> None:
    offer_id = _seed_offer(conn)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=LONG_HTML)

    monkeypatch.setattr("jobwatch.enrich._summarize", lambda config, markdown: ["Poste IA", "Paris"])

    result = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)

    assert result.fetched_ok == 1
    assert result.fetched_failed == 0
    assert result.summaries_written == 1

    content = conn.execute(
        "SELECT status, fetch_method, markdown FROM offer_content WHERE offer_id = ?", (offer_id,)
    ).fetchone()
    assert content["status"] == "ok"
    assert content["fetch_method"] == "http"
    assert "Ingénieur IA Paris" in content["markdown"]

    summary = conn.execute(
        "SELECT source FROM offer_summary WHERE offer_id = ?", (offer_id,)
    ).fetchone()
    assert summary["source"] == "auto"
    bullets = [
        row["text"]
        for row in conn.execute(
            "SELECT text FROM summary_bullet sb "
            "JOIN offer_summary os ON os.id = sb.summary_id "
            "WHERE os.offer_id = ? ORDER BY sb.position",
            (offer_id,),
        )
    ]
    assert bullets == ["Poste IA", "Paris"]


def test_enrich_does_not_overwrite_manual_summary(conn: sqlite3.Connection, monkeypatch) -> None:
    offer_id = _seed_offer(conn)
    cur = conn.execute(
        "INSERT INTO offer_summary (offer_id, source) VALUES (?, 'manual')", (offer_id,)
    )
    summary_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO summary_bullet (summary_id, position, text) VALUES (?, 0, 'bullet manuel')",
        (summary_id,),
    )
    conn.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=LONG_HTML)

    called = False

    def fake_summarize(config, markdown):
        nonlocal called
        called = True
        return ["ne doit jamais être écrit"]

    monkeypatch.setattr("jobwatch.enrich._summarize", fake_summarize)

    result = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)

    assert result.fetched_ok == 1
    assert result.summaries_written == 0
    assert called is False

    row = conn.execute(
        "SELECT source FROM offer_summary WHERE offer_id = ?", (offer_id,)
    ).fetchone()
    assert row["source"] == "manual"
    bullets = [
        row["text"]
        for row in conn.execute(
            "SELECT text FROM summary_bullet sb "
            "JOIN offer_summary os ON os.id = sb.summary_id "
            "WHERE os.offer_id = ? ORDER BY sb.position",
            (offer_id,),
        )
    ]
    assert bullets == ["bullet manuel"]
    # Le contenu complet est stocké même quand le résumé manuel reste intact.
    content = conn.execute(
        "SELECT status FROM offer_content WHERE offer_id = ?", (offer_id,)
    ).fetchone()
    assert content["status"] == "ok"


def test_enrich_falls_back_to_playwright_on_http_failure(
    conn: sqlite3.Connection, monkeypatch
) -> None:
    offer_id = _seed_offer(conn)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    monkeypatch.setattr("jobwatch.enrich._fetch_playwright", lambda url: LONG_HTML)
    monkeypatch.setattr("jobwatch.enrich._summarize", lambda config, markdown: None)

    result = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)

    assert result.fetched_ok == 1
    content = conn.execute(
        "SELECT status, fetch_method FROM offer_content WHERE offer_id = ?", (offer_id,)
    ).fetchone()
    assert content["status"] == "ok"
    assert content["fetch_method"] == "playwright"


def test_enrich_falls_back_to_playwright_on_short_text(
    conn: sqlite3.Connection, monkeypatch
) -> None:
    offer_id = _seed_offer(conn)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=SHORT_HTML)

    monkeypatch.setattr("jobwatch.enrich._fetch_playwright", lambda url: LONG_HTML)
    monkeypatch.setattr("jobwatch.enrich._summarize", lambda config, markdown: None)

    enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)

    content = conn.execute(
        "SELECT status, fetch_method FROM offer_content WHERE offer_id = ?", (offer_id,)
    ).fetchone()
    assert content["status"] == "ok"
    assert content["fetch_method"] == "playwright"


def test_enrich_marks_failed_when_all_fetches_fail(
    conn: sqlite3.Connection, monkeypatch
) -> None:
    offer_id = _seed_offer(conn)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    monkeypatch.setattr("jobwatch.enrich._fetch_playwright", lambda url: None)

    result = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)

    assert result.fetched_ok == 0
    assert result.fetched_failed == 1
    content = conn.execute(
        "SELECT status, markdown, fetch_method FROM offer_content WHERE offer_id = ?", (offer_id,)
    ).fetchone()
    assert content["status"] == "failed"
    assert content["markdown"] is None
    assert content["fetch_method"] is None
    summary = conn.execute(
        "SELECT id FROM offer_summary WHERE offer_id = ?", (offer_id,)
    ).fetchone()
    assert summary is None


def test_enrich_never_retries_processed_offers(conn: sqlite3.Connection, monkeypatch) -> None:
    _seed_offer(conn)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=LONG_HTML)

    monkeypatch.setattr("jobwatch.enrich._summarize", lambda config, markdown: None)

    first = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)
    assert first.fetched_ok == 1
    assert calls == 1

    second = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)
    assert second.fetched_ok == 0
    assert second.fetched_failed == 0
    assert calls == 1  # aucun nouveau fetch
    assert conn.execute("SELECT COUNT(*) AS n FROM offer_content").fetchone()["n"] == 1


def test_enrich_never_retries_failed_offers(conn: sqlite3.Connection, monkeypatch) -> None:
    _seed_offer(conn)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, text="boom")

    monkeypatch.setattr("jobwatch.enrich._fetch_playwright", lambda url: None)

    first = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)
    assert first.fetched_failed == 1
    assert calls == 1

    second = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)
    assert second.fetched_failed == 0
    assert calls == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM offer_content").fetchone()["n"] == 1


def test_enrich_sleeps_between_offers(conn: sqlite3.Connection, monkeypatch) -> None:
    _seed_offer(conn, url="https://example.com/1")
    _seed_offer(conn, url="https://example.com/2")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=LONG_HTML)

    monkeypatch.setattr("jobwatch.enrich._summarize", lambda config, markdown: None)

    sleeps: list[float] = []
    enrich(conn, _config(), client=_http_client(handler), sleep=sleeps.append)

    assert len(sleeps) == 1  # entre les 2 offres, jamais après la dernière
    assert 1.0 <= sleeps[0] <= 2.0
