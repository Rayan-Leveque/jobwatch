"""SQLite connection, schema initialization and row helpers."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path
from typing import Any

SCHEMA_FILE = "schema.sql"


class JobwatchError(Exception):
    """Expected, user-facing error. CLI prints a clean message and exits 1."""


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys and a row factory enabled."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Execute the packaged schema.sql against the connection."""
    schema = resources.files("jobwatch").joinpath(SCHEMA_FILE).read_text()
    conn.executescript(schema)


def row(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    """Return a single row or None."""
    return conn.execute(query, params).fetchone()


def rows(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    """Return all rows as a list."""
    return list(conn.execute(query, params).fetchall())
