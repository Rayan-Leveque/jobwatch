"""Analyse du CV et persistance des pistes métier confirmées à l'onboarding."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

from jobwatch import draft
from jobwatch.config import DraftConfig, SearchConfig
from jobwatch.matching import run_matching

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

ANALYSIS_PROMPT = """Analyse uniquement le ou les CV fournis dans le bloc <stdin>.
Propose entre une et quatre pistes de postes cohérentes avec les compétences du candidat.
Une piste représente une volonté professionnelle distincte, pas une compétence isolée.
Pour chaque piste, retourne :
- label : intitulé court et compréhensible ;
- keywords : 3 à 8 intitulés ou expressions utiles pour chercher des offres ;
- exclude : termes de postes proches mais manifestement hors cible, uniquement si nécessaire.

Le CV montre ce que la personne sait faire, pas forcément ce qu'elle veut. Formule donc des
propositions sobres que l'utilisateur devra confirmer. N'invente aucune expérience.
Réponds uniquement avec ce JSON :
{"intents":[{"label":"...","keywords":["..."],"exclude":["..."]}]}"""


class OnboardingError(Exception):
    """Échec attendu de l'analyse ou de la validation de l'onboarding."""


@dataclass(frozen=True)
class CareerIntent:
    label: str
    keywords: list[str]
    exclude: list[str]


def profile_complete(conn: sqlite3.Connection, account_id: int) -> bool:
    row = conn.execute(
        "SELECT completed_at FROM candidate_profile WHERE account_id = ?", (account_id,)
    ).fetchone()
    return row is not None and row["completed_at"] is not None


def profile_intents(conn: sqlite3.Connection, account_id: int) -> list[CareerIntent]:
    rows = conn.execute(
        "SELECT label, keywords_json, exclude_json FROM career_intent "
        "WHERE account_id = ? AND active = 1 ORDER BY position, id",
        (account_id,),
    ).fetchall()
    return [
        CareerIntent(
            label=str(row["label"]),
            keywords=list(json.loads(str(row["keywords_json"]))),
            exclude=list(json.loads(str(row["exclude_json"]))),
        )
        for row in rows
    ]


def profile_cv_library_ids(conn: sqlite3.Connection, account_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT document_library_id FROM candidate_profile_document "
        "WHERE account_id = ? ORDER BY position, document_library_id",
        (account_id,),
    ).fetchall()
    return [int(row["document_library_id"]) for row in rows]


def analyze_cvs(
    conn: sqlite3.Connection,
    config: DraftConfig | None,
    cv_library_ids: list[int],
) -> list[CareerIntent]:
    if config is None:
        raise OnboardingError("analyse IA non configurée sur cette instance")
    if not cv_library_ids:
        raise OnboardingError("ajoutez au moins un CV")
    try:
        cv_sections = [
            f"# CV {position}\n\n{draft._cv_text(conn, cv_library_id)}"
            for position, cv_library_id in enumerate(cv_library_ids, start=1)
        ]
        response = draft._call_llm(config, ANALYSIS_PROMPT, "\n\n".join(cv_sections))
    except draft.DraftError as exc:
        raise OnboardingError(str(exc)) from exc
    return parse_intents(response)


def analyze_cv(
    conn: sqlite3.Connection,
    config: DraftConfig | None,
    cv_library_id: int,
) -> list[CareerIntent]:
    return analyze_cvs(conn, config, [cv_library_id])


def parse_intents(response: str) -> list[CareerIntent]:
    match = _JSON_FENCE_RE.search(response)
    text = match.group(1).strip() if match else response.strip()
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise OnboardingError("l'IA n'a pas renvoyé de pistes valides") from exc
    rows = payload.get("intents") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise OnboardingError("l'IA n'a pas renvoyé de pistes valides")
    intents: list[CareerIntent] = []
    for row in rows[:4]:
        if not isinstance(row, dict):
            continue
        label = row.get("label")
        keywords = row.get("keywords")
        exclude = row.get("exclude", [])
        if not isinstance(label, str) or not label.strip():
            continue
        if not isinstance(keywords, list) or not all(
            isinstance(keyword, str) and keyword.strip() for keyword in keywords
        ):
            continue
        if not isinstance(exclude, list) or not all(isinstance(term, str) for term in exclude):
            continue
        intents.append(
            CareerIntent(
                label=label.strip(),
                keywords=list(dict.fromkeys(keyword.strip() for keyword in keywords))[:8],
                exclude=list(dict.fromkeys(term.strip() for term in exclude if term.strip()))[:8],
            )
        )
    if not intents:
        raise OnboardingError("l'IA n'a proposé aucune piste exploitable")
    labels: set[str] = set()
    for intent in intents:
        if not intent.keywords:
            raise OnboardingError("chaque catégorie doit contenir au moins un mot-clé")
        key = intent.label.casefold()
        if key in labels:
            raise OnboardingError("les catégories doivent avoir des noms différents")
        labels.add(key)
    return intents


def _sync_intent_searches(conn: sqlite3.Connection, intents: list[CareerIntent]) -> None:
    searches = [
        SearchConfig(name=intent.label, include=intent.keywords, exclude=intent.exclude)
        for intent in intents
    ]
    names = {search.name for search in searches}
    for search in searches:
        include_json = json.dumps(search.include, ensure_ascii=True, separators=(",", ":"))
        exclude_json = json.dumps(search.exclude, ensure_ascii=True, separators=(",", ":"))
        locations_json = json.dumps(search.locations, ensure_ascii=True, separators=(",", ":"))
        row = conn.execute("SELECT id FROM search WHERE name = ?", (search.name,)).fetchone()
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
        else:
            conn.execute(
                "UPDATE search SET include_json = ?, exclude_json = ?, locations_json = ?, "
                "contract = ?, active = 1 WHERE id = ?",
                (
                    include_json,
                    exclude_json,
                    locations_json,
                    search.contract,
                    int(row["id"]),
                ),
            )
    placeholders = ",".join("?" for _ in names)
    conn.execute(
        f"UPDATE search SET active = 0 WHERE name NOT IN ({placeholders})", tuple(names)
    )


def sync_profile_searches(conn: sqlite3.Connection) -> bool:
    """Maintient les recherches de l'instance sur le profil confirmé, si présent."""
    account = conn.execute(
        "SELECT account_id FROM candidate_profile WHERE completed_at IS NOT NULL "
        "ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    if account is None:
        return False
    intents = profile_intents(conn, int(account["account_id"]))
    if not intents:
        return False
    _sync_intent_searches(conn, intents)
    conn.commit()
    return True


def complete_profile(
    conn: sqlite3.Connection,
    account_id: int,
    workspace_id: int,
    cv_library_ids: list[int],
    rows: object,
) -> list[CareerIntent]:
    if not isinstance(rows, list):
        raise OnboardingError("les pistes doivent être une liste")
    if any(
        not isinstance(value, int) or isinstance(value, bool) for value in cv_library_ids
    ):
        raise OnboardingError("CV invalide")
    if len(cv_library_ids) != len(set(cv_library_ids)):
        raise OnboardingError("un même CV ne peut être sélectionné qu'une fois")
    intents = parse_intents(json.dumps({"intents": rows}, ensure_ascii=False))
    conn.execute("BEGIN IMMEDIATE")
    try:
        for cv_library_id in cv_library_ids:
            cv = conn.execute(
                "SELECT id FROM document_library WHERE id = ? AND type = 'cv'", (cv_library_id,)
            ).fetchone()
            if cv is None:
                raise OnboardingError("CV introuvable dans la bibliothèque")
        primary_cv_id = cv_library_ids[0] if cv_library_ids else None
        conn.execute(
            "INSERT INTO candidate_profile "
            "(account_id, workspace_id, cv_library_id, completed_at) "
            "VALUES (?, ?, ?, datetime('now')) "
            "ON CONFLICT(account_id) DO UPDATE SET cv_library_id = excluded.cv_library_id, "
            "workspace_id = excluded.workspace_id, completed_at = datetime('now'), "
            "updated_at = datetime('now')",
            (account_id, workspace_id, primary_cv_id),
        )
        conn.execute("DELETE FROM candidate_profile_document WHERE account_id = ?", (account_id,))
        conn.executemany(
            "INSERT INTO candidate_profile_document "
            "(account_id, document_library_id, position) VALUES (?, ?, ?)",
            [
                (account_id, cv_library_id, position)
                for position, cv_library_id in enumerate(cv_library_ids)
            ],
        )
        conn.execute("DELETE FROM career_intent WHERE account_id = ?", (account_id,))
        conn.executemany(
            "INSERT INTO career_intent "
            "(account_id, label, keywords_json, exclude_json, position) VALUES (?, ?, ?, ?, ?)",
            [
                (
                    account_id,
                    intent.label,
                    json.dumps(intent.keywords, ensure_ascii=False),
                    json.dumps(intent.exclude, ensure_ascii=False),
                    position,
                )
                for position, intent in enumerate(intents)
            ],
        )
        _sync_intent_searches(conn, intents)
        conn.commit()
    except (OnboardingError, sqlite3.Error):
        conn.rollback()
        raise
    run_matching(conn)
    return intents
