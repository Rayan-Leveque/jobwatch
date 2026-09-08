"""Parcours d'import CLI sur fichiers réels et SQLite, sans mocks internes."""

import json

import pytest
from click.testing import CliRunner

from jobwatch.cli import cli
from jobwatch.db import connect, init_db

DAILY_OFFER = {
    "title": "Ingénieur IA",
    "company": "DxO Labs",
    "location": "Boulogne-Billancourt, Île-de-France, France",
    "url": "https://www.linkedin.com/jobs/view/1",
    "source": "linkedin",
    "released": "2026-08-05",
    "first_seen": "2026-08-06",
}
DIGEST = """# Veille

## Fit high

| Fit | Poste | Entreprise | Lieu | Source | URL |
|---|---|---|---|---|---|
| high | Titre moins précis | DxO Labs | Paris | LinkedIn | https://www.linkedin.com/jobs/view/1 |
| high | GenAI Engineer | SFEIR | Paris | LinkedIn | https://www.linkedin.com/jobs/view/2 |

## Fit medium

| Fit | Poste | Entreprise | Lieu | Source | URL |
|---|---|---|---|---|---|
| medium | AI Engineer | LCL | Villejuif | LinkedIn | https://www.linkedin.com/jobs/view/3 |

Note narrative ignorée.
"""
TRACKER = """# Suivi

## Candidatures manuelles

| Envoyé | Date | Fit | Employeur | Poste | Deadline | CV | LDM |
|---|---|---|---|---|---|---|---|
| [x] | 2026-08-05 | **high** | CNIL | Ingénieur IA | 03/09/2026 | documents/cv/cnil.pdf | documents/cover_letters/cnil.tex |
| [ ] | | medium | DGE | Chef de projets IA | 22/08/2026 | à faire | à faire |
| [ ] | | low | OSIIC | Chef de produit IA | n.r. | | |

## Veille automatisée

| Envoyé | Date | Fit | Entreprise | Poste | Vu le | URL | CV | LDM |
|---|---|---|---|---|---|---|---|---|
| [ ] | | **high** | DHM IT | Ingénieur Plateforme IA | 2026-08-03 | https://www.linkedin.com/jobs/view/4 | | |
| [x] | ~2026-07-15 | **high** | Wavestone | Consultant IA | 2026-08-04 | https://jobs.smartrecruiters.com/Wavestone/5 | documents/cv/ats.pdf (probable) | documents/applications/wavestone/cover.pdf |
| [ ] | | medium | Wypoon | Cloud | MLOps | GenAI | 2026-08-04 | https://www.linkedin.com/jobs/view/6 | | |
"""


@pytest.fixture()
def instance(tmp_path):
    db = tmp_path / "jw.db"
    config = tmp_path / "config.yaml"
    config.write_text(
        f"db: {db}\nsearches: [{{name: test, include: [AI]}}]\nsources: {{}}\nnotify: {{}}\n"
    )
    conn = connect(db)
    init_db(conn)
    runner = CliRunner()

    def invoke(*args, exit_code=0):
        result = runner.invoke(cli, [*map(str, args), "--config", str(config)])
        assert result.exit_code == exit_code, result.output
        assert "Traceback" not in result.output
        return result.output

    yield invoke, conn
    conn.close()


def test_daily_merge_preserves_metadata_fit_and_triage(instance, tmp_path):
    invoke, conn = instance
    api, digest = tmp_path / "daily.json", tmp_path / "daily.md"
    api.write_text(json.dumps({"li-1": DAILY_OFFER}))
    digest.write_text(DIGEST)
    args = ("ingest-daily", "--api-json", api, "--digest", digest, "--search-name", "ma-veille")
    assert "3 offre(s) créée(s)" in invoke(*args)
    row = conn.execute(
        "SELECT o.*, c.name AS company, m.fit, m.state, s.name AS source, s.type "
        "FROM offer o JOIN company c ON c.id = o.company_id "
        "JOIN source s ON s.id = o.source_id JOIN match m ON m.offer_id = o.id "
        "WHERE o.url = ?",
        (DAILY_OFFER["url"],),
    ).fetchone()
    for key in ("title", "company", "location"):
        assert row[key] == DAILY_OFFER[key]
    assert (row["published_at"], row["collected_at"]) == ("2026-08-05", "2026-08-06")
    assert (row["source"], row["type"], row["platform"]) == ("linkedin", "web", "LinkedIn")
    assert (row["state"], row["fit"], row["deadline"]) == ("new", "high", None)
    assert conn.execute("SELECT name FROM search").fetchone()[0] == "ma-veille"
    assert conn.execute("SELECT COUNT(*) FROM source").fetchone()[0] == 1
    assert "Ingénieur IA" in invoke("list", "--search", "ma-veille")

    invoke("list", "--ack")
    invoke("discard", "2")
    invoke("apply", "3", "--note", "CV envoyé")
    before = list(conn.iterdump())
    output = invoke(*args)
    assert "0 offre(s) créée(s)" in output and "0 match(s) créé(s)" in output
    assert list(conn.iterdump()) == before
    assert [r[0] for r in conn.execute("SELECT state FROM match ORDER BY id")] == [
        "seen",
        "discarded",
        "applied",
    ]
    assert "LCL" in invoke("apps")
    assert conn.execute("SELECT note FROM application").fetchone()[0] == "CV envoyé"


