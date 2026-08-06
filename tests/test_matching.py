"""Tests for offer matching against searches."""

from __future__ import annotations

import json
import sqlite3

import pytest

from jobwatch.config import SearchConfig
from jobwatch.db import connect, init_db
from jobwatch.matching import offer_matches_search, run_matching, sync_searches

MATCH_QUERY = "SELECT * FROM match WHERE search_id = ? AND offer_id = ?"


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = connect(":memory:")
    init_db(connection)
    yield connection
    connection.close()


def _insert_search(
    conn: sqlite3.Connection,
    name: str,
    include: list[str],
    exclude: list[str] | None = None,
    locations: list[str] | None = None,
    contract: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO search (name, include_json, exclude_json, locations_json, contract) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            name,
            json.dumps(include),
            json.dumps(exclude or []),
            json.dumps(locations or []),
            contract,
        ),
    )
    return int(cur.lastrowid)


def _company_id(conn: sqlite3.Connection, name: str) -> int:
    conn.execute("INSERT OR IGNORE INTO company (name) VALUES (?)", (name,))
    return int(conn.execute("SELECT id FROM company WHERE name = ?", (name,)).fetchone()["id"])


def _insert_offer(
    conn: sqlite3.Connection,
    title: str,
    url: str,
    location: str | None = "Paris",
    contract: str | None = "permanent",
    collected_at: str | None = None,
) -> int:
    conn.execute("INSERT OR IGNORE INTO source (type, name) VALUES ('test', 'test')")
    source_id = int(conn.execute("SELECT id FROM source WHERE name = 'test'").fetchone()["id"])
    company_id = _company_id(conn, "Acme")
    cur = conn.execute(
        "INSERT INTO offer "
        "(source_id, company_id, title, url, platform, location, contract, collected_at) "
        "VALUES (?, ?, ?, ?, 'Test', ?, ?, COALESCE(?, datetime('now')))",
        (source_id, company_id, title, url, location, contract, collected_at),
    )
    return int(cur.lastrowid)


def test_sync_searches_inserts_new(conn: sqlite3.Connection) -> None:
    sync_searches(conn, [SearchConfig(name="s1", include=["AI"])])
    row = conn.execute("SELECT * FROM search WHERE name = 's1'").fetchone()
    assert row is not None
    assert row["active"] == 1
    assert json.loads(row["include_json"]) == ["AI"]


def test_sync_searches_updates_changed(conn: sqlite3.Connection) -> None:
    sync_searches(conn, [SearchConfig(name="s1", include=["AI"])])
    sync_searches(conn, [SearchConfig(name="s1", include=["AI", "LLM"], contract="permanent")])
    row = conn.execute("SELECT * FROM search WHERE name = 's1'").fetchone()
    assert json.loads(row["include_json"]) == ["AI", "LLM"]
    assert row["contract"] == "permanent"


def test_sync_searches_reactivates(conn: sqlite3.Connection) -> None:
    sync_searches(conn, [SearchConfig(name="s1", include=["AI"])])
    conn.execute("UPDATE search SET active = 0 WHERE name = 's1'")
    conn.commit()
    sync_searches(conn, [SearchConfig(name="s1", include=["AI"])])
    row = conn.execute("SELECT * FROM search WHERE name = 's1'").fetchone()
    assert row["active"] == 1


def test_sync_searches_deactivates_removed(conn: sqlite3.Connection) -> None:
    sync_searches(
        conn, [SearchConfig(name="s1", include=["AI"]), SearchConfig(name="s2", include=["X"])]
    )
    sync_searches(conn, [SearchConfig(name="s1", include=["AI"])])
    active = [
        str(r["name"]) for r in conn.execute("SELECT name FROM search WHERE active = 1").fetchall()
    ]
    assert active == ["s1"]


def _run_matching_for(conn: sqlite3.Connection, search_id: int, offer_id: int) -> bool:
    run_matching(conn)
    return conn.execute(MATCH_QUERY, (search_id, offer_id)).fetchone() is not None


def test_match_on_include_keyword_case_insensitive(conn: sqlite3.Connection) -> None:
    search_id = _insert_search(conn, "s", ["machine learning"])
    offer_id = _insert_offer(conn, "Senior Machine Learning Engineer", "https://a/1")
    assert _run_matching_for(conn, search_id, offer_id)


def test_match_on_any_include_keyword(conn: sqlite3.Connection) -> None:
    search_id = _insert_search(conn, "s", ["AI engineer", "LLM"])
    offer_id = _insert_offer(conn, "LLM Platform Engineer", "https://a/1")
    assert _run_matching_for(conn, search_id, offer_id)


