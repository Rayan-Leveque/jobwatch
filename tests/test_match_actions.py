"""Tests directs de jobwatch.match_actions : aucune couche HTTP requise."""

from __future__ import annotations

import sqlite3

import pytest

from jobwatch.db import connect, init_db
from jobwatch.match_actions import (
    MatchActionRequest,
    MatchActionResult,
    apply_match_action,
    parse_match_action,
)


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = connect(":memory:")
    init_db(connection)
    yield connection
    connection.close()


def _seed_match(conn: sqlite3.Connection, state: str = "new") -> int:
    conn.execute("INSERT INTO source (type, name) VALUES ('test', 'test')")
    source_id = conn.execute("SELECT id FROM source WHERE name = 'test'").fetchone()["id"]
    conn.execute("INSERT INTO company (name) VALUES ('Acme')")
    company_id = conn.execute("SELECT id FROM company WHERE name = 'Acme'").fetchone()["id"]
    conn.execute(
        "INSERT INTO offer (source_id, company_id, title, url, collected_at) "
        "VALUES (?, ?, 'AI Engineer', 'https://example.com/offre', '2026-08-01 10:00:00')",
        (source_id, company_id),
    )
    offer_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO search (name, include_json, exclude_json, locations_json, active) "
        "VALUES ('ai-paris', '[]', '[]', '[]', 1)"
    )
    search_id = conn.execute("SELECT id FROM search WHERE name = 'ai-paris'").fetchone()["id"]
    conn.execute(
        "INSERT INTO match (search_id, offer_id, state) VALUES (?, ?, ?)",
        (search_id, offer_id, state),
    )
    match_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    return int(match_id)


def _seed_library(conn: sqlite3.Connection, doc_type: str, path: str) -> int:
    cur = conn.execute(
        "INSERT INTO document_library (type, label, file_path) VALUES (?, ?, ?)",
        (doc_type, f"Doc {doc_type}", path),
    )
    conn.commit()
    return int(cur.lastrowid)


def _match_row(conn: sqlite3.Connection, match_id: int) -> sqlite3.Row:
    return conn.execute(
        "SELECT state, discarded_at FROM match WHERE id = ?", (match_id,)
    ).fetchone()


def test_parse_later_and_discard_ignore_the_body(conn: sqlite3.Connection) -> None:
    for action in ("later", "discard"):
        request = parse_match_action(action, None)
        assert request == MatchActionRequest(action)


def test_parse_restore_accepts_each_valid_state() -> None:
    for state in ("new", "seen", "later"):
        request = parse_match_action("restore", {"state": state})
        assert request == MatchActionRequest("restore", target_state=state)


def test_parse_restore_rejects_invalid_state() -> None:
    for body in (None, {}, {"state": "applied"}, {"state": 3}, "later"):
        result = parse_match_action("restore", body)
        assert result == MatchActionResult("invalid", "état de restauration invalide")


def test_parse_apply_accepts_missing_or_integer_library_ids() -> None:
    assert parse_match_action("apply", None) == MatchActionRequest("apply")
    assert parse_match_action("apply", {"cv_library_id": ""}) == MatchActionRequest("apply")
    request = parse_match_action(
        "apply", {"cv_library_id": 3, "cover_letter_library_id": 7}
    )
    assert request == MatchActionRequest("apply", cv_library_id=3, cover_letter_library_id=7)


def test_parse_apply_rejects_non_integer_library_ids() -> None:
    for body, field in (
        ({"cv_library_id": "3"}, "cv_library_id"),
        ({"cv_library_id": True}, "cv_library_id"),
        ({"cover_letter_library_id": [1]}, "cover_letter_library_id"),
    ):
        result = parse_match_action("apply", body)
        assert result == MatchActionResult("invalid", f"champ {field} invalide")


def test_later_updates_state_and_clears_discarded_at(conn: sqlite3.Connection) -> None:
    match_id = _seed_match(conn, state="discarded")
    conn.execute(
        "UPDATE match SET discarded_at = datetime('now') WHERE id = ?", (match_id,)
    )
    conn.commit()

    result = apply_match_action(conn, match_id, MatchActionRequest("later"))

    assert result == MatchActionResult("ok")
    row = _match_row(conn, match_id)
    assert row["state"] == "later"
    assert row["discarded_at"] is None


def test_discard_sets_state_and_discarded_at(conn: sqlite3.Connection) -> None:
    match_id = _seed_match(conn)

    result = apply_match_action(conn, match_id, MatchActionRequest("discard"))

    assert result == MatchActionResult("ok")
    row = _match_row(conn, match_id)
    assert row["state"] == "discarded"
    assert row["discarded_at"] is not None


def test_restore_applies_the_target_state(conn: sqlite3.Connection) -> None:
    match_id = _seed_match(conn, state="discarded")

    result = apply_match_action(
        conn, match_id, MatchActionRequest("restore", target_state="seen")
    )

    assert result == MatchActionResult("ok")
    row = _match_row(conn, match_id)
    assert row["state"] == "seen"
    assert row["discarded_at"] is None


def test_unknown_match_returns_not_found(conn: sqlite3.Connection) -> None:
    result = apply_match_action(conn, 999, MatchActionRequest("later"))

    assert result == MatchActionResult("not_found")


def test_apply_records_application_and_documents(conn: sqlite3.Connection) -> None:
    match_id = _seed_match(conn)
    cv_id = _seed_library(conn, "cv", "/tmp/cv.pdf")
    letter_id = _seed_library(conn, "cover_letter", "/tmp/lm.pdf")

    result = apply_match_action(
        conn,
        match_id,
        MatchActionRequest("apply", cv_library_id=cv_id, cover_letter_library_id=letter_id),
    )

    assert result == MatchActionResult("ok")
    assert _match_row(conn, match_id)["state"] == "applied"
    application = conn.execute(
        "SELECT id FROM application WHERE match_id = ?", (match_id,)
    ).fetchone()
    assert application is not None
    documents = conn.execute(
        "SELECT type, path FROM document WHERE application_id = ? ORDER BY type",
        (application["id"],),
    ).fetchall()
    assert [(d["type"], d["path"]) for d in documents] == [
        ("cover_letter", "/tmp/lm.pdf"),
        ("cv", "/tmp/cv.pdf"),
    ]


def test_apply_on_discarded_match_is_a_conflict(conn: sqlite3.Connection) -> None:
    match_id = _seed_match(conn, state="discarded")

    result = apply_match_action(conn, match_id, MatchActionRequest("apply"))

    assert result == MatchActionResult("conflict", "match écarté : restaurez-le d'abord")
    assert _match_row(conn, match_id)["state"] == "discarded"


def test_apply_with_unknown_library_id_records_no_document(
    conn: sqlite3.Connection,
) -> None:
    match_id = _seed_match(conn)

    result = apply_match_action(
        conn, match_id, MatchActionRequest("apply", cv_library_id=999)
    )

    assert result == MatchActionResult("ok")
    assert _match_row(conn, match_id)["state"] == "applied"
    assert conn.execute("SELECT count(*) FROM document").fetchone()[0] == 0


def test_apply_twice_surfaces_the_application_error(conn: sqlite3.Connection) -> None:
    match_id = _seed_match(conn)
    assert apply_match_action(conn, match_id, MatchActionRequest("apply")).kind == "ok"

    result = apply_match_action(conn, match_id, MatchActionRequest("apply"))

    assert result.kind == "conflict"
    assert result.error
    assert conn.execute("SELECT count(*) FROM application").fetchone()[0] == 1
