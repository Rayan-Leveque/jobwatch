from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from jobwatch.collectors.base import RawOffer, store_offers
from jobwatch.config import ConfigError, ResearchConfig, SearchConfig, load_config
from jobwatch.db import connect, init_db
from jobwatch.matching import run_matching, sync_searches
from jobwatch.research import (
    ResearchResult,
    _parse_result,
    apply_research_fits,
    research_offers,
)


def _config(**overrides) -> ResearchConfig:
    values = {"model": "gpt-test", "runner": "codex", "codex_bin": "codex-test"}
    values.update(overrides)
    return ResearchConfig(**values)


def _searches() -> list[SearchConfig]:
    return [SearchConfig(name="ai", include=["AI"], locations=["Paris"])]


def test_config_parses_research_and_heartbeat(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""db: {tmp_path / 'db.sqlite'}
searches:
  - name: ai
    include: [AI]
sources: {{}}
notify:
  heartbeat: true
research:
  runner: codex
  codex_bin: codex-local
  model: gpt-test
  variant: max
  recency_days: 5
  max_results: 12
  instructions: Prioriser le secteur public.
"""
    )
    config = load_config(path)
    assert config.notify.heartbeat is True
    assert config.research == ResearchConfig(
        model="gpt-test",
        runner="codex",
        codex_bin="codex-local",
        variant="max",
        recency_days=5,
        max_results=12,
        instructions="Prioriser le secteur public.",
    )


@pytest.mark.parametrize(
    "research_yaml,message",
    [
        ("research: enabled", "research.*mapping"),
        ("research:\n  runner: codex", "research.model"),
        ("research:\n  model: test\n  recency_days: 0", "research.recency_days"),
        ("research:\n  model: test\n  instructions: []", "research.instructions"),
    ],
)
def test_config_rejects_invalid_research(tmp_path, research_yaml: str, message: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"db: {tmp_path / 'db.sqlite'}\nsearches:\n  - name: ai\n    include: [AI]\n"
        f"sources: {{}}\nnotify: {{}}\n{research_yaml}\n"
    )
    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_parse_result_validates_and_deduplicates_rows() -> None:
    payload = {
        "offers": [
            {
                "title": "AI Engineer",
                "url": "https://jobs.example/1",
                "company": "Acme",
                "platform": "Carrières Acme",
                "location": "Paris",
                "contract": "permanent",
                "published_at": "2026-08-09",
                "fit": "high",
            },
            {
                "title": "AI Engineer",
                "url": "https://jobs.example/duplicate",
                "company": "Acme",
                "platform": "Carrières Acme",
                "location": "Paris",
                "contract": "invalid",
                "published_at": None,
                "fit": "medium",
            },
            {
                "title": "Fausse offre",
                "url": "javascript:alert(1)",
                "company": "Bad",
                "platform": "Bad",
                "location": None,
                "contract": None,
                "published_at": None,
                "fit": "high",
            },
        ]
    }
    result = _parse_result(json.dumps(payload), max_results=50)
    assert result.failed is False
    assert len(result.offers) == 1
    assert result.offers[0].contract == "permanent"
    assert result.fits_by_url == {"https://jobs.example/1": "high"}


def test_codex_research_uses_schema_and_candidate_attachment(monkeypatch) -> None:
    candidate = RawOffer(
        title="AI Engineer",
        url="https://jobs.example/1",
        company="Acme",
        platform="LinkedIn",
        location="Paris",
    )

    def fake_run(command, **kwargs):
        assert command[command.index("-s") + 1] == "read-only"
        assert "--ignore-user-config" in command
        assert "shell_tool" in command
        schema_path = Path(command[command.index("--output-schema") + 1])
        assert json.loads(schema_path.read_text(encoding="utf-8"))["required"] == ["offers"]
        assert "https://jobs.example/1" in kwargs["input"]
        out_path = Path(command[command.index("-o") + 1])
        row = {
            "title": "AI Engineer",
            "url": "https://jobs.example/1",
            "company": "Acme",
            "platform": "LinkedIn",
            "location": "Paris",
            "contract": None,
            "published_at": "2026-08-09",
            "fit": "high",
        }
        out_path.write_text(json.dumps({"offers": [row]}), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("jobwatch.llm_runner.subprocess.run", fake_run)
    result = research_offers(_config(variant="max"), _searches(), [candidate])
    assert result.fits_by_url[candidate.url] == "high"
    assert result.offers[0].company == "Acme"


def test_opencode_research_denies_local_tools_and_allows_web(monkeypatch) -> None:
    def fake_run(command, **kwargs):
        from jobwatch.enrich import OPENCODE_TOOLS

        expected = {"*": "deny"}
        expected.update(
            {tool: "deny" for tool in OPENCODE_TOOLS if tool not in ("webfetch", "websearch")}
        )
        expected.update({"webfetch": "allow", "websearch": "allow"})
        config_path = Path(kwargs["cwd"]) / "opencode.json"
        assert json.loads(config_path.read_text(encoding="utf-8"))["permission"] == expected
        assert json.loads(kwargs["env"]["OPENCODE_PERMISSION"]) == expected
        assert "--pure" in command
        assert "--auto" in command
        event = {"type": "text", "part": {"text": json.dumps({"offers": []})}}
        return subprocess.CompletedProcess(command, 0, json.dumps(event), "")

    monkeypatch.setattr("jobwatch.llm_runner.subprocess.run", fake_run)

    result = research_offers(
        _config(runner="opencode", opencode_bin="opencode-test"), _searches(), []
    )

    assert result == ResearchResult([], {})


def test_research_failure_is_non_blocking(monkeypatch) -> None:
    monkeypatch.setattr(
        "jobwatch.llm_runner.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "boom"),
    )
    assert research_offers(_config(), _searches(), []) == ResearchResult([], {}, failed=True)


def test_apply_research_fits_never_overwrites_existing_fit() -> None:
    conn = connect(":memory:")
    init_db(conn)
    sync_searches(conn, _searches())
    offers = [
        RawOffer("AI One", "https://jobs.example/1", "Acme", "Test", "Paris"),
        RawOffer("AI Two", "https://jobs.example/2", "Beta", "Test", "Paris"),
    ]
    store_offers(conn, "research", "research", offers)
    run_matching(conn)
    conn.execute(
        "UPDATE match SET fit = 'low' WHERE offer_id = (SELECT id FROM offer WHERE url = ?)",
        (offers[1].url,),
    )
    conn.commit()
    assert apply_research_fits(conn, {offers[0].url: "high", offers[1].url: "high"}) == 1
    rows = {
        row["url"]: row["fit"]
        for row in conn.execute(
            "SELECT o.url, m.fit FROM match m JOIN offer o ON o.id = m.offer_id"
        )
    }
    assert rows == {offers[0].url: "high", offers[1].url: "low"}
    conn.close()


def test_max_results_counts_valid_offers_not_rejected_rows() -> None:
    rows = [
        {
            "title": "Doublon",
            "url": "https://jobs.example/dup",
            "company": "Acme",
            "platform": "Acme",
            "location": None,
            "contract": None,
            "published_at": None,
            "fit": "high",
        },
        {
            "title": "Doublon",
            "url": "https://jobs.example/dup-2",
            "company": "Acme",
            "platform": "Acme",
            "location": None,
            "contract": None,
            "published_at": None,
            "fit": "high",
        },
        {"title": "Invalide", "url": "javascript:alert(1)", "company": "Bad", "fit": "high"},
    ]
    rows += [
        {
            "title": f"Offre {index}",
            "url": f"https://jobs.example/{index}",
            "company": f"Entreprise {index}",
            "platform": "Jobs",
            "location": None,
            "contract": None,
            "published_at": None,
            "fit": "high",
        }
        for index in range(2)
    ]

    result = _parse_result(json.dumps({"offers": rows}), max_results=3)

    assert [offer.url for offer in result.offers] == [
        "https://jobs.example/dup",
        "https://jobs.example/0",
        "https://jobs.example/1",
    ]
