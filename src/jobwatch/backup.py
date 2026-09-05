"""Copie vérifiée d'une instance dont serveur et collecte sont arrêtés."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import yaml

from jobwatch.config import load_config


class BackupError(Exception):
    pass


def _hashes(root: Path) -> dict[str, str]:
    result = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise BackupError("les liens symboliques ne sont pas acceptés")
        if path.is_file() and path != root / "manifest.json":
            with path.open("rb") as stream:
                result[str(path.relative_to(root))] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def _relocate(db: Path, old_root: Path, new_root: Path) -> None:
    conn = sqlite3.connect(db)
    try:
        with conn:
            for table, column in (("document_library", "file_path"), ("document", "path"),
                                  ("draft_job", "tex_path"), ("draft_job", "pdf_path")):
                for row_id, value in conn.execute(f"SELECT id, {column} FROM {table}").fetchall():
                    if not value:
                        continue
                    try:
                        relative = Path(value).expanduser().relative_to(old_root)
                    except ValueError as exc:
                        raise BackupError("migrez les documents externes avant la sauvegarde") from exc
                    if ".." in relative.parts or not (new_root / relative).is_file():
                        raise BackupError("document référencé absent ou chemin invalide")
                    conn.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?",
                                 (str(new_root / relative), row_id))
            conn.execute("DELETE FROM web_session")
            conn.execute("DELETE FROM account_invite")
    finally:
        conn.close()


def create_backup(config_path: Path, destination: Path) -> None:
    config = load_config(config_path)
    root = config.db.parent.resolve()
    destination = destination.resolve()
    if destination == root or root in destination.parents:
        raise BackupError("la sauvegarde doit être extérieure au dossier de données")
    _hashes(root)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    data = destination / "data"
    shutil.copytree(root, data, ignore=shutil.ignore_patterns(
        config.db.name, config.db.name + "-wal", config.db.name + "-shm",
        config.db.name + "-journal", "maintenance.lock"))
    data.chmod(0o700)
    source = sqlite3.connect(f"{config.db.resolve().as_uri()}?mode=ro", uri=True)
    target = sqlite3.connect(data / "jobwatch.db")
    try:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError("la vérification SQLite a échoué")
    finally:
        source.close()
        target.close()
    _relocate(data / "jobwatch.db", root, data)
    raw = yaml.safe_load(config_path.read_text())
    raw["db"] = str(data / "jobwatch.db")
    if (raw.get("draft") or {}).get("examples"):
        try:
            raw["draft"]["examples"] = {
                track: [str(data / Path(p).expanduser().relative_to(root)) for p in paths]
                for track, paths in raw["draft"]["examples"].items()
            }
        except ValueError as exc:
            raise BackupError("migrez les exemples externes avant la sauvegarde") from exc
    (destination / "config.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True))
    for path in destination.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    manifest = {"data_root": str(data), "files": _hashes(destination)}
    (destination / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (destination / "manifest.json").chmod(0o600)


def restore_backup(source: Path, destination: Path) -> Path:
    source = source.resolve()
    manifest = json.loads((source / "manifest.json").read_text())
    if _hashes(source) != manifest["files"]:
        raise BackupError("sauvegarde incomplète ou altérée")
    destination = destination.resolve()
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    shutil.copytree(source / "data", destination / "data")
    data = destination / "data"
    _relocate(data / "jobwatch.db", Path(manifest["data_root"]), data)
    raw = yaml.safe_load((source / "config.yaml").read_text())
    raw["db"] = str(data / "jobwatch.db")
    if (raw.get("draft") or {}).get("examples"):
        old = Path(manifest["data_root"])
        raw["draft"]["examples"] = {
            track: [str(data / Path(p).relative_to(old)) for p in paths]
            for track, paths in raw["draft"]["examples"].items()
        }
    target = destination / "config.yaml"
    target.write_text(yaml.safe_dump(raw, allow_unicode=True))
    for path in destination.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    return target
