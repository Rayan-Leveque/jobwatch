"""Tests for the click CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from jobwatch.cli import cli
from jobwatch.db import connect, init_db

SAMPLE_JSON = {
    "li-1": {
        "title": "Ingénieur IA",
        "company": "DxO Labs",
        "url": "https://www.linkedin.com/jobs/view/4447379908",
        "source": "linkedin",
        "first_seen": "2026-08-06",
    },
    "li-2": {
        "title": "GenAI Engineer",
        "company": "SFEIR",
        "url": "https://www.linkedin.com/jobs/view/4326775827",
        "source": "linkedin",
        "first_seen": "2026-08-06",
    },
}

SAMPLE_DIGEST = """# Veille emploi - 2026-08-06

## Fit high

| Fit | Poste | Entreprise | Lieu | XP demandée | Source | URL |
|---|---|---|---|---|---|---|
| high | Ingénieur IA | DxO Labs | Boulogne-Billancourt | non précisée | LinkedIn | https://www.linkedin.com/jobs/view/4447379908 |
| high | GenAI Engineer | SFEIR | Paris | non précisée | LinkedIn | https://www.linkedin.com/jobs/view/4326775827 |

## Fit medium

| Fit | Poste | Entreprise | Lieu | XP demandée | Source | URL |
|---|---|---|---|---|---|---|
| medium | AI Engineer H/F | LCL | Villejuif | non précisée | LinkedIn | https://www.linkedin.com/jobs/view/4440973597 |

*Note ignorée.*
"""

SAMPLE_TRACKER = """# Suivi candidatures - Test

## Candidatures manuelles - secteur public

| Envoyé | Date | Fit | Employeur | Poste (réf.) | Deadline | CV | LDM |
|---|---|---|---|---|---|---|---|
| [x] | 2026-08-05 | **high** | CNIL | Ingénieur IA - Service (2026-2346240) | 03/09/2026 | documents/cv/cv_cnil.pdf | documents/cover_letters/cover_cnil.tex |
| [ ] | | medium | DGE (Bercy) | Chef de projets IA (MEF_2026-32117) | 22/08/2026 | à faire | à faire |
"""


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


def test_init_then_run_with_unmodified_example_makes_no_network_calls(
    runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "data" / "jw.db"

    result = runner.invoke(cli, ["init", "--config", str(config_path)])
    assert result.exit_code == 0, result.output

    text = config_path.read_text()
    config_path.write_text(text.replace("~/.local/share/jobwatch/jobwatch.db", str(db_path)))

    def _no_network(*args, **kwargs):
        raise AssertionError("aucun appel réseau attendu avec la config d'exemple")

    monkeypatch.setattr("jobwatch.collectors.httpx.Client", _no_network)
    monkeypatch.setattr("jobwatch.digest.httpx.Client", _no_network)

    result = runner.invoke(cli, ["run", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    assert "0 nouvelles offres collectées, 0 nouveaux matchs" in result.output


def test_run_rejects_placeholder_france_travail_credentials(
    runner: CliRunner, tmp_path: Path
) -> None:
    db_path = tmp_path / "jw.db"
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""db: {db_path}
searches:
  - name: ai-paris
    include: ["AI"]
sources:
  france_travail:
    client_id: YOUR_CLIENT_ID
    client_secret: YOUR_CLIENT_SECRET
    keywords: "IA"
"""
    )

    result = runner.invoke(cli, ["run", "--config", str(config)])
    assert result.exit_code == 1
    assert "identifiants factices" in result.output


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


# --- ingest-daily ---------------------------------------------------------


def _json_file(tmp_path: Path) -> Path:
    path = tmp_path / "daily.json"
    path.write_text(json.dumps(SAMPLE_JSON))
    return path


def _digest_file(tmp_path: Path) -> Path:
    path = tmp_path / "daily.md"
    path.write_text(SAMPLE_DIGEST)
    return path


