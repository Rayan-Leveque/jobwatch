"""Tests for jobwatch.db and the shared offer-storage logic."""

from __future__ import annotations

import sqlite3

import pytest

from jobwatch.collectors.base import RawOffer, store_offers
from jobwatch.db import connect, init_db


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = connect(":memory:")
    init_db(connection)
    yield connection
    connection.close()


EXPECTED_TABLES = {
    "source",
    "company",
    "offer",
    "search",
    "match",
    "application",
    "event",
    "document",
}


def test_init_db_creates_all_tables(conn: sqlite3.Connection) -> None:
    tables = {
        str(row["name"])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert EXPECTED_TABLES <= tables


def test_foreign_keys_are_enforced(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO offer (source_id, title, url) VALUES (999, 'x', 'y')")


def test_source_name_is_unique(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO source (type, name) VALUES ('test', 's1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO source (type, name) VALUES ('test', 's1')")


def _offer(title: str, url: str, company: str, **kwargs) -> RawOffer:
    return RawOffer(title=title, url=url, company=company, platform="Test", **kwargs)


def test_store_offers_inserts_source_and_offer(conn: sqlite3.Connection) -> None:
    new_ids = store_offers(conn, "test-source", "test", [_offer("Engineer", "https://a/1", "Acme")])
    assert len(new_ids) == 1

    offer = conn.execute("SELECT * FROM offer WHERE id = ?", (new_ids[0],)).fetchone()
    assert offer["title"] == "Engineer"
    assert offer["url"] == "https://a/1"
    assert offer["platform"] == "Test"

    source = conn.execute("SELECT * FROM source WHERE name = 'test-source'").fetchone()
    assert source["type"] == "test"
    assert source["last_run_at"] is not None

    company = conn.execute("SELECT * FROM company WHERE name = 'Acme'").fetchone()
    assert company is not None
    assert offer["company_id"] == company["id"]


def test_store_offers_dedupes_by_url(conn: sqlite3.Connection) -> None:
    store_offers(conn, "s", "test", [_offer("Engineer", "https://a/1", "Acme")])
    second = store_offers(
        conn, "s", "test", [_offer("Engineer (new title)", "https://a/1", "Acme")]
    )
    assert len(second) == 0
    count = conn.execute("SELECT count(*) FROM offer").fetchone()[0]
    assert count == 1


def test_store_offers_dedupes_by_company_and_title(conn: sqlite3.Connection) -> None:
    first = store_offers(
        conn, "s", "test", [_offer("Machine Learning Engineer", "https://a/1", "Acme")]
    )
    second = store_offers(
        conn, "s", "test", [_offer("machine learning engineer", "https://a/2", "Acme")]
    )
    assert len(first) == 1
    assert len(second) == 0
    count = conn.execute("SELECT count(*) FROM offer").fetchone()[0]
    assert count == 1


def test_store_offers_keeps_same_title_different_company(conn: sqlite3.Connection) -> None:
    first = store_offers(conn, "s", "test", [_offer("Engineer", "https://a/1", "Acme")])
    second = store_offers(conn, "s", "test", [_offer("Engineer", "https://a/2", "Other")])
    assert len(first) == 1
    assert len(second) == 1
    count = conn.execute("SELECT count(*) FROM offer").fetchone()[0]
    assert count == 2


def test_store_offers_skips_network_duplicates_within_one_batch(
    conn: sqlite3.Connection,
) -> None:
    offers = [_offer("Engineer", "https://a/1", "Acme"), _offer("Engineer", "https://a/2", "Acme")]
    new_ids = store_offers(conn, "s", "test", offers)
    assert len(new_ids) == 1
