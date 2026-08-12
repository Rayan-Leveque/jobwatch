"""Requêtes SQLite du tableau de bord : lecture et mise en forme des données.

Fonctions pures extraites de jobwatch.serve : chacune prend une connexion
sqlite3 et renvoie des lignes ou dictionnaires prêts pour le rendu HTML.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from dataclasses import field as dataclasses_field

# Deux onglets étanches : la piste « Chef de projet / PO » (track « project »)
# regroupe offres et candidatures dont le titre contient un des motifs LIKE
# ci-dessous (insensibles à la casse ASCII) ; l'onglet « Ingénieur IA »
# (track « engineer », racine /) montre tout le reste.
PROJECT_TITLE_PATTERNS = (
    "%chef%de projet%",
    "%chef%de produit%",
    "%product owner%",
    "%product manager%",
)


_PROJECT_TITLE_SQL = "(" + " OR ".join("o.title LIKE ?" for _ in PROJECT_TITLE_PATTERNS) + ")"


TRACKS = ("engineer", "project", "all")


def _track_filter(track: str) -> str:
    """Clause SQL restreignant une requête (alias offre `o`) à la piste demandée."""
    if track == "all":
        return ""
    if track == "project":
        return f"AND {_PROJECT_TITLE_SQL} "
    return f"AND NOT {_PROJECT_TITLE_SQL} "


def _track_params(track: str) -> tuple[str, ...]:
    return PROJECT_TITLE_PATTERNS if track in ("engineer", "project") else ()


def _matches(conn: sqlite3.Connection, state: str, track: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT m.id AS id, o.id AS offer_id, m.state AS state, m.fit AS fit, "
        "       s.name AS search_name, "
        "       c.name AS company, o.title AS title, o.location AS location, "
        "       o.contract AS contract, o.platform AS platform, o.url AS url, "
        "       o.collected_at AS collected_at, o.deadline AS deadline "
        "FROM match m "
        "JOIN search s ON s.id = m.search_id AND s.archived_at IS NULL "
        "JOIN offer o ON o.id = m.offer_id "
        "LEFT JOIN company c ON c.id = o.company_id "
        "WHERE m.state = ? AND (m.fit IS NULL OR m.fit != 'high') AND NOT EXISTS "
        "    (SELECT 1 FROM application a WHERE a.match_id = m.id) "
        f"{_track_filter(track)}"
        "ORDER BY CASE m.fit WHEN 'high' THEN 0 WHEN 'medium' THEN 1 "
        "         WHEN 'low' THEN 2 ELSE 3 END, "
        "         o.collected_at DESC, m.id DESC",
        (state, *_track_params(track)),
    ).fetchall()


def _priority_matches(conn: sqlite3.Connection, track: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT m.id AS id, o.id AS offer_id, m.state AS state, m.fit AS fit, "
        "       s.name AS search_name, "
        "       c.name AS company, o.title AS title, o.location AS location, "
        "       o.contract AS contract, o.platform AS platform, o.url AS url, "
        "       o.collected_at AS collected_at, o.deadline AS deadline "
        "FROM match m "
        "JOIN search s ON s.id = m.search_id AND s.archived_at IS NULL "
        "JOIN offer o ON o.id = m.offer_id "
        "LEFT JOIN company c ON c.id = o.company_id "
        "WHERE m.fit = 'high' AND m.state IN ('new', 'seen') AND NOT EXISTS "
        "    (SELECT 1 FROM application a WHERE a.match_id = m.id) "
        f"{_track_filter(track)}"
        "ORDER BY o.collected_at DESC, m.id DESC",
        _track_params(track),
    ).fetchall()


def _later_matches(conn: sqlite3.Connection, track: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT m.id AS id, o.id AS offer_id, m.state AS state, m.fit AS fit, "
        "       s.name AS search_name, "
        "       c.name AS company, o.title AS title, o.location AS location, "
        "       o.contract AS contract, o.platform AS platform, o.url AS url, "
        "       o.collected_at AS collected_at, o.deadline AS deadline "
        "FROM match m "
        "JOIN search s ON s.id = m.search_id AND s.archived_at IS NULL "
        "JOIN offer o ON o.id = m.offer_id "
        "LEFT JOIN company c ON c.id = o.company_id "
        "WHERE m.state = 'later' AND NOT EXISTS "
        "    (SELECT 1 FROM application a WHERE a.match_id = m.id) "
        f"{_track_filter(track)}"
        "ORDER BY CASE m.fit WHEN 'high' THEN 0 WHEN 'medium' THEN 1 "
        "         WHEN 'low' THEN 2 ELSE 3 END, "
        "         o.collected_at DESC, m.id DESC",
        _track_params(track),
    ).fetchall()


def _discarded_matches(conn: sqlite3.Connection, track: str) -> list[sqlite3.Row]:
    """Matchs écartés depuis moins de 30 jours ; filtre d'affichage pur, jamais de suppression."""
    return conn.execute(
        "SELECT m.id AS id, o.id AS offer_id, m.state AS state, m.fit AS fit, "
        "       s.name AS search_name, "
        "       c.name AS company, o.title AS title, o.location AS location, "
        "       o.contract AS contract, o.platform AS platform, o.url AS url, "
        "       o.collected_at AS collected_at, o.deadline AS deadline "
        "FROM match m "
        "JOIN search s ON s.id = m.search_id AND s.archived_at IS NULL "
        "JOIN offer o ON o.id = m.offer_id "
        "LEFT JOIN company c ON c.id = o.company_id "
        "WHERE m.state = 'discarded' AND m.discarded_at > datetime('now', '-30 days') "
        f"{_track_filter(track)}"
        "ORDER BY m.discarded_at DESC, m.id DESC",
        _track_params(track),
    ).fetchall()