def test_ingest_daily_requires_an_artifact(runner: CliRunner, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    config = _write_config(tmp_path, db_path)

    result = runner.invoke(cli, ["ingest-daily", "--config", str(config)])
    assert result.exit_code == 1
    assert "au moins l'un de --api-json ou --digest est requis" in result.output


def test_ingest_daily_missing_json_is_usage_error(runner: CliRunner, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    config = _write_config(tmp_path, db_path)

    result = runner.invoke(
        cli, ["ingest-daily", "--api-json", str(tmp_path / "absent.json"), "--config", str(config)]
    )
    assert result.exit_code == 2
    assert "absent.json" in result.output


def test_ingest_daily_without_config_fails_cleanly(runner: CliRunner, tmp_path: Path) -> None:
    _json_file(tmp_path)
    result = runner.invoke(cli, ["ingest-daily", "--api-json", str(tmp_path / "daily.json"),
                                 "--config", str(tmp_path / "missing.yaml")])
    assert result.exit_code == 1
    assert "erreur :" in result.output


def test_ingest_daily_json_only_success(runner: CliRunner, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    config = _write_config(tmp_path, db_path)
    json_path = _json_file(tmp_path)

    result = runner.invoke(
        cli, ["ingest-daily", "--api-json", str(json_path), "--config", str(config)]
    )
    assert result.exit_code == 0, result.output
    assert "2 offre(s) créée(s)" in result.output
    assert "0 déjà présente(s)" in result.output
    assert "2 match(s) créé(s)" in result.output
    assert "0 fit(s) mis à jour" in result.output


def test_ingest_daily_digest_only_success(runner: CliRunner, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    config = _write_config(tmp_path, db_path)
    digest_path = _digest_file(tmp_path)

    result = runner.invoke(cli, ["ingest-daily", "--digest", str(digest_path), "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "3 offre(s) créée(s)" in result.output
    assert "3 match(s) créé(s)" in result.output


def test_ingest_daily_merge_json_and_digest(runner: CliRunner, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    config = _write_config(tmp_path, db_path)
    json_path = _json_file(tmp_path)
    digest_path = _digest_file(tmp_path)

    result = runner.invoke(
        cli,
        ["ingest-daily", "--api-json", str(json_path), "--digest", str(digest_path),
         "--config", str(config)],
    )
    assert result.exit_code == 0, result.output
    assert "3 offre(s) créée(s)" in result.output
    assert "0 déjà présente(s)" in result.output
    assert "3 match(s) créé(s)" in result.output

    conn = connect(db_path)
    init_db(conn)
    assert conn.execute("SELECT count(*) FROM offer").fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM match").fetchone()[0] == 3
    conn.close()


def test_ingest_daily_is_idempotent(runner: CliRunner, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    config = _write_config(tmp_path, db_path)
    json_path = _json_file(tmp_path)
    digest_path = _digest_file(tmp_path)
    args = ["ingest-daily", "--api-json", str(json_path), "--digest", str(digest_path),
            "--config", str(config)]

    first = runner.invoke(cli, args)
    assert first.exit_code == 0, first.output

    second = runner.invoke(cli, args)
    assert second.exit_code == 0, second.output
    assert "0 offre(s) créée(s)" in second.output
    assert "3 déjà présente(s)" in second.output
    assert "0 match(s) créé(s)" in second.output
    assert "0 fit(s) mis à jour" in second.output


def test_ingest_daily_invalid_json_leaves_no_artifacts(runner: CliRunner, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    config = _write_config(tmp_path, db_path)
    bad = tmp_path / "bad.json"
    bad.write_text("{ nope")

    result = runner.invoke(cli, ["ingest-daily", "--api-json", str(bad), "--config", str(config)])
    assert result.exit_code == 1
    assert "erreur :" in result.output
    assert "JSON invalide" in result.output
    assert "Traceback" not in result.output

    conn = connect(db_path)
    init_db(conn)
    for table in ("offer", "match", "search", "source", "company"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    conn.close()


def test_ingest_daily_custom_search_name(runner: CliRunner, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    config = _write_config(tmp_path, db_path)
    json_path = _json_file(tmp_path)

    result = runner.invoke(
        cli, ["ingest-daily", "--api-json", str(json_path), "--search-name", "ma-veille",
              "--config", str(config)]
    )
    assert result.exit_code == 0, result.output

    conn = connect(db_path)
    init_db(conn)
    assert conn.execute("SELECT count(*) FROM search WHERE name = 'ma-veille'").fetchone()[0] == 1
    conn.close()


# --- import-md ------------------------------------------------------------


def test_import_md_missing_file_is_usage_error(runner: CliRunner, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    config = _write_config(tmp_path, db_path)

    result = runner.invoke(cli, ["import-md", str(tmp_path / "absent.md"), "--config", str(config)])
    assert result.exit_code == 2
    assert "absent.md" in result.output


def test_import_md_without_config_fails_cleanly(runner: CliRunner, tmp_path: Path) -> None:
    tracker = tmp_path / "suivi.md"
    tracker.write_text(SAMPLE_TRACKER)
    result = runner.invoke(
        cli, ["import-md", str(tracker), "--config", str(tmp_path / "missing.yaml")]
    )
    assert result.exit_code == 1
    assert "erreur :" in result.output


def test_import_md_success(runner: CliRunner, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    config = _write_config(tmp_path, db_path)
    tracker = tmp_path / "suivi.md"
    tracker.write_text(SAMPLE_TRACKER)

    result = runner.invoke(cli, ["import-md", str(tracker), "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "2 ligne(s) importée(s)" in result.output
    assert "2 offre(s) créée(s)" in result.output
    assert "2 match(s) créé(s)" in result.output
    assert "1 candidature(s) créée(s)" in result.output
    assert "2 document(s) créé(s)" in result.output
    assert "0 déjà présente(s)" in result.output


def test_import_md_is_idempotent(runner: CliRunner, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    config = _write_config(tmp_path, db_path)
    tracker = tmp_path / "suivi.md"
    tracker.write_text(SAMPLE_TRACKER)
    args = ["import-md", str(tracker), "--config", str(config)]

    first = runner.invoke(cli, args)
    assert first.exit_code == 0, first.output

    second = runner.invoke(cli, args)
    assert second.exit_code == 0, second.output
    assert "0 offre(s) créée(s)" in second.output
    assert "0 match(s) créé(s)" in second.output
    assert "0 candidature(s) créée(s)" in second.output
    assert "0 document(s) créé(s)" in second.output
    assert "2 déjà présente(s)" in second.output

    conn = connect(db_path)
    init_db(conn)
    assert conn.execute("SELECT count(*) FROM offer").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM match").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM application").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM document").fetchone()[0] == 2
    conn.close()


def test_import_md_invalid_line_fails_cleanly(runner: CliRunner, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    config = _write_config(tmp_path, db_path)
    bad = tmp_path / "suivi.md"
    bad.write_text(
        "# Suivi\n\n## Section\n\n"
        "| Envoyé | Date | Fit | Employeur | Poste | Deadline | CV | LDM |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| [ ] | | high | | Poste sans entreprise | | | |\n"
    )

    result = runner.invoke(cli, ["import-md", str(bad), "--config", str(config)])
    assert result.exit_code == 1
    assert "erreur :" in result.output
    assert "Traceback" not in result.output

    conn = connect(db_path)
    init_db(conn)
    for table in ("offer", "match", "search", "source", "company"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    conn.close()
