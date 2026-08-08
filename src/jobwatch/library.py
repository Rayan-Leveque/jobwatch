"""Bibliothèque de documents réutilisables (CV, lettres de motivation) pour le dashboard.

Les fichiers sont uploadés en base64 via POST /documents (voir serve.py) et
stockés sous <db>/../documents/, préfixés d'un id court pour éviter les
collisions. Le nom de fichier client n'est jamais utilisé tel quel comme
chemin sur disque.
"""

from __future__ import annotations

import base64
import binascii
import sqlite3
import uuid
from pathlib import Path, PurePosixPath

DOCUMENT_TYPES = ("cv", "cover_letter")


class LibraryError(Exception):
    """Échec attendu : upload invalide. Le serveur renvoie un 400 avec ce message."""


def _sanitize_filename(filename: str) -> str:
    """Ne garde que le nom de base, sans composants de répertoire ni traversée."""
    name = PurePosixPath(filename.replace("\\", "/")).name.strip()
    return name if name and name not in (".", "..") else "fichier"


def documents_dir(db_path: Path) -> Path:
    return db_path.parent / "documents"


def list_library(conn: sqlite3.Connection, doc_type: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, label, file_path FROM document_library WHERE type = ? "
        "ORDER BY uploaded_at DESC, id DESC",
        (doc_type,),
    ).fetchall()


def resolve_path(conn: sqlite3.Connection, library_id: int) -> str | None:
    row = conn.execute(
        "SELECT file_path FROM document_library WHERE id = ?", (library_id,)
    ).fetchone()
    return row["file_path"] if row else None


def save_upload(
    conn: sqlite3.Connection,
    db_path: Path,
    doc_type: str,
    label: str | None,
    filename: str,
    content_base64: str,
) -> sqlite3.Row:
    """Décode le contenu, écrit le fichier sur disque et enregistre l'entrée de bibliothèque."""
    if doc_type not in DOCUMENT_TYPES:
        raise LibraryError(f"type de document invalide : {doc_type!r}")
    if not isinstance(filename, str) or not filename.strip():
        raise LibraryError("nom de fichier manquant")
    try:
        content = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise LibraryError(f"contenu base64 invalide : {exc}") from exc

    safe_name = _sanitize_filename(filename)
    target_dir = documents_dir(db_path)
    target_dir.mkdir(parents=True, exist_ok=True)
    file_path = target_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"
    file_path.write_bytes(content)

    final_label = (label or "").strip() or safe_name
    cur = conn.execute(
        "INSERT INTO document_library (type, label, file_path) VALUES (?, ?, ?)",
        (doc_type, final_label, str(file_path)),
    )
    conn.commit()
    return conn.execute(
        "SELECT id, type, label, file_path FROM document_library WHERE id = ?",
        (cur.lastrowid,),
    ).fetchone()
