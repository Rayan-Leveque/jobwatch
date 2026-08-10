"""Tests du cœur d'authentification sans serveur HTTP."""

from __future__ import annotations

import datetime
import sqlite3

import pytest

from jobwatch.auth import (
    AuthError,
    accept_invite,
    auth_required,
    clear_login_failures,
    create_invite,
    create_session,
    delete_session,
    hash_password,
    invite_status,
    login_allowed,
    login_throttle_key,
    record_login_failure,
    resolve_session,
    validate_password,
    verify_password,
)
from jobwatch.db import connect, init_db

NOW = datetime.datetime(2026, 8, 9, 12, tzinfo=datetime.UTC)
PASSWORD = "une longue phrase secrète"


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = connect(":memory:")
    init_db(connection)
    yield connection
    connection.close()


def test_password_hash_uses_unique_salt_and_verifies() -> None:
    first = hash_password(PASSWORD)
    second = hash_password(PASSWORD)
    assert first.startswith("scrypt$131072$8$1$")
    assert first != second
    assert verify_password(PASSWORD, first)
    assert not verify_password("phrase secrète incorrecte", first)
    assert not verify_password(PASSWORD, "hash-corrompu")


def test_password_policy_is_length_only_and_accepts_unicode() -> None:
    assert validate_password(PASSWORD) == PASSWORD
    assert validate_password("mot de passe avec espaces")
    with pytest.raises(AuthError, match="8"):
        validate_password("court")


def test_invite_creates_owner_and_is_one_time(conn: sqlite3.Connection) -> None:
    token = create_invite(conn, "alice", "Alice@Example.com", now=NOW)
    assert auth_required(conn)
    assert len(token) >= 40

    account_id = accept_invite(conn, token, PASSWORD, now=NOW)
    account = conn.execute("SELECT * FROM account WHERE id = ?", (account_id,)).fetchone()
    assert account["email"] == "alice@example.com"
    assert account["password_hash"] != PASSWORD
    membership = conn.execute("SELECT role FROM membership").fetchone()
    assert membership["role"] == "owner"
    with pytest.raises(AuthError, match="invalide|utilisée"):
        accept_invite(conn, token, PASSWORD, now=NOW)


def test_expired_invite_is_rejected(conn: sqlite3.Connection) -> None:
    token = create_invite(conn, "alice", "alice@example.com", now=NOW)
    later = NOW + datetime.timedelta(hours=49)
    with pytest.raises(AuthError, match="expirée"):
        accept_invite(conn, token, PASSWORD, now=later)


def test_invite_cannot_be_consumed_by_another_workspace(conn: sqlite3.Connection) -> None:
    token = create_invite(conn, "alice", "alice@example.com", now=NOW)
    with pytest.raises(AuthError, match="invalide"):
        accept_invite(conn, token, PASSWORD, workspace_slug="bob", now=NOW)
    assert accept_invite(conn, token, PASSWORD, workspace_slug="alice", now=NOW) > 0


def test_new_invite_expires_previous_for_same_email(conn: sqlite3.Connection) -> None:
    first = create_invite(conn, "alice", "alice@example.com", now=NOW)
    second = create_invite(conn, "alice", "alice@example.com", now=NOW)
    with pytest.raises(AuthError):
        accept_invite(conn, first, PASSWORD, now=NOW)
    assert accept_invite(conn, second, PASSWORD, now=NOW) > 0


def test_session_is_opaque_scoped_and_revocable(conn: sqlite3.Connection) -> None:
    invite = create_invite(conn, "alice", "alice@example.com", now=NOW)
    account_id = accept_invite(conn, invite, PASSWORD, now=NOW)
    token, created = create_session(conn, "alice@example.com", PASSWORD, "alice", now=NOW)
    assert created.account_id == account_id
    assert created.role == "owner"
    assert token not in {
        row["token_hash"] for row in conn.execute("SELECT token_hash FROM web_session")
    }

    resolved = resolve_session(conn, token, now=NOW)
    assert resolved == created
    delete_session(conn, token)
    assert resolve_session(conn, token, now=NOW) is None


def test_session_rejects_wrong_credentials_and_expiration(conn: sqlite3.Connection) -> None:
    invite = create_invite(conn, "alice", "alice@example.com", now=NOW)
    accept_invite(conn, invite, PASSWORD, now=NOW)
    with pytest.raises(AuthError, match="identifiants invalides"):
        create_session(conn, "alice@example.com", "mauvaise phrase secrète", "alice", now=NOW)
    token, _ = create_session(conn, "alice@example.com", PASSWORD, "alice", now=NOW)
    assert resolve_session(conn, token, now=NOW + datetime.timedelta(hours=25)) is None


def test_login_throttle_blocks_fifth_failure_and_can_be_cleared(
    conn: sqlite3.Connection,
) -> None:
    key = login_throttle_key("Alice@Example.com", "100.64.0.2")
    assert "alice@example.com" not in key
    for _ in range(4):
        record_login_failure(conn, key, now=NOW)
        assert login_allowed(conn, key, now=NOW)
    record_login_failure(conn, key, now=NOW)
    assert not login_allowed(conn, key, now=NOW)
    assert login_allowed(conn, key, now=NOW + datetime.timedelta(minutes=16))
    clear_login_failures(conn, key)
    assert login_allowed(conn, key, now=NOW)


def test_login_throttle_resets_old_window(conn: sqlite3.Connection) -> None:
    key = login_throttle_key("alice@example.com", "100.64.0.2")
    for _ in range(4):
        record_login_failure(conn, key, now=NOW)
    record_login_failure(conn, key, now=NOW + datetime.timedelta(minutes=16))
    row = conn.execute(
        "SELECT failures, blocked_until FROM login_throttle WHERE key_hash = ?", (key,)
    ).fetchone()
    assert (row["failures"], row["blocked_until"]) == (1, None)


def test_invite_refuses_a_second_owner_email(tmp_path) -> None:
    conn = connect(":memory:")
    init_db(conn)
    token = create_invite(conn, "alice", "alice@example.com")
    accept_invite(conn, token, "une très longue phrase secrète", workspace_slug="alice")

    with pytest.raises(AuthError, match="alice@example.com"):
        create_invite(conn, "alice", "bob@example.com")

    assert conn.execute("SELECT count(*) FROM account").fetchone()[0] == 1
    assert create_invite(conn, "alice", "ALICE@example.com")
    conn.close()


def test_new_invite_replaces_a_pending_one_sent_to_a_typo(tmp_path) -> None:
    conn = connect(":memory:")
    init_db(conn)
    mistyped = create_invite(conn, "alice", "alic@example.com")

    token = create_invite(conn, "alice", "alice@example.com")

    assert invite_status(conn, mistyped, "alice") == "expired"
    assert invite_status(conn, token, "alice") == "valid"
    account_id = accept_invite(
        conn, token, "une très longue phrase secrète", workspace_slug="alice"
    )
    assert conn.execute(
        "SELECT email FROM account WHERE id = ?", (account_id,)
    ).fetchone()[0] == "alice@example.com"
    conn.close()
