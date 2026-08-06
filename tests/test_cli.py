"""Tests for the click CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from jobwatch.cli import cli
from jobwatch.db import connect, init_db


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _write_config(tmp_path: Path, db_path: Path) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""db: {db_path}
searches:
  - name: ai-paris
    include: ["AI engineer", "LLM"]
    exclude: ["stage"]
    locations: ["Paris", "remote"]
    contract: permanent
sources: {{}}
notify: {{}}
"""
    )
    return config


def _seed_match(db_path: Path, title: str = "AI Engineer") -> None:
    conn = connect(db_path)
    init_db(conn)
    conn.execute("INSERT OR IGNORE INTO company (name) VALUES ('Acme')")
    company_id = conn.execute("SELECT id FROM company WHERE name = 'Acme'").fetchone()["id"]
    conn.execute("INSERT OR IGNORE INTO source (type, name) VALUES ('test', 'test')")
    source_id = conn.execute("SELECT id FROM source WHERE name = 'test'").fetchone()["id"]
    conn.execute(
        "INSERT INTO offer (source_id, company_id, title, url, platform, location, contract) "
        "VALUES (?, ?, ?, 'https://example.com/job', 'Test', 'Paris', 'permanent')",
        (source_id, company_id, title),
    )
    conn.execute(
        "INSERT INTO search (name, include_json, exclude_json, locations_json, contract, active) "
        "VALUES ('ai-paris', ?, '[]', '[\"Paris\"]', 'permanent', 1)",
        (json.dumps([title]),),
    )
    conn.execute(
        "INSERT INTO match (search_id, offer_id, state) "
        "SELECT s.id, o.id, 'new' FROM search s, offer o "
        "WHERE s.name = 'ai-paris' AND o.title = ?",
        (title,),
    )
    conn.commit()
    conn.close()


def test_init_creates_config_and_db(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    from jobwatch import cli as cli_module

    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "data" / "jw.db"
    monkeypatch.setattr(
        cli_module,
        "example_config_text",
        lambda: f"db: {db_path}\nsearches:\n  - name: s1\n    include: [AI]\n",
    )

    result = runner.invoke(cli, ["init", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    assert config_path.exists()
    assert db_path.exists()
    assert "prochaines étapes" in result.output

    second = runner.invoke(cli, ["init", "--config", str(config_path)])
    assert second.exit_code == 1
    assert "refus d'écraser" in second.output


def test_run_with_no_sources_succeeds(runner: CliRunner, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    config = _write_config(tmp_path, db_path)

    result = runner.invoke(cli, ["run", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "0 nouvelles offres collectées, 0 nouveaux matchs" in result.output


def test_run_without_config_fails_cleanly(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(cli, ["run", "--config", str(tmp_path / "missing.yaml")])
    assert result.exit_code == 1
    assert "erreur :" in result.output


def _apply_flow(runner: CliRunner, config: Path) -> None:
    result = runner.invoke(cli, ["list", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "AI Engineer" in result.output

    result = runner.invoke(cli, ["apply", "1", "--note", "sent cv", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "candidature 1 enregistrée" in result.output

    result = runner.invoke(cli, ["apply", "1", "--config", str(config)])
    assert result.exit_code == 1
    assert "déjà été postulé" in result.output

    result = runner.invoke(
        cli, ["log", "1", "interview", "-m", "phone screen", "--config", str(config)]
    )
    assert result.exit_code == 0, result.output

    result = runner.invoke(cli, ["apps", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "interview" in result.output

    result = runner.invoke(cli, ["show", "1", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "https://example.com/job" in result.output

    result = runner.invoke(cli, ["list", "--state", "applied", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "AI Engineer" in result.output


def test_apply_flow_on_seeded_db(runner: CliRunner, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    _seed_match(db_path)
    config = _write_config(tmp_path, db_path)
    _apply_flow(runner, config)


def test_discard_and_log_errors(runner: CliRunner, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    config = _write_config(tmp_path, db_path)

    result = runner.invoke(cli, ["discard", "999", "--config", str(config)])
    assert result.exit_code == 1
    assert "aucun match avec l'id 999" in result.output

    result = runner.invoke(cli, ["log", "1", "interview", "--config", str(config)])
    assert result.exit_code == 1
    assert "aucune candidature avec l'id 1" in result.output

    result = runner.invoke(cli, ["log", "1", "bogus", "--config", str(config)])
    assert result.exit_code == 2


def test_list_ack_marks_seen(runner: CliRunner, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    _seed_match(db_path)
    config = _write_config(tmp_path, db_path)

    result = runner.invoke(cli, ["list", "--ack", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "1 match(s) marqué(s) comme vus" in result.output

    result = runner.invoke(cli, ["list", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "AI Engineer" not in result.output

    result = runner.invoke(cli, ["list", "--state", "seen", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "AI Engineer" in result.output


def test_list_filters_by_search(runner: CliRunner, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    _seed_match(db_path)
    config = _write_config(tmp_path, db_path)

    result = runner.invoke(cli, ["list", "--search", "nope", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "AI Engineer" not in result.output


def test_show_unknown_match_exits_one(runner: CliRunner, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    config = _write_config(tmp_path, db_path)
    result = runner.invoke(cli, ["show", "1", "--config", str(config)])
    assert result.exit_code == 1
    assert "aucun match avec l'id 1" in result.output


def test_list_ack_with_no_rows_does_not_crash(runner: CliRunner, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    config = _write_config(tmp_path, db_path)
    conn = connect(db_path)
    init_db(conn)
    conn.close()
    result = runner.invoke(cli, ["list", "--config", str(config), "--ack"])
    assert result.exit_code == 0, result.output
