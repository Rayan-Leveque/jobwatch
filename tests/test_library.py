"""Tests for jobwatch.library : bibliothèque de documents réutilisables."""

from __future__ import annotations

import base64
import sqlite3
from pathlib import Path

import pytest

from jobwatch.db import connect, init_db
from jobwatch.library import (
    MAX_UPLOAD_BASE64_CHARS,
    LibraryError,
    documents_dir,
    examples_dir,
    list_library,
    migrate_draft_examples,
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
    data = b"%PDF-1.4\ncontenu pdf"
    entry = save_upload(conn, db_path, "cv", "Mon CV", "cv.pdf", _b64(data))
    assert entry["type"] == "cv"
    assert entry["label"] == "Mon CV"
    file_path = Path(entry["file_path"])
    assert file_path.parent == documents_dir(db_path)
    assert file_path.read_bytes() == data
    assert file_path.name.endswith("_cv.pdf")


def test_save_upload_blank_label_falls_back_to_sanitized_filename(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    db_path = tmp_path / "jw.db"
    entry = save_upload(conn, db_path, "cv", "  ", "mon cv final.pdf", _b64(b"%PDF-1.4\nx"))
    assert entry["label"] == "mon cv final.pdf"


def test_save_upload_sanitizes_path_traversal_filename(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    db_path = tmp_path / "jw.db"
    entry = save_upload(conn, db_path, "cv", None, "../../etc/passwd", _b64(b"%PDF-1.4\nx"))
    file_path = Path(entry["file_path"])
    assert file_path.parent == documents_dir(db_path)
    assert file_path.name.endswith("_passwd")
    assert ".." not in file_path.parts


def test_save_upload_sanitizes_absolute_path_filename(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    db_path = tmp_path / "jw.db"
    entry = save_upload(conn, db_path, "cv", None, "/etc/passwd", _b64(b"%PDF-1.4\nx"))
    file_path = Path(entry["file_path"])
    assert file_path.parent == documents_dir(db_path)
    assert file_path.name.endswith("_passwd")


def test_save_upload_two_uploads_same_filename_do_not_collide(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    db_path = tmp_path / "jw.db"
    first_data = b"%PDF-1.4\nun"
    second_data = b"%PDF-1.4\ndeux"
    first = save_upload(conn, db_path, "cv", "A", "cv.pdf", _b64(first_data))
    second = save_upload(conn, db_path, "cv", "B", "cv.pdf", _b64(second_data))
    assert first["file_path"] != second["file_path"]
    assert Path(first["file_path"]).read_bytes() == first_data
    assert Path(second["file_path"]).read_bytes() == second_data


def test_save_upload_rejects_invalid_type(conn: sqlite3.Connection, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    with pytest.raises(LibraryError):
        save_upload(conn, db_path, "resume", None, "cv.pdf", _b64(b"x"))


def test_save_upload_rejects_invalid_base64(conn: sqlite3.Connection, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    with pytest.raises(LibraryError):
        save_upload(conn, db_path, "cv", None, "cv.pdf", "not base64 at all!!")


def test_save_upload_rejects_non_pdf_cv(conn: sqlite3.Connection, tmp_path: Path) -> None:
    with pytest.raises(LibraryError, match="fichier PDF"):
        save_upload(conn, tmp_path / "jw.db", "cv", None, "cv.pdf", _b64(b"not pdf"))


def test_save_upload_accepts_tex_letter_example(conn: sqlite3.Connection, tmp_path: Path) -> None:
    entry = save_upload(
        conn, tmp_path / "jw.db", "letter_example", "Mon style",
        "lettre.tex", _b64(b"\\documentclass{article}"),
    )
    assert entry["type"] == "letter_example"
    assert Path(entry["file_path"]).name.endswith("_lettre.tex")


def test_save_upload_rejects_non_tex_letter_example(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    with pytest.raises(LibraryError, match="\\.tex"):
        save_upload(
            conn, tmp_path / "jw.db", "letter_example", None, "lettre.pdf", _b64(b"%PDF-1.4\nx")
        )


def test_save_upload_rejects_oversized_base64_before_decoding(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    with pytest.raises(LibraryError, match="10 Mo maximum"):
        save_upload(
            conn,
            tmp_path / "jw.db",
            "cover_letter",
            None,
            "letter.pdf",
            "A" * (MAX_UPLOAD_BASE64_CHARS + 1),
        )


def test_list_library_filters_by_type_and_orders_newest_first(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    db_path = tmp_path / "jw.db"
    save_upload(conn, db_path, "cv", "CV 1", "a.pdf", _b64(b"%PDF-1.4\n1"))
    save_upload(conn, db_path, "cover_letter", "LM 1", "b.pdf", _b64(b"2"))
    save_upload(conn, db_path, "cv", "CV 2", "c.pdf", _b64(b"%PDF-1.4\n3"))

    cv_rows = list_library(conn, "cv")
    assert [r["label"] for r in cv_rows] == ["CV 2", "CV 1"]
    letter_rows = list_library(conn, "cover_letter")
    assert [r["label"] for r in letter_rows] == ["LM 1"]


def test_resolve_path_returns_none_for_unknown_id(conn: sqlite3.Connection) -> None:
    assert resolve_path(conn, 999, "cv") is None


def test_resolve_path_returns_file_path(conn: sqlite3.Connection, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    entry = save_upload(conn, db_path, "cv", "CV", "a.pdf", _b64(b"%PDF-1.4\nx"))
    assert resolve_path(conn, entry["id"], "cv") == entry["file_path"]


def test_resolve_path_returns_none_for_mismatched_type(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    db_path = tmp_path / "jw.db"
    entry = save_upload(conn, db_path, "cv", "CV", "a.pdf", _b64(b"%PDF-1.4\nx"))
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


def test_migrate_draft_examples_preserves_yaml_and_is_idempotent(tmp_path: Path) -> None:
    source_root = tmp_path / "legacy"
    source_root.mkdir()
    source = source_root / "letter.tex"
    source.write_text("lettre exemple", encoding="utf-8")
    instance = tmp_path / "instance"
    instance.mkdir()
    config_path = instance / "config.yaml"
    original = (
        "# commentaire conservé\n"
        "draft:\n"
        "  model: test\n"
        "  examples:\n"
        "    engineer:\n"
        "      - letter.tex  # exemple IA\n"
    )
    config_path.write_text(original, encoding="utf-8")
    db_path = instance / "jobwatch.db"

    first = migrate_draft_examples(config_path, db_path, source_root)

    assert first.copied == 1
    assert first.missing == []
    updated = config_path.read_text(encoding="utf-8")
    assert "# commentaire conservé" in updated
    assert "# exemple IA" in updated
    target = next(examples_dir(db_path).iterdir())
    assert target.read_text(encoding="utf-8") == "lettre exemple"
    assert str(target.resolve()) in updated

    second = migrate_draft_examples(config_path, db_path, source_root)
    assert second.copied == 0
    assert second.already_managed == 1
    assert config_path.read_text(encoding="utf-8") == updated
