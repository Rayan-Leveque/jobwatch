"""Collecteur Workday : API cxs servie par un vrai serveur HTTP local."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml
from click.testing import CliRunner

from jobwatch.cli import cli
from jobwatch.collectors import build_collectors
from jobwatch.collectors.workday import WorkdayCollector, cxs_jobs_url
from jobwatch.config import ConfigError, load_config

POSTINGS = {
    "total": 282,
    "jobPostings": [
        {
            "title": "Consultant AMOA F/H",
            "externalPath": "/job/Toulouse-France/Consultant-AMOA-F-H_JR102645-3",
            "locationsText": "Toulouse, France",
            "postedOn": "Publié il y a 2 jours",
        },
        {"title": "   ", "externalPath": "/job/x/1"},
        {"title": "Sans chemin"},
    ],
}

SITE_URL = "https://onepoint.wd3.myworkdayjobs.com/en-US/OnepointFR"


@pytest.fixture()
def cxs_server():
    calls = []
    status = [200]
    body = [POSTINGS]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            length = int(self.headers["Content-Length"])
            calls.append((self.path, json.loads(self.rfile.read(length))))
            self.send_response(status[0])
            self.end_headers()
            self.wfile.write(json.dumps(body[0]).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, calls, status, body
    server.shutdown()
    server.server_close()
    thread.join(5)


def _collector(server) -> WorkdayCollector:
    collector = WorkdayCollector(
        base_url="https://onepoint.wd3.myworkdayjobs.com/en-US/OnepointFR",
        company="Onepoint",
    )
    collector.jobs_url = f"http://127.0.0.1:{server.server_port}/wday/cxs/onepoint/OnepointFR/jobs"
    return collector


def test_cxs_url_is_derived_from_browser_url() -> None:
    assert cxs_jobs_url(SITE_URL) == "https://onepoint.wd3.myworkdayjobs.com/wday/cxs/onepoint/OnepointFR/jobs"


def test_workday_parses_postings_and_skips_invalid_items(cxs_server) -> None:
    server, calls, _status, _body = cxs_server
    offers = _collector(server).fetch()
    assert calls == [(
        "/wday/cxs/onepoint/OnepointFR/jobs",
        {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""},
    )]
    assert len(offers) == 1
    offer = offers[0]
    assert offer.title == "Consultant AMOA F/H"
    assert offer.company == "Onepoint"
    assert offer.platform == "Workday"
    assert offer.location == "Toulouse, France"
    assert offer.url == "https://onepoint.wd3.myworkdayjobs.com/job/Toulouse-France/Consultant-AMOA-F-H_JR102645-3"
    assert offer.published_at is None


def test_workday_marks_http_and_json_failures(cxs_server) -> None:
    server, _calls, status, body = cxs_server
    collector = _collector(server)
    status[0] = 503
    assert collector.fetch() == []
    assert collector.failed_requests == 1
    status[0] = 200
    body[0] = {"jobPostings": "oops"}
    assert collector.fetch() == []
    assert collector.failed_requests == 1
    body[0] = "not json"
    assert collector.fetch() == []
    assert collector.failed_requests == 1


def test_workday_config_and_build_naming(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""db: {tmp_path / 'db.sqlite'}
searches:
  - name: ai
    include: [AI]
sources:
  workday:
    interval_days: 4
    sites:
      - url: {SITE_URL}
        name: Onepoint
"""
    )
    sources = load_config(path).sources
    assert sources.workday is not None
    assert sources.workday.sites[0].interval_days == 4
    assert [collector.name for collector in build_collectors(sources)] == ["workday:onepointfr"]


@pytest.mark.parametrize(
    "url",
    [
        "https://onepoint.wd3.myworkdayjobs.com/OnepointFR",
        "ftp://onepoint.wd3.myworkdayjobs.com/en-US/OnepointFR",
        "https://onepoint.example.com/en-US/OnepointFR",
    ],
)
def test_workday_rejects_non_workday_urls(tmp_path, url: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"db: {tmp_path / 'db.sqlite'}\nsearches:\n  - name: ai\n    include: [AI]\n"
        f"sources:\n  workday:\n    sites:\n      - url: {url}\n        name: X\n"
    )
    with pytest.raises(ConfigError, match="URL Workday"):
        load_config(path)


def test_run_stores_workday_offers_and_defers_rerun(tmp_path, monkeypatch) -> None:
    """Parcours CLI : le site Workday fournit son vrai chemin cxs au serveur local."""
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            calls.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(POSTINGS).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    db = tmp_path / "jobwatch.db"
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "db": str(db),
        "searches": [{"name": "AMOA", "include": ["AMOA"]}],
        "sources": {"workday": {"sites": [
            {"url": "https://onepoint.wd3.myworkdayjobs.com/en-US/OnepointFR", "name": "Onepoint"},
        ]}},
    }))
    monkeypatch.setattr(
        "jobwatch.collectors.workday.cxs_jobs_url",
        lambda base_url: f"http://127.0.0.1:{server.server_port}/wday/cxs/onepoint/OnepointFR/jobs",
    )
    runner = CliRunner()
    args = ["run", "--config", str(config)]
    try:
        first = runner.invoke(cli, args)
        assert first.exit_code == 0, first.output
        assert calls == ["/wday/cxs/onepoint/OnepointFR/jobs"]
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
