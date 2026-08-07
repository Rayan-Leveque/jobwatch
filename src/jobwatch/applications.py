"""Enregistrement d'une candidature : logique partagée entre la CLI et le dashboard.

`jw apply` et la route POST /match/<id>/apply passent tous deux par
`record_application`, garantissant le même effet en base quel que soit le
chemin d'entrée.
"""

from __future__ import annotations

import sqlite3


class ApplicationError(Exception):
    """Échec attendu : match inexistant ou déjà postulé."""


def record_application(
    conn: sqlite3.Connection,
    match_id: int,
    note: str | None = None,
    cv_path: str | None = None,
    cover_letter_path: str | None = None,
) -> int:
    """Crée la candidature, son événement 'applied', les documents éventuels,
    et passe le match à l'état 'applied'. Renvoie l'id de la candidature."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        match = conn.execute(
            "SELECT id, offer_id FROM match WHERE id = ?", (match_id,)
        ).fetchone()
        if match is None:
            raise ApplicationError(f"aucun match avec l'id {match_id}")
        existing = conn.execute(
            "SELECT id FROM application WHERE match_id = ?", (match_id,)
        ).fetchone()
        if existing is not None:
            raise ApplicationError(
                f"le match {match_id} a déjà été postulé (candidature {existing['id']})"
            )
    except ApplicationError:
        conn.rollback()
        raise
    cur = conn.execute(
        "INSERT INTO application (match_id, offer_id, note) VALUES (?, ?, ?)",
        (match_id, match["offer_id"], note),
    )
    application_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO event (application_id, type, comment) VALUES (?, 'applied', ?)",
        (application_id, note),
    )
    for doc_type, path in (("cv", cv_path), ("cover_letter", cover_letter_path)):
        if path:
            conn.execute(
                "INSERT INTO document (application_id, type, path) VALUES (?, ?, ?)",
                (application_id, doc_type, path),
            )
    conn.execute("UPDATE match SET state = 'applied' WHERE id = ?", (match_id,))
    conn.commit()
    return application_id
