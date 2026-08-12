"""Classification explicite de séniorité et filtrage propre à un compte."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass

MIN_LEVEL = 0
MAX_LEVEL = 5
DEFAULT_MIN_LEVEL = MIN_LEVEL
DEFAULT_MAX_LEVEL = MAX_LEVEL

OFFER_WINDOW_DAYS = 60

SENIORITY_LEVELS = (
    (0, "Stage"),
    (1, "Alternance"),
    (2, "Junior"),
    (3, "Intermédiaire / confirmé"),
    (4, "Senior"),
    (5, "Lead / management"),
)
LEVEL_LABELS = dict(SENIORITY_LEVELS)

_YEARS_RE = re.compile(
    r"\b(\d{1,2})\s*(?:\+|(?:[-–à/]\s*\d{1,2}))?\s*(?:ans?|annees?|years?)\b",
    re.IGNORECASE,
)
_UNCLASSIFIED_VALUES = {"", "non precise", "non renseigne", "inconnu", "unknown"}


@dataclass(frozen=True)
class SeniorityAssessment:
    level: int | None
    reason: str
    evidence: str | None = None


def _fold(value: object) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(char for char in normalized if not unicodedata.combining(char))


def validate_range(minimum: object, maximum: object) -> tuple[int, int]:
    if (
        not isinstance(minimum, int)
        or isinstance(minimum, bool)
        or not isinstance(maximum, int)
        or isinstance(maximum, bool)
        or not MIN_LEVEL <= minimum <= maximum <= MAX_LEVEL
    ):
        raise ValueError("la séniorité minimale doit précéder la séniorité maximale")
    return minimum, maximum


def _level_from_years(years: int) -> int:
    if years <= 2:
        return 2
    if years <= 4:
        return 3
    if years <= 7:
        return 4
    return 5


def _explicit_years(value: object, *, semantic_field: bool) -> tuple[int, str] | None:
    text = str(value or "").strip()
    folded = _fold(text)
    if folded in _UNCLASSIFIED_VALUES:
        return None
    for match in _YEARS_RE.finditer(folded):
        nearby = folded[max(0, match.start() - 55) : match.end() + 55]
        if semantic_field or "experien" in nearby:
            return int(match.group(1)), text
    return None


def assess_offer(conn: sqlite3.Connection, offer_id: int) -> SeniorityAssessment:
    """Classe uniquement les exigences explicites, sans déduire un niveau absent."""
    row = conn.execute(
        "SELECT o.title, o.contract, sf.value AS experience, sf.quote, oc.markdown "
        "FROM offer o "
        "LEFT JOIN offer_summary os ON os.offer_id = o.id "
        "LEFT JOIN summary_field sf ON sf.summary_id = os.id AND sf.key = 'experience' "
        "LEFT JOIN offer_content oc ON oc.offer_id = o.id AND oc.status = 'ok' "
        "WHERE o.id = ?",
        (offer_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"offre introuvable : {offer_id}")

    title = _fold(row["title"])
    contract = _fold(row["contract"])
    if contract == "internship" or re.search(r"\b(stage|intern(?:ship)?)\b", title):
        return SeniorityAssessment(0, "stage explicitement indiqué", str(row["title"]))
    if re.search(r"\b(alternan(?:ce|t)|apprenti(?:e|ssage)?|work.study)\b", title):
        return SeniorityAssessment(1, "alternance explicitement indiquée", str(row["title"]))

    title_levels = (
        (5, r"\b(lead|staff|principal|head of|responsable d.equipe)\b"),
        (4, r"\bsenior\b"),
        (3, r"\b(confirme|intermediaire|mid.level)\b"),
        (2, r"\b(junior|debutant|graduate)\b"),
    )
    title_level = next((level for level, pattern in title_levels if re.search(pattern, title)), None)

    years_evidence = _explicit_years(row["quote"], semantic_field=True)
    if years_evidence is None:
        years_evidence = _explicit_years(row["experience"], semantic_field=True)
    if years_evidence is None:
        years_evidence = _explicit_years(row["markdown"], semantic_field=False)

    years_level = _level_from_years(years_evidence[0]) if years_evidence is not None else None
    levels = [level for level in (title_level, years_level) if level is not None]
    if not levels:
        return SeniorityAssessment(
            None,
            "Aucune exigence explicite de séniorité détectée : offre conservée",
        )
    level = max(levels)
    if years_evidence is not None and level == years_level:
        years, evidence = years_evidence
        return SeniorityAssessment(
            level,
            f"{years} an(s) d’expérience explicitement requis ({LEVEL_LABELS[level]})",
            evidence[:500],
        )
    return SeniorityAssessment(
        level,
        f"niveau {LEVEL_LABELS[level]} explicitement indiqué dans l’intitulé",
        str(row["title"])[:500],
    )


def _store_assessment(
    conn: sqlite3.Connection,
    match_id: int,
    account_id: int,
    minimum: int,
    maximum: int,
) -> None:
    offer_id = int(
        conn.execute("SELECT offer_id FROM match WHERE id = ?", (match_id,)).fetchone()["offer_id"]
    )
    assessment = assess_offer(conn, offer_id)
    if assessment.level is None:
        status = "unclassified"
    elif minimum <= assessment.level <= maximum:
        status = "compatible"
    else:
        status = "excluded"
    conn.execute(
        "INSERT INTO match_seniority "
        "(match_id, account_id, status, level, reason, evidence, evaluated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(match_id, account_id) DO UPDATE SET "
        "status = excluded.status, level = excluded.level, reason = excluded.reason, "
        "evidence = excluded.evidence, evaluated_at = datetime('now')",
        (
            match_id,
            account_id,
            status,
            assessment.level,
            assessment.reason,
            assessment.evidence,
        ),
    )


def assess_new_match(conn: sqlite3.Connection, match_id: int) -> None:
    """Évalue un nouveau match pour chaque profil confirmé de la base d'instance."""
    profiles = conn.execute(
        "SELECT account_id, seniority_min, seniority_max FROM candidate_profile "
        "WHERE completed_at IS NOT NULL"
    ).fetchall()
    for profile in profiles:
        _store_assessment(
            conn,
            match_id,
            int(profile["account_id"]),
            int(profile["seniority_min"]),
            int(profile["seniority_max"]),
        )