@pytest.mark.parametrize("state", ["seen", "later", "discarded", "applied"])
def test_daily_later_digest_updates_fit_without_resetting_decisions(instance, tmp_path, state):
    invoke, conn = instance
    api, digest = tmp_path / "daily.json", tmp_path / "daily.md"
    api.write_text(json.dumps({"li-1": DAILY_OFFER}))
    invoke("ingest-daily", "--api-json", api)
    if state == "applied":
        invoke("apply", "1")
    else:
        conn.execute("UPDATE match SET state = ?", (state,))
        conn.commit()
    digest.write_text(DIGEST)
    assert "1 fit(s) mis à jour" in invoke("ingest-daily", "--digest", digest)
    assert tuple(conn.execute("SELECT state, fit FROM match WHERE id = 1").fetchone()) == (
        state,
        "high",
    )
    assert "0 fit(s) mis à jour" in invoke("ingest-daily", "--digest", digest)
    assert conn.execute("SELECT COUNT(*) FROM application").fetchone()[0] == (state == "applied")
    assert conn.execute("SELECT COUNT(*) FROM event").fetchone()[0] == (state == "applied")


def test_daily_json_sources_and_unknown_date(instance, tmp_path):
    invoke, conn = instance
    api = tmp_path / "daily.json"
    api.write_text(
        json.dumps(
            {
                "sr": {**DAILY_OFFER, "source": "smartrecruiters", "url": "https://jobs.sr.com/1"},
                "wttj": {**DAILY_OFFER, "source": "wttj", "url": "https://wttj.com/2"},
                "custom": {
                    **DAILY_OFFER,
                    "source": "custom",
                    "url": "https://www.safran.com/3",
                    "first_seen": "hier",
                },
            }
        )
    )
    invoke("ingest-daily", "--api-json", api)
    rows = conn.execute(
        "SELECT s.name, s.type, o.platform FROM offer o JOIN source s ON s.id = o.source_id "
        "ORDER BY s.name"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("safran.com", "web", "safran.com"),
        ("smartrecruiters", "smartrecruiters", "SmartRecruiters"),
        ("wttj", "web", "WTTJ"),
    ]
    assert (
        conn.execute(
            "SELECT date(collected_at) = date('now') FROM offer WHERE url = 'https://www.safran.com/3'"
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize(
    "header,source,url,platform",
    [
        ("Poste", "[CSP](https://choisirleservicepublic.gouv.fr/job)", "", "Service public"),
        ("Poste / rôle", "WTTJ", "https://www.welcometothejungle.com/job", "WTTJ"),
    ],
)
def test_daily_digest_historical_formats(instance, tmp_path, header, source, url, platform):
    invoke, conn = instance
    path = tmp_path / "daily.md"
    path.write_text(
        f"## Fit low\n\n| fit | {header} | entreprise | lieu | source | url |\n"
        "|---|---|---|---|---|---|\n"
        f"| | **Cloud** | MLOps | GenAI | Acme | Paris | {source} | {url} |\n"
        "| high | Sans lien | Acme | Paris | | |\n\n- Note narrative ignorée\n"
    )
    invoke("ingest-daily", "--digest", path)
    rows = conn.execute(
        "SELECT o.title, c.name, o.location, m.fit, o.platform FROM offer o "
        "JOIN match m ON m.offer_id = o.id JOIN company c ON c.id = o.company_id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [("Cloud | MLOps | GenAI", "Acme", "Paris", "low", platform)]


@pytest.mark.parametrize(
    "bad",
    [
        "{ nope",
        "[]",
        "{}",
        *[
            json.dumps({"good": DAILY_OFFER, "bad": {**DAILY_OFFER, **change}})
            for change in (
                {"title": ""},
                {"company": None},
                {"url": "ftp://example.com/job"},
                {"released": 20260805},
            )
        ],
    ],
)
def test_daily_invalid_json_writes_nothing(instance, tmp_path, bad):
    invoke, conn = instance
    path = tmp_path / "daily.json"
    path.write_text(bad)
    before = list(conn.iterdump())
    output = invoke("ingest-daily", "--api-json", path, exit_code=1)
    assert "JSON" in output or "importer" in output or "'bad'" in output
    assert list(conn.iterdump()) == before


def test_daily_invalid_digest_rolls_back_valid_json(instance, tmp_path):
    invoke, conn = instance
    api, digest = tmp_path / "daily.json", tmp_path / "daily.md"
    api.write_text(json.dumps({"li-1": DAILY_OFFER}))
    digest.write_text("# Veille\n\nAucune table\n")
    before = list(conn.iterdump())
    assert "aucune offre valide" in invoke(
        "ingest-daily",
        "--api-json",
        api,
        "--digest",
        digest,
        exit_code=1,
    )
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("prefix", ["documents/", ""])
def test_tracker_import_preserves_documents_dates_and_is_idempotent(instance, tmp_path, prefix):
    invoke, conn = instance
    path = tmp_path / "suivi.md"
    path.write_text(
        TRACKER.replace("documents/cv/", f"{prefix}cv/").replace(
            "documents/cover_letters/",
            f"{prefix}cover_letters/",
        )
    )
    assert "6 offre(s) créée(s)" in invoke("import-md", path)
    rows = conn.execute(
        "SELECT c.name, o.title, m.state, m.fit FROM match m JOIN offer o ON o.id = m.offer_id "
        "JOIN company c ON c.id = o.company_id ORDER BY m.id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("CNIL", "Ingénieur IA", "applied", "high"),
        ("DGE", "Chef de projets IA", "seen", "medium"),
        ("OSIIC", "Chef de produit IA", "seen", "low"),
        ("DHM IT", "Ingénieur Plateforme IA", "seen", "high"),
        ("Wavestone", "Consultant IA", "applied", "high"),
        ("Wypoon", "Cloud | MLOps | GenAI", "seen", "medium"),
    ]
    assert conn.execute("SELECT COUNT(*) FROM offer WHERE url LIKE 'jobwatch:%'").fetchone()[0] == 3
    assert tuple(
        conn.execute("SELECT deadline, published_at FROM offer WHERE id = 1").fetchone()
    ) == (
        "03/09/2026",
        None,
    )
    assert conn.execute("SELECT deadline FROM offer WHERE id = 3").fetchone()[0] is None
    assert conn.execute("SELECT collected_at FROM offer WHERE id = 4").fetchone()[0] == "2026-08-03"
    assert [tuple(r) for r in conn.execute("SELECT type, at FROM event ORDER BY id")] == [
        ("applied", "2026-08-05"),
        ("applied", "2026-07-15"),
    ]
    assert [r[0] for r in conn.execute("SELECT created_at FROM application ORDER BY id")] == [
        "2026-08-05",
        "2026-07-15",
    ]
    assert [
        tuple(r)
        for r in conn.execute(
            "SELECT type, path, sent_at FROM document ORDER BY application_id, type"
        )
    ] == [
        ("cover_letter", f"{prefix}cover_letters/cnil.tex", "2026-08-05"),
        ("cv", f"{prefix}cv/cnil.pdf", "2026-08-05"),
        ("cover_letter", "documents/applications/wavestone/cover.pdf", "2026-07-15"),
        ("cv", f"{prefix}cv/ats.pdf", "2026-07-15"),
    ]
    before = list(conn.iterdump())
    assert "0 offre(s) créée(s)" in invoke("import-md", path)
    assert list(conn.iterdump()) == before
    # Les lignes envoyées et non envoyées respectent les décisions ultérieures.
    invoke("discard", "1")
    invoke("discard", "4")
    conn.execute("UPDATE match SET state = 'new' WHERE id = 5")
    conn.commit()
    invoke("import-md", path)
    assert [
        r[0] for r in conn.execute("SELECT state FROM match WHERE id IN (1, 4, 5) ORDER BY id")
    ] == [
        "discarded",
        "discarded",
        "applied",
    ]


def test_tracker_merges_existing_collected_offer(instance, tmp_path):
    invoke, conn = instance
    api, tracker = tmp_path / "daily.json", tmp_path / "suivi.md"
    api.write_text(
        json.dumps(
            {
                "sr": {
                    **DAILY_OFFER,
                    "company": "Wavestone",
                    "title": "Consultant IA",
                    "url": "https://jobs.smartrecruiters.com/Wavestone/5",
                    "source": "smartrecruiters",
                }
            }
        )
    )
    invoke("ingest-daily", "--api-json", api)
    tracker.write_text(TRACKER)
    assert "5 offre(s) créée(s)" in invoke(
        "import-md",
        tracker,
        "--search-name",
        "veille-importee",
    )
    assert conn.execute("SELECT COUNT(*) FROM offer").fetchone()[0] == 6
    assert conn.execute("SELECT state FROM match WHERE offer_id = 1").fetchone()[0] == "applied"


@pytest.mark.parametrize(
    "text,message",
    [
        ("## Notes\n\n| A | B |\n|---|---|\n| x | y |\n", "aucune ligne"),
        (TRACKER + "| [ ] | | high | | Sans entreprise | | https://example.com/9 | | |\n", "ligne"),
        (
            TRACKER + "| [ ] | | high | Société X | | | https://example.com/9 | | |\n",
            "Veille automatisée",
        ),
    ],
    ids=["no-table", "missing-company", "missing-title"],
)
def test_tracker_invalid_file_writes_nothing(instance, tmp_path, text, message):
    invoke, conn = instance
    path = tmp_path / "suivi.md"
    path.write_text(text)
    before = list(conn.iterdump())
    assert message in invoke("import-md", path, exit_code=1)
    assert list(conn.iterdump()) == before


def test_summary_import_order_idempotency_replacement_and_atomic_validation(instance, tmp_path):
    invoke, conn = instance
    digest, summaries = tmp_path / "daily.md", tmp_path / "resumes.md"
    digest.write_text(DIGEST)
    invoke("ingest-daily", "--digest", digest)
    summaries.write_text(
        "# Résumés\n\n> Note\n\n## https://www.linkedin.com/jobs/view/1\n- Premier\n- Deuxième\n"
        "## https://www.linkedin.com/jobs/view/2\n- Unique\n"
    )
    assert "2 résumé(s) créé(s)" in invoke("import-summaries", summaries)
    assert [
        r[0] for r in conn.execute("SELECT text FROM summary_bullet ORDER BY summary_id, position")
    ] == [
        "Premier",
        "Deuxième",
        "Unique",
    ]
    before = list(conn.iterdump())
    assert "2 inchangé(s)" in invoke("import-summaries", summaries)
    assert list(conn.iterdump()) == before
    summaries.write_text("## https://www.linkedin.com/jobs/view/1\n- Nouveau\n")
    assert "1 remplacé(s)" in invoke("import-summaries", summaries)
    assert [
        r[0] for r in conn.execute("SELECT text FROM summary_bullet ORDER BY summary_id, position")
    ] == [
        "Nouveau",
        "Unique",
    ]
    before = list(conn.iterdump())
    for invalid in (
        "## ftp://example.com/job\n- Fait\n",
        "## https://example.com/job invalide\n- Fait\n",
        "## https://www.linkedin.com/jobs/view/2\n",
        "## https://www.linkedin.com/jobs/view/2\n- \n",
        "## https://www.linkedin.com/jobs/view/1\n- Doublon\n",
        "## https://example.com/absent\n- Absent\n",
    ):
        summaries.write_text("## https://www.linkedin.com/jobs/view/1\n- Remplacé\n" + invalid)
        invoke("import-summaries", summaries, exit_code=1)
        assert list(conn.iterdump()) == before
    summaries.write_text("- Fait sans URL\n")
    invoke("import-summaries", summaries, exit_code=1)
    assert list(conn.iterdump()) == before