def test_no_match_when_include_absent(conn: sqlite3.Connection) -> None:
    search_id = _insert_search(conn, "s", ["machine learning"])
    offer_id = _insert_offer(conn, "Frontend Developer", "https://a/1")
    assert not _run_matching_for(conn, search_id, offer_id)


def test_no_match_when_exclude_present(conn: sqlite3.Connection) -> None:
    search_id = _insert_search(conn, "s", ["AI"], exclude=["stage", "internship"])
    offer_id = _insert_offer(conn, "AI Engineer Stage", "https://a/1")
    assert not _run_matching_for(conn, search_id, offer_id)


def test_location_substring_match(conn: sqlite3.Connection) -> None:
    search_id = _insert_search(conn, "s", ["AI"], locations=["Île-de-France", "Paris"])
    offer_id = _insert_offer(conn, "AI Engineer", "https://a/1", location="Paris 11e")
    assert _run_matching_for(conn, search_id, offer_id)


def test_no_match_when_location_outside(conn: sqlite3.Connection) -> None:
    search_id = _insert_search(conn, "s", ["AI"], locations=["Paris"])
    offer_id = _insert_offer(conn, "AI Engineer", "https://a/1", location="Lyon")
    assert not _run_matching_for(conn, search_id, offer_id)


def test_match_when_offer_location_missing(conn: sqlite3.Connection) -> None:
    search_id = _insert_search(conn, "s", ["AI"], locations=["Paris"])
    offer_id = _insert_offer(conn, "AI Engineer", "https://a/1", location=None)
    assert _run_matching_for(conn, search_id, offer_id)


def test_match_when_locations_empty(conn: sqlite3.Connection) -> None:
    search_id = _insert_search(conn, "s", ["AI"])
    offer_id = _insert_offer(conn, "AI Engineer", "https://a/1", location="Somewhere")
    assert _run_matching_for(conn, search_id, offer_id)


def test_contract_filter_blocks_mismatch(conn: sqlite3.Connection) -> None:
    search_id = _insert_search(conn, "s", ["AI"], contract="permanent")
    offer_id = _insert_offer(conn, "AI Engineer", "https://a/1", contract="fixed_term")
    assert not _run_matching_for(conn, search_id, offer_id)


def test_contract_filter_allows_match(conn: sqlite3.Connection) -> None:
    search_id = _insert_search(conn, "s", ["AI"], contract="permanent")
    offer_id = _insert_offer(conn, "AI Engineer", "https://a/1", contract="permanent")
    assert _run_matching_for(conn, search_id, offer_id)


def test_offer_contract_missing_matches_any_search_contract(conn: sqlite3.Connection) -> None:
    search_id = _insert_search(conn, "s", ["AI"], contract="permanent")
    offer_id = _insert_offer(conn, "AI Engineer", "https://a/1", contract=None)
    assert _run_matching_for(conn, search_id, offer_id)


def test_contract_null_matches_any(conn: sqlite3.Connection) -> None:
    search_id = _insert_search(conn, "s", ["AI"])
    offer_id = _insert_offer(conn, "AI Engineer", "https://a/1", contract="fixed_term")
    assert _run_matching_for(conn, search_id, offer_id)


def test_old_offers_are_not_matched(conn: sqlite3.Connection) -> None:
    search_id = _insert_search(conn, "s", ["AI"])
    offer_id = _insert_offer(conn, "AI Engineer", "https://a/1", collected_at="2020-01-01 00:00:00")
    assert not _run_matching_for(conn, search_id, offer_id)


def test_run_matching_is_idempotent(conn: sqlite3.Connection) -> None:
    _insert_search(conn, "s", ["AI"])
    _insert_offer(conn, "AI Engineer", "https://a/1")
    run_matching(conn)
    run_matching(conn)
    count = conn.execute("SELECT count(*) FROM match").fetchone()[0]
    assert count == 1


def test_inactive_search_does_not_match(conn: sqlite3.Connection) -> None:
    search_id = _insert_search(conn, "s", ["AI"])
    conn.execute("UPDATE search SET active = 0 WHERE id = ?", (search_id,))
    conn.commit()
    offer_id = _insert_offer(conn, "AI Engineer", "https://a/1")
    assert not _run_matching_for(conn, search_id, offer_id)


def test_offer_matches_search_with_contract_none(conn: sqlite3.Connection) -> None:
    search = conn.execute(
        "SELECT * FROM search WHERE id = ?", (_insert_search(conn, "s", ["AI"]),)
    ).fetchone()
    offer = conn.execute(
        "SELECT * FROM offer WHERE id = ?",
        (_insert_offer(conn, "AI Engineer", "https://a/1"),),
    ).fetchone()
    assert offer_matches_search(offer, search)
