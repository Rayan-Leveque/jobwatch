"""Analyse du CV et persistance des pistes métier confirmées à l'onboarding."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass

from jobwatch import draft
from jobwatch.config import DraftConfig, SearchConfig
from jobwatch.matching import run_matching
from jobwatch.seniority import (
    DEFAULT_MAX_LEVEL,
    DEFAULT_MIN_LEVEL,
    reclassify_recent_matches,
    validate_range,
)

log = logging.getLogger(__name__)

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


MAX_INTENTS = 4
DASHBOARD_TRACKS_KEY = "dashboard_tracks"


@dataclass(frozen=True)
class CareerIntent:
    label: str
    keywords: list[str]
    exclude: list[str]
    intent_id: int | None = None
    search_id: int | None = None


def profile_complete(conn: sqlite3.Connection, account_id: int) -> bool:
    row = conn.execute(
        "SELECT completed_at FROM candidate_profile WHERE account_id = ?", (account_id,)
    ).fetchone()
    return row is not None and row["completed_at"] is not None


def profile_intents(conn: sqlite3.Connection, account_id: int) -> list[CareerIntent]:
    rows = conn.execute(
        "SELECT id, label, keywords_json, exclude_json, search_id FROM career_intent "
        "WHERE account_id = ? AND active = 1 ORDER BY position, id",
        (account_id,),
    ).fetchall()
    return [
        CareerIntent(
            label=str(row["label"]),
            keywords=list(json.loads(str(row["keywords_json"]))),
            exclude=list(json.loads(str(row["exclude_json"]))),
            intent_id=int(row["id"]),
            search_id=None if row["search_id"] is None else int(row["search_id"]),
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
    """Décode la réponse JSON de l'IA, puis valide les pistes proposées."""
    match = _JSON_FENCE_RE.search(response)
    text = match.group(1).strip() if match else response.strip()
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise OnboardingError("l'IA n'a pas renvoyé de pistes valides") from exc
    rows = payload.get("intents") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise OnboardingError("l'IA n'a pas renvoyé de pistes valides")
    return validate_intents(rows, strict=False)


def validate_intents(rows: list[object], *, strict: bool = True) -> list[CareerIntent]:
    """Valide des pistes déjà décodées.

    Le mode strict, utilisé pour la saisie de l'utilisateur, ne laisse rien
    tomber en silence : chaque ligne refusée devient une erreur affichée dans le
    formulaire. La proposition de l'IA reste tolérante et ignore l'inexploitable.
    """
    if strict and len(rows) > MAX_INTENTS:
        raise OnboardingError(f"{MAX_INTENTS} catégories maximum")

    intents: list[CareerIntent] = []
    for row in rows[:MAX_INTENTS]:
        if not isinstance(row, dict):
            if strict:
                raise OnboardingError("catégorie invalide")
            continue
        label = row.get("label")
        keywords = row.get("keywords")
        exclude = row.get("exclude", [])
        intent_id = row.get("id")
        if not isinstance(intent_id, int) or isinstance(intent_id, bool):
            intent_id = None
        if not isinstance(label, str) or not label.strip():
            if strict:
                raise OnboardingError("chaque catégorie doit avoir un nom")
            continue
        if not isinstance(keywords, list) or not all(
            isinstance(keyword, str) and keyword.strip() for keyword in keywords
        ):
            if strict:
                raise OnboardingError("chaque catégorie doit contenir au moins un mot-clé")
            continue
        if not isinstance(exclude, list) or not all(isinstance(term, str) for term in exclude):
            if strict:
                raise OnboardingError("les termes à exclure doivent être du texte")
            continue
        intents.append(
            CareerIntent(
                label=label.strip(),
                keywords=list(dict.fromkeys(keyword.strip() for keyword in keywords))[:8],
                exclude=list(dict.fromkeys(term.strip() for term in exclude if term.strip()))[:8],
                intent_id=intent_id,
            )
        )
    if not intents:
        raise OnboardingError(
            "ajoutez au moins une catégorie avec un mot-clé"
            if strict
            else "l'IA n'a proposé aucune piste exploitable"
        )
    labels: set[str] = set()
    for intent in intents:
        if not intent.keywords:
            raise OnboardingError("chaque catégorie doit contenir au moins un mot-clé")
        key = intent.label.casefold()
        if key in labels:
            raise OnboardingError("les catégories doivent avoir des noms différents")
        labels.add(key)
    return intents


