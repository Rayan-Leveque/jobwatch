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
    "offer_summary",
    "summary_bullet",
    "search",
    "match",
    "application",
    "event",
    "document",
    "bug_report",
}


def test_init_db_creates_all_tables(conn: sqlite3.Connection) -> None:
    tables = {
        str(row["name"])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert EXPECTED_TABLES <= tables


def test_init_db_creates_v03_columns(conn: sqlite3.Connection) -> None:
    offer_cols = {row["name"] for row in conn.execute("PRAGMA table_info(offer)")}
    match_cols = {row["name"] for row in conn.execute("PRAGMA table_info(match)")}
    assert "deadline" in offer_cols
    assert "fit" in match_cols


def test_init_db_creates_v04_discarded_at_column(conn: sqlite3.Connection) -> None:
    match_cols = {row["name"] for row in conn.execute("PRAGMA table_info(match)")}
    assert "discarded_at" in match_cols
    assert "fit" in match_cols


def test_init_db_migrates_historical_offer_content_failure_metadata() -> None:
    conn = connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE offer_content (
          id INTEGER PRIMARY KEY,
          offer_id INTEGER NOT NULL UNIQUE,
          markdown TEXT,
          fetch_method TEXT,
          extract_method TEXT,
          html_gz BLOB,
          status TEXT NOT NULL,
          fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO offer_content (offer_id, status) VALUES (42, 'failed');
        """
    )

    init_db(conn)

    migrated = conn.execute(
        "SELECT status, fetch_attempts, failure_reason FROM offer_content WHERE offer_id = 42"
    ).fetchone()
    assert tuple(migrated) == ("failed", 1, None)
    init_db(conn)
    assert conn.execute(
        "SELECT fetch_attempts FROM offer_content WHERE offer_id = 42"
    ).fetchone()["fetch_attempts"] == 1
    conn.close()


V02_SCHEMA = """
CREATE TABLE source (
  id INTEGER PRIMARY KEY,
  type TEXT NOT NULL,
  name TEXT NOT NULL UNIQUE,
  last_run_at TEXT
);
CREATE TABLE company (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE
);
CREATE TABLE offer (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES source(id),
  company_id INTEGER REFERENCES company(id),
  title TEXT NOT NULL,
  url TEXT NOT NULL UNIQUE,
  platform TEXT,
  location TEXT,
  contract TEXT,
  published_at TEXT,
  collected_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE search (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  include_json TEXT NOT NULL,
  exclude_json TEXT NOT NULL,
  locations_json TEXT NOT NULL,
  contract TEXT,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE match (
  id INTEGER PRIMARY KEY,
  search_id INTEGER NOT NULL REFERENCES search(id),
  offer_id INTEGER NOT NULL REFERENCES offer(id),
  state TEXT NOT NULL DEFAULT 'new',
  notified_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(search_id, offer_id)
);
CREATE TABLE application (
  id INTEGER PRIMARY KEY,
  match_id INTEGER REFERENCES match(id),
  offer_id INTEGER NOT NULL REFERENCES offer(id),
  note TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE event (
  id INTEGER PRIMARY KEY,
  application_id INTEGER NOT NULL REFERENCES application(id),
  type TEXT NOT NULL,
  at TEXT NOT NULL DEFAULT (datetime('now')),
  comment TEXT
);
CREATE TABLE document (
  id INTEGER PRIMARY KEY,
  application_id INTEGER NOT NULL REFERENCES application(id),
  type TEXT NOT NULL,
  path TEXT NOT NULL,
  sent_at TEXT
);
"""


def test_init_db_migrates_v02_database_without_data_loss() -> None:
    conn = connect(":memory:")
    conn.executescript(V02_SCHEMA)
    conn.execute("INSERT INTO source (type, name) VALUES ('test', 's1')")
    conn.execute(
        "INSERT INTO offer (source_id, title, url) VALUES (1, 'Engineer', 'https://a/1')"
    )
    conn.execute(
        "INSERT INTO search (name, include_json, exclude_json, locations_json) VALUES ('dev', '[]', '[]', '[]')"
    )
    conn.execute("INSERT INTO match (search_id, offer_id, state) VALUES (1, 1, 'seen')")
    conn.commit()

    init_db(conn)

    offer = conn.execute("SELECT * FROM offer WHERE id = 1").fetchone()
    match = conn.execute("SELECT * FROM match WHERE id = 1").fetchone()
    assert offer["title"] == "Engineer"
    assert offer["url"] == "https://a/1"
    assert offer["deadline"] is None
    assert match["search_id"] == 1
    assert match["state"] == "seen"
    assert match["fit"] is None

    offer_cols = {row["name"] for row in conn.execute("PRAGMA table_info(offer)")}
    match_cols = {row["name"] for row in conn.execute("PRAGMA table_info(match)")}
    assert "deadline" in offer_cols
    assert "fit" in match_cols
    assert conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'table' "
        "AND name IN ('offer_summary', 'summary_bullet')"
    ).fetchone()[0] == 2
    conn.close()


def test_init_db_twice_is_idempotent(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO source (type, name) VALUES ('test', 's1')")
    conn.execute(
        "INSERT INTO offer (source_id, title, url) VALUES (1, 'Engineer', 'https://a/1')"
    )
    conn.execute(
        "INSERT INTO search (name, include_json, exclude_json, locations_json) VALUES ('dev', '[]', '[]', '[]')"
    )
    conn.execute("INSERT INTO match (search_id, offer_id) VALUES (1, 1)")
    conn.commit()

    init_db(conn)
    init_db(conn)

    count = conn.execute("SELECT count(*) FROM offer").fetchone()[0]
    assert count == 1
    offer = conn.execute("SELECT * FROM offer WHERE id = 1").fetchone()
    assert offer["deadline"] is None
    match = conn.execute("SELECT * FROM match WHERE id = 1").fetchone()
    assert match["fit"] is None
    assert conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'table' "
        "AND name IN ('offer_summary', 'summary_bullet')"
    ).fetchone()[0] == 2


def test_init_db_migrates_v03_database_repeatably() -> None:
    conn = connect(":memory:")
    conn.executescript(V02_SCHEMA)
    conn.execute("ALTER TABLE offer ADD COLUMN deadline TEXT")
    conn.execute("ALTER TABLE match ADD COLUMN fit TEXT")
    conn.execute("INSERT INTO source (type, name) VALUES ('test', 's1')")
    conn.execute(
        "INSERT INTO offer (source_id, title, url, deadline) "
        "VALUES (1, 'Engineer', 'https://a/1', '2026-09-01')"
    )
    conn.commit()

    init_db(conn)
    init_db(conn)

    assert conn.execute("SELECT deadline FROM offer WHERE id = 1").fetchone()[0] == "2026-09-01"
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"offer_summary", "summary_bullet"} <= tables
    conn.close()


def test_init_db_migrates_v03_database_to_discarded_at_without_data_loss() -> None:
    """Deux migrations distinctes de la même table (fit, discarded_at) doivent coexister."""
    conn = connect(":memory:")
    conn.executescript(V02_SCHEMA)
    conn.execute("ALTER TABLE offer ADD COLUMN deadline TEXT")
    conn.execute("ALTER TABLE match ADD COLUMN fit TEXT")
    conn.execute("INSERT INTO source (type, name) VALUES ('test', 's1')")
    conn.execute(
        "INSERT INTO offer (source_id, title, url) VALUES (1, 'Engineer', 'https://a/1')"
    )
    conn.execute(
        "INSERT INTO search (name, include_json, exclude_json, locations_json) "
        "VALUES ('dev', '[]', '[]', '[]')"
    )
    conn.execute(
        "INSERT INTO match (search_id, offer_id, state, fit) VALUES (1, 1, 'discarded', 'high')"
    )
    conn.commit()

    init_db(conn)
    init_db(conn)

    match = conn.execute("SELECT * FROM match WHERE id = 1").fetchone()
    assert match["state"] == "discarded"
    assert match["fit"] == "high"
    assert match["discarded_at"] is None
    match_cols = {row["name"] for row in conn.execute("PRAGMA table_info(match)")}
    assert {"fit", "discarded_at"} <= match_cols
    conn.close()


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