def reclassify_recent_matches(conn: sqlite3.Connection, account_id: int) -> int:
    """Réévalue au plus les matchs actifs de la fenêtre de collecte de 60 jours."""
    profile = conn.execute(
        "SELECT seniority_min, seniority_max FROM candidate_profile "
        "WHERE account_id = ? AND completed_at IS NOT NULL",
        (account_id,),
    ).fetchone()
    if profile is None:
        return 0
    match_ids = [
        int(row["id"])
        for row in conn.execute(
            "SELECT m.id FROM match m JOIN offer o ON o.id = m.offer_id "
            "WHERE m.state IN ('new', 'seen') "
            f"AND o.collected_at >= datetime('now', '-{OFFER_WINDOW_DAYS} days') ORDER BY m.id",
        ).fetchall()
    ]
    for match_id in match_ids:
        _store_assessment(
            conn,
            match_id,
            account_id,
            int(profile["seniority_min"]),
            int(profile["seniority_max"]),
        )
    conn.commit()
    return len(match_ids)


def reclassify_all_profiles(conn: sqlite3.Connection) -> int:
    """Réévalue les profils confirmés après l'arrivée de contenu ou de résumés."""
    account_ids = [
        int(row["account_id"])
        for row in conn.execute(
            "SELECT account_id FROM candidate_profile WHERE completed_at IS NOT NULL"
        ).fetchall()
    ]
    return sum(reclassify_recent_matches(conn, account_id) for account_id in account_ids)


def excluded_match_count(conn: sqlite3.Connection, account_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM match_seniority ms "
        "JOIN match m ON m.id = ms.match_id "
        "JOIN search s ON s.id = m.search_id AND s.archived_at IS NULL "
        "WHERE ms.account_id = ? AND ms.status = 'excluded' "
        "AND m.state IN ('new', 'seen')",
        (account_id,),
    ).fetchone()
    return int(row["n"])