def _sync_intent_searches(
    conn: sqlite3.Connection, account_id: int, intents: list[CareerIntent]
) -> list[int]:
    """Aligne les recherches sur les catégories et renvoie leur id, dans l'ordre.

    La recherche déjà rattachée à une catégorie est renommée sur place : les
    matchs conservent leur recherche, donc le tri déjà fait par l'utilisateur.
    Seules les recherches déjà rattachées à une catégorie de ce compte sont
    touchées : celles de config.yaml et du pont Markdown restent actives.
    """
    owned = {
        int(row["search_id"])
        for row in conn.execute(
            "SELECT DISTINCT search_id FROM career_intent "
            "WHERE account_id = ? AND search_id IS NOT NULL",
            (account_id,),
        )
    }
    searches = [
        SearchConfig(name=intent.label, include=intent.keywords, exclude=intent.exclude)
        for intent in intents
    ]
    resolved: list[int | None] = []
    taken: set[int] = set()
    for intent, search in zip(intents, searches):
        row = None
        if intent.search_id is not None and intent.search_id not in taken:
            row = conn.execute(
                "SELECT id FROM search WHERE id = ?", (intent.search_id,)
            ).fetchone()
        if row is None:
            # Seule une recherche déjà rattachée à une catégorie de ce compte est
            # reprise par son nom : celles de config.yaml et du pont Markdown
            # gardent leurs critères, et le nom est refusé plus bas.
            row = conn.execute(
                "SELECT id FROM search WHERE name = ?", (search.name,)
            ).fetchone()
            if row is not None and (int(row["id"]) in taken or int(row["id"]) not in owned):
                row = None
        search_id = None if row is None else int(row["id"])
        if search_id is not None:
            taken.add(search_id)
        resolved.append(search_id)

    # Une catégorie retirée libère son nom : sa recherche est archivée, jamais
    # supprimée, pour que ses matchs restent en base.
    stale = sorted(owned - taken)

    for search, search_id in zip(searches, resolved):
        clash = conn.execute("SELECT id FROM search WHERE name = ?", (search.name,)).fetchone()
        if (
            clash is not None
            and int(clash["id"]) != search_id
            and int(clash["id"]) not in taken
            and int(clash["id"]) not in stale
        ):
            raise OnboardingError(
                f"« {search.name} » est déjà le nom d'une autre recherche de l'instance, "
                "choisissez-en un autre"
            )

    # Renommage en deux temps : sans nom provisoire, échanger deux noms de
    # catégories heurterait la contrainte d'unicité de search.name.
    conn.executemany(
        "UPDATE search SET name = ? WHERE id = ?",
        [(f"__jobwatch_sync_{search_id}", search_id) for search_id in sorted(taken)],
    )
    if stale:
        conn.execute(
            "UPDATE search SET name = name || ' (archivée ' || id || ')', active = 0, "
            "archived_at = datetime('now') "
            f"WHERE id IN ({','.join('?' for _ in stale)})",
            tuple(stale),
        )

    search_ids: list[int] = []
    for search, search_id in zip(searches, resolved):
        include_json = json.dumps(search.include, ensure_ascii=True, separators=(",", ":"))
        exclude_json = json.dumps(search.exclude, ensure_ascii=True, separators=(",", ":"))
        locations_json = json.dumps(search.locations, ensure_ascii=True, separators=(",", ":"))
        if search_id is None:
            search_id = int(
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
                ).lastrowid
            )
        else:
            conn.execute(
                "UPDATE search SET name = ?, include_json = ?, exclude_json = ?, "
                "locations_json = ?, contract = ?, active = 1, archived_at = NULL "
                "WHERE id = ?",
                (
                    search.name,
                    include_json,
                    exclude_json,
                    locations_json,
                    search.contract,
                    search_id,
                ),
            )
        search_ids.append(search_id)
    return search_ids


