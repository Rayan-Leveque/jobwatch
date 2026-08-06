"""Tests for daily ingestion: JSON and digest parsing, merge, atomic idempotent writes."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from jobwatch.db import connect, init_db
from jobwatch.importing import (
    ImportError,
    import_summaries,
    import_tracker,
    ingest_daily,
    merge_offers,
    parse_daily_digest,
    parse_daily_json,
    parse_summaries_markdown,
    parse_tracker_markdown,
)

SEARCH = "veille-importee"

SAMPLE_JSON = {
    "li-4447379908": {
        "title": "Ingénieur IA",
        "company": "DxO Labs",
        "location": "Boulogne-Billancourt, Île-de-France, France",
        "url": "https://www.linkedin.com/jobs/view/4447379908",
        "released": "2026-08-05",
        "source": "linkedin",
        "first_seen": "2026-08-06",
    },
    "wttj-5186667e-181c-46ec-aaab-cddcc07d2fcb": {
        "title": "DATA ENGINEER (F/H)",
        "company": "SNCF Connect & Tech",
        "location": "Saint-Denis, FR",
        "url": "https://www.welcometothejungle.com/fr/companies/sncf-connect-tech/jobs/data-engineer-f-h_saint-denis",
        "released": "2026-08-05",
        "source": "wttj",
        "first_seen": "2026-08-06",
    },
}

DIGEST_V05 = """# Veille emploi - 2026-08-05

## High (7)

