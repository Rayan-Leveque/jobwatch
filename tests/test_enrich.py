"""Tests for jobwatch.enrich : fetch/convert/résumé automatique des offres."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import httpx
import pytest

from jobwatch.config import ENRICH_RUNNERS, EnrichConfig
from jobwatch.db import connect, init_db
from jobwatch.enrich import FIELD_UNKNOWN, EnrichError, FetchPage, enrich


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
    match_state: str | None = "new",
) -> int:
    """Sème une offre, avec un match actif par défaut (enrich ignore le reste)."""
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
    offer_id = int(cur.lastrowid)
    if match_state is not None:
        conn.execute(
            "INSERT OR IGNORE INTO search (name, include_json, exclude_json, locations_json) "
            "VALUES ('test', '[]', '[]', '[]')"
        )
        search_id = conn.execute("SELECT id FROM search WHERE name = 'test'").fetchone()["id"]
        conn.execute(
            "INSERT INTO match (search_id, offer_id, state) VALUES (?, ?, ?)",
            (search_id, offer_id, match_state),
        )
    conn.commit()
    return offer_id


def _config() -> EnrichConfig:
    return EnrichConfig(opencode_bin="opencode", model="opencode/deepseek-v4-flash-free")


def _pi_config() -> EnrichConfig:
    return EnrichConfig(runner="pi", pi_bin="pi", model="openai-codex/gpt-5.6-luna")


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


def test_enrich_ignores_offers_without_active_match(conn: sqlite3.Connection) -> None:
    """Sans match new/seen/later ni candidature, aucune offre n'est enrichie (aucun token)."""
    _seed_offer(conn, url="https://example.com/no-match", match_state=None)
    _seed_offer(
        conn, url="https://example.com/discarded", match_state="discarded"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no fetch expected for inactive offers")

    result = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)
    assert result.fetched_ok == 0
    assert result.fetched_failed == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM offer_content").fetchone()["n"] == 0


def test_enrich_processes_bridge_offers_with_active_match(
    conn: sqlite3.Connection, monkeypatch
) -> None:
    """Les offres importées du bridge sont enrichies dès qu'un match les rend visibles."""
    offer_id = _seed_offer(
        conn, source_type="web", source_name="linkedin", url="https://linkedin.com/x"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=LONG_HTML)

    monkeypatch.setattr(
        "jobwatch.enrich._summarize",
        lambda config, markdown: ({"experience": "junior"}, {}, ["Poste IA"]),
    )
    result = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)
    assert result.fetched_ok == 1
    assert result.summaries_written == 1
    assert result.fields_written == 1
    field = conn.execute(
        "SELECT sf.value AS value FROM summary_field sf "
        "JOIN offer_summary os ON os.id = sf.summary_id "
        "WHERE os.offer_id = ? AND sf.key = 'experience'",
        (offer_id,),
    ).fetchone()
    assert field["value"] == "junior"


def test_enrich_stores_content_and_summary(conn: sqlite3.Connection, monkeypatch) -> None:
    offer_id = _seed_offer(conn)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=LONG_HTML)

    monkeypatch.setattr(
        "jobwatch.enrich._summarize",
        lambda config, markdown: ({"experience": "3 ans"}, {}, ["Poste IA", "Paris"]),
    )

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

    def fake_summarize(config, markdown):
        return {"experience": "5 ans"}, {}, ["ne doit jamais être écrit"]

    monkeypatch.setattr("jobwatch.enrich._summarize", fake_summarize)

    result = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)

    assert result.fetched_ok == 1
    assert result.summaries_written == 0
    assert result.fields_written == 1

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

    monkeypatch.setattr("jobwatch.enrich._fetch_playwright", lambda url: FetchPage(LONG_HTML))
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

    monkeypatch.setattr("jobwatch.enrich._fetch_playwright", lambda url: FetchPage(LONG_HTML))
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

    monkeypatch.setattr(
        "jobwatch.enrich._fetch_playwright", lambda url: FetchPage(None, "browser_error")
    )

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
        "SELECT id, source, status FROM offer_summary WHERE offer_id = ?", (offer_id,)
    ).fetchone()
    assert summary["source"] == "metadata"
    assert summary["status"] == "limited_retryable"


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