def sync_profile_searches(conn: sqlite3.Connection) -> bool:
    """Maintient les recherches de l'instance sur le profil confirmé, si présent."""
    dashboard_tracks = conn.execute(
        "SELECT value FROM instance_setting WHERE key = ?", (DASHBOARD_TRACKS_KEY,)
    ).fetchone()
    if dashboard_tracks is not None and dashboard_tracks["value"] == "split":
        return False
    account = conn.execute(
        "SELECT account_id FROM candidate_profile WHERE completed_at IS NOT NULL "
        "ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    if account is None:
        return False
    account_id = int(account["account_id"])
    intents = profile_intents(conn, account_id)
    if not intents:
        return False
    search_ids = _sync_intent_searches(conn, account_id, intents)
    _store_intent_search_ids(conn, intents, search_ids)
    conn.commit()
    return True


def _store_intent_search_ids(
    conn: sqlite3.Connection, intents: list[CareerIntent], search_ids: list[int]
) -> None:
    conn.executemany(
        "UPDATE career_intent SET search_id = ? WHERE id = ?",
        [
            (search_id, intent.intent_id)
            for intent, search_id in zip(intents, search_ids)
            if intent.intent_id is not None and intent.search_id != search_id
        ],
    )


def complete_profile(
    conn: sqlite3.Connection,
    account_id: int,
    workspace_id: int,
    cv_library_ids: list[int],
    rows: object,
    *,
    seniority_min: int = DEFAULT_MIN_LEVEL,
    seniority_max: int = DEFAULT_MAX_LEVEL,
    cover_letters_enabled: bool = True,
) -> list[CareerIntent]:
    if not isinstance(rows, list):
        raise OnboardingError("les pistes doivent être une liste")
    if any(
        not isinstance(value, int) or isinstance(value, bool) for value in cv_library_ids
    ):
        raise OnboardingError("CV invalide")
    if len(cv_library_ids) != len(set(cv_library_ids)):
        raise OnboardingError("un même CV ne peut être sélectionné qu'une fois")
    try:
        seniority_min, seniority_max = validate_range(seniority_min, seniority_max)
    except ValueError as exc:
        raise OnboardingError(str(exc)) from exc
    if not isinstance(cover_letters_enabled, bool):
        raise OnboardingError("le choix de génération de lettres est invalide")
    intents = validate_intents(rows)
    conn.execute("BEGIN IMMEDIATE")
    try:
        owned = {
            int(row["id"]): (None if row["search_id"] is None else int(row["search_id"]))
            for row in conn.execute(
                "SELECT id, search_id FROM career_intent WHERE account_id = ?", (account_id,)
            )
        }
        intents = [
            CareerIntent(
                label=intent.label,
                keywords=intent.keywords,
                exclude=intent.exclude,
                intent_id=intent.intent_id,
                search_id=owned.get(intent.intent_id) if intent.intent_id is not None else None,
            )
            for intent in intents
        ]
        for cv_library_id in cv_library_ids:
            cv = conn.execute(
                "SELECT id FROM document_library WHERE id = ? AND type = 'cv'", (cv_library_id,)
            ).fetchone()
            if cv is None:
                raise OnboardingError("CV introuvable dans la bibliothèque")
        primary_cv_id = cv_library_ids[0] if cv_library_ids else None
        conn.execute(
            "INSERT INTO candidate_profile "
            "(account_id, workspace_id, cv_library_id, seniority_min, seniority_max, "
            "cover_letters_enabled, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(account_id) DO UPDATE SET cv_library_id = excluded.cv_library_id, "
            "seniority_min = excluded.seniority_min, seniority_max = excluded.seniority_max, "
            "cover_letters_enabled = excluded.cover_letters_enabled, "
            "workspace_id = excluded.workspace_id, completed_at = datetime('now'), "
            "updated_at = datetime('now')",
            (
                account_id,
                workspace_id,
                primary_cv_id,
                seniority_min,
                seniority_max,
                int(cover_letters_enabled),
            ),
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
        dashboard_tracks = conn.execute(
            "SELECT value FROM instance_setting WHERE key = ?", (DASHBOARD_TRACKS_KEY,)
        ).fetchone()
        split_tracks = dashboard_tracks is not None and dashboard_tracks["value"] == "split"
        if split_tracks:
            owned_search_ids = sorted(
                search_id for search_id in owned.values() if search_id is not None
            )
            if owned_search_ids:
                conn.execute(
                    "UPDATE search SET active = 0, archived_at = COALESCE(archived_at, datetime('now')) "
                    f"WHERE id IN ({','.join('?' for _ in owned_search_ids)})",
                    owned_search_ids,
                )
            search_ids: list[int | None] = [None] * len(intents)
        else:
            search_ids = _sync_intent_searches(conn, account_id, intents)
        conn.execute("DELETE FROM career_intent WHERE account_id = ?", (account_id,))
        conn.executemany(
            "INSERT INTO career_intent (account_id, label, keywords_json, exclude_json, "
            "position, search_id) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    account_id,
                    intent.label,
                    json.dumps(intent.keywords, ensure_ascii=False),
                    json.dumps(intent.exclude, ensure_ascii=False),
                    position,
                    search_ids[position],
                )
                for position, intent in enumerate(intents)
            ],
        )
        conn.commit()
    except (OnboardingError, sqlite3.Error):
        conn.rollback()
        raise
    try:
        if not split_tracks:
            run_matching(conn)
        reclassify_recent_matches(conn, account_id)
    except sqlite3.Error:
        # Le profil est déjà enregistré : la mise en correspondance repassera au
        # prochain 'jw run', inutile de faire échouer un enregistrement réussi.
        log.warning("onboarding: matching failed after profile save", exc_info=True)
    return intents
