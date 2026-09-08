"""Tests for offer matching against searches."""

from __future__ import annotations

import json
import sqlite3

import pytest
from click.testing import CliRunner

from jobwatch.cli import cli
from jobwatch.db import connect, init_db
from jobwatch.onboarding import complete_profile
from jobwatch.profile import save_profile_details
from jobwatch.seniority import reclassify_all_profiles
from jobwatch.serve_render import render_page


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = connect(":memory:")
    init_db(connection)
    yield connection
    connection.close()


def _company_id(conn: sqlite3.Connection, name: str) -> int:
    conn.execute("INSERT OR IGNORE INTO company (name) VALUES (?)", (name,))
    return int(conn.execute("SELECT id FROM company WHERE name = ?", (name,)).fetchone()["id"])


def _insert_offer(
    conn: sqlite3.Connection,
    title: str,
    url: str,
    location: str | None = "Paris",
    contract: str | None = "permanent",
    collected_at: str | None = None,
) -> int:
    conn.execute("INSERT OR IGNORE INTO source (type, name) VALUES ('test', 'test')")
    source_id = int(conn.execute("SELECT id FROM source WHERE name = 'test'").fetchone()["id"])
    company_id = _company_id(conn, "Acme")
    cur = conn.execute(
        "INSERT INTO offer "
        "(source_id, company_id, title, url, platform, location, contract, collected_at) "
        "VALUES (?, ?, ?, ?, 'Test', ?, ?, COALESCE(?, datetime('now')))",
        (source_id, company_id, title, url, location, contract, collected_at),
    )
    return int(cur.lastrowid)


def _add_experience(conn: sqlite3.Connection, offer_id: int, value: str, content: str) -> None:
    conn.execute(
        "INSERT INTO offer_content (offer_id, markdown, status, fetch_attempts) "
        "VALUES (?, ?, 'ok', 1)",
        (offer_id, content),
    )
    summary_id = int(
        conn.execute(
            "INSERT INTO offer_summary (offer_id, source) VALUES (?, 'auto')", (offer_id,)
        ).lastrowid
    )
    conn.execute(
        "INSERT INTO summary_field (summary_id, key, value, quote) VALUES (?, 'experience', ?, ?)",
        (summary_id, value, content),
    )


def _insert_owner(conn: sqlite3.Connection) -> tuple[int, int]:
    workspace_id = int(
        conn.execute("INSERT INTO workspace (slug, name) VALUES ('alice', 'Alice')").lastrowid
    )
    account_id = int(
        conn.execute("INSERT INTO account (email) VALUES ('alice@example.com')").lastrowid
    )
    conn.execute(
        "INSERT INTO membership (account_id, workspace_id, role) VALUES (?, ?, 'owner')",
        (account_id, workspace_id),
    )
    return account_id, workspace_id


def test_junior_profile_filters_explicit_senior_offer_but_keeps_compatible_and_unknown(
    conn: sqlite3.Connection,
) -> None:
    account_id, workspace_id = _insert_owner(conn)
    senior_id = _insert_offer(conn, "Product Owner IA H/F - Asnières-sur-Seine", "https://a/senior")
    compatible_id = _insert_offer(conn, "AI Solutions engineer", "https://a/junior")
    unknown_id = _insert_offer(conn, "AI Product Owner", "https://a/unknown")
    _add_experience(
        conn,
        senior_id,
        "6 ans ou plus",
        "Vous avez 6 ans ou plus d’expérience en gestion de produit.",
    )
    _add_experience(
        conn,
        compatible_id,
        "2+ ans",
        "2+ years' experience in AI/LLM implementation.",
    )
    _add_experience(conn, unknown_id, "non précisé", "Vous rejoignez une équipe produit.")
    conn.commit()

    complete_profile(
        conn,
        account_id,
        workspace_id,
        [],
        [{"label": "Ingénierie IA", "keywords": ["AI", "IA"], "exclude": []}],
        seniority_min=2,
        seniority_max=2,
        cover_letters_enabled=False,
    )

    page = render_page(conn, track="all", account_id=account_id)
    assert "Product Owner IA H/F - Asnières-sur-Seine" not in page
    assert "AI Solutions engineer" in page
    assert "AI Product Owner" in page
    assessments = {
        int(row["offer_id"]): (str(row["status"]), str(row["reason"]))
        for row in conn.execute(
            "SELECT m.offer_id, ms.status, ms.reason FROM match_seniority ms "
            "JOIN match m ON m.id = ms.match_id WHERE ms.account_id = ?",
            (account_id,),
        )
    }
    assert assessments[senior_id][0] == "excluded"
    assert assessments[compatible_id][0] == "compatible"
    assert assessments[unknown_id][0] == "unclassified"
    assert "aucune exigence explicite" in assessments[unknown_id][1].lower()


def test_seniority_change_reclassifies_recent_feed_without_destroying_decisions(
    conn: sqlite3.Connection,
) -> None:
    account_id, workspace_id = _insert_owner(conn)
    new_offer_id = _insert_offer(conn, "AI Engineer", "https://a/new")
    later_offer_id = _insert_offer(conn, "AI Platform Engineer", "https://a/later")
    _add_experience(conn, new_offer_id, "5 ans minimum", "Au moins 5 ans d’expérience.")
    _add_experience(conn, later_offer_id, "6 ans", "6 ans d’expérience sont requis.")
    conn.commit()
    complete_profile(
        conn,
        account_id,
        workspace_id,
        [],
        [{"label": "IA", "keywords": ["AI"], "exclude": []}],
    )
    later_match_id = int(
        conn.execute("SELECT id FROM match WHERE offer_id = ?", (later_offer_id,)).fetchone()["id"]
    )
    conn.execute("UPDATE match SET state = 'later' WHERE id = ?", (later_match_id,))
    conn.commit()

    save_profile_details(
        conn,
        account_id,
        workspace_id,
        {"seniority_min": 2, "seniority_max": 2, "cover_letters_enabled": False},
    )

    page = render_page(conn, track="all", account_id=account_id)
    assert "AI Engineer" not in page
    assert "AI Platform Engineer" in page
    assert conn.execute("SELECT COUNT(*) AS n FROM match").fetchone()["n"] == 2
    assert (
        conn.execute("SELECT state FROM match WHERE id = ?", (later_match_id,)).fetchone()["state"]
        == "later"
    )