@pytest.mark.parametrize(
    ("http_failure", "browser_page", "failure_reason"),
    (
        ("403", FetchPage(None, "browser_error"), "http_403+browser_error"),
        ("timeout", FetchPage(None, "browser_error"), "http_timeout+browser_error"),
        ("short", FetchPage(None), "http_too_short+playwright_empty"),
        ("short", FetchPage(SHORT_HTML), "http_too_short+playwright_too_short"),
    ),
)
def test_enrich_retries_transient_failure_after_delay_then_succeeds(
    conn: sqlite3.Connection,
    monkeypatch,
    http_failure: str,
    browser_page: FetchPage,
    failure_reason: str,
) -> None:
    offer_id = _seed_offer(conn)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls > 1:
            return httpx.Response(200, text=LONG_HTML)
        if http_failure == "timeout":
            raise httpx.ReadTimeout("temporary timeout", request=request)
        if http_failure == "short":
            return httpx.Response(200, text=SHORT_HTML)
        return httpx.Response(403, text="temporary block")

    monkeypatch.setattr("jobwatch.enrich._fetch_playwright", lambda url: browser_page)
    monkeypatch.setattr(
        "jobwatch.enrich._summarize",
        lambda config, markdown: ({"experience": "3 ans"}, {}, ["Résumé récupéré"]),
    )

    first = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)
    assert first.fetched_failed == 1
    failed = conn.execute(
        "SELECT status, fetch_attempts, failure_reason FROM offer_content WHERE offer_id = ?",
        (offer_id,),
    ).fetchone()
    assert tuple(failed) == ("failed", 1, failure_reason)

    conn.execute(
        "UPDATE offer_content SET fetched_at = datetime('now', '-2 days') WHERE offer_id = ?",
        (offer_id,),
    )
    conn.commit()
    second = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)

    assert second.fetched_ok == 1
    assert second.summaries_written == 1
    recovered = conn.execute(
        "SELECT status, fetch_attempts, failure_reason, markdown "
        "FROM offer_content WHERE offer_id = ?",
        (offer_id,),
    ).fetchone()
    assert recovered["status"] == "ok"
    assert recovered["fetch_attempts"] == 2
    assert recovered["failure_reason"] is None
    assert "Ingénieur IA Paris" in recovered["markdown"]


def test_enrich_does_not_retry_before_delay(conn: sqlite3.Connection, monkeypatch) -> None:
    offer_id = _seed_offer(conn)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, text="temporary block")

    monkeypatch.setattr(
        "jobwatch.enrich._fetch_playwright", lambda url: FetchPage(None, "browser_error")
    )

    first = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)
    second = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)

    assert first.fetched_failed == 1
    assert second.fetched_failed == 0
    assert calls == 1
    attempts = conn.execute(
        "SELECT fetch_attempts FROM offer_content WHERE offer_id = ?", (offer_id,)
    ).fetchone()["fetch_attempts"]
    assert attempts == 1


def test_enrich_stops_at_attempt_ceiling(conn: sqlite3.Connection) -> None:
    offer_id = _seed_offer(conn)
    conn.execute(
        "INSERT INTO offer_content "
        "(offer_id, status, fetch_attempts, failure_reason, fetched_at) "
        "VALUES (?, 'failed', 3, 'browser_error', datetime('now', '-2 days'))",
        (offer_id,),
    )
    conn.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("attempt ceiling must prevent every fetch")

    result = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)

    assert result.fetched_ok == 0
    assert result.fetched_failed == 0
    assert conn.execute(
        "SELECT fetch_attempts FROM offer_content WHERE offer_id = ?", (offer_id,)
    ).fetchone()["fetch_attempts"] == 3


@pytest.mark.parametrize("status_code", (404, 410))
def test_enrich_marks_terminal_http_failures_without_retrying(
    conn: sqlite3.Connection, monkeypatch, status_code: int
) -> None:
    offer_id = _seed_offer(conn)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code, text="gone")

    def unexpected_browser(_url: str) -> FetchPage:
        raise AssertionError("terminal HTTP failures must not launch a browser")

    monkeypatch.setattr("jobwatch.enrich._fetch_playwright", unexpected_browser)

    first = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)
    conn.execute(
        "UPDATE offer_content SET fetched_at = datetime('now', '-2 days') WHERE offer_id = ?",
        (offer_id,),
    )
    conn.commit()
    second = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)

    assert first.fetched_failed == 1
    assert second.fetched_failed == 0
    assert calls == 1
    failed = conn.execute(
        "SELECT status, fetch_attempts, failure_reason FROM offer_content WHERE offer_id = ?",
        (offer_id,),
    ).fetchone()
    assert tuple(failed) == ("failed", 1, f"http_{status_code}")


