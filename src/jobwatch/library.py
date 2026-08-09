"""Bibliothèque de documents réutilisables (CV, lettres de motivation) pour le dashboard.

Les fichiers sont uploadés en base64 via POST /documents (voir serve.py) et
stockés sous <db>/../documents/, préfixés d'un id court pour éviter les
collisions. Le nom de fichier client n'est jamais utilisé tel quel comme
chemin sur disque.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

DOCUMENT_TYPES = ("cv", "cover_letter")


class LibraryError(Exception):
    """Échec attendu : upload invalide. Le serveur renvoie un 400 avec ce message."""


@dataclass
class StorageMigrationResult:
    """Bilan d'une migration de chemins externes vers le stockage jobwatch."""

    copied: int = 0
    already_managed: int = 0
    missing: list[str] = field(default_factory=list)


def _sanitize_filename(filename: str) -> str:
    """Ne garde que le nom de base, sans composants de répertoire ni traversée."""
    name = PurePosixPath(filename.replace("\\", "/")).name.strip()
    return name if name and name not in (".", "..") else "fichier"


def documents_dir(db_path: Path) -> Path:
    return db_path.parent / "documents"


def _is_managed(path: Path, db_path: Path) -> bool:
    try:
        path.resolve().relative_to(documents_dir(db_path).resolve())
    except (OSError, ValueError):
        return False
    return True


def _source_path(raw_path: str, source_root: Path | None) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute() or source_root is None:
        return path
    return source_root / path


def _managed_copy_path(db_path: Path, source: Path) -> Path:
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:12]
    safe_name = _sanitize_filename(source.name)
    return documents_dir(db_path) / f"import_{digest}_{safe_name}"


def migrate_external_documents(
    conn: sqlite3.Connection,
    db_path: Path,
    source_root: Path | None = None,
) -> StorageMigrationResult:
    """Copie dans jobwatch les documents encore référencés hors de son stockage.

    Les chemins relatifs issus de l'ancien tracker sont résolus depuis ``source_root``.
    L'opération est idempotente : le nom cible dépend du contenu du fichier et les
    références déjà gérées sont laissées intactes.
    """
    result = StorageMigrationResult()
    updates: list[tuple[str, str, int]] = []
    created: list[Path] = []
    tables = (
        ("document_library", "file_path"),
        ("document", "path"),
    )

    try:
        for table, column in tables:
            rows = conn.execute(
                f"SELECT id, {column} AS file_path FROM {table} ORDER BY id"
            ).fetchall()
            for row in rows:
                raw_path = str(row["file_path"])
                stored_path = Path(raw_path).expanduser()
                if stored_path.is_absolute() and _is_managed(stored_path, db_path):
                    result.already_managed += 1
                    continue
                source = _source_path(raw_path, source_root)
                if not source.is_file():
                    result.missing.append(raw_path)
                    continue
                target = _managed_copy_path(db_path, source)
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    shutil.copy2(source, target)
                    created.append(target)
                updates.append((table, str(target.resolve()), int(row["id"])))

        if not updates:
            return result

        conn.execute("BEGIN IMMEDIATE")
        for table, target, row_id in updates:
            column = "file_path" if table == "document_library" else "path"
            conn.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (target, row_id))
        conn.commit()
    except (OSError, sqlite3.Error):
        if conn.in_transaction:
            conn.rollback()
        for path in created:
            path.unlink(missing_ok=True)
        raise
    result.copied = len(updates)
    return result


def list_library(conn: sqlite3.Connection, doc_type: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, label, file_path FROM document_library WHERE type = ? "
        "ORDER BY uploaded_at DESC, id DESC",
        (doc_type,),
    ).fetchall()


def resolve_path(conn: sqlite3.Connection, library_id: int, doc_type: str) -> str | None:
    row = conn.execute(
        "SELECT file_path FROM document_library WHERE id = ? AND type = ?",
        (library_id, doc_type),
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
