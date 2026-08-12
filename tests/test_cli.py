"""Tests for the click CLI."""

from __future__ import annotations

import json
import stat
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


def test_init_db_option_writes_given_path(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    from jobwatch import cli as cli_module

    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "isolated" / "jw.db"
    opened: list[Path] = []
    real_open_db = cli_module._open_db

    def _spy_open_db(config):
        opened.append(config.db)
        return real_open_db(config)

    monkeypatch.setattr(cli_module, "_open_db", _spy_open_db)

    result = runner.invoke(cli, ["init", "--config", str(config_path), "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    text = config_path.read_text()
    assert f"db: {db_path}" in text
    assert "~/.local/share/jobwatch/jobwatch.db" not in text
    assert db_path.exists()

    run_result = runner.invoke(cli, ["run", "--config", str(config_path)])
    assert run_result.exit_code == 0, run_result.output
    assert opened == [db_path, db_path]


def test_init_without_db_keeps_default_line(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    from jobwatch import cli as cli_module

    config_path = tmp_path / "config.yaml"
    opened: list[Path] = []
    real_open_db = cli_module._open_db

    def _spy_open_db(config):
        opened.append(config.db)
        config.db = tmp_path / "default.db"
        return real_open_db(config)

    monkeypatch.setattr(cli_module, "_open_db", _spy_open_db)

    result = runner.invoke(cli, ["init", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    assert "db: ~/.local/share/jobwatch/jobwatch.db" in config_path.read_text()
    assert opened == [Path("~/.local/share/jobwatch/jobwatch.db").expanduser()]


def test_named_instance_init_and_run_use_isolated_xdg_paths(
    runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    init_result = runner.invoke(cli, ["--instance", "alice", "init"])
    assert init_result.exit_code == 0, init_result.output
    config = config_home / "jobwatch/instances/alice/config.yaml"
    db = data_home / "jobwatch/instances/alice/jobwatch.db"
    assert config.exists()
    assert db.exists()
    assert f"db: {db}" in config.read_text()
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert stat.S_IMODE(config.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(db.stat().st_mode) == 0o600
    assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700

    run_result = runner.invoke(cli, ["--instance", "alice", "run"])
    assert run_result.exit_code == 0, run_result.output


def test_named_instance_can_come_from_environment(
    runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("JOBWATCH_INSTANCE", "bob")

    result = runner.invoke(cli, ["init"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "config/jobwatch/instances/bob/config.yaml").exists()
    assert (tmp_path / "data/jobwatch/instances/bob/jobwatch.db").exists()


def test_named_instance_rejects_path_traversal(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--instance", "../alice", "init"])
    assert result.exit_code == 2
    assert "Invalid value for '--instance'" in result.output


def test_migrate_storage_command_copies_external_library_file(
    runner: CliRunner, tmp_path: Path
) -> None:
    db_path = tmp_path / "instance" / "jobwatch.db"
    config = _write_config(tmp_path, db_path)
    db_path.parent.mkdir()
    external = tmp_path / "legacy" / "cv.pdf"
    external.parent.mkdir()
    external.write_bytes(b"cv")
    conn = connect(db_path)
    init_db(conn)
    conn.execute(
        "INSERT INTO document_library (type, label, file_path) VALUES ('cv', 'CV', ?)",
        (str(external),),
    )
    conn.commit()
    conn.close()

    result = runner.invoke(cli, ["migrate-storage", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "1 document(s) copié(s)" in result.output
    conn = connect(db_path)
    managed = Path(conn.execute("SELECT file_path FROM document_library").fetchone()[0])
    conn.close()
    assert managed.parent == db_path.parent / "documents"
    assert managed.read_bytes() == b"cv"


def test_account_invite_requires_named_instance(runner: CliRunner, tmp_path: Path) -> None:
    config = _write_config(tmp_path, tmp_path / "jobwatch.db")
    result = runner.invoke(
        cli, ["account", "invite", "alice@example.com", "--config", str(config)]
    )
    assert result.exit_code == 1
    assert "nécessite --instance" in result.output


def test_account_invite_enables_auth_and_prints_one_time_path(
    runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert runner.invoke(cli, ["--instance", "alice", "init"]).exit_code == 0

    result = runner.invoke(
        cli, ["--instance", "alice", "account", "invite", "Alice@Example.com"]
    )
    assert result.exit_code == 0, result.output
    assert "alice@example.com" in result.output
    assert "/invite/" in result.output

    db = tmp_path / "data/jobwatch/instances/alice/jobwatch.db"
    conn = connect(db)
    assert conn.execute(
        "SELECT value FROM instance_setting WHERE key = 'auth_required'"
    ).fetchone()[0] == "1"
    assert conn.execute("SELECT count(*) FROM account_invite").fetchone()[0] == 1
    conn.close()


def test_named_instance_serve_refuses_unprotected_workspace(
    runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert runner.invoke(cli, ["--instance", "alice", "init"]).exit_code == 0

    result = runner.invoke(cli, ["--instance", "alice", "serve"])

    assert result.exit_code == 1
    assert "n'a pas de compte protégé" in result.output
    assert "account invite" in result.output


def test_allow_open_is_an_explicit_local_development_escape_hatch(
    runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    from jobwatch import cli as cli_module

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert runner.invoke(cli, ["--instance", "alice", "init"]).exit_code == 0
    called: list[Path] = []
    monkeypatch.setattr(
        cli_module,
        "serve_http",
        lambda db_path, *_args, **_kwargs: called.append(db_path),
    )

    result = runner.invoke(cli, ["--instance", "alice", "serve", "--allow-open"])

    assert result.exit_code == 0, result.output
    assert called == [tmp_path / "data/jobwatch/instances/alice/jobwatch.db"]


def test_run_with_no_sources_succeeds(runner: CliRunner, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    config = _write_config(tmp_path, db_path)

    result = runner.invoke(cli, ["run", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "0 nouvelles offres collectées, 0 nouveaux matchs" in result.output


def test_run_stores_research_offers_and_their_fit(
    runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    from jobwatch.collectors.base import RawOffer
    from jobwatch.research import ResearchResult

    db_path = tmp_path / "jw.db"
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""db: {db_path}
searches:
  - name: ai-paris
    include: [AI]
    locations: [Paris]
sources: {{}}
notify: {{}}
research:
  runner: codex
  model: test-model
"""
    )
    offer = RawOffer(
        title="AI Engineer",
        url="https://jobs.example/ai",
        company="Acme",
        platform="Carrières Acme",
        location="Paris",
    )
    monkeypatch.setattr(
        "jobwatch.cli.research_offers",
        lambda config, searches, candidates: ResearchResult(
            [offer], {offer.url: "high"}
        ),
    )

    result = runner.invoke(cli, ["run", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "1 nouvelles offres collectées, 1 nouveaux matchs, 1 fit(s) renseigné(s)" in result.output
    conn = connect(db_path)
    row = conn.execute(
        "SELECT o.url, m.fit FROM match m JOIN offer o ON o.id = m.offer_id"
    ).fetchone()
    assert dict(row) == {"url": offer.url, "fit": "high"}
    conn.close()


def test_run_research_uses_profile_categories_and_keeps_config_searches(
    runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    from jobwatch.research import ResearchResult

    db_path = tmp_path / "jw.db"
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""db: {db_path}
searches:
  - name: historique
    include: [PO]
sources: {{}}
notify: {{}}
research:
  runner: codex
  model: test-model
"""
    )
    conn = connect(db_path)
    init_db(conn)
    workspace_id = conn.execute(
        "INSERT INTO workspace (slug, name) VALUES ('alice', 'Alice')"
    ).lastrowid
    account_id = conn.execute(
        "INSERT INTO account (email) VALUES ('alice@example.com')"
    ).lastrowid
    conn.execute(
        "INSERT INTO candidate_profile (account_id, workspace_id, completed_at) "
        "VALUES (?, ?, datetime('now'))",
        (account_id, workspace_id),
    )
    conn.execute(
        "INSERT INTO career_intent (account_id, label, keywords_json, exclude_json) "
        "VALUES (?, 'Data', '[\"Data Engineer\"]', '[]')",
        (account_id,),
    )
    conn.commit()
    conn.close()
    received: list[str] = []

    def fake_research(config, searches, candidates):
        received.extend(search.name for search in searches)
        return ResearchResult([], {})

    monkeypatch.setattr("jobwatch.cli.research_offers", fake_research)

    result = runner.invoke(cli, ["run", "--config", str(config)])

    assert result.exit_code == 0, result.output
    assert sorted(received) == ["Data", "historique"]


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


def test_bugs_lists_dashboard_reports(runner: CliRunner, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    config = _write_config(tmp_path, db_path)
    conn = connect(db_path)
    init_db(conn)
    conn.execute(
        "INSERT INTO bug_report (message, page, user_agent) VALUES (?, ?, ?)",
        ("Le bouton ne répond pas.\nAprès le swipe.", "/swipe", "Test Browser/1.0"),
    )
    conn.commit()
    conn.close()

    result = runner.invoke(cli, ["bugs", "--config", str(config)])

    assert result.exit_code == 0, result.output
    assert "instance locale · /swipe" in result.output
    assert "Le bouton ne répond pas." in result.output
    assert "Après le swipe." in result.output
    assert "Navigateur : Test Browser/1.0" in result.output


def test_discard_sets_state_and_discarded_at(runner: CliRunner, tmp_path: Path) -> None:
    db_path = tmp_path / "jw.db"
    _seed_match(db_path)
    config = _write_config(tmp_path, db_path)

    result = runner.invoke(cli, ["discard", "1", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "match 1 écarté" in result.output

    conn = connect(db_path)
    match = conn.execute("SELECT state, discarded_at FROM match WHERE id = 1").fetchone()
    conn.close()
    assert match["state"] == "discarded"
    assert match["discarded_at"] is not None


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


def test_enrich_without_config_block_fails_cleanly(runner: CliRunner, tmp_path: Path) -> None:
    """`jw enrich` avec un bloc `enrich` inerte ne plante pas et explique quoi faire."""
    db_path = tmp_path / "jw.db"
    config = _write_config(tmp_path, db_path)  # enrich: {} implicite (absent du fichier)

    result = runner.invoke(cli, ["enrich", "--config", str(config)])

    assert result.exit_code == 1
    assert "erreur :" in result.output
    assert "enrich" in result.output


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


# --- import-summaries -----------------------------------------------------


def test_import_summaries_help_and_missing_file(runner: CliRunner, tmp_path: Path) -> None:
    help_result = runner.invoke(cli, ["import-summaries", "--help"])
    assert help_result.exit_code == 0
    assert "PATH" in help_result.output
    assert "--config" in help_result.output
    config = _write_config(tmp_path, tmp_path / "jw.db")

    result = runner.invoke(
        cli,
        ["import-summaries", str(tmp_path / "absent.md"), "--config", str(config)],
    )
    assert result.exit_code == 1
    assert result.output == f"erreur : fichier introuvable : {tmp_path / 'absent.md'}\n"


def test_import_summaries_reports_created_then_unchanged(
    runner: CliRunner, tmp_path: Path
) -> None:
    db_path = tmp_path / "jw.db"
    _seed_match(db_path)
    config = _write_config(tmp_path, db_path)
    summaries = tmp_path / "resumes.md"
    summaries.write_text(
        "## https://example.com/job\n- Premier fait\n- Deuxième fait\n"
    )
    args = ["import-summaries", str(summaries), "--config", str(config)]

    first = runner.invoke(cli, args)
    second = runner.invoke(cli, args)

    assert first.exit_code == 0, first.output
    assert first.output == (
        "1 résumé(s) créé(s), 0 remplacé(s), 0 inchangé(s), 2 puce(s) écrite(s)\n"
    )
    assert second.exit_code == 0, second.output
    assert second.output == (
        "0 résumé(s) créé(s), 0 remplacé(s), 1 inchangé(s), 0 puce(s) écrite(s)\n"
    )


def test_import_summaries_missing_offer_fails_without_phantom_offer(
    runner: CliRunner, tmp_path: Path
) -> None:
    db_path = tmp_path / "jw.db"
    config = _write_config(tmp_path, db_path)
    summaries = tmp_path / "resumes.md"
    summaries.write_text("## https://example.com/absent\n- Fait\n")

    result = runner.invoke(
        cli, ["import-summaries", str(summaries), "--config", str(config)]
    )

    assert result.exit_code == 1
    assert result.output == (
        "erreur : offre(s) absente(s) de la base :\n- https://example.com/absent\n"
    )
    conn = connect(db_path)
    assert conn.execute("SELECT count(*) FROM offer").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM offer_summary").fetchone()[0] == 0
    conn.close()