| Fit | Poste | Entreprise | Lieu | Source |
|---|---|---|---|---|
| High | Ingénieur(e) appliqué(e) en intelligence artificielle H/F **(XP 3+)** | DINUM | Paris 7e | [CSP](https://choisirleservicepublic.gouv.fr/offre-emploi/ingenieure-appliquee-en-intelligence-artificielle-hf-reference-2026-2359818/) |
| High | Consultant Compliance Data & IA H/F | Converteo | Paris | [WTTJ](https://www.welcometothejungle.com/fr/companies/converteo/jobs/consultant-compliance-data-ia-h-f-cdi_paris) |

## Medium (7)

| Fit | Poste | Entreprise | Lieu | Source |
|---|---|---|---|---|
| Medium | Architecte de solutions IA - accompagnement ministériel H/F | DINUM | Paris 7e | [CSP](https://choisirleservicepublic.gouv.fr/offre-emploi/architecte-de-solutions-ia---accompagnement-ministeriel-hf-reference-2026-2241962/) |

## Notes de recherche large

- texte narratif hors tableau ignoré
"""

DIGEST_V06 = """# Veille emploi - 2026-08-06

## Fit high

| Fit | Poste | Entreprise | Lieu | XP demandée | Source | URL |
|---|---|---|---|---|---|---|
| high | Ingénieur IA | DxO Labs | Boulogne-Billancourt | non précisée | LinkedIn | https://www.linkedin.com/jobs/view/4447379908 |
| high | GenAI Engineer | SFEIR | Paris | non précisée | LinkedIn | https://www.linkedin.com/jobs/view/4326775827 |

## Fit medium

| Fit | Poste | Entreprise | Lieu | XP demandée | Source | URL |
|---|---|---|---|---|---|---|
| medium | AI Engineer H/F | LCL | Villejuif | non précisée | LinkedIn | https://www.linkedin.com/jobs/view/4440973597 |

*Note : texte narratif ignoré.*
"""

TRACKER_SAMPLE = """# Suivi candidatures - Test

> Note narrative hors tableau, ignorée.

## Candidatures manuelles - secteur public

| Envoyé | Date | Fit | Employeur | Poste (réf.) | Deadline | CV | LDM |
|---|---|---|---|---|---|---|---|
| [x] | 2026-08-05 | **high** | CNIL | Ingénieur IA - Service (2026-2346240) | 03/09/2026 | documents/cv/cv_cnil.pdf | documents/cover_letters/cover_cnil.tex |
| [ ] | | medium | DGE (Bercy) | Chef de projets IA (MEF_2026-32117) | 22/08/2026 | à faire | à faire |
| [ ] | | low | OSIIC | Chef de produit IA (2026-2253864) | n.r. | | |

## Veille automatisée

| Envoyé | Date | Fit | Entreprise | Poste | Vu le | URL | CV | LDM |
|---|---|---|---|---|---|---|---|---|
| [ ] | | **high** | DHM IT | Ingénieur Plateforme IA Agentique (H/F) | 2026-08-03 | https://www.linkedin.com/jobs/view/4447660376 | | |
| [x] | ~2026-07-15 | **high** | Wavestone | Consultant·e Agentic & GenAI Engineer (H/F) | 2026-08-04 | https://jobs.smartrecruiters.com/Wavestone1/744000140044025 | documents/cv/cv_cdi_ats.pdf (probable - à confirmer) | documents/applications/wavestone_agentic_genai/cover_wavestone_agentic_genai.pdf |
| [ ] | | medium | Wypoon Technologies | Machine Learning Engineer - Cloud | MLOps | GenAI (Relocation provided) | 2026-08-04 | https://www.linkedin.com/jobs/view/4446207403 | | |
"""

TRACKER_SAMPLE_COUNTS = {
    "rows": 6,
    "offers": 6,
    "matches": 6,
    "applications": 2,
    "events": 2,
    "documents": 4,
}


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = connect(":memory:")
    init_db(connection)
    yield connection
    connection.close()


def _json_file(tmp_path: Path, entries: dict) -> Path:
    path = tmp_path / "daily.json"
    path.write_text(json.dumps(entries))
    return path


def _digest_file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "daily.md"
    path.write_text(text)
    return path


def _summaries_file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "resumes.md"
    path.write_text(text)
    return path


def _seed_summary_offer(conn: sqlite3.Connection, url: str) -> int:
    conn.execute("INSERT OR IGNORE INTO source (type, name) VALUES ('test', 'test')")
    source_id = conn.execute("SELECT id FROM source WHERE name = 'test'").fetchone()["id"]
    cur = conn.execute(
        "INSERT INTO offer (source_id, title, url) VALUES (?, 'AI Engineer', ?)",
        (source_id, url),
    )
    conn.commit()
    return int(cur.lastrowid)


def _match_row(conn: sqlite3.Connection, search_name: str, url: str):
    return conn.execute(
        "SELECT m.* FROM match m "
        "JOIN search s ON s.id = m.search_id "
        "JOIN offer o ON o.id = m.offer_id "
        "WHERE s.name = ? AND o.url = ?",
        (search_name, url),
    ).fetchone()


def _tracker_file(tmp_path: Path, text: str = TRACKER_SAMPLE) -> Path:
    path = tmp_path / "suivi.md"
    path.write_text(text)
    return path


def _match_for_company_title(
    conn: sqlite3.Connection, search_name: str, company: str, title: str
):
    return conn.execute(
        "SELECT m.* FROM match m "
        "JOIN search s ON s.id = m.search_id "
        "JOIN offer o ON o.id = m.offer_id "
        "JOIN company c ON c.id = o.company_id "
        "WHERE s.name = ? AND c.name = ? AND o.title = ?",
        (search_name, company, title),
    ).fetchone()


# --- parsing JSON -----------------------------------------------------------


def test_parse_daily_json_maps_fields(tmp_path: Path) -> None:
    offers = parse_daily_json(_json_file(tmp_path, SAMPLE_JSON))
    assert len(offers) == 2
    first = offers[0]
    assert first.title == "Ingénieur IA"
    assert first.company == "DxO Labs"
    assert first.url == "https://www.linkedin.com/jobs/view/4447379908"
    assert first.platform == "LinkedIn"
    assert first.source_name == "linkedin"
    assert first.source_type == "web"
    assert first.published_at == "2026-08-05"
    assert first.collected_at == "2026-08-06"
    assert first.fit is None
    assert first.location == "Boulogne-Billancourt, Île-de-France, France"


def test_parse_daily_json_source_mapping(tmp_path: Path) -> None:
    json_file = _json_file(
        tmp_path,
        {
            "sr-1": {
                "title": "Consultant Data & IA",
                "company": "Wavestone",
                "url": "https://jobs.smartrecruiters.com/Wavestone1/744000140958619",
                "source": "smartrecruiters",
            }
        },
    )
    offer = parse_daily_json(json_file)[0]
    assert offer.source_name == "smartrecruiters"
    assert offer.source_type == "smartrecruiters"
    assert offer.platform == "SmartRecruiters"


def test_parse_daily_json_unknown_source_uses_host(tmp_path: Path) -> None:
    json_file = _json_file(
        tmp_path,
        {
            "custom-1": {
                "title": "AI Officer",
                "company": "Safran",
                "url": "https://www.safran-group.com/jobs/france/chateaufort/ai-compliance-officer-fh-176099",
                "source": "custom",
            }
        },
    )
    offer = parse_daily_json(json_file)[0]
    assert offer.source_name == "safran-group.com"
    assert offer.source_type == "web"
    assert offer.platform == "safran-group.com"


def test_parse_daily_json_non_iso_first_seen_keeps_none(tmp_path: Path) -> None:
    json_file = _json_file(
        tmp_path,
        {
            "li-1": {
                "title": "AI Engineer",
                "company": "Acme",
                "url": "https://www.linkedin.com/jobs/view/1",
                "source": "linkedin",
                "first_seen": "hier",
            }
        },
    )
    offer = parse_daily_json(json_file)[0]
    assert offer.collected_at is None


def test_parse_daily_json_rejects_non_object_root(tmp_path: Path) -> None:
    path = _json_file(tmp_path, {})
    path.write_text("[]")
    with pytest.raises(ImportError):
        parse_daily_json(path)


def test_parse_daily_json_rejects_missing_title(tmp_path: Path) -> None:
    entries = {
        "li-1": {
            "company": "Acme",
            "url": "https://www.linkedin.com/jobs/view/1",
            "source": "linkedin",
        }
    }
    with pytest.raises(ImportError):
        parse_daily_json(_json_file(tmp_path, entries))


def test_parse_daily_json_rejects_bad_url(tmp_path: Path) -> None:
    entries = {
        "li-1": {
            "title": "AI Engineer",
            "company": "Acme",
            "url": "ftp://www.linkedin.com/jobs/view/1",
            "source": "linkedin",
        }
    }
    with pytest.raises(ImportError):
        parse_daily_json(_json_file(tmp_path, entries))


def test_parse_daily_json_rejects_bad_optional_type(tmp_path: Path) -> None:
    entries = {
        "li-1": {
            "title": "AI Engineer",
            "company": "Acme",
            "url": "https://www.linkedin.com/jobs/view/1",
            "source": "linkedin",
            "released": 20260805,
        }
    }
    with pytest.raises(ImportError):
        parse_daily_json(_json_file(tmp_path, entries))


def test_parse_daily_json_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ImportError):
        parse_daily_json(tmp_path / "absent.json")


def test_parse_daily_json_invalid_syntax_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{ nope")
    with pytest.raises(ImportError):
        parse_daily_json(path)


# --- parsing digest ---------------------------------------------------------


def test_parse_daily_digest_v05_markdown_source(tmp_path: Path) -> None:
    offers = parse_daily_digest(_digest_file(tmp_path, DIGEST_V05))
    assert len(offers) == 3
    first = offers[0]
    assert first.title == "Ingénieur(e) appliqué(e) en intelligence artificielle H/F (XP 3+)"
    assert first.company == "DINUM"
    assert first.location == "Paris 7e"
    assert first.fit == "high"
    assert first.url == (
        "https://choisirleservicepublic.gouv.fr/offre-emploi/"
        "ingenieure-appliquee-en-intelligence-artificielle-hf-reference-2026-2359818/"
    )
    assert first.source_name == "choisirleservicepublic.gouv.fr"
    assert first.platform == "Service public"

    wttj = offers[1]
    assert wttj.source_name == "wttj"
    assert wttj.platform == "WTTJ"
    assert wttj.fit == "high"

    assert offers[2].fit == "medium"


def test_parse_daily_digest_v06_bare_url(tmp_path: Path) -> None:
    offers = parse_daily_digest(_digest_file(tmp_path, DIGEST_V06))
    assert len(offers) == 3
    assert offers[0].title == "Ingénieur IA"
    assert offers[0].url == "https://www.linkedin.com/jobs/view/4447379908"
    assert offers[0].fit == "high"
    assert offers[0].source_name == "linkedin"
    assert offers[2].fit == "medium"


def test_parse_daily_digest_fit_from_section_fallback(tmp_path: Path) -> None:
    text = """## Fit low

| Fit | Poste | Entreprise | Lieu | Source | URL |
|---|---|---|---|---|---|
|  | Data Engineer | Papernest | Paris | WTTJ | https://www.welcometothejungle.com/fr/companies/papernest/jobs/data-engineer-cdi-paris_paris |
"""
    offers = parse_daily_digest(_digest_file(tmp_path, text))
    assert offers[0].fit == "low"


def test_parse_daily_digest_title_with_pipe(tmp_path: Path) -> None:
    text = """## Fit high

| Fit | Poste | Entreprise | Lieu | Source |
|---|---|---|---|---|
| High | Cloud | MLOps | GenAI Engineer | Acme | Paris | [LI](https://www.linkedin.com/jobs/view/123) |
"""
    offers = parse_daily_digest(_digest_file(tmp_path, text))
    assert len(offers) == 1
    assert offers[0].title == "Cloud | MLOps | GenAI Engineer"
    assert offers[0].company == "Acme"
    assert offers[0].location == "Paris"
    assert offers[0].url == "https://www.linkedin.com/jobs/view/123"


def test_parse_daily_digest_ignores_narrative(tmp_path: Path) -> None:
    text = """# Titre

## High (2)

Du texte narratif, puis une liste :

- **Acme - AI Engineer** - description longue

| Fit | Poste | Entreprise | Lieu | Source |
|---|---|---|---|---|
| High | AI Engineer | Acme | Paris | [LI](https://www.linkedin.com/jobs/view/1) |

*Note de fin ignorée.*
"""
    offers = parse_daily_digest(_digest_file(tmp_path, text))
    assert len(offers) == 1
    assert offers[0].title == "AI Engineer"


def test_parse_daily_digest_no_offers_raises(tmp_path: Path) -> None:
    text = "# Veille\n\nDu texte narratif sans aucune table.\n"
    with pytest.raises(ImportError):
        parse_daily_digest(_digest_file(tmp_path, text))


def test_parse_daily_digest_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ImportError):
        parse_daily_digest(tmp_path / "absent.md")


def test_parse_daily_digest_skips_row_without_url(tmp_path: Path) -> None:
    text = """## Fit high

| Fit | Poste | Entreprise | Lieu | Source |
|---|---|---|---|---|
| High | AI Engineer | Acme | Paris | [LI](https://www.linkedin.com/jobs/view/1) |
| High | Offre sans lien | Acme | Paris | |
"""
    offers = parse_daily_digest(_digest_file(tmp_path, text))
    assert len(offers) == 1
    assert offers[0].title == "AI Engineer"


# --- merge ------------------------------------------------------------------


def test_merge_offers_prefers_json_and_applies_digest_fit(tmp_path: Path) -> None:
    json_offers = parse_daily_json(_json_file(tmp_path, SAMPLE_JSON))
    digest_offers = parse_daily_digest(_digest_file(tmp_path, DIGEST_V06))
    merged = merge_offers(json_offers, digest_offers)

    by_url = {offer.url: offer for offer in merged}
    json_url = "https://www.linkedin.com/jobs/view/4447379908"
    assert json_url in by_url
    assert by_url[json_url].title == "Ingénieur IA"
    assert by_url[json_url].company == "DxO Labs"
    assert by_url[json_url].location == "Boulogne-Billancourt, Île-de-France, France"
    assert by_url[json_url].fit == "high"

    assert len(merged) == 4


def test_merge_offers_keeps_web_only(tmp_path: Path) -> None:
    json_offers = parse_daily_json(_json_file(tmp_path, SAMPLE_JSON))
    digest_offers = parse_daily_digest(_digest_file(tmp_path, DIGEST_V06))
    merged = merge_offers(json_offers, digest_offers)
    urls = {offer.url for offer in merged}
    assert "https://www.linkedin.com/jobs/view/4326775827" in urls
    assert "https://www.linkedin.com/jobs/view/4440973597" in urls


def test_merge_offers_sorted_by_url() -> None:
    from jobwatch.importing import DailyOffer

    def offer(url: str) -> DailyOffer:
        return DailyOffer(
            url=url,
            title="T",
            company="C",
            platform="LinkedIn",
            source_name="linkedin",
            source_type="web",
        )

    merged = merge_offers([offer("https://z.example/job/2")], [offer("https://a.example/job/1")])
    assert [offer.url for offer in merged] == ["https://a.example/job/1", "https://z.example/job/2"]


# --- ingestion --------------------------------------------------------------


def test_ingest_daily_json_only(conn: sqlite3.Connection, tmp_path: Path) -> None:
    result = ingest_daily(conn, _json_file(tmp_path, SAMPLE_JSON), None, SEARCH)
    assert result.offers_created == 2
    assert result.offers_already_present == 0
    assert result.matches_created == 2
    assert result.fits_updated == 0

    offer = conn.execute(
        "SELECT * FROM offer WHERE url = ?", ("https://www.linkedin.com/jobs/view/4447379908",)
    ).fetchone()
    assert offer["title"] == "Ingénieur IA"
    assert offer["company_id"] is not None
    assert offer["published_at"] == "2026-08-05"
    assert offer["collected_at"] == "2026-08-06"
    assert offer["deadline"] is None

    match = _match_row(conn, SEARCH, offer["url"])
    assert match["state"] == "new"
    assert match["fit"] is None

    search = conn.execute("SELECT * FROM search WHERE name = ?", (SEARCH,)).fetchone()
    assert search["active"] == 1
    assert json.loads(search["include_json"]) == []
    assert json.loads(search["exclude_json"]) == []
    assert json.loads(search["locations_json"]) == []


def test_ingest_daily_digest_only(conn: sqlite3.Connection, tmp_path: Path) -> None:
    result = ingest_daily(conn, None, _digest_file(tmp_path, DIGEST_V05), SEARCH)
    assert result.offers_created == 3
    assert result.matches_created == 3

    match = _match_row(
        conn, SEARCH, "https://choisirleservicepublic.gouv.fr/offre-emploi/architecte-de-solutions-ia---accompagnement-ministeriel-hf-reference-2026-2241962/"
    )
    assert match["state"] == "new"
    assert match["fit"] == "medium"


def test_ingest_daily_merge_json_and_digest(conn: sqlite3.Connection, tmp_path: Path) -> None:
    result = ingest_daily(
        conn,
        _json_file(tmp_path, SAMPLE_JSON),
        _digest_file(tmp_path, DIGEST_V06),
        SEARCH,
    )
    assert result.offers_created == 4
    assert result.offers_already_present == 0
    assert result.matches_created == 4

    offer = conn.execute(
        "SELECT * FROM offer WHERE url = ?", ("https://www.linkedin.com/jobs/view/4447379908",)
    ).fetchone()
    assert offer["title"] == "Ingénieur IA"
    match = _match_row(conn, SEARCH, offer["url"])
    assert match["fit"] == "high"

    assert conn.execute("SELECT count(*) FROM offer").fetchone()[0] == 4


def test_ingest_daily_web_only_offer(conn: sqlite3.Connection, tmp_path: Path) -> None:
    result = ingest_daily(conn, None, _digest_file(tmp_path, DIGEST_V06), SEARCH)
    assert result.offers_created == 3
    url = "https://www.linkedin.com/jobs/view/4326775827"
    offer = conn.execute("SELECT * FROM offer WHERE url = ?", (url,)).fetchone()
    assert offer is not None
    assert offer["title"] == "GenAI Engineer"
    assert offer["company_id"] is not None
    match = _match_row(conn, SEARCH, url)
    assert match["fit"] == "high"


def test_ingest_daily_is_idempotent(conn: sqlite3.Connection, tmp_path: Path) -> None:
    json_file = _json_file(tmp_path, SAMPLE_JSON)
    digest_file = _digest_file(tmp_path, DIGEST_V06)

    first = ingest_daily(conn, json_file, digest_file, SEARCH)
    second = ingest_daily(conn, json_file, digest_file, SEARCH)

    assert second.offers_created == 0
    assert second.offers_already_present == first.offers_created
    assert second.matches_created == 0
    assert second.fits_updated == 0

    assert conn.execute("SELECT count(*) FROM offer").fetchone()[0] == 4
    assert conn.execute("SELECT count(*) FROM match").fetchone()[0] == 4
    assert conn.execute("SELECT count(*) FROM search").fetchone()[0] == 1


def test_ingest_daily_preserves_seen_state(conn: sqlite3.Connection, tmp_path: Path) -> None:
    ingest_daily(conn, _json_file(tmp_path, SAMPLE_JSON), None, SEARCH)
    url = "https://www.linkedin.com/jobs/view/4447379908"
    match = _match_row(conn, SEARCH, url)
    conn.execute("UPDATE match SET state = 'seen' WHERE id = ?", (match["id"],))
    conn.commit()

    ingest_daily(conn, None, _digest_file(tmp_path, DIGEST_V06), SEARCH)

    match = _match_row(conn, SEARCH, url)
    assert match["state"] == "seen"
    assert match["fit"] == "high"


def test_ingest_daily_preserves_discarded_and_updates_fit(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    ingest_daily(conn, _json_file(tmp_path, SAMPLE_JSON), None, SEARCH)
    url = "https://www.linkedin.com/jobs/view/4447379908"
    match = _match_row(conn, SEARCH, url)
    conn.execute("UPDATE match SET state = 'discarded' WHERE id = ?", (match["id"],))
    conn.commit()

    result = ingest_daily(conn, None, _digest_file(tmp_path, DIGEST_V06), SEARCH)
    assert result.fits_updated == 1

    match = _match_row(conn, SEARCH, url)
    assert match["state"] == "discarded"
    assert match["fit"] == "high"

    second = ingest_daily(conn, None, _digest_file(tmp_path, DIGEST_V06), SEARCH)
    assert second.fits_updated == 0


def test_ingest_daily_preserves_applied_and_application(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    ingest_daily(conn, _json_file(tmp_path, SAMPLE_JSON), None, SEARCH)
    url = "https://www.linkedin.com/jobs/view/4447379908"
    match = _match_row(conn, SEARCH, url)
    conn.execute(
        "INSERT INTO application (match_id, offer_id, note) VALUES (?, ?, 'cv')",
        (match["id"], match["offer_id"]),
    )
    app_id = int(
        conn.execute("SELECT id FROM application WHERE match_id = ?", (match["id"],)).fetchone()["id"]
    )
    conn.execute(
        "INSERT INTO event (application_id, type) VALUES (?, 'applied')", (app_id,)
    )
    conn.execute("UPDATE match SET state = 'applied' WHERE id = ?", (match["id"],))
    conn.commit()

    result = ingest_daily(conn, None, _digest_file(tmp_path, DIGEST_V06), SEARCH)
    assert result.matches_created == 2

    match = _match_row(conn, SEARCH, url)
    assert match["state"] == "applied"
    assert conn.execute("SELECT count(*) FROM application").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM event").fetchone()[0] == 1


def test_ingest_daily_invalid_json_writes_nothing(conn: sqlite3.Connection, tmp_path: Path) -> None:
    valid = {"li-1": {"title": "AI", "company": "Acme", "url": "https://www.linkedin.com/jobs/view/1"}}
    bad = dict(valid)
    bad["li-2"] = {"title": "AI", "url": "https://www.linkedin.com/jobs/view/2"}
    json_file = _json_file(tmp_path, bad)

    with pytest.raises(ImportError):
        ingest_daily(conn, json_file, None, SEARCH)

    assert conn.execute("SELECT count(*) FROM offer").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM search").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM source").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM company").fetchone()[0] == 0


def test_ingest_daily_invalid_digest_writes_nothing(conn: sqlite3.Connection, tmp_path: Path) -> None:
    json_file = _json_file(tmp_path, SAMPLE_JSON)
    digest_file = _digest_file(tmp_path, "# Veille\n\nPas de tableau ici.\n")

    with pytest.raises(ImportError):
        ingest_daily(conn, json_file, digest_file, SEARCH)

    assert conn.execute("SELECT count(*) FROM offer").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM search").fetchone()[0] == 0


def test_ingest_daily_requires_an_artifact(conn: sqlite3.Connection) -> None:
    with pytest.raises(ImportError):
        ingest_daily(conn, None, None, SEARCH)


def test_ingest_daily_empty_artifacts_raise(conn: sqlite3.Connection, tmp_path: Path) -> None:
    json_file = _json_file(tmp_path, {})
    with pytest.raises(ImportError):
        ingest_daily(conn, json_file, None, SEARCH)


def test_ingest_daily_reuses_existing_source(conn: sqlite3.Connection, tmp_path: Path) -> None:
    conn.execute("INSERT INTO source (type, name) VALUES ('web', 'linkedin')")
    conn.commit()
    source_id = int(
        conn.execute("SELECT id FROM source WHERE name = 'linkedin'").fetchone()["id"]
    )

    result = ingest_daily(conn, _json_file(tmp_path, SAMPLE_JSON), None, SEARCH)
    assert result.offers_created == 2

    count = conn.execute("SELECT count(*) FROM source WHERE name = 'linkedin'").fetchone()[0]
    assert count == 1
    offer = conn.execute(
        "SELECT * FROM offer WHERE url = ?", ("https://www.linkedin.com/jobs/view/4447379908",)
    ).fetchone()
    assert offer["source_id"] == source_id


# --- import du suivi Markdown ------------------------------------------------


def test_parse_tracker_two_tables(tmp_path: Path) -> None:
    rows = parse_tracker_markdown(_tracker_file(tmp_path))
    assert len(rows) == TRACKER_SAMPLE_COUNTS["rows"]

    manual = rows[:3]
    assert all(row.url.startswith("jobwatch:") for row in manual)
    assert [row.company for row in manual] == ["CNIL", "DGE (Bercy)", "OSIIC"]
    assert [row.sent for row in manual] == [True, False, False]

    veille = rows[3:]
    assert all(row.url.startswith("http") for row in veille)
    assert veille[0].company == "DHM IT"
    assert veille[0].seen_on == "2026-08-03"


def test_parse_tracker_maps_cells(tmp_path: Path) -> None:
    rows = parse_tracker_markdown(_tracker_file(tmp_path))

    cnil = rows[0]
    assert cnil.title == "Ingénieur IA - Service (2026-2346240)"
    assert cnil.sent is True
    assert cnil.applied_at == "2026-08-05"
    assert cnil.fit == "high"
    assert cnil.deadline == "03/09/2026"
    assert cnil.cv_paths == ["documents/cv/cv_cnil.pdf"]
    assert cnil.cover_paths == ["documents/cover_letters/cover_cnil.tex"]

    osiic = rows[2]
    assert osiic.fit == "low"
    assert osiic.deadline is None
    assert osiic.cv_paths == []
    assert osiic.cover_paths == []

    wavestone = rows[4]
    assert wavestone.sent is True
    assert wavestone.applied_at == "2026-07-15"
    assert wavestone.cv_paths == ["documents/cv/cv_cdi_ats.pdf"]
    assert wavestone.cover_paths == ["documents/applications/wavestone_agentic_genai/cover_wavestone_agentic_genai.pdf"]


def test_parse_tracker_accepts_legacy_root_document_paths(tmp_path: Path) -> None:
    text = TRACKER_SAMPLE.replace(
        "documents/cv/cv_cnil.pdf",
        "cv/cv_cnil.pdf",
    ).replace(
        "documents/cover_letters/cover_cnil.tex",
        "cover_letters/cover_cnil.tex",
    )
    cnil = parse_tracker_markdown(_tracker_file(tmp_path, text))[0]
    assert cnil.cv_paths == ["cv/cv_cnil.pdf"]
    assert cnil.cover_paths == ["cover_letters/cover_cnil.tex"]


def test_parse_tracker_title_with_pipe(tmp_path: Path) -> None:
    rows = parse_tracker_markdown(_tracker_file(tmp_path))
    assert rows[5].title == (
        "Machine Learning Engineer - Cloud | MLOps | GenAI (Relocation provided)"
    )
    assert rows[5].company == "Wypoon Technologies"


def test_parse_tracker_synthetic_url_stable(tmp_path: Path) -> None:
    path = _tracker_file(tmp_path)
    first = parse_tracker_markdown(path)[0]
    second = parse_tracker_markdown(path)[0]
    assert first.url.startswith("jobwatch:")
    assert first.url == second.url


def test_parse_tracker_synthetic_url_uses_section_and_company(tmp_path: Path) -> None:
    path = _tracker_file(tmp_path)
    rows = parse_tracker_markdown(path)
    urls = {row.url for row in rows}
    assert len(urls) == len(rows)


def test_parse_tracker_missing_company_raises_with_line(tmp_path: Path) -> None:
    text = TRACKER_SAMPLE + (
        "| [ ] | | high | | Poste sans entreprise | 2026-08-04 | "
        "https://www.linkedin.com/jobs/view/9 | | |\n"
    )
    path = _tracker_file(tmp_path, text)
    with pytest.raises(ImportError, match="ligne"):
        parse_tracker_markdown(path)


def test_parse_tracker_missing_title_raises_with_section(tmp_path: Path) -> None:
    text = TRACKER_SAMPLE + (
        "| [ ] | | high | Société X | | 2026-08-04 | "
        "https://www.linkedin.com/jobs/view/10 | | |\n"
    )
    path = _tracker_file(tmp_path, text)
    with pytest.raises(ImportError, match="Veille automatisée"):
        parse_tracker_markdown(path)


def test_parse_tracker_ignores_non_tracker_tables(tmp_path: Path) -> None:
    text = """## Notes

| Colonne A | Colonne B |
|---|---|
| x | y |
"""
    rows = parse_tracker_markdown(_tracker_file(tmp_path, text))
    assert rows == []


def test_parse_tracker_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ImportError):
        parse_tracker_markdown(tmp_path / "absent.md")


def test_import_tracker_creates_everything(conn: sqlite3.Connection, tmp_path: Path) -> None:
    result = import_tracker(conn, _tracker_file(tmp_path), SEARCH)
    assert result.rows_imported == TRACKER_SAMPLE_COUNTS["rows"]
    assert result.offers_created == TRACKER_SAMPLE_COUNTS["offers"]
    assert result.matches_created == TRACKER_SAMPLE_COUNTS["matches"]
    assert result.applications_created == TRACKER_SAMPLE_COUNTS["applications"]
    assert result.events_created == TRACKER_SAMPLE_COUNTS["events"]
    assert result.documents_created == TRACKER_SAMPLE_COUNTS["documents"]
    assert result.rows_already_present == 0

    assert conn.execute("SELECT count(*) FROM offer").fetchone()[0] == 6
    assert conn.execute("SELECT count(*) FROM match").fetchone()[0] == 6
    assert conn.execute("SELECT count(*) FROM application").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM event").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM document").fetchone()[0] == 4
    assert conn.execute("SELECT count(*) FROM source").fetchone()[0] == 1
    assert conn.execute(
        "SELECT count(*) FROM source WHERE name = 'markdown-import'"
    ).fetchone()[0] == 1


def test_import_tracker_rejects_file_without_tracker_rows(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    path = _tracker_file(tmp_path, "# Notes\n\nAucune table de suivi.\n")
    with pytest.raises(ImportError, match="aucune ligne"):
        import_tracker(conn, path, SEARCH)
    assert conn.execute("SELECT count(*) FROM search").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM source").fetchone()[0] == 0


def test_import_tracker_states_and_fit(conn: sqlite3.Connection, tmp_path: Path) -> None:
    import_tracker(conn, _tracker_file(tmp_path), SEARCH)

    cnil = _match_for_company_title(conn, SEARCH, "CNIL", "Ingénieur IA - Service (2026-2346240)")
    assert cnil["state"] == "applied"
    assert cnil["fit"] == "high"

    dge = _match_for_company_title(conn, SEARCH, "DGE (Bercy)", "Chef de projets IA (MEF_2026-32117)")
    assert dge["state"] == "seen"
    assert dge["fit"] == "medium"

    dhm = _match_for_company_title(
        conn, SEARCH, "DHM IT", "Ingénieur Plateforme IA Agentique (H/F)"
    )
    assert dhm["state"] == "seen"
    assert dhm["fit"] == "high"

    wavestone = _match_for_company_title(
        conn, SEARCH, "Wavestone", "Consultant·e Agentic & GenAI Engineer (H/F)"
    )
    assert wavestone["state"] == "applied"
    assert wavestone["fit"] == "high"


def test_import_tracker_dates_and_deadline(conn: sqlite3.Connection, tmp_path: Path) -> None:
    import_tracker(conn, _tracker_file(tmp_path), SEARCH)

    cnil = _match_for_company_title(conn, SEARCH, "CNIL", "Ingénieur IA - Service (2026-2346240)")
    offer = conn.execute("SELECT * FROM offer WHERE id = ?", (cnil["offer_id"],)).fetchone()
    assert offer["deadline"] == "03/09/2026"
    assert offer["published_at"] is None

    event = conn.execute(
        "SELECT * FROM event WHERE application_id = "
        "(SELECT id FROM application WHERE match_id = ?)",
        (cnil["id"],),
    ).fetchone()
    assert event["type"] == "applied"
    assert event["at"] == "2026-08-05"
    application = conn.execute(
        "SELECT * FROM application WHERE match_id = ?", (cnil["id"],)
    ).fetchone()
    assert application["created_at"] == "2026-08-05"
    sent_dates = {
        row["sent_at"]
        for row in conn.execute(
            "SELECT sent_at FROM document WHERE application_id = ?", (application["id"],)
        )
    }
    assert sent_dates == {"2026-08-05"}

    dhm = _match_for_company_title(
        conn, SEARCH, "DHM IT", "Ingénieur Plateforme IA Agentique (H/F)"
    )
    dhm_offer = conn.execute("SELECT * FROM offer WHERE id = ?", (dhm["offer_id"],)).fetchone()
    assert dhm_offer["collected_at"] == "2026-08-03"

    wavestone = _match_for_company_title(
        conn, SEARCH, "Wavestone", "Consultant·e Agentic & GenAI Engineer (H/F)"
    )
    wavestone_event = conn.execute(
        "SELECT * FROM event WHERE application_id = "
        "(SELECT id FROM application WHERE match_id = ?)",
        (wavestone["id"],),
    ).fetchone()
    assert wavestone_event["at"] == "2026-07-15"


def test_import_tracker_documents_per_type(conn: sqlite3.Connection, tmp_path: Path) -> None:
    import_tracker(conn, _tracker_file(tmp_path), SEARCH)

    cnil = _match_for_company_title(conn, SEARCH, "CNIL", "Ingénieur IA - Service (2026-2346240)")
    cnil_docs = conn.execute(
        "SELECT type, path FROM document WHERE application_id = "
        "(SELECT id FROM application WHERE match_id = ?) ORDER BY type",
        (cnil["id"],),
    ).fetchall()
    assert [(d["type"], d["path"]) for d in cnil_docs] == [
        ("cover_letter", "documents/cover_letters/cover_cnil.tex"),
        ("cv", "documents/cv/cv_cnil.pdf"),
    ]

    wavestone = _match_for_company_title(
        conn, SEARCH, "Wavestone", "Consultant·e Agentic & GenAI Engineer (H/F)"
    )
    wavestone_docs = conn.execute(
        "SELECT type, path FROM document WHERE application_id = "
        "(SELECT id FROM application WHERE match_id = ?) ORDER BY type",
        (wavestone["id"],),
    ).fetchall()
    assert [(d["type"], d["path"]) for d in wavestone_docs] == [
        ("cover_letter", "documents/applications/wavestone_agentic_genai/cover_wavestone_agentic_genai.pdf"),
        ("cv", "documents/cv/cv_cdi_ats.pdf"),
    ]


def test_import_tracker_idempotent(conn: sqlite3.Connection, tmp_path: Path) -> None:
    path = _tracker_file(tmp_path)
    first = import_tracker(conn, path, SEARCH)
    second = import_tracker(conn, path, SEARCH)

    assert second.rows_imported == first.rows_imported
    assert second.rows_already_present == first.rows_imported
    assert second.offers_created == 0
    assert second.matches_created == 0
    assert second.applications_created == 0
    assert second.events_created == 0
    assert second.documents_created == 0
    assert second.fits_updated == 0

    assert conn.execute("SELECT count(*) FROM offer").fetchone()[0] == 6
    assert conn.execute("SELECT count(*) FROM match").fetchone()[0] == 6
    assert conn.execute("SELECT count(*) FROM application").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM event").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM document").fetchone()[0] == 4
    assert conn.execute("SELECT count(*) FROM search").fetchone()[0] == 1


def test_import_tracker_preserves_existing_states(conn: sqlite3.Connection, tmp_path: Path) -> None:
    import_tracker(conn, _tracker_file(tmp_path), SEARCH)

    dhm = _match_for_company_title(
        conn, SEARCH, "DHM IT", "Ingénieur Plateforme IA Agentique (H/F)"
    )
    conn.execute("UPDATE match SET state = 'discarded' WHERE id = ?", (dhm["id"],))
    conn.commit()

    import_tracker(conn, _tracker_file(tmp_path), SEARCH)

    dhm = _match_for_company_title(
        conn, SEARCH, "DHM IT", "Ingénieur Plateforme IA Agentique (H/F)"
    )
    assert dhm["state"] == "discarded"


def test_import_tracker_preserves_discarded_sent_match(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    import_tracker(conn, _tracker_file(tmp_path), SEARCH)
    cnil = _match_for_company_title(
        conn, SEARCH, "CNIL", "Ingénieur IA - Service (2026-2346240)"
    )
    conn.execute("UPDATE match SET state = 'discarded' WHERE id = ?", (cnil["id"],))
    conn.commit()

    import_tracker(conn, _tracker_file(tmp_path), SEARCH)

    cnil = _match_for_company_title(
        conn, SEARCH, "CNIL", "Ingénieur IA - Service (2026-2346240)"
    )
    assert cnil["state"] == "discarded"


def test_import_tracker_sent_row_upgrades_new_match(conn: sqlite3.Connection, tmp_path: Path) -> None:
    import_tracker(conn, _tracker_file(tmp_path), SEARCH)

    cnil = _match_for_company_title(conn, SEARCH, "CNIL", "Ingénieur IA - Service (2026-2346240)")
    conn.execute("UPDATE match SET state = 'new' WHERE id = ?", (cnil["id"],))
    conn.commit()

    import_tracker(conn, _tracker_file(tmp_path), SEARCH)

    cnil = _match_for_company_title(conn, SEARCH, "CNIL", "Ingénieur IA - Service (2026-2346240)")
    assert cnil["state"] == "applied"


def test_import_tracker_merges_existing_offer_by_url(conn: sqlite3.Connection, tmp_path: Path) -> None:
    ingest_daily(conn, None, _digest_file(tmp_path, DIGEST_V06), SEARCH)
    wavestone_url = "https://jobs.smartrecruiters.com/Wavestone1/744000140044025"
    wavestone_title = "Consultant·e Agentic & GenAI Engineer (H/F)"
    source_id = int(
        conn.execute("INSERT INTO source (type, name) VALUES ('web', 'smartrecruiters')").lastrowid
    )
    company_id = int(conn.execute("INSERT INTO company (name) VALUES ('Wavestone')").lastrowid)
    conn.execute(
        "INSERT INTO offer (source_id, company_id, title, url, platform) VALUES (?, ?, ?, ?, 'SmartRecruiters')",
        (source_id, company_id, wavestone_title, wavestone_url),
    )
    conn.commit()

    result = import_tracker(conn, _tracker_file(tmp_path), SEARCH)
    assert result.offers_created == 5
    assert result.rows_already_present == 0

    wavestone = _match_for_company_title(conn, SEARCH, "Wavestone", wavestone_title)
    assert wavestone is not None
    assert wavestone["state"] == "applied"
    assert conn.execute("SELECT count(*) FROM offer WHERE url = ?", (wavestone_url,)).fetchone()[0] == 1


def test_import_tracker_invalid_line_writes_nothing(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    text = TRACKER_SAMPLE + (
        "| [ ] | | high | | Poste sans entreprise | 2026-08-04 | "
        "https://www.linkedin.com/jobs/view/9 | | |\n"
    )
    path = _tracker_file(tmp_path, text)

    with pytest.raises(ImportError, match="ligne"):
        import_tracker(conn, path, SEARCH)

    assert conn.execute("SELECT count(*) FROM offer").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM match").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM search").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM source").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM company").fetchone()[0] == 0


def test_import_tracker_missing_file_raises(conn: sqlite3.Connection, tmp_path: Path) -> None:
    with pytest.raises(ImportError):
        import_tracker(conn, tmp_path / "absent.md", SEARCH)


def test_parse_summaries_preserves_sections_bullets_and_order(tmp_path: Path) -> None:
    path = _summaries_file(
        tmp_path,
        "# Résumés d'offres (high)\n\n> Note liminaire.\n\n"
        "## https://example.com/one\n- Premier fait\n- Deuxième fait\n\n"
        "## https://example.com/two\n- Fait unique\n",
    )

    summaries = parse_summaries_markdown(path)

    assert [summary.url for summary in summaries] == [
        "https://example.com/one",
        "https://example.com/two",
    ]
    assert summaries[0].bullets == ("Premier fait", "Deuxième fait")
    assert summaries[1].bullets == ("Fait unique",)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("## ftp://example.com/job\n- Fait\n", r"URL HTTP\(S\) invalide"),
        ("## https://example.com/job invalide\n- Fait\n", r"URL HTTP\(S\) invalide"),
        ("## https://example.com/job\n", "aucun bullet"),
        ("- Fait sans URL\n", "bullet hors section"),
        ("## https://example.com/job\n- \n", "bullet vide"),
        (
            "## https://example.com/job\n- Un\n## https://example.com/job\n- Deux\n",
            "URL dupliquée",
        ),
    ],
)
def test_parse_summaries_rejects_invalid_content(
    tmp_path: Path, text: str, message: str
) -> None:
    with pytest.raises(ImportError, match=message):
        parse_summaries_markdown(_summaries_file(tmp_path, text))


def test_import_summaries_is_atomic_when_offer_is_missing(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _seed_summary_offer(conn, "https://example.com/known")
    path = _summaries_file(
        tmp_path,
        "## https://example.com/known\n- Connu\n"
        "## https://example.com/missing\n- Absent\n",
    )

    with pytest.raises(ImportError, match="offre.s. absente.s. de la base"):
        import_summaries(conn, path)

    assert conn.execute("SELECT count(*) FROM offer").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM offer_summary").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM summary_bullet").fetchone()[0] == 0


def test_import_summaries_validation_preserves_existing_data(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    first_id = _seed_summary_offer(conn, "https://example.com/first")
    _seed_summary_offer(conn, "https://example.com/second")
    valid = _summaries_file(
        tmp_path,
        "## https://example.com/first\n- Fait original\n",
    )
    import_summaries(conn, valid)
    valid.write_text(
        "## https://example.com/first\n- Fait remplacé\n"
        "## https://example.com/second\n- \n"
    )

    with pytest.raises(ImportError, match="bullet vide"):
        import_summaries(conn, valid)

    stored = conn.execute(
        "SELECT sb.text FROM offer_summary os "
        "JOIN summary_bullet sb ON sb.summary_id = os.id "
        "WHERE os.offer_id = ? ORDER BY sb.position",
        (first_id,),
    ).fetchall()
    assert [row["text"] for row in stored] == ["Fait original"]
    assert conn.execute("SELECT count(*) FROM offer_summary").fetchone()[0] == 1


def test_import_summaries_is_idempotent_and_replaces_modified_summary(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    offer_id = _seed_summary_offer(conn, "https://example.com/job")
    path = _summaries_file(
        tmp_path,
        "## https://example.com/job\n- Premier fait\n- Deuxième fait\n",
    )

    first = import_summaries(conn, path)
    second = import_summaries(conn, path)

    assert first.summaries_created == 1
    assert first.bullets_written == 2
    assert second.summaries_unchanged == 1
    assert second.bullets_written == 0
    summary_id = conn.execute(
        "SELECT id FROM offer_summary WHERE offer_id = ?", (offer_id,)
    ).fetchone()["id"]
    assert [
        row["text"]
        for row in conn.execute(
            "SELECT text FROM summary_bullet WHERE summary_id = ? ORDER BY position",
            (summary_id,),
        )
    ] == ["Premier fait", "Deuxième fait"]

    path.write_text("## https://example.com/job\n- Nouveau fait\n")
    replaced = import_summaries(conn, path)

    assert replaced.summaries_updated == 1
    assert replaced.bullets_written == 1
    assert conn.execute("SELECT count(*) FROM offer_summary").fetchone()[0] == 1
    assert [
        row["text"]
        for row in conn.execute(
            "SELECT text FROM summary_bullet WHERE summary_id = ? ORDER BY position",
            (summary_id,),
        )
    ] == ["Nouveau fait"]
