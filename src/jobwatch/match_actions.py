"""Transitions d'état d'un match : décisions pures, sans HTTP.

Le dashboard (`serve.py`) traduit les résultats en réponses HTTP ; ici on ne
manipule que la connexion SQLite et des valeurs structurées, ce qui permet de
tester later/discard/restore/apply sans serveur.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from jobwatch.applications import ApplicationError, record_application
from jobwatch.library import resolve_path

RESTORE_STATES = ("new", "seen", "later")


@dataclass(frozen=True)
class MatchActionRequest:
    """Entrées validées d'une action sur un match."""

    action: str
    target_state: str | None = None
    cv_library_id: int | None = None
    cover_letter_library_id: int | None = None


@dataclass(frozen=True)
class MatchActionResult:
    """Issue d'une action ; l'adaptateur HTTP mappe kind → statut."""

    kind: Literal["ok", "invalid", "not_found", "conflict"]
    error: str | None = None


def parse_match_action(action: str, body: object) -> MatchActionRequest | MatchActionResult:
    """Valide les champs du corps JSON de ``restore`` et ``apply``."""
    if action == "restore":
        target_state = body.get("state") if isinstance(body, dict) else None
        if target_state not in RESTORE_STATES:
            return MatchActionResult("invalid", "état de restauration invalide")
        return MatchActionRequest(action, target_state=target_state)
    if action == "apply":
        fields = body if isinstance(body, dict) else {}
        library_ids: list[int | None] = []
        for key in ("cv_library_id", "cover_letter_library_id"):
            value = fields.get(key)
            if value in (None, ""):
                library_ids.append(None)
            elif isinstance(value, int) and not isinstance(value, bool):
                library_ids.append(value)
            else:
                return MatchActionResult("invalid", f"champ {key} invalide")
        return MatchActionRequest(
            action, cv_library_id=library_ids[0], cover_letter_library_id=library_ids[1]
        )
    return MatchActionRequest(action)


def apply_match_action(
    conn: sqlite3.Connection, match_id: int, request: MatchActionRequest
) -> MatchActionResult:
    """Applique l'action au match et commit."""
    match_row = conn.execute("SELECT state FROM match WHERE id = ?", (match_id,)).fetchone()
    if match_row is None:
        return MatchActionResult("not_found")
    if request.action == "apply":
        if match_row["state"] == "discarded":
            return MatchActionResult("conflict", "match écarté : restaurez-le d'abord")
        cv_path = (
            resolve_path(conn, request.cv_library_id, "cv")
            if request.cv_library_id is not None
            else None
        )
        cover_letter_path = (
            resolve_path(conn, request.cover_letter_library_id, "cover_letter")
            if request.cover_letter_library_id is not None
            else None
        )
        try:
            record_application(
                conn, match_id,
                cv_path=cv_path, cover_letter_path=cover_letter_path,
            )
        except ApplicationError as exc:
            return MatchActionResult("conflict", str(exc))
    elif request.action == "later":
        conn.execute(
            "UPDATE match SET state = 'later', discarded_at = NULL WHERE id = ?",
            (match_id,),
        )
    elif request.action == "discard":
        conn.execute(
            "UPDATE match SET state = 'discarded', "
            "discarded_at = datetime('now') WHERE id = ?",
            (match_id,),
        )
    else:
        conn.execute(
            "UPDATE match SET state = ?, discarded_at = NULL WHERE id = ?",
            (request.target_state, match_id),
        )
    conn.commit()
    return MatchActionResult("ok")
