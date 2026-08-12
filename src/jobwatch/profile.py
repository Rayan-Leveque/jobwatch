"""Contexte personnel facultatif utilisé pour personnaliser les lettres."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass

MAX_PROFILE_FIELD_LENGTH = 3_000


class ProfileError(Exception):
    """Saisie de profil invalide destinée à être affichée dans le formulaire."""


@dataclass(frozen=True)
class ProfileDetails:
    motivations: str = ""
    targets: str = ""
    highlights: str = ""
    preferred_tone: str = ""
    constraints_text: str = ""
    reusable_details: str = ""

    @property
    def has_personalization(self) -> bool:
        return any(asdict(self).values())


PROFILE_COLUMNS = tuple(ProfileDetails.__dataclass_fields__)


def _clean_value(value: object, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProfileError(f"{label} doit être du texte")
    cleaned = value.strip()
    if len(cleaned) > MAX_PROFILE_FIELD_LENGTH:
        raise ProfileError(
            f"{label} dépasse {MAX_PROFILE_FIELD_LENGTH} caractères"
        )
    return cleaned


def profile_details(conn: sqlite3.Connection, account_id: int) -> ProfileDetails:
    columns = ", ".join(PROFILE_COLUMNS)
    row = conn.execute(
        f"SELECT {columns} FROM candidate_profile WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    if row is None:
        return ProfileDetails()
    return ProfileDetails(**{column: str(row[column] or "") for column in PROFILE_COLUMNS})


def save_profile_details(
    conn: sqlite3.Connection,
    account_id: int,
    workspace_id: int,
    payload: object,
) -> ProfileDetails:
    """Valide puis enregistre uniquement le profil du compte connecté."""
    if not isinstance(payload, dict):
        raise ProfileError("profil invalide")
    owner = conn.execute(
        "SELECT 1 FROM candidate_profile "
        "WHERE account_id = ? AND workspace_id = ? AND completed_at IS NOT NULL",
        (account_id, workspace_id),
    ).fetchone()
    if owner is None:
        raise ProfileError("terminez d'abord votre recherche de postes")
    labels = {
        "motivations": "vos motivations",
        "targets": "vos cibles",
        "highlights": "vos réalisations",
        "preferred_tone": "le ton préféré",
        "constraints_text": "vos contraintes",
        "reusable_details": "vos informations réutilisables",
    }
    values = {
        column: _clean_value(payload.get(column), labels[column])
        for column in PROFILE_COLUMNS
    }
    conn.execute(
        "UPDATE candidate_profile SET motivations = ?, targets = ?, highlights = ?, "
        "preferred_tone = ?, constraints_text = ?, reusable_details = ?, "
        "updated_at = datetime('now') WHERE account_id = ? AND workspace_id = ?",
        (*values.values(), account_id, workspace_id),
    )
    conn.commit()
    return ProfileDetails(**values)


def draft_profile_context(conn: sqlite3.Connection) -> str | None:
    """Renvoie le contexte du seul profil de l'instance, jamais un profil ambigu."""
    columns = ", ".join(PROFILE_COLUMNS)
    rows = conn.execute(
        f"SELECT {columns} FROM candidate_profile "
        "WHERE completed_at IS NOT NULL ORDER BY account_id LIMIT 2"
    ).fetchall()
    if len(rows) != 1:
        return None
    details = ProfileDetails(
        **{column: str(rows[0][column] or "") for column in PROFILE_COLUMNS}
    )
    sections = (
        ("Motivations", details.motivations),
        ("Postes ou entreprises ciblés", details.targets),
        ("Projets et réalisations à valoriser", details.highlights),
        ("Ton préféré", details.preferred_tone),
        ("Contraintes à respecter", details.constraints_text),
        ("Informations personnelles réutilisables", details.reusable_details),
    )
    populated = [f"## {label}\n\n{value}" for label, value in sections if value]
    return "\n\n".join(populated) or None
