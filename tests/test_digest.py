from __future__ import annotations

from pathlib import Path

import httpx

from jobwatch.config import Config, NotifyConfig, NtfyConfig, SourcesConfig
from jobwatch.db import connect, init_db
from jobwatch.digest import send_digest


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
