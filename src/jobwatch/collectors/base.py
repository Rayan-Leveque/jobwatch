"""Protocole de collecteur, dataclass RawOffer et logique partagée de stockage des offres."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)


@dataclass
class RawOffer:
    """Une offre d'emploi telle que renvoyée par un collecteur, avant stockage."""

    title: str
    url: str
    company: str
    platform: str
    location: str | None = None
    contract: str | None = None
    published_at: str | None = None


class Collector(Protocol):
    """Récupère les offres d'emploi d'un job board distant."""

    name: str
    source_type: str
    platform: str

    def fetch(self) -> list[RawOffer]:
        """Renvoie les offres de la source. Ne lève jamais d'erreur réseau."""
        ...


def _get_company_id(conn: sqlite3.Connection, name: str) -> int:
    cur = conn.execute("INSERT OR IGNORE INTO company (name) VALUES (?)", (name,))
    if cur.rowcount == 0:
        row = conn.execute("SELECT id FROM company WHERE name = ?", (name,)).fetchone()
        return int(row["id"])
    return int(cur.lastrowid)


def store_offers(
    conn: sqlite3.Connection,
    source_name: str,
    source_type: str,
    offers: list[RawOffer],
) -> list[int]:
    """Upsert des sociétés et offres, en renvoyant les ids des offres nouvellement insérées.

    Une offre est dédupliquée par URL ou (company, lower(title)), en conservant
    toute information de télétravail complet reçue.
    """
    conn.execute(
        "INSERT OR IGNORE INTO source (type, name) VALUES (?, ?)", (source_type, source_name)
    )
    source_row = conn.execute("SELECT id FROM source WHERE name = ?", (source_name,)).fetchone()
    source_id = int(source_row["id"])

    existing = conn.execute(
        "SELECT o.id, o.url, o.location, c.name AS company, o.title "
        "FROM offer o JOIN company c ON c.id = o.company_id"
    ).fetchall()
    existing_urls = {str(row["url"]): int(row["id"]) for row in existing}
    existing_titles = {
        (str(row["company"]), str(row["title"]).lower()): int(row["id"])
        for row in existing
    }
    locations = {int(row["id"]): row["location"] for row in existing}

    new_ids: list[int] = []
    for offer in offers:
        key = (offer.company, offer.title.lower())
        existing_id = existing_urls.get(offer.url) or existing_titles.get(key)
        if existing_id is not None:
            marker = "Télétravail complet"
            location = locations[existing_id] or ""
            if marker.casefold() in (offer.location or "").casefold() and (
                marker.casefold() not in location.casefold()
            ):
                location = f"{location} · {marker}" if location else offer.location
                conn.execute(
                    "UPDATE offer SET location = ? WHERE id = ?", (location, existing_id)
                )
                locations[existing_id] = location
            continue
        company_id = _get_company_id(conn, offer.company)
        cur = conn.execute(
            "INSERT OR IGNORE INTO offer "
            "(source_id, company_id, title, url, platform, location, contract, published_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_id,
                company_id,
                offer.title,
                offer.url,
                offer.platform,
                offer.location,
                offer.contract,
                offer.published_at,
            ),
        )
        if cur.rowcount == 0:
            continue
        offer_id = int(cur.lastrowid)
        new_ids.append(offer_id)
        existing_urls[offer.url] = offer_id
        existing_titles[key] = offer_id
        locations[offer_id] = offer.location

    conn.execute("UPDATE source SET last_run_at = datetime('now') WHERE id = ?", (source_id,))
    conn.commit()
    return new_ids