def test_enrich_retries_unclassified_legacy_failure_immediately(
    conn: sqlite3.Connection, monkeypatch
) -> None:
    offer_id = _seed_offer(conn)
    conn.execute(
        "INSERT INTO offer_content (offer_id, status, fetch_attempts, failure_reason) "
        "VALUES (?, 'failed', 1, NULL)",
        (offer_id,),
    )
    conn.commit()
    monkeypatch.setattr("jobwatch.enrich._summarize", lambda config, markdown: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=LONG_HTML)

    result = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)

    assert result.fetched_ok == 1
    recovered = conn.execute(
        "SELECT status, fetch_attempts, failure_reason FROM offer_content WHERE offer_id = ?",
        (offer_id,),
    ).fetchone()
    assert tuple(recovered) == ("ok", 2, None)


def test_enrich_retries_transient_pi_summary_failure_without_refetch(
    conn: sqlite3.Connection, monkeypatch
) -> None:
    offer_id = _seed_offer(conn)
    fetch_calls = 0
    summary_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fetch_calls
        fetch_calls += 1
        return httpx.Response(200, text=LONG_HTML)

    def summarize_pi(config: EnrichConfig, markdown: str):
        nonlocal summary_calls
        summary_calls += 1
        if summary_calls == 1:
            return None
        return {"experience": "3 ans"}, {}, ["Résumé Pi récupéré"]

    monkeypatch.setattr("jobwatch.enrich._summarize_pi", summarize_pi)
    client = _http_client(handler)
    first = enrich(conn, _pi_config(), client=client, sleep=_no_sleep)
    limited = conn.execute(
        "SELECT source, status, attempt_count FROM offer_summary WHERE offer_id = ?",
        (offer_id,),
    ).fetchone()
    assert first.fetched_ok == 1
    assert tuple(limited) == ("metadata", "limited_retryable", 1)

    conn.execute(
        "UPDATE offer_summary SET attempted_at = datetime('now', '-2 hours') "
        "WHERE offer_id = ?",
        (offer_id,),
    )
    conn.commit()
    second = enrich(conn, _pi_config(), client=client, sleep=_no_sleep)
    client.close()

    assert second.fetched_ok == 0
    assert second.summaries_written == 1
    assert fetch_calls == 1
    assert summary_calls == 2
    upgraded = conn.execute(
        "SELECT source, status, attempt_count FROM offer_summary WHERE offer_id = ?",
        (offer_id,),
    ).fetchone()
    assert tuple(upgraded) == ("auto", "ready", 2)


def test_enrich_bounds_summary_retries_and_respects_delay(
    conn: sqlite3.Connection, monkeypatch
) -> None:
    offer_id = _seed_offer(conn)
    conn.execute(
        "INSERT INTO offer_content (offer_id, markdown, fetch_method, status) "
        "VALUES (?, ?, 'http', 'ok')",
        (offer_id, "Annonce fiable " * 30),
    )
    conn.commit()
    calls = 0

    def failed_summary(config: EnrichConfig, markdown: str):
        nonlocal calls
        calls += 1

    monkeypatch.setattr("jobwatch.enrich._summarize_pi", failed_summary)
    first = enrich(conn, _pi_config(), client=_http_client(lambda request: None), sleep=_no_sleep)
    immediate = enrich(
        conn, _pi_config(), client=_http_client(lambda request: None), sleep=_no_sleep
    )
    assert first.summaries_written == 1
    assert immediate.summaries_written == 0
    assert calls == 1

    for expected_attempts in (2, 3):
        conn.execute(
            "UPDATE offer_summary SET attempted_at = datetime('now', '-2 hours') "
            "WHERE offer_id = ?",
            (offer_id,),
        )
        conn.commit()
        enrich(conn, _pi_config(), client=_http_client(lambda request: None), sleep=_no_sleep)
        attempts = conn.execute(
            "SELECT attempt_count FROM offer_summary WHERE offer_id = ?", (offer_id,)
        ).fetchone()["attempt_count"]
        assert attempts == expected_attempts

    conn.execute(
        "UPDATE offer_summary SET attempted_at = datetime('now', '-2 hours') WHERE offer_id = ?",
        (offer_id,),
    )
    conn.commit()
    capped = enrich(conn, _pi_config(), client=_http_client(lambda request: None), sleep=_no_sleep)
    assert capped.summaries_written == 0
    assert calls == 3


