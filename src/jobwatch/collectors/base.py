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

    Une offre est ignorée quand son url existe déjà, ou quand une offre avec le
    même (company, lower(title)) existe déjà.
    """
    conn.execute(
        "INSERT OR IGNORE INTO source (type, name) VALUES (?, ?)", (source_type, source_name)
    )
    source_row = conn.execute("SELECT id FROM source WHERE name = ?", (source_name,)).fetchone()
    source_id = int(source_row["id"])

    existing_urls = {str(r["url"]) for r in conn.execute("SELECT url FROM offer").fetchall()}
    existing_titles = {
        (str(r["company"]), str(r["title"]).lower())
        for r in conn.execute(
            "SELECT c.name AS company, o.title AS title "
            "FROM offer o JOIN company c ON c.id = o.company_id"
        ).fetchall()
    }

    new_ids: list[int] = []
    for offer in offers:
        if offer.url in existing_urls:
            continue
        key = (offer.company, offer.title.lower())
        if key in existing_titles:
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
        new_ids.append(int(cur.lastrowid))
        existing_urls.add(offer.url)
        existing_titles.add(key)

    conn.execute("UPDATE source SET last_run_at = datetime('now') WHERE id = ?", (source_id,))
    conn.commit()
    return new_ids