def _applications(conn: sqlite3.Connection, track: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT a.id AS id, o.id AS offer_id, m.id AS match_id, m.fit AS fit, "
        "       c.name AS company, o.title AS title, "
        "       o.location AS location, o.contract AS contract, "
        "       o.platform AS platform, o.url AS url, a.note AS note, "
        "       s.name AS search_name, "
        "       a.created_at AS created_at, "
        "       (SELECT e.type FROM event e WHERE e.application_id = a.id "
        "        ORDER BY e.at DESC, e.id DESC LIMIT 1) AS status, "
        "       (SELECT e.at FROM event e WHERE e.application_id = a.id "
        "        ORDER BY e.at DESC, e.id DESC LIMIT 1) AS status_at "
        "FROM application a "
        "JOIN offer o ON o.id = a.offer_id "
        "LEFT JOIN company c ON c.id = o.company_id "
        "LEFT JOIN match m ON m.id = a.match_id "
        "LEFT JOIN search s ON s.id = m.search_id AND s.archived_at IS NULL "
        f"WHERE 1=1 {_track_filter(track)}"
        "ORDER BY a.created_at DESC, a.id DESC",
        _track_params(track),
    ).fetchall()


FIELD_LABELS = {
    "experience": "Expérience souhaitée",
    "salary": "Salaire",
    "remote": "Télétravail",
    "stack": "Stack",
}


@dataclass
class Summary:
    """Résumé affichable d'une offre : champs structurés + puces mission."""

    bullets: list[str] = dataclasses_field(default_factory=list)
    fields: list[tuple[str, str]] = dataclasses_field(default_factory=list)
    source: str = "manual"
    status: str = "ready"

    def __bool__(self) -> bool:
        return bool(self.bullets or self.fields)


def _summary_bullets(
    conn: sqlite3.Connection, offer_ids: list[int]
) -> dict[int, Summary]:
    if not offer_ids:
        return {}
    summaries: dict[int, Summary] = {}
    for start in range(0, len(offer_ids), 500):
        chunk = offer_ids[start : start + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT os.offer_id AS offer_id, os.source AS source, os.status AS status, "
            "       sb.text AS text "
            "FROM offer_summary os "
            "JOIN summary_bullet sb ON sb.summary_id = os.id "
            f"WHERE os.offer_id IN ({placeholders}) "
            "ORDER BY os.offer_id, sb.position",
            chunk,
        ).fetchall()
        for row in rows:
            offer_id = int(row["offer_id"])
            summary = summaries.setdefault(
                offer_id,
                Summary(source=str(row["source"] or "manual"), status=str(row["status"])),
            )
            summary.bullets.append(str(row["text"]))
        field_rows = conn.execute(
            "SELECT os.offer_id AS offer_id, os.source AS source, os.status AS status, "
            "       sf.key AS key, sf.value AS value "
            "FROM offer_summary os "
            "JOIN summary_field sf ON sf.summary_id = os.id "
            f"WHERE os.offer_id IN ({placeholders})",
            chunk,
        ).fetchall()
        by_offer: dict[int, dict[str, str]] = {}
        for row in field_rows:
            offer_id = int(row["offer_id"])
            summaries.setdefault(
                offer_id,
                Summary(source=str(row["source"] or "manual"), status=str(row["status"])),
            )
            by_offer.setdefault(offer_id, {})[str(row["key"])] = str(row["value"])
        for offer_id, values in by_offer.items():
            summaries.setdefault(offer_id, Summary()).fields = [
                (key, values[key]) for key in FIELD_LABELS if key in values
            ]
    return summaries


