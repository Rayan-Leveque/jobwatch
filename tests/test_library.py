"""Tests for jobwatch.library : bibliothèque de documents réutilisables."""

from __future__ import annotations

import base64
import sqlite3
from pathlib import Path

import pytest

from jobwatch.db import connect, init_db
from jobwatch.library import (
    LibraryError,
    documents_dir,
    list_library,
    migrate_external_documents,
    resolve_path,
    save_upload,
)


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = connect(":memory:")
    init_db(connection)
    yield connection
    connection.close()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def test_save_upload_writes_file_and_library_row(conn: sqlite3.Connection, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    entry = save_upload(conn, db_path, "cv", "Mon CV", "cv.pdf", _b64(b"contenu pdf"))
    assert entry["type"] == "cv"
    assert entry["label"] == "Mon CV"
    file_path = Path(entry["file_path"])
    assert file_path.parent == documents_dir(db_path)
    assert file_path.read_bytes() == b"contenu pdf"
    assert file_path.name.endswith("_cv.pdf")


def test_save_upload_blank_label_falls_back_to_sanitized_filename(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    db_path = tmp_path / "jw.db"
    entry = save_upload(conn, db_path, "cv", "  ", "mon cv final.pdf", _b64(b"x"))
    assert entry["label"] == "mon cv final.pdf"


def test_save_upload_sanitizes_path_traversal_filename(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    db_path = tmp_path / "jw.db"
    entry = save_upload(conn, db_path, "cv", None, "../../etc/passwd", _b64(b"x"))
    file_path = Path(entry["file_path"])
    assert file_path.parent == documents_dir(db_path)
    assert file_path.name.endswith("_passwd")
    assert ".." not in file_path.parts


def test_save_upload_sanitizes_absolute_path_filename(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    db_path = tmp_path / "jw.db"
    entry = save_upload(conn, db_path, "cv", None, "/etc/passwd", _b64(b"x"))
    file_path = Path(entry["file_path"])
    assert file_path.parent == documents_dir(db_path)
    assert file_path.name.endswith("_passwd")


def test_save_upload_two_uploads_same_filename_do_not_collide(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    db_path = tmp_path / "jw.db"
    first = save_upload(conn, db_path, "cv", "A", "cv.pdf", _b64(b"un"))
    second = save_upload(conn, db_path, "cv", "B", "cv.pdf", _b64(b"deux"))
    assert first["file_path"] != second["file_path"]
    assert Path(first["file_path"]).read_bytes() == b"un"
    assert Path(second["file_path"]).read_bytes() == b"deux"


def test_save_upload_rejects_invalid_type(conn: sqlite3.Connection, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    with pytest.raises(LibraryError):
        save_upload(conn, db_path, "resume", None, "cv.pdf", _b64(b"x"))


def test_save_upload_rejects_invalid_base64(conn: sqlite3.Connection, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    with pytest.raises(LibraryError):
        save_upload(conn, db_path, "cv", None, "cv.pdf", "not base64 at all!!")


def test_list_library_filters_by_type_and_orders_newest_first(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    db_path = tmp_path / "jw.db"
    save_upload(conn, db_path, "cv", "CV 1", "a.pdf", _b64(b"1"))
    save_upload(conn, db_path, "cover_letter", "LM 1", "b.pdf", _b64(b"2"))
    save_upload(conn, db_path, "cv", "CV 2", "c.pdf", _b64(b"3"))

    cv_rows = list_library(conn, "cv")
    assert [r["label"] for r in cv_rows] == ["CV 2", "CV 1"]
    letter_rows = list_library(conn, "cover_letter")
    assert [r["label"] for r in letter_rows] == ["LM 1"]


def test_resolve_path_returns_none_for_unknown_id(conn: sqlite3.Connection) -> None:
    assert resolve_path(conn, 999, "cv") is None


def test_resolve_path_returns_file_path(conn: sqlite3.Connection, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    entry = save_upload(conn, db_path, "cv", "CV", "a.pdf", _b64(b"x"))
    assert resolve_path(conn, entry["id"], "cv") == entry["file_path"]


def test_resolve_path_returns_none_for_mismatched_type(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    db_path = tmp_path / "jw.db"
    entry = save_upload(conn, db_path, "cv", "CV", "a.pdf", _b64(b"x"))
    assert resolve_path(conn, entry["id"], "cover_letter") is None


def test_migrate_external_documents_copies_absolute_and_relative_paths(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    db_path = tmp_path / "instance" / "jobwatch.db"
    legacy_root = tmp_path / "legacy"
    cv = legacy_root / "cv.pdf"
    letter = legacy_root / "documents" / "letter.tex"
    letter.parent.mkdir(parents=True)
    cv.write_bytes(b"cv")
    letter.write_bytes(b"letter")
    conn.execute(
        "INSERT INTO document_library (type, label, file_path) VALUES ('cv', 'CV', ?)",
        (str(cv),),
    )
    conn.execute("INSERT INTO source (type, name) VALUES ('test', 'test')")
    conn.execute("INSERT INTO company (name) VALUES ('Acme')")
    conn.execute(
        "INSERT INTO offer (source_id, company_id, title, url) VALUES (1, 1, 'IA', 'https://x')"
    )
    conn.execute("INSERT INTO application (offer_id) VALUES (1)")
    conn.execute(
        "INSERT INTO document (application_id, type, path) "
        "VALUES (1, 'cover_letter', 'documents/letter.tex')"
    )
    conn.commit()

    first = migrate_external_documents(conn, db_path, legacy_root)
    assert first.copied == 2
    assert first.missing == []
    paths = [
        Path(row[0])
        for row in conn.execute(
            "SELECT file_path FROM document_library UNION ALL SELECT path FROM document"
        )
    ]
    assert all(path.parent == documents_dir(db_path) for path in paths)
    assert {path.read_bytes() for path in paths} == {b"cv", b"letter"}

    second = migrate_external_documents(conn, db_path, legacy_root)
    assert second.copied == 0
    assert second.already_managed == 2


def test_migrate_external_documents_reports_missing_without_changing_path(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    db_path = tmp_path / "instance" / "jobwatch.db"
    conn.execute(
        "INSERT INTO document_library (type, label, file_path) "
        "VALUES ('cv', 'Absent', 'missing.pdf')"
    )
    conn.commit()

    result = migrate_external_documents(conn, db_path, tmp_path / "legacy")
    assert result.copied == 0
    assert result.missing == ["missing.pdf"]
    assert conn.execute("SELECT file_path FROM document_library").fetchone()[0] == "missing.pdf"
