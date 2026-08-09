"""Comptes, invitations et sessions opaques stockées dans SQLite."""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import secrets
import sqlite3
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 1024
SCRYPT_N = 2**17
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 256 * 1024 * 1024
INVITE_HOURS = 48
SESSION_HOURS = 24
AUTH_REQUIRED_KEY = "auth_required"
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_MINUTES = 15
LOGIN_BLOCK_MINUTES = 15


class AuthError(Exception):
    """Échec d'authentification attendu, sans détail sensible."""


@dataclass(frozen=True)
class Session:
    account_id: int
    workspace_id: int
    email: str
    role: str
    csrf_token: str
    expires_at: str


def _now(now: datetime.datetime | None = None) -> datetime.datetime:
    value = now or datetime.datetime.now(datetime.UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC)


def _timestamp(value: datetime.datetime) -> str:
    return value.astimezone(datetime.UTC).isoformat()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def validate_password(password: str) -> str:
    """Applique une politique de longueur sans règle de composition."""
    if not isinstance(password, str):
        raise AuthError("mot de passe invalide")
    normalized = unicodedata.normalize("NFC", password)
    if len(normalized) < PASSWORD_MIN_LENGTH:
        raise AuthError(f"le mot de passe doit contenir au moins {PASSWORD_MIN_LENGTH} caractères")
    if len(normalized) > PASSWORD_MAX_LENGTH:
        raise AuthError(f"le mot de passe dépasse {PASSWORD_MAX_LENGTH} caractères")
    return normalized