def _offer_contents(conn: sqlite3.Connection, offer_ids: list[int]) -> dict[int, str]:
    if not offer_ids:
        return {}
    contents: dict[int, str] = {}
    for start in range(0, len(offer_ids), 500):
        chunk = offer_ids[start : start + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT offer_id AS offer_id, markdown AS markdown FROM offer_content "
            f"WHERE offer_id IN ({placeholders}) AND status = 'ok'",
            chunk,
        ).fetchall()
        for row in rows:
            contents[int(row["offer_id"])] = str(row["markdown"])
    return contents


def _offer_content_failures(
    conn: sqlite3.Connection, offer_ids: list[int]
) -> dict[int, tuple[str, int]]:
    """Cause persistée des annonces indisponibles et nombre de tentatives."""
    if not offer_ids:
        return {}
    failures: dict[int, tuple[str, int]] = {}
    for start in range(0, len(offer_ids), 500):
        chunk = offer_ids[start : start + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT offer_id, failure_reason, fetch_attempts FROM offer_content "
            f"WHERE offer_id IN ({placeholders}) AND status = 'failed'",
            chunk,
        ).fetchall()
        for row in rows:
            failures[int(row["offer_id"])] = (
                str(row["failure_reason"] or "unclassified"),
                int(row["fetch_attempts"] or 0),
            )
    return failures


def _draft_rows(conn: sqlite3.Connection, match_ids: list[int]) -> dict[int, sqlite3.Row]:
    """Dernier job de génération de lettre par match (le plus grand id gagne)."""
    if not match_ids:
        return {}
    drafts: dict[int, sqlite3.Row] = {}
    for start in range(0, len(match_ids), 500):
        chunk = match_ids[start : start + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT * FROM draft_job "
            f"WHERE match_id IN ({placeholders}) ORDER BY match_id, id",
            chunk,
        ).fetchall()
        for row in rows:
            drafts[int(row["match_id"])] = row
    return drafts


def _swipe_deck(conn: sqlite3.Connection, track: str) -> list[sqlite3.Row]:
    """Offres 'new' de la piste à trier : fit high d'abord, puis par date de collecte."""
    return conn.execute(
        "SELECT m.id AS id, o.id AS offer_id, m.state AS state, m.fit AS fit, "
        "       s.name AS search_name, c.name AS company, o.title AS title, "
        "       o.location AS location, o.contract AS contract, o.platform AS platform, "
        "       o.url AS url, o.collected_at AS collected_at, o.deadline AS deadline "
        "FROM match m "
        "JOIN search s ON s.id = m.search_id AND s.archived_at IS NULL "
        "JOIN offer o ON o.id = m.offer_id "
        "LEFT JOIN company c ON c.id = o.company_id "
        "WHERE m.state = 'new' AND NOT EXISTS "
        "    (SELECT 1 FROM application a WHERE a.match_id = m.id) "
        f"{_track_filter(track)}"
        "ORDER BY CASE WHEN m.fit = 'high' THEN 0 ELSE 1 END, "
        "         o.collected_at DESC, m.id DESC",
        _track_params(track),
    ).fetchall()


# Cibles de la génération groupée : offres « À candidater » sans lettre générée
# ni génération en cours (les échecs précédents sont réessayés).
_BATCH_ELIGIBLE_SQL = (
    "FROM match m "
    "JOIN search s ON s.id = m.search_id AND s.archived_at IS NULL "
    "JOIN offer o ON o.id = m.offer_id "
    "WHERE m.state = 'later' AND NOT EXISTS "
    "    (SELECT 1 FROM application a WHERE a.match_id = m.id) "
    "AND NOT EXISTS (SELECT 1 FROM draft_job dj WHERE dj.match_id = m.id "
    "                AND dj.status IN ('ok', 'running', 'queued')) "
)


def _batch_eligible_ids(conn: sqlite3.Connection, track: str) -> list[int]:
    rows = conn.execute(
        f"SELECT m.id AS id {_BATCH_ELIGIBLE_SQL}{_track_filter(track)}ORDER BY m.id",
        _track_params(track),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def _batch_status(conn: sqlite3.Connection, track: str) -> dict[str, int]:
    """État du dernier job de chaque offre « À candidater » de la piste."""
    rows = conn.execute(
        "SELECT (SELECT dj.status FROM draft_job dj WHERE dj.match_id = m.id "
        "        ORDER BY dj.id DESC LIMIT 1) AS status "
        "FROM match m "
        "JOIN search s ON s.id = m.search_id AND s.archived_at IS NULL "
        "JOIN offer o ON o.id = m.offer_id "
        "WHERE m.state = 'later' AND NOT EXISTS "
        "    (SELECT 1 FROM application a WHERE a.match_id = m.id) "
        f"{_track_filter(track)}",
        _track_params(track),
    ).fetchall()
    counts = {"queued": 0, "running": 0, "ok": 0, "failed": 0, "none": 0}
    for row in rows:
        status = str(row["status"]) if row["status"] in counts else "none"
        counts[status] += 1
    return counts
