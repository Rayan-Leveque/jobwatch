"""Tests for jobwatch.enrich : fetch/convert/résumé automatique des offres."""

from __future__ import annotations

import sqlite3

import httpx
import pytest

from jobwatch.config import EnrichConfig
from jobwatch.db import connect, init_db
from jobwatch.enrich import EnrichError, enrich


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = connect(":memory:")
    init_db(connection)
    yield connection
    connection.close()


def _seed_offer(
    conn: sqlite3.Connection,
    source_type: str = "france_travail",
    source_name: str = "france_travail",
    url: str = "https://example.com/offer-1",
    title: str = "AI Engineer",
    match_state: str | None = "new",
) -> int:
    """Sème une offre, avec un match actif par défaut (enrich ignore le reste)."""
    conn.execute("INSERT OR IGNORE INTO source (type, name) VALUES (?, ?)", (source_type, source_name))
    source_id = conn.execute(
        "SELECT id FROM source WHERE name = ?", (source_name,)
    ).fetchone()["id"]
    conn.execute("INSERT OR IGNORE INTO company (name) VALUES ('Acme')")
    company_id = conn.execute("SELECT id FROM company WHERE name = 'Acme'").fetchone()["id"]
    cur = conn.execute(
        "INSERT INTO offer (source_id, company_id, title, url) VALUES (?, ?, ?, ?)",
        (source_id, company_id, title, url),
    )
    offer_id = int(cur.lastrowid)
    if match_state is not None:
        conn.execute(
            "INSERT OR IGNORE INTO search (name, include_json, exclude_json, locations_json) "
            "VALUES ('test', '[]', '[]', '[]')"
        )
        search_id = conn.execute("SELECT id FROM search WHERE name = 'test'").fetchone()["id"]
        conn.execute(
            "INSERT INTO match (search_id, offer_id, state) VALUES (?, ?, ?)",
            (search_id, offer_id, match_state),
        )
    conn.commit()
    return offer_id


def _config() -> EnrichConfig:
    return EnrichConfig(opencode_bin="opencode", model="opencode/deepseek-v4-flash-free")


def _no_sleep(_seconds: float) -> None:
    return None


LONG_HTML = "<html><body><p>" + ("Ingénieur IA Paris. " * 30) + "</p></body></html>"
SHORT_HTML = "<html><body><p>trop court</p></body></html>"


def _http_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_enrich_without_config_raises() -> None:
    conn = connect(":memory:")
    init_db(conn)
    with pytest.raises(EnrichError):
        enrich(conn, None)


def test_enrich_ignores_offers_without_active_match(conn: sqlite3.Connection) -> None:
    """Sans match new/seen/later ni candidature, aucune offre n'est enrichie (aucun token)."""
    _seed_offer(conn, url="https://example.com/no-match", match_state=None)
    _seed_offer(
        conn, url="https://example.com/discarded", match_state="discarded"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no fetch expected for inactive offers")

    result = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)
    assert result.fetched_ok == 0
    assert result.fetched_failed == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM offer_content").fetchone()["n"] == 0


def test_enrich_processes_bridge_offers_with_active_match(
    conn: sqlite3.Connection, monkeypatch
) -> None:
    """Les offres importées du bridge sont enrichies dès qu'un match les rend visibles."""
    offer_id = _seed_offer(
        conn, source_type="web", source_name="linkedin", url="https://linkedin.com/x"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=LONG_HTML)

    monkeypatch.setattr(
        "jobwatch.enrich._summarize",
        lambda config, markdown: ({"experience": "junior"}, ["Poste IA"]),
    )
    result = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)
    assert result.fetched_ok == 1
    assert result.summaries_written == 1
    assert result.fields_written == 1
    field = conn.execute(
        "SELECT sf.value AS value FROM summary_field sf "
        "JOIN offer_summary os ON os.id = sf.summary_id "
        "WHERE os.offer_id = ? AND sf.key = 'experience'",
        (offer_id,),
    ).fetchone()
    assert field["value"] == "junior"


def test_enrich_stores_content_and_summary(conn: sqlite3.Connection, monkeypatch) -> None:
    offer_id = _seed_offer(conn)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=LONG_HTML)

    monkeypatch.setattr(
        "jobwatch.enrich._summarize",
        lambda config, markdown: ({"experience": "3 ans"}, ["Poste IA", "Paris"]),
    )

    result = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)

    assert result.fetched_ok == 1
    assert result.fetched_failed == 0
    assert result.summaries_written == 1

    content = conn.execute(
        "SELECT status, fetch_method, markdown FROM offer_content WHERE offer_id = ?", (offer_id,)
    ).fetchone()
    assert content["status"] == "ok"
    assert content["fetch_method"] == "http"
    assert "Ingénieur IA Paris" in content["markdown"]

    summary = conn.execute(
        "SELECT source FROM offer_summary WHERE offer_id = ?", (offer_id,)
    ).fetchone()
    assert summary["source"] == "auto"
    bullets = [
        row["text"]
        for row in conn.execute(
            "SELECT text FROM summary_bullet sb "
            "JOIN offer_summary os ON os.id = sb.summary_id "
            "WHERE os.offer_id = ? ORDER BY sb.position",
            (offer_id,),
        )
    ]
    assert bullets == ["Poste IA", "Paris"]