def test_enrich_summarizes_stored_content_without_fetch(
    conn: sqlite3.Connection, monkeypatch
) -> None:
    offer_id = _seed_offer(conn)
    conn.execute(
        "INSERT INTO offer_content (offer_id, markdown, fetch_method, status) "
        "VALUES (?, ?, 'http', 'ok')",
        (offer_id, "Annonce fiable " * 30),
    )
    conn.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("stored usable content must not be fetched again")

    monkeypatch.setattr(
        "jobwatch.enrich._summarize_pi",
        lambda config, markdown: ({"remote": "hybride"}, {}, ["Résumé depuis la base"]),
    )
    result = enrich(conn, _pi_config(), client=_http_client(handler), sleep=_no_sleep)

    assert result.fetched_ok == 0
    assert result.summaries_written == 1
    summary = conn.execute(
        "SELECT source, status FROM offer_summary WHERE offer_id = ?", (offer_id,)
    ).fetchone()
    assert tuple(summary) == ("auto", "ready")


def test_terminal_fetch_gets_trustworthy_metadata_fallback(
    conn: sqlite3.Connection, monkeypatch
) -> None:
    offer_id = _seed_offer(
        conn,
        source_name="wttj",
        url="https://example.com/gone",
        title="Product Owner IA",
    )
    conn.execute(
        "UPDATE offer SET location = 'Paris', contract = 'permanent', platform = 'WTTJ' "
        "WHERE id = ?",
        (offer_id,),
    )
    conn.execute("UPDATE match SET fit = 'high' WHERE offer_id = ?", (offer_id,))
    conn.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410, text="gone")

    monkeypatch.setattr(
        "jobwatch.enrich._fetch_playwright",
        lambda url: (_ for _ in ()).throw(AssertionError("410 is terminal")),
    )
    result = enrich(conn, _pi_config(), client=_http_client(handler), sleep=_no_sleep)

    assert result.fetched_failed == 1
    summary = conn.execute(
        "SELECT id, source, status FROM offer_summary WHERE offer_id = ?", (offer_id,)
    ).fetchone()
    assert summary["source"] == "metadata"
    assert summary["status"] == "limited_no_content"
    bullets = "\n".join(
        row["text"]
        for row in conn.execute(
            "SELECT text FROM summary_bullet WHERE summary_id = ? ORDER BY position",
            (summary["id"],),
        )
    )
    for expected in ("Product Owner IA", "Paris", "permanent", "WTTJ", "test", "fit high"):
        assert expected in bullets


def test_metadata_fallback_upgrades_when_real_content_arrives(
    conn: sqlite3.Connection, monkeypatch
) -> None:
    offer_id = _seed_offer(conn)
    conn.execute(
        "INSERT INTO offer_content "
        "(offer_id, status, fetch_attempts, failure_reason) VALUES (?, 'failed', 1, 'http_410')",
        (offer_id,),
    )
    conn.commit()
    first = enrich(conn, _pi_config(), client=_http_client(lambda request: None), sleep=_no_sleep)
    assert first.summaries_written == 1

    conn.execute(
        "UPDATE offer_content SET status = 'ok', markdown = ?, fetch_method = 'http', "
        "failure_reason = NULL WHERE offer_id = ?",
        ("Contenu réel de l'annonce. " * 30, offer_id),
    )
    conn.commit()
    monkeypatch.setattr(
        "jobwatch.enrich._summarize_pi",
        lambda config, markdown: ({"stack": "Python"}, {}, ["Mission issue du contenu réel"]),
    )

    second = enrich(
        conn,
        _pi_config(),
        client=_http_client(
            lambda request: (_ for _ in ()).throw(AssertionError("no refetch after ok"))
        ),
        sleep=_no_sleep,
    )

    assert second.summaries_written == 1
    summary = conn.execute(
        "SELECT id, source, status FROM offer_summary WHERE offer_id = ?", (offer_id,)
    ).fetchone()
    assert tuple(summary)[1:] == ("auto", "ready")
    bullets = [
        row["text"]
        for row in conn.execute(
            "SELECT text FROM summary_bullet WHERE summary_id = ? ORDER BY position",
            (summary["id"],),
        )
    ]
    assert bullets == ["Mission issue du contenu réel"]


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