def test_later_experience_enrichment_reclassifies_previous_unknown_match(
    conn: sqlite3.Connection,
) -> None:
    account_id, workspace_id = _insert_owner(conn)
    offer_id = _insert_offer(conn, "AI Engineer", "https://a/enriched-later")
    conn.commit()
    complete_profile(
        conn,
        account_id,
        workspace_id,
        [],
        [{"label": "IA", "keywords": ["AI"], "exclude": []}],
        seniority_min=2,
        seniority_max=2,
    )
    assert "AI Engineer" in render_page(conn, track="all", account_id=account_id)

    _add_experience(
        conn,
        offer_id,
        "5 ans minimum",
        "Vous justifiez d’au moins 5 ans d’expérience en intelligence artificielle.",
    )
    conn.commit()
    assert reclassify_all_profiles(conn) >= 1

    assert "AI Engineer" not in render_page(conn, track="all", account_id=account_id)


def test_cli_run_filters_offers_and_resyncs_searches_without_resetting_triage(tmp_path) -> None:
    db = tmp_path / "jw.db"
    conn = connect(db)
    init_db(conn)
    for key, title, location, contract in (
        ("ml", "Machine Learning Engineer", "Paris 11e", "permanent"),
        ("llm", "LLM Platform Engineer", "Paris", "permanent"),
        ("other", "Frontend Developer", "Paris", "permanent"),
        ("excluded", "AI Engineer Stage", "Paris", "permanent"),
        ("outside", "AI Engineer Lyon", "Lyon", "permanent"),
        ("no-location", "AI Engineer Anywhere", None, "permanent"),
        ("contract", "AI Engineer CDD", "Paris", "fixed_term"),
        ("no-contract", "AI Engineer Unknown", "Paris", None),
    ):
        _insert_offer(conn, title, f"https://a/{key}", location=location, contract=contract)
    _insert_offer(conn, "AI Engineer Old", "https://a/old", collected_at="2020-01-01")
    conn.commit()
    config = tmp_path / "config.yaml"
    strict = {
        "name": "strict",
        "include": ["machine learning", "AI", "LLM"],
        "exclude": ["stage"],
        "locations": ["Paris"],
        "contract": "permanent",
    }
    broad = {"name": "broad", "include": ["AI"]}
    runner = CliRunner()
    for step, searches, expected in (
        ("initial", [strict, broad], {"ml", "llm", "no-location", "no-contract"}),
        ("disabled", [broad], {"ml", "llm", "no-location", "no-contract"}),
        (
            "updated",
            [{**strict, "contract": None, "locations": [], "include": ["AI"]}, broad],
            {"ml", "llm", "outside", "no-location", "contract", "no-contract", "later"},
        ),
    ):
        config.write_text(json.dumps({"db": str(db), "searches": searches}))
        result = runner.invoke(cli, ["run", "--config", str(config)])
        assert result.exit_code == 0, result.output
        urls = {
            r[0].removeprefix("https://a/")
            for r in conn.execute(
                "SELECT o.url FROM match m JOIN offer o ON o.id = m.offer_id "
                "JOIN search s ON s.id = m.search_id WHERE s.name = 'strict'"
            )
        }
        assert urls == expected
        assert [
            r[0] for r in conn.execute("SELECT name FROM search WHERE active = 1 ORDER BY name")
        ] == (sorted(s["name"] for s in searches))
        before = list(conn.execute("SELECT id, state FROM match ORDER BY id"))
        assert runner.invoke(cli, ["run", "--config", str(config)]).exit_code == 0
        assert list(conn.execute("SELECT id, state FROM match ORDER BY id")) == before
        broad_urls = {
            r[0].removeprefix("https://a/")
            for r in conn.execute(
                "SELECT o.url FROM match m JOIN offer o ON o.id = m.offer_id "
                "JOIN search s ON s.id = m.search_id WHERE s.name = 'broad'"
            )
        }
        assert broad_urls == {"excluded", "outside", "no-location", "contract", "no-contract"} | (
            set() if step == "initial" else {"later"}
        )
        if step == "initial":
            initial_ids = [r[0] for r in before]
            conn.execute("UPDATE match SET state = 'later'")
            _insert_offer(conn, "AI Engineer Later", "https://a/later")
            conn.commit()
    assert [
        r[0] for r in conn.execute("SELECT id FROM match WHERE state = 'later' ORDER BY id")
    ] == initial_ids
    search = conn.execute("SELECT * FROM search WHERE name = 'strict'").fetchone()
    assert json.loads(search["include_json"]) == ["AI"]
    assert search["contract"] is None
    assert json.loads(search["locations_json"]) == []
    listing = runner.invoke(cli, ["list", "--state", "later", "--config", str(config)])
    assert listing.exit_code == 0, listing.output
    assert "Machine Learning Engineer" in listing.output
    conn.close()
