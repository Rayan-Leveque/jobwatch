from __future__ import annotations

from pathlib import Path

import httpx

from jobwatch.config import Config, NotifyConfig, NtfyConfig, SourcesConfig
from jobwatch.db import connect, init_db
from jobwatch.digest import _collect_unnotified, send_digest


def test_heartbeat_notifies_when_no_offer() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    conn = connect(":memory:")
    init_db(conn)
    config = Config(
        db=Path(":memory:"),
        searches=[],
        sources=SourcesConfig(),
        notify=NotifyConfig(ntfy=NtfyConfig("test-topic"), heartbeat=True),
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert send_digest(conn, config, client=client) == ["ntfy"]
    assert len(requests) == 1
    assert requests[0].content == b"Aucune nouvelle offre aujourd'hui.\n"
    assert requests[0].headers["title"] == "jobwatch : 0 nouvelles offres"
    conn.close()


def test_no_heartbeat_keeps_zero_offer_run_silent() -> None:
    conn = connect(":memory:")
    init_db(conn)
    config = Config(
        db=Path(":memory:"), searches=[], sources=SourcesConfig(), notify=NotifyConfig()
    )
    assert send_digest(conn, config) == []
    conn.close()


def test_digest_skips_matches_of_an_archived_search() -> None:
    conn = connect(":memory:")
    init_db(conn)
    conn.execute("INSERT INTO source (type, name) VALUES ('test', 'test')")
    source_id = conn.execute("SELECT id FROM source WHERE name = 'test'").fetchone()["id"]
    conn.execute("INSERT INTO company (name) VALUES ('Acme')")
    company_id = conn.execute("SELECT id FROM company WHERE name = 'Acme'").fetchone()["id"]
    for name, url, archived in (("Data", "https://example.com/a", True),
                                ("Ops", "https://example.com/b", False)):
        conn.execute(
            "INSERT INTO search (name, include_json, exclude_json, locations_json, active, "
            "archived_at) VALUES (?, '[]', '[]', '[]', 1, ?)",
            (name, "2026-08-10 10:00:00" if archived else None),
        )
        search_id = conn.execute("SELECT id FROM search WHERE name = ?", (name,)).fetchone()["id"]
        conn.execute(
            "INSERT INTO offer (source_id, company_id, title, url, collected_at) "
            "VALUES (?, ?, 'AI Engineer', ?, datetime('now'))",
            (source_id, company_id, url),
        )
        offer_id = conn.execute("SELECT id FROM offer WHERE url = ?", (url,)).fetchone()["id"]
        conn.execute(
            "INSERT INTO match (search_id, offer_id, state) VALUES (?, ?, 'new')",
            (search_id, offer_id),
        )
    conn.commit()

    groups = _collect_unnotified(conn)

    assert sorted(groups) == ["Ops"]
    conn.close()
