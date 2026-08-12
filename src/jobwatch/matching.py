"""Met en correspondance les offres stockées avec les recherches et insère les nouveaux matchs."""

from __future__ import annotations

import json
import sqlite3

from jobwatch.config import SearchConfig
from jobwatch.seniority import OFFER_WINDOW_DAYS, assess_new_match


def _json_list(values: list[str]) -> str:
    return json.dumps(values, ensure_ascii=True, separators=(",", ":"))


def _stored_search_key(row: sqlite3.Row) -> tuple[str, str, str, str | None]:
    """Renvoie le tuple qui identifie les champs configurés d'une ligne de recherche."""
    return (
        row["include_json"],
        row["exclude_json"],
        row["locations_json"],
        row["contract"],
    )


def sync_searches(conn: sqlite3.Connection, searches: list[SearchConfig]) -> None:
    """Insère les nouvelles recherches, met à jour les modifiées, désactive les supprimées."""
    rows = conn.execute("SELECT * FROM search").fetchall()
    existing = {str(r["name"]): r for r in rows}

    for search in searches:
        include_json = _json_list(search.include)
        exclude_json = _json_list(search.exclude)
        locations_json = _json_list(search.locations)
        row = existing.get(search.name)
        if row is None:
            conn.execute(
                "INSERT INTO search "
                "(name, include_json, exclude_json, locations_json, contract, active) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (
                    search.name,
                    include_json,
                    exclude_json,
                    locations_json,
                    search.contract,
                ),
            )
            continue
        key = (include_json, exclude_json, locations_json, search.contract)
        if _stored_search_key(row) != key or row["active"] != 1:
            conn.execute(
                "UPDATE search SET include_json = ?, exclude_json = ?, locations_json = ?, "
                "contract = ?, active = 1 WHERE id = ?",
                (include_json, exclude_json, locations_json, search.contract, row["id"]),
            )

    config_names = {search.name for search in searches}
    for row in rows:
        if row["name"] not in config_names and row["active"] == 1:
            conn.execute("UPDATE search SET active = 0 WHERE id = ?", (row["id"],))

    conn.commit()


def active_search_configs(conn: sqlite3.Connection) -> list[SearchConfig]:
    """Relit les recherches actives, y compris celles confirmées dans l'onboarding."""
    rows = conn.execute(
        "SELECT name, include_json, exclude_json, locations_json, contract "
        "FROM search WHERE active = 1 ORDER BY id"
    ).fetchall()
    return [
        SearchConfig(
            name=str(row["name"]),
            include=list(json.loads(str(row["include_json"]))),
            exclude=list(json.loads(str(row["exclude_json"]))),
            locations=list(json.loads(str(row["locations_json"]))),
            contract=row["contract"],
        )
        for row in rows
    ]


def _search_row(conn: sqlite3.Connection, search_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM search WHERE id = ? AND active = 1", (search_id,)).fetchone()
    if row is None:
        raise ValueError(f"no active search with id {search_id}")
    return row


def _offer_candidates(conn: sqlite3.Connection, search_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT o.id, o.title, o.location, o.contract FROM offer o "
            "WHERE NOT EXISTS (SELECT 1 FROM match m "
            "                     WHERE m.search_id = ? AND m.offer_id = o.id) "
            "AND o.collected_at >= datetime('now', ?)",
            (search_id, f"-{OFFER_WINDOW_DAYS} days"),
        ).fetchall()
    )


def offer_matches_search(offer: sqlite3.Row, search: sqlite3.Row) -> bool:
    """Renvoie True quand l'offre satisfait tous les critères de la recherche."""
    include = json.loads(search["include_json"])
    exclude = json.loads(search["exclude_json"])
    locations = json.loads(search["locations_json"])

    title = str(offer["title"] or "").lower()
    if not any(keyword.lower() in title for keyword in include):
        return False
    if any(keyword.lower() in title for keyword in exclude):
        return False

    offer_location = offer["location"]
    if locations and offer_location:
        offer_location_lower = str(offer_location).lower()
        if not any(location.lower() in offer_location_lower for location in locations):
            return False

    contract = search["contract"]
    if contract is None or offer["contract"] is None:
        return True
    return offer["contract"] == contract


def run_matching(conn: sqlite3.Connection) -> list[int]:
    """Met en correspondance les offres non encore appariées avec chaque recherche active,
    en insérant de nouveaux matchs.

    Renvoie les ids des matchs nouvellement insérés.
    """
    search_ids = [
        int(r["id"]) for r in conn.execute("SELECT id FROM search WHERE active = 1").fetchall()
    ]
    new_match_ids: list[int] = []
    for search_id in search_ids:
        search = _search_row(conn, search_id)
        for offer in _offer_candidates(conn, search_id):
            if not offer_matches_search(offer, search):
                continue
            cur = conn.execute(
                "INSERT INTO match (search_id, offer_id) VALUES (?, ?)",
                (search_id, offer["id"]),
            )
            match_id = int(cur.lastrowid)
            assess_new_match(conn, match_id)
            excluded = conn.execute(
                "SELECT 1 FROM match_seniority WHERE match_id = ? AND status = 'excluded'",
                (match_id,),
            ).fetchone()
            if excluded is None:
                new_match_ids.append(match_id)
    conn.commit()
    return new_match_ids