def test_parse_summary_extracts_fields_and_bullets() -> None:
    from jobwatch.enrich import _parse_summary

    text = (
        "EXPERIENCE: 3-5 ans\n"
        "SALAIRE: 45-55k\n"
        "TELETRAVAIL: hybride 2 jours\n"
        "STACK: Python, RAG, AWS\n"
        "- Poste d'ingénieur IA\n"
        "- CDI à Paris\n"
    )
    fields, quotes, bullets = _parse_summary(text)
    assert fields == {
        "experience": "3-5 ans",
        "salary": "45-55k",
        "remote": "hybride 2 jours",
        "stack": "Python, RAG, AWS",
    }
    assert quotes == {}
    assert bullets == ["Poste d'ingénieur IA", "CDI à Paris"]


def test_parse_summary_tolerates_noise_and_missing_fields() -> None:
    from jobwatch.enrich import _parse_summary

    text = "Voici le résumé :\n**SALAIRE:** \n- Une puce\nTexte libre ignoré\n"
    fields, quotes, bullets = _parse_summary(text)
    assert fields == {"salary": "non précisé"}
    assert quotes == {}
    assert bullets == ["Une puce"]


def test_summary_only_keeps_citations_found_in_offer_text() -> None:
    from jobwatch.enrich import _parse_summary, _verified_quotes

    text = (
        "EXPERIENCE: 3 ans\n"
        "EXPERIENCE_CITATION: Vous avez au moins 3 ans d’expérience.\n"
        "SALAIRE: 60k\n"
        "SALAIRE_CITATION: Salaire annuel garanti de 60k.\n"
        "- Mission IA\n"
    )
    fields, quotes, bullets = _parse_summary(text)

    verified = _verified_quotes(
        quotes,
        "Le profil recherché : vous avez au moins 3 ans d’expérience.",
    )

    assert fields == {"experience": "3 ans", "salary": "60k"}
    assert verified == {"experience": "Vous avez au moins 3 ans d’expérience."}
    assert bullets == ["Mission IA"]


def test_enrich_adds_fields_to_offer_with_content_without_refetch(
    conn: sqlite3.Connection, monkeypatch
) -> None:
    """Backfill : contenu déjà en base -> champs ajoutés sans aucun fetch web."""
    offer_id = _seed_offer(conn, match_state="later")
    conn.execute(
        "INSERT INTO offer_content (offer_id, markdown, fetch_method, status) "
        "VALUES (?, 'Annonce IA détaillée.', 'http', 'ok')",
        (offer_id,),
    )
    cur = conn.execute(
        "INSERT INTO offer_summary (offer_id, source) VALUES (?, 'auto')", (offer_id,)
    )
    conn.execute(
        "INSERT INTO summary_bullet (summary_id, position, text) VALUES (?, 0, 'ancienne puce')",
        (int(cur.lastrowid),),
    )
    conn.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no fetch expected: content already stored")

    monkeypatch.setattr(
        "jobwatch.enrich._summarize",
        lambda config, markdown: ({"experience": "senior"}, {}, ["nouvelle puce ignorée"]),
    )
    sleeps: list[float] = []
    result = enrich(conn, _config(), client=_http_client(handler), sleep=sleeps.append)

    assert result.fetched_ok == 0
    assert result.fields_written == 1
    assert result.summaries_written == 0
    assert sleeps == []  # pas de fetch web, pas de pause anti-martèlement
    bullets = [
        row["text"]
        for row in conn.execute(
            "SELECT text FROM summary_bullet sb "
            "JOIN offer_summary os ON os.id = sb.summary_id WHERE os.offer_id = ?",
            (offer_id,),
        )
    ]
    assert bullets == ["ancienne puce"]

    # Une fois les champs écrits, l'offre n'est plus jamais retraitée.
    second = enrich(conn, _config(), client=_http_client(handler), sleep=sleeps.append)
    assert second.fields_written == 0


