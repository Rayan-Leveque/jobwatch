"""Connexion SQLite, initialisation du schéma et helpers de lignes."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path
from typing import Any

SCHEMA_FILE = "schema.sql"

# Colonnes ajoutées après la création de la table (migration d'une base v0.2 existante).
COLUMN_MIGRATIONS = (
    ("offer", "deadline", "TEXT"),
    ("match", "fit", "TEXT"),
    ("offer_summary", "source", "TEXT DEFAULT 'manual'"),
    ("offer_summary", "status", "TEXT NOT NULL DEFAULT 'ready'"),
    ("offer_summary", "attempt_count", "INTEGER NOT NULL DEFAULT 0"),
    ("offer_summary", "attempted_at", "TEXT"),
    ("match", "discarded_at", "TEXT"),
    ("summary_field", "quote", "TEXT"),
    ("offer_content", "extract_method", "TEXT"),
    ("offer_content", "html_gz", "BLOB"),
    # Les lignes existantes ont déjà subi une tentative. Leur raison reste NULL :
    # enrich leur accorde ainsi un retry legacy immédiat, puis les classifie.
    ("offer_content", "fetch_attempts", "INTEGER NOT NULL DEFAULT 1"),
    ("offer_content", "failure_reason", "TEXT"),
    ("offer_content", "wttj_recovery_version", "INTEGER NOT NULL DEFAULT 0"),
    ("career_intent", "search_id", "INTEGER REFERENCES search(id)"),
    ("search", "archived_at", "TEXT"),
    ("candidate_profile", "motivations", "TEXT"),
    ("candidate_profile", "targets", "TEXT"),
    ("candidate_profile", "highlights", "TEXT"),
    ("candidate_profile", "preferred_tone", "TEXT"),
    ("candidate_profile", "constraints_text", "TEXT"),
    ("candidate_profile", "reusable_details", "TEXT"),
)


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
    for table, column, column_type in COLUMN_MIGRATIONS:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def row(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    """Renvoie une ligne unique ou None."""
    return conn.execute(query, params).fetchone()


def rows(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    """Renvoie toutes les lignes sous forme de liste."""
    return list(conn.execute(query, params).fetchall())