def test_enrich_does_not_overwrite_manual_summary(conn: sqlite3.Connection, monkeypatch) -> None:
    offer_id = _seed_offer(conn)
    cur = conn.execute(
        "INSERT INTO offer_summary (offer_id, source) VALUES (?, 'manual')", (offer_id,)
    )
    summary_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO summary_bullet (summary_id, position, text) VALUES (?, 0, 'bullet manuel')",
        (summary_id,),
    )
    conn.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=LONG_HTML)

    def fake_summarize(config, markdown):
        return {"experience": "5 ans"}, ["ne doit jamais être écrit"]

    monkeypatch.setattr("jobwatch.enrich._summarize", fake_summarize)

    result = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)

    assert result.fetched_ok == 1
    assert result.summaries_written == 0
    assert result.fields_written == 1

    row = conn.execute(
        "SELECT source FROM offer_summary WHERE offer_id = ?", (offer_id,)
    ).fetchone()
    assert row["source"] == "manual"
    bullets = [
        row["text"]
        for row in conn.execute(
            "SELECT text FROM summary_bullet sb "
            "JOIN offer_summary os ON os.id = sb.summary_id "
            "WHERE os.offer_id = ? ORDER BY sb.position",
            (offer_id,),
        )
    ]
    assert bullets == ["bullet manuel"]
    # Le contenu complet est stocké même quand le résumé manuel reste intact.
    content = conn.execute(
        "SELECT status FROM offer_content WHERE offer_id = ?", (offer_id,)
    ).fetchone()
    assert content["status"] == "ok"


def test_enrich_falls_back_to_playwright_on_http_failure(
    conn: sqlite3.Connection, monkeypatch
) -> None:
    offer_id = _seed_offer(conn)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    monkeypatch.setattr("jobwatch.enrich._fetch_playwright", lambda url: LONG_HTML)
    monkeypatch.setattr("jobwatch.enrich._summarize", lambda config, markdown: None)

    result = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)

    assert result.fetched_ok == 1
    content = conn.execute(
        "SELECT status, fetch_method FROM offer_content WHERE offer_id = ?", (offer_id,)
    ).fetchone()
    assert content["status"] == "ok"
    assert content["fetch_method"] == "playwright"


def test_enrich_falls_back_to_playwright_on_short_text(
    conn: sqlite3.Connection, monkeypatch
) -> None:
    offer_id = _seed_offer(conn)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=SHORT_HTML)

    monkeypatch.setattr("jobwatch.enrich._fetch_playwright", lambda url: LONG_HTML)
    monkeypatch.setattr("jobwatch.enrich._summarize", lambda config, markdown: None)

    enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)

    content = conn.execute(
        "SELECT status, fetch_method FROM offer_content WHERE offer_id = ?", (offer_id,)
    ).fetchone()
    assert content["status"] == "ok"
    assert content["fetch_method"] == "playwright"


def test_enrich_marks_failed_when_all_fetches_fail(
    conn: sqlite3.Connection, monkeypatch
) -> None:
    offer_id = _seed_offer(conn)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    monkeypatch.setattr("jobwatch.enrich._fetch_playwright", lambda url: None)

    result = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)

    assert result.fetched_ok == 0
    assert result.fetched_failed == 1
    content = conn.execute(
        "SELECT status, markdown, fetch_method FROM offer_content WHERE offer_id = ?", (offer_id,)
    ).fetchone()
    assert content["status"] == "failed"
    assert content["markdown"] is None
    assert content["fetch_method"] is None
    summary = conn.execute(
        "SELECT id FROM offer_summary WHERE offer_id = ?", (offer_id,)
    ).fetchone()
    assert summary is None


def test_enrich_never_retries_processed_offers(conn: sqlite3.Connection, monkeypatch) -> None:
    _seed_offer(conn)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=LONG_HTML)

    monkeypatch.setattr("jobwatch.enrich._summarize", lambda config, markdown: None)

    first = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)
    assert first.fetched_ok == 1
    assert calls == 1

    second = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)
    assert second.fetched_ok == 0
    assert second.fetched_failed == 0
    assert calls == 1  # aucun nouveau fetch
    assert conn.execute("SELECT COUNT(*) AS n FROM offer_content").fetchone()["n"] == 1


def test_enrich_never_retries_failed_offers(conn: sqlite3.Connection, monkeypatch) -> None:
    _seed_offer(conn)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, text="boom")

    monkeypatch.setattr("jobwatch.enrich._fetch_playwright", lambda url: None)

    first = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)
    assert first.fetched_failed == 1
    assert calls == 1

    second = enrich(conn, _config(), client=_http_client(handler), sleep=_no_sleep)
    assert second.fetched_failed == 0
    assert calls == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM offer_content").fetchone()["n"] == 1