def test_summarize_passes_variant_to_opencode(monkeypatch) -> None:
    import json
    import subprocess as sp

    from jobwatch.enrich import _summarize

    captured: dict[str, list[str]] = {}

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        event = json.dumps({"type": "text", "part": {"text": "EXPERIENCE: 2 ans\n- Puce"}})
        return sp.CompletedProcess(command, 0, stdout=event, stderr="")

    monkeypatch.setattr("jobwatch.llm_runner.subprocess.run", fake_run)
    result = _summarize(
        EnrichConfig(opencode_bin="opencode", model="opencode-go/gpt-5.6-luna", variant="max"),
        "texte d'offre",
    )
    assert result == ({"experience": "2 ans"}, {}, ["Puce"])
    command = captured["command"]
    assert command[command.index("--variant") + 1] == "max"
    assert command[command.index("--model") + 1] == "opencode-go/gpt-5.6-luna"

    # Sans variant configuré, le drapeau n'apparaît pas.
    _summarize(EnrichConfig(opencode_bin="opencode", model="m"), "texte")
    assert "--variant" not in captured["command"]


def test_enrich_summarizes_in_parallel_pool(conn: sqlite3.Connection, monkeypatch) -> None:
    """Les résumés partent en parallèle : deux appels simultanés observés avec 2 workers."""
    import threading as th
    import time as time_mod

    for i in range(3):
        _seed_offer(conn, url=f"https://example.com/par-{i}")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=LONG_HTML)

    active = 0
    peak = 0
    lock = th.Lock()

    def slow_summarize(config, markdown):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time_mod.sleep(0.05)
        with lock:
            active -= 1
        return {"experience": "x"}, {}, ["puce"]

    monkeypatch.setattr("jobwatch.enrich._summarize", slow_summarize)
    config = EnrichConfig(opencode_bin="opencode", model="m", concurrency=2)
    result = enrich(conn, config, client=_http_client(handler), sleep=_no_sleep)

    assert result.summaries_written == 3
    assert result.fields_written == 3
    assert peak == 2  # borné par concurrency, mais bien parallèle


def test_config_parses_codex_runner(tmp_path, monkeypatch) -> None:
    from jobwatch.config import ConfigError, load_config

    fake_bin_dir = tmp_path / "bin"
    fake_bin_dir.mkdir()
    fake_codex = fake_bin_dir / "codex"
    fake_codex.write_text("#!/bin/sh\n")
    fake_codex.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin_dir}{os.pathsep}{os.environ['PATH']}")

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"db: {tmp_path / 'db.sqlite'}\n"
        "searches:\n  - name: test\n    include: ['AI']\n"
        "enrich:\n"
        "  runner: codex\n"
        "  model: gpt-5.6-luna\n"
        "  variant: max\n"
    )
    config = load_config(config_file).enrich
    assert config is not None
    assert config.runner == "codex"
    # codex_bin résolu en chemin absolu (pas juste 'codex') : un cron a un PATH
    # minimal, donc jw enrich doit échouer fort ici plutôt que de tourner avec
    # un binaire introuvable au moment de résumer chaque offre.
    assert config.codex_bin == str(fake_codex)
    assert config.model == "gpt-5.6-luna"

    config_file.write_text(
        f"db: {tmp_path / 'db.sqlite'}\n"
        "searches:\n  - name: test\n    include: ['AI']\n"
        "enrich:\n"
        "  runner: gemini\n"
        "  model: m\n"
    )
    import pytest as _pytest

    with _pytest.raises(ConfigError, match="runner"):
        load_config(config_file)

    # Le runner opencode exige toujours opencode_bin explicitement.
    config_file.write_text(
        f"db: {tmp_path / 'db.sqlite'}\n"
        "searches:\n  - name: test\n    include: ['AI']\n"
        "enrich:\n"
        "  model: m\n"
    )
    with _pytest.raises(ConfigError, match="opencode_bin"):
        load_config(config_file)