def hash_password(password: str, salt: bytes | None = None) -> str:
    """Produit un hash scrypt auto-descriptif avec sel aléatoire."""
    normalized = validate_password(password)
    actual_salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        normalized.encode("utf-8"),
        salt=actual_salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=SCRYPT_MAXMEM,
        dklen=SCRYPT_DKLEN,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(actual_salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    """Vérifie un mot de passe sans lever sur un hash corrompu."""
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        normalized = unicodedata.normalize("NFC", password)
        actual = hashlib.scrypt(
            normalized.encode("utf-8"),
            salt=_unb64(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=SCRYPT_MAXMEM,
            dklen=len(_unb64(expected)),
        )
        return hmac.compare_digest(actual, _unb64(expected))
    except (TypeError, ValueError, UnicodeError):
        return False


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    """Hash factice pour éviter de révéler l'existence d'un compte par le temps de réponse."""
    return hash_password("mot de passe factice assez long", salt=b"jobwatch-auth-00")


def normalize_email(email: str) -> str:
    value = email.strip().casefold()
    if not value or "@" not in value or value.startswith("@") or value.endswith("@"):
        raise AuthError("adresse email invalide")
    return value


def ensure_workspace(conn: sqlite3.Connection, slug: str, name: str | None = None) -> int:
    """Crée ou retrouve l'espace de l'instance."""
    conn.execute(
        "INSERT OR IGNORE INTO workspace (slug, name) VALUES (?, ?)",
        (slug, (name or slug).strip() or slug),
    )
    row = conn.execute("SELECT id FROM workspace WHERE slug = ?", (slug,)).fetchone()
    return int(row["id"])


def auth_required(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT value FROM instance_setting WHERE key = ?", (AUTH_REQUIRED_KEY,)
    ).fetchone()
    return row is not None and row["value"] == "1"


def _refuse_second_owner(
    conn: sqlite3.Connection,
    workspace_id: int,
    email: str,
    now: datetime.datetime,
) -> None:
    """Interdit un second compte sur l'instance.

    Offres, documents et candidatures sont communs à l'instance : tant qu'ils ne
    portent pas de propriétaire, un deuxième compte lirait les CV, les lettres et
    le tri du premier. Une instance par personne reste la règle.
    """
    other = conn.execute(
        "SELECT a.email FROM membership m JOIN account a ON a.id = m.account_id "
        "WHERE m.workspace_id = ? AND a.email <> ? LIMIT 1",
        (workspace_id, email),
    ).fetchone()
    if other is None:
        other = conn.execute(
            "SELECT email FROM account_invite WHERE workspace_id = ? AND email <> ? "
            "AND accepted_at IS NULL AND expires_at > ? LIMIT 1",
            (workspace_id, email, _timestamp(now)),
        ).fetchone()
    if other is not None:
        raise AuthError(
            f"cette instance appartient déjà à {other['email']} : "
            "créez une instance dédiée avec --instance NAME"
        )


def create_invite(
    conn: sqlite3.Connection,
    workspace_slug: str,
    email: str,
    *,
    now: datetime.datetime | None = None,
) -> str:
    """Crée une invitation propriétaire et active l'authentification de l'instance."""
    normalized_email = normalize_email(email)
    current = _now(now)
    token = secrets.token_urlsafe(32)
    conn.execute("BEGIN IMMEDIATE")
    try:
        workspace_id = ensure_workspace(conn, workspace_slug)
        _refuse_second_owner(conn, workspace_id, normalized_email, current)
        conn.execute(
            "UPDATE account_invite SET expires_at = ? "
            "WHERE workspace_id = ? AND email = ? AND accepted_at IS NULL",
            (_timestamp(current), workspace_id, normalized_email),
        )
        conn.execute(
            "INSERT INTO account_invite "
            "(workspace_id, email, role, token_hash, expires_at) VALUES (?, ?, 'owner', ?, ?)",
            (
                workspace_id,
                normalized_email,
                _token_hash(token),
                _timestamp(current + datetime.timedelta(hours=INVITE_HOURS)),
            ),
        )
        conn.execute(
            "INSERT INTO instance_setting (key, value) VALUES (?, '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'",
            (AUTH_REQUIRED_KEY,),
        )
        conn.commit()
    except (AuthError, sqlite3.Error):
        conn.rollback()
        raise
    return token


def invite_status(
    conn: sqlite3.Connection,
    token: str,
    workspace_slug: str,
    *,
    now: datetime.datetime | None = None,
) -> str:
    """Renvoie `valid`, `accepted`, `expired` ou `invalid` pour un lien d'invitation."""
    row = conn.execute(
        "SELECT i.expires_at, i.accepted_at FROM account_invite i "
        "JOIN workspace w ON w.id = i.workspace_id "
        "WHERE i.token_hash = ? AND w.slug = ?",
        (_token_hash(token), workspace_slug),
    ).fetchone()
    if row is None:
        return "invalid"
    if row["accepted_at"] is not None:
        return "accepted"
    if row["expires_at"] <= _timestamp(_now(now)):
        return "expired"
    return "valid"


def accept_invite(
    conn: sqlite3.Connection,
    token: str,
    password: str,
    *,
    workspace_slug: str | None = None,
    now: datetime.datetime | None = None,
) -> int:
    """Consomme une invitation et crée le compte et son appartenance."""
    current = _now(now)
    password_hash = hash_password(password)
    row = conn.execute(
        "SELECT i.id, i.workspace_id, i.email, i.role, i.expires_at, i.accepted_at "
        "FROM account_invite i JOIN workspace w ON w.id = i.workspace_id "
        "WHERE i.token_hash = ? AND (? IS NULL OR w.slug = ?)",
        (_token_hash(token), workspace_slug, workspace_slug),
    ).fetchone()
    if row is None or row["accepted_at"] is not None or row["expires_at"] <= _timestamp(current):
        raise AuthError("invitation invalide ou expirée")
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT id FROM account WHERE email = ?", (row["email"],)
        ).fetchone()
        if existing is None:
            cursor = conn.execute(
                "INSERT INTO account (email, password_hash) VALUES (?, ?)",
                (row["email"], password_hash),
            )
            account_id = int(cursor.lastrowid)
        else:
            account_id = int(existing["id"])
            conn.execute(
                "UPDATE account SET password_hash = ?, disabled = 0 WHERE id = ?",
                (password_hash, account_id),
            )
        conn.execute(
            "INSERT INTO membership (account_id, workspace_id, role) VALUES (?, ?, ?) "
            "ON CONFLICT(account_id, workspace_id) DO UPDATE SET role = excluded.role",
            (account_id, int(row["workspace_id"]), row["role"]),
        )
        cursor = conn.execute(
            "UPDATE account_invite SET accepted_at = ? WHERE id = ? AND accepted_at IS NULL",
            (_timestamp(current), int(row["id"])),
        )
        if cursor.rowcount != 1:
            raise AuthError("invitation déjà utilisée")
        conn.commit()
    except (AuthError, sqlite3.Error):
        conn.rollback()
        raise
    return account_id


def create_session(
    conn: sqlite3.Connection,
    email: str,
    password: str,
    workspace_slug: str,
    *,
    now: datetime.datetime | None = None,
) -> tuple[str, Session]:
    """Vérifie les identifiants puis crée une session serveur de 24 heures."""
    current = _now(now)
    normalized_email = normalize_email(email)
    row = conn.execute(
        "SELECT a.id AS account_id, a.email, a.password_hash, a.disabled, "
        "w.id AS workspace_id, m.role "
        "FROM account a JOIN membership m ON m.account_id = a.id "
        "JOIN workspace w ON w.id = m.workspace_id "
        "WHERE a.email = ? AND w.slug = ?",
        (normalized_email, workspace_slug),
    ).fetchone()
    encoded = (
        str(row["password_hash"])
        if row is not None and row["password_hash"]
        else _dummy_password_hash()
    )
    if row is None or row["disabled"] or not verify_password(password, encoded):
        raise AuthError("identifiants invalides")
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    expires = _timestamp(current + datetime.timedelta(hours=SESSION_HOURS))
    conn.execute(
        "INSERT INTO web_session "
        "(token_hash, account_id, workspace_id, csrf_token, expires_at, created_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            _token_hash(token),
            int(row["account_id"]),
            int(row["workspace_id"]),
            csrf_token,
            expires,
            _timestamp(current),
            _timestamp(current),
        ),
    )
    conn.commit()
    return token, Session(
        account_id=int(row["account_id"]),
        workspace_id=int(row["workspace_id"]),
        email=str(row["email"]),
        role=str(row["role"]),
        csrf_token=csrf_token,
        expires_at=expires,
    )


