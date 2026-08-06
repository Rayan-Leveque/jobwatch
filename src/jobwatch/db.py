"""Connexion SQLite, initialisation du schéma et helpers de lignes."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path
from typing import Any

SCHEMA_FILE = "schema.sql"

# Colonnes ajoutées après la création de la table (migration d'une base v0.2 existante).
COLUMN_MIGRATIONS = {
    "offer": ("deadline", "TEXT"),
    "match": ("fit", "TEXT"),
}


class JobwatchError(Exception):
    """Erreur attendue, destinée à l'utilisateur. La CLI affiche un message clair et sort avec le code 1."""


def connect(path: Path | str) -> sqlite3.Connection:
    """Ouvre une connexion SQLite avec les clés étrangères et un row factory activés."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Exécute le schema.sql fourni avec le paquet, puis migre une base v0.2 existante."""
    schema = resources.files("jobwatch").joinpath(SCHEMA_FILE).read_text()
    conn.executescript(schema)
    _migrate_columns(conn)


def _migrate_columns(conn: sqlite3.Connection) -> None:
    """Ajoute les colonnes manquantes avec ALTER TABLE, de façon idempotente."""
    for table, (column, column_type) in COLUMN_MIGRATIONS.items():
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def row(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    """Renvoie une ligne unique ou None."""
    return conn.execute(query, params).fetchone()


def rows(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    """Renvoie toutes les lignes sous forme de liste."""
    return list(conn.execute(query, params).fetchall())