def test_config_fails_loudly_when_codex_bin_missing_from_path(tmp_path, monkeypatch) -> None:
    from jobwatch.config import ConfigError, load_config

    monkeypatch.setenv("PATH", str(tmp_path))  # répertoire vide : rien n'y est résoluble

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"db: {tmp_path / 'db.sqlite'}\n"
        "searches:\n  - name: test\n    include: ['AI']\n"
        "enrich:\n"
        "  runner: codex\n"
        "  model: gpt-5.6-luna\n"
        "  codex_bin: codex-introuvable\n"
    )
    with pytest.raises(ConfigError, match="codex-introuvable"):
        load_config(config_file)


def test_config_parses_pi_runner_and_resolves_binary(tmp_path, monkeypatch) -> None:
    from jobwatch.config import load_config

    fake_bin_dir = tmp_path / "bin"
    fake_bin_dir.mkdir()
    fake_pi = fake_bin_dir / "pi"
    fake_pi.write_text("#!/bin/sh\n")
    fake_pi.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin_dir))

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"db: {tmp_path / 'db.sqlite'}\n"
        "searches:\n  - name: test\n    include: ['AI']\n"
        "enrich:\n"
        "  runner: pi\n"
        "  model: openai-codex/gpt-5.6-luna\n"
        "  variant: max\n"
    )

    config = load_config(config_file).enrich
    assert config is not None
    assert config.runner == "pi"
    assert config.pi_bin == str(fake_pi)
    assert config.model == "openai-codex/gpt-5.6-luna"
    assert config.variant == "max"


def test_config_pi_accepts_absolute_binary_with_empty_path(tmp_path, monkeypatch) -> None:
    from jobwatch.config import load_config

    fake_pi = tmp_path / "pi"
    fake_pi.write_text("#!/bin/sh\n")
    fake_pi.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"db: {tmp_path / 'db.sqlite'}\n"
        "searches:\n  - name: test\n    include: ['AI']\n"
        "enrich:\n"
        "  runner: pi\n"
        f"  pi_bin: {fake_pi}\n"
        "  model: model\n"
    )

    config = load_config(config_file).enrich
    assert config is not None
    assert config.pi_bin == str(fake_pi)


def test_config_fails_loudly_when_pi_bin_missing(tmp_path, monkeypatch) -> None:
    from jobwatch.config import ConfigError, load_config

    monkeypatch.setenv("PATH", str(tmp_path))
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"db: {tmp_path / 'db.sqlite'}\n"
        "searches:\n  - name: test\n    include: ['AI']\n"
        "enrich:\n"
        "  runner: pi\n"
        "  pi_bin: pi-introuvable\n"
        "  model: model\n"
    )

    with pytest.raises(ConfigError, match="pi-introuvable"):
        load_config(config_file)


_ACTIVE_CONFIG_PATH = Path.home() / ".config" / "jobwatch" / "config.yaml"


@pytest.mark.skipif(
    not _ACTIVE_CONFIG_PATH.exists(),
    reason=f"pas de config active à {_ACTIVE_CONFIG_PATH} sur cette machine",
)
def test_active_config_loads_without_enrichment_or_llm_calls() -> None:
    """Charge la config de production réelle : aucune requête réseau ni appel LLM,
    seulement la résolution de chemins/binaires que fait déjà load_config()."""
    from jobwatch.config import load_config

    config = load_config(_ACTIVE_CONFIG_PATH)

    assert config.db is not None
    if config.enrich is not None:
        assert config.enrich.runner in ENRICH_RUNNERS
        if config.enrich.runner == "pi":
            assert Path(config.enrich.pi_bin).is_absolute()
            assert os.access(config.enrich.pi_bin, os.X_OK)
        elif config.enrich.runner == "codex":
            assert Path(config.enrich.codex_bin).is_absolute()


def test_summarize_codex_builds_command_and_reads_output(monkeypatch) -> None:
    import subprocess as sp
    from pathlib import Path as P

    from jobwatch.enrich import _summarize

    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        captured["input"] = kwargs.get("input")
        out_path = P(command[command.index("-o") + 1])
        out_path.write_text("EXPERIENCE: 4 ans\nSALAIRE: 50k\n- Mission IA\n", encoding="utf-8")
        return sp.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("jobwatch.llm_runner.subprocess.run", fake_run)
    config = EnrichConfig(model="gpt-5.6-luna", runner="codex", variant="max")
    result = _summarize(config, "texte de l'offre")

    assert result == ({"experience": "4 ans", "salary": "50k"}, {}, ["Mission IA"])
    command = captured["command"]
    assert command[:2] == ["codex", "exec"]
    assert command[command.index("--model") + 1] == "gpt-5.6-luna"
    assert "model_reasoning_effort=max" in command
    assert "-s" in command and command[command.index("-s") + 1] == "read-only"
    assert "--ignore-user-config" in command
    disabled = {command[index + 1] for index, item in enumerate(command) if item == "--disable"}
    assert disabled == {"shell_tool", "code_mode_host", "apps", "plugins"}
    assert captured["input"] == "texte de l'offre"