def resolve_session(
    conn: sqlite3.Connection,
    token: str,
    *,
    now: datetime.datetime | None = None,
) -> Session | None:
    """Résout une session non expirée sans exposer le jeton brut en base."""
    current = _now(now)
    row = conn.execute(
        "SELECT s.account_id, s.workspace_id, s.csrf_token, s.expires_at, "
        "a.email, a.disabled, m.role "
        "FROM web_session s JOIN account a ON a.id = s.account_id "
        "JOIN membership m ON m.account_id = s.account_id AND m.workspace_id = s.workspace_id "
        "WHERE s.token_hash = ?",
        (_token_hash(token),),
    ).fetchone()
    if row is None or row["disabled"] or row["expires_at"] <= _timestamp(current):
        return None
    conn.execute(
        "UPDATE web_session SET last_seen_at = ? WHERE token_hash = ?",
        (_timestamp(current), _token_hash(token)),
    )
    conn.commit()
    return Session(
        account_id=int(row["account_id"]),
        workspace_id=int(row["workspace_id"]),
        email=str(row["email"]),
        role=str(row["role"]),
        csrf_token=str(row["csrf_token"]),
        expires_at=str(row["expires_at"]),
    )


def delete_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM web_session WHERE token_hash = ?", (_token_hash(token),))
    conn.commit()


def login_throttle_key(email: str, remote_address: str) -> str:
    """Ne conserve ni l'email ni l'adresse réseau en clair dans la table de débit."""
    value = f"{email.strip().casefold()}\0{remote_address.strip()}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def login_allowed(
    conn: sqlite3.Connection,
    key_hash: str,
    *,
    now: datetime.datetime | None = None,
) -> bool:
    current = _timestamp(_now(now))
    row = conn.execute(
        "SELECT blocked_until FROM login_throttle WHERE key_hash = ?", (key_hash,)
    ).fetchone()
    return row is None or row["blocked_until"] is None or row["blocked_until"] <= current


def record_login_failure(
    conn: sqlite3.Connection,
    key_hash: str,
    *,
    now: datetime.datetime | None = None,
) -> None:
    """Bloque temporairement une paire email/adresse après cinq échecs."""
    current = _now(now)
    current_text = _timestamp(current)
    cutoff = _timestamp(current - datetime.timedelta(minutes=LOGIN_WINDOW_MINUTES))
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT failures, window_started_at FROM login_throttle WHERE key_hash = ?",
            (key_hash,),
        ).fetchone()
        if row is None or row["window_started_at"] <= cutoff:
            failures = 1
            window_started = current_text
        else:
            failures = int(row["failures"]) + 1
            window_started = str(row["window_started_at"])
        blocked_until = (
            _timestamp(current + datetime.timedelta(minutes=LOGIN_BLOCK_MINUTES))
            if failures >= LOGIN_MAX_FAILURES
            else None
        )
        conn.execute(
            "INSERT INTO login_throttle (key_hash, failures, window_started_at, blocked_until) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(key_hash) DO UPDATE SET "
            "failures = excluded.failures, window_started_at = excluded.window_started_at, "
            "blocked_until = excluded.blocked_until",
            (key_hash, failures, window_started, blocked_until),
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


def clear_login_failures(conn: sqlite3.Connection, key_hash: str) -> None:
    conn.execute("DELETE FROM login_throttle WHERE key_hash = ?", (key_hash,))
    conn.commit()
