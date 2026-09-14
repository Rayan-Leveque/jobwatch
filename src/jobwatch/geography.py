"""Localisations du profil, communes à toutes les sources d'offres."""

from __future__ import annotations

import json
import sqlite3

REMOTE_LOCATIONS = ["Télétravail complet", "full remote"]


def validate_locations(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > 5 or any(
        not isinstance(item, str) or not item.strip() or len(item) > 100 for item in value
    ):
        raise ValueError("indiquez au maximum cinq villes ou régions de 100 caractères")
    return list(dict.fromkeys(item.strip() for item in value))


def profile_geography(
    conn: sqlite3.Connection, account_id: int | None = None,
) -> tuple[list[str], bool]:
    if account_id is not None:
        rows = conn.execute(
            "SELECT locations_json, include_remote FROM candidate_profile WHERE account_id = ?",
            (account_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT locations_json, include_remote FROM candidate_profile "
            "WHERE completed_at IS NOT NULL LIMIT 2"
        ).fetchall()
    if len(rows) != 1:
        return [], False
    return json.loads(rows[0]["locations_json"]), bool(rows[0]["include_remote"])


def location_matches(location: str | None, locations: list[str]) -> bool:
    return not locations or not location or any(
        item.casefold() in location.casefold() for item in locations
    )


def filter_profile_locations(
    conn: sqlite3.Connection, rows: list[sqlite3.Row], account_id: int | None = None,
) -> list[sqlite3.Row]:
    locations, include_remote = profile_geography(conn, account_id)
    if locations and include_remote:
        locations += REMOTE_LOCATIONS
    return [row for row in rows if location_matches(row["location"], locations)]