def test_summarize_pi_preserves_parsing_and_quote_verification(monkeypatch) -> None:
    from jobwatch.enrich import SUMMARY_PROMPT, _summarize

    captured: dict[str, object] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return (
            "TELETRAVAIL: hybride\n"
            "TELETRAVAIL_CITATION: Télétravail deux jours par semaine.\n"
            "STACK: Python\n"
            "STACK_CITATION: citation inventée\n"
            "- Mission IA\n"
        )

    monkeypatch.setattr("jobwatch.enrich.run_pi", fake_run)
    markdown = "Télétravail deux jours par semaine. Stack Python."
    result = _summarize(
        EnrichConfig(
            model="openai-codex/gpt-5.6-luna",
            runner="pi",
            pi_bin="/usr/bin/pi",
            variant="max",
        ),
        markdown,
    )

    assert result == (
        {"remote": "hybride", "stack": "Python"},
        {"remote": "Télétravail deux jours par semaine."},
        ["Mission IA"],
    )
    assert captured == {
        "binary": "/usr/bin/pi",
        "model": "openai-codex/gpt-5.6-luna",
        "prompt": SUMMARY_PROMPT,
        "attachment": markdown,
        "timeout": 300,
        "thinking": "max",
    }


def test_summarize_opencode_denies_every_tool_by_name(monkeypatch) -> None:
    """opencode.json et OPENCODE_PERMISSION sont les contrats lus par opencode."""
    import json
    import subprocess as sp
    from pathlib import Path as P

    from jobwatch.enrich import OPENCODE_TOOLS, _summarize

    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        captured["file"] = json.loads(
            (P(kwargs["cwd"]) / "opencode.json").read_text(encoding="utf-8")
        )
        captured["env"] = json.loads(kwargs["env"]["OPENCODE_PERMISSION"])
        event = json.dumps({"type": "text", "part": {"text": "EXPERIENCE: 2 ans\n- Puce"}})
        return sp.CompletedProcess(command, 0, stdout=event, stderr="")

    monkeypatch.setattr("jobwatch.llm_runner.subprocess.run", fake_run)
    _summarize(EnrichConfig(opencode_bin="opencode", model="m"), "texte d'offre")

    assert "--pure" in captured["command"]
    expected = {"*": "deny", **{tool: "deny" for tool in OPENCODE_TOOLS}}
    assert captured["file"]["permission"] == expected
    assert captured["env"] == expected


def test_enrich_logs_one_line_per_offer_and_counts_the_run(
    conn: sqlite3.Connection, monkeypatch, caplog
) -> None:
    """Un run doit laisser une trace : ligne par offre en -v, bilan chiffré toujours."""
    import logging as logging_mod

    _seed_offer(conn, url="https://example.com/log-1")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=LONG_HTML)

    monkeypatch.setattr(
        "jobwatch.enrich._summarize",
        lambda config, markdown: (
            {"experience": "3 ans", "salary": FIELD_UNKNOWN},
            {"experience": "Ingénieur IA Paris"},
            ["Poste IA"],
        ),
    )
    with caplog.at_level(logging_mod.INFO, logger="jobwatch.enrich"):
        result = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)

    messages = [record.getMessage() for record in caplog.records]
    assert any("https://example.com/log-1" in message for message in messages)
    assert any("car." in message for message in messages)

    assert sum(result.extract_methods.values()) == 1
    assert result.raw_chars > 0
    assert result.kept_chars > 0
    assert result.quotes_verified == 1
    # 'non précisé' n'est pas un champ sans citation : il n'y a rien à ancrer.
    assert result.quotes_rejected == 0

    line = result.summary_line()
    assert "extraction" in line
    assert "citation(s) vérifiée(s)" in line