def test_enrich_sleeps_between_offers(conn: sqlite3.Connection, monkeypatch) -> None:
    _seed_offer(conn, url="https://example.com/1")
    _seed_offer(conn, url="https://example.com/2")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=LONG_HTML)

    monkeypatch.setattr("jobwatch.enrich._summarize", lambda config, markdown: None)

    sleeps: list[float] = []
    enrich(conn, _config(), client=_http_client(handler), sleep=sleeps.append)

    assert len(sleeps) == 1  # entre les 2 offres, jamais après la dernière
    assert 1.0 <= sleeps[0] <= 2.0


def test_parse_summary_extracts_fields_and_bullets() -> None:
    from jobwatch.enrich import _parse_summary

    text = (
        "EXPERIENCE: 3-5 ans\n"
        "SALAIRE: 45-55k\n"
        "TELETRAVAIL: hybride 2 jours\n"
        "STACK: Python, RAG, AWS\n"
        "- Poste d'ingénieur IA\n"
        "- CDI à Paris\n"
    )
    fields, bullets = _parse_summary(text)
    assert fields == {
        "experience": "3-5 ans",
        "salary": "45-55k",
        "remote": "hybride 2 jours",
        "stack": "Python, RAG, AWS",
    }
    assert bullets == ["Poste d'ingénieur IA", "CDI à Paris"]


def test_parse_summary_tolerates_noise_and_missing_fields() -> None:
    from jobwatch.enrich import _parse_summary

    text = "Voici le résumé :\n**SALAIRE:** \n- Une puce\nTexte libre ignoré\n"
    fields, bullets = _parse_summary(text)
    assert fields == {"salary": "non précisé"}
    assert bullets == ["Une puce"]


def test_enrich_adds_fields_to_offer_with_content_without_refetch(
    conn: sqlite3.Connection, monkeypatch
) -> None:
    """Backfill : contenu déjà en base -> champs ajoutés sans aucun fetch web."""
    offer_id = _seed_offer(conn, match_state="later")
    conn.execute(
        "INSERT INTO offer_content (offer_id, markdown, fetch_method, status) "
        "VALUES (?, 'Annonce IA détaillée.', 'http', 'ok')",
        (offer_id,),
    )
    cur = conn.execute(
        "INSERT INTO offer_summary (offer_id, source) VALUES (?, 'auto')", (offer_id,)
    )
    conn.execute(
        "INSERT INTO summary_bullet (summary_id, position, text) VALUES (?, 0, 'ancienne puce')",
        (int(cur.lastrowid),),
    )
    conn.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no fetch expected: content already stored")

    monkeypatch.setattr(
        "jobwatch.enrich._summarize",
        lambda config, markdown: ({"experience": "senior"}, ["nouvelle puce ignorée"]),
    )
    sleeps: list[float] = []
    result = enrich(conn, _config(), client=_http_client(handler), sleep=sleeps.append)

    assert result.fetched_ok == 0
    assert result.fields_written == 1
    assert result.summaries_written == 0
    assert sleeps == []  # pas de fetch web, pas de pause anti-martèlement
    bullets = [
        row["text"]
        for row in conn.execute(
            "SELECT text FROM summary_bullet sb "
            "JOIN offer_summary os ON os.id = sb.summary_id WHERE os.offer_id = ?",
            (offer_id,),
        )
    ]
    assert bullets == ["ancienne puce"]

    # Une fois les champs écrits, l'offre n'est plus jamais retraitée.
    second = enrich(conn, _config(), client=_http_client(handler), sleep=sleeps.append)
    assert second.fields_written == 0


def test_summarize_passes_variant_to_opencode(monkeypatch) -> None:
    import json
    import subprocess as sp

    from jobwatch.enrich import _summarize

    captured: dict[str, list[str]] = {}

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        event = json.dumps({"type": "text", "part": {"text": "EXPERIENCE: 2 ans\n- Puce"}})
        return sp.CompletedProcess(command, 0, stdout=event, stderr="")

    monkeypatch.setattr("jobwatch.enrich.subprocess.run", fake_run)
    result = _summarize(
        EnrichConfig(opencode_bin="opencode", model="opencode-go/gpt-5.6-luna", variant="max"),
        "texte d'offre",
    )
    assert result == ({"experience": "2 ans"}, ["Puce"])
    command = captured["command"]
    assert command[command.index("--variant") + 1] == "max"
    assert command[command.index("--model") + 1] == "opencode-go/gpt-5.6-luna"

    # Sans variant configuré, le drapeau n'apparaît pas.
    _summarize(EnrichConfig(opencode_bin="opencode", model="m"), "texte")
    assert "--variant" not in captured["command"]
