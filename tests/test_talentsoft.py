"""Collecteur Talentsoft : flux RSS servis par un vrai serveur HTTP local."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml
from click.testing import CliRunner

from jobwatch.cli import cli
from jobwatch.collectors import build_collectors
from jobwatch.collectors.talentsoft import TalentsoftCollector
from jobwatch.config import ConfigError, load_config

RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>Offres</title>
<item>
  <link>https://career.example.talent-soft.com/Pages/Offre/detailoffre.aspx?idOffre=31957</link>
  <category>Numérique/Cheffe de projet SI</category>
  <category>  40 avenue des Terroirs de France 75012 PARIS</category>
  <title>2026-31957 - Chef de projet SI H/F</title>
  <pubDate>Mon, 14 Sep 2026 10:52:35 Z</pubDate>
  <description>&lt;b&gt;Domaine :&lt;/b&gt; Numérique</description>
</item>
<item>
  <link>javascript:void(0)</link>
  <title>Offre sans lien valable</title>
</item>
</channel></rss>
"""


@pytest.fixture()
def feed_server():
    calls = []
    status = [200]
    body = [RSS]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            calls.append(self.path)
            self.send_response(status[0])
            self.end_headers()
            self.wfile.write(body[0].encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, calls, status, body
    server.shutdown()
    server.server_close()
    thread.join(5)


def _collector(server) -> TalentsoftCollector:
    return TalentsoftCollector(
        base_url=f"http://127.0.0.1:{server.server_port}", company="Ministère test"
    )


def test_talentsoft_parses_feed_items(feed_server) -> None:
    server, calls, _status, _body = feed_server
    offers = _collector(server).fetch()
    assert calls == ["/handlers/offerRss.ashx"]
    assert len(offers) == 1
    offer = offers[0]
    assert offer.title == "2026-31957 - Chef de projet SI H/F"
    assert offer.company == "Ministère test"
    assert offer.platform == "Talentsoft"
    assert offer.location == "40 avenue des Terroirs de France 75012 PARIS"
    assert offer.url.endswith("idOffre=31957")
    assert offer.published_at == "2026-09-14T10:52:35+00:00"


@pytest.mark.parametrize("status", [500, 404])
def test_talentsoft_marks_http_failures(feed_server, status: int) -> None:
    server, _calls, statuses, _body = feed_server
    statuses[0] = status
    collector = _collector(server)
    assert collector.fetch() == []
    assert collector.failed_requests == 1


def test_talentsoft_marks_invalid_xml(feed_server) -> None:
    server, _calls, _statuses, bodies = feed_server
    bodies[0] = "<rss><channel>"
    collector = _collector(server)
    assert collector.fetch() == []
    assert collector.failed_requests == 1


def test_talentsoft_config_and_build_naming(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""db: {tmp_path / 'db.sqlite'}
searches:
  - name: ai
    include: [AI]
sources:
  talentsoft:
    interval_days: 4
    sites:
      - url: https://ministereinterieur-career.talent-soft.com/
        name: Ministère de l'Intérieur
      - url: https://passerelles.economie.gouv.fr
        name: Passerelles
        interval_days: 1
"""
    )
    sources = load_config(path).sources
    assert sources.talentsoft is not None
    assert [site.interval_days for site in sources.talentsoft.sites] == [4, 1]
    collectors = build_collectors(sources)
    assert [collector.name for collector in collectors] == [
        "talentsoft:ministereinterieur-career.talent-soft.com",
        "talentsoft:passerelles.economie.gouv.fr",
    ]


@pytest.mark.parametrize(
    "block, message",
    [
        ("talentsoft: {}", "sites"),
        ("talentsoft:\n    interval_days: 2\n    sites:\n      - url: https://a.example\n        name: A", "interval_days"),
        ("talentsoft:\n    sites:\n      - url: ftp://a.example\n        name: A", "URL HTTP\\(S\\)"),
        ("talentsoft:\n    sites:\n      - url: https://a.example", "name"),
        (("talentsoft:\n    sites:\n      - url: https://a.example\n        name: A\n"
         "      - url: https://A.example\n        name: B"), "en double"),
    ],
)
def test_talentsoft_rejects_invalid_config(tmp_path, block: str, message: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"db: {tmp_path / 'db.sqlite'}\nsearches:\n  - name: ai\n    include: [AI]\n"
        f"sources:\n  {block}\n"
    )
    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_run_stores_talentsoft_offers_and_defers_rerun(tmp_path) -> None:
    """Parcours CLI : collecte, stockage, matching, puis source différée au run suivant."""

    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            calls.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(RSS.encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    db = tmp_path / "jobwatch.db"
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "db": str(db),
        "searches": [{"name": "SI", "include": ["chef de projet"]}],
        "sources": {"talentsoft": {"sites": [
            {"url": f"http://127.0.0.1:{server.server_port}", "name": "Ministère test"},
        ]}},
    }))
    runner = CliRunner()
    args = ["run", "--config", str(config)]
    try:
        first = runner.invoke(cli, args)
        assert first.exit_code == 0, first.output
        assert calls == ["/handlers/offerRss.ashx"]
        assert "1 nouvelles offres collectées, 1 nouveaux matchs" in first.output
        calls.clear()
        second = runner.invoke(cli, args)
        assert second.exit_code == 0, second.output
        assert calls == []
        assert "1 source(s) différée(s)" in second.output
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
