"""Collecteurs des ESN (Capgemini, SuccessFactors) servis par un vrai serveur HTTP local."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from click.testing import CliRunner

from jobwatch.cli import cli
from jobwatch.collectors import build_collectors
from jobwatch.collectors.capgemini import CapgeminiCollector
from jobwatch.config import ConfigError, load_config

CAPGEMINI = {"total": 3, "data": [
    {"title": "Ingénieur IA générative", "brand": "Capgemini Invent", "contract_type": "CDI",
     "location": "Issy-les-Moulineaux", "updated_at": "2026-10-08T09:55:30.000Z",
     "apply_job_url": "https://careers.capgemini.com/job/Issy-IA/1/"},
    {"title": "Data scientist IA", "brand": "Sogeti", "contract_type": "CDI",
     "location": "Pau", "apply_job_url": "https://careers.capgemini.com/job/Pau-IA/2/"},
    {"title": "Stage IA", "brand": "Capgemini", "contract_type": "Stage",
     "location": "Paris", "apply_job_url": "https://careers.capgemini.com/job/Paris-Stage/3/"},
]}

RSS = """<?xml version="1.0" encoding="UTF-8" ?><rss version="2.0"><channel>
<item><title><![CDATA[Consultant IA générative H/F (Bezons, FR)]]></title>
<link>https://jobs.example.net/job/Bezons-Consultant-IA/11/?feedId=null&amp;utm_source=J2WRSS</link>
<pubDate>Wed, 21 Oct 2026 2:00:00 GMT</pubDate></item>
<item><title><![CDATA[Ingénieur Innovation H/F (Nantes, FR)]]></title>
<link>https://jobs.example.net/job/Nantes-Innovation/12/?feedId=null</link></item>
</channel></rss>"""


@pytest.fixture()
def server():
    calls: list[str] = []
    status = [200]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            calls.append(self.path)
            self.send_response(status[0])
            self.end_headers()
            body = json.dumps(CAPGEMINI) if self.path.startswith("/api/") else RSS
            self.wfile.write(body.encode())

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}", calls, status
    httpd.shutdown()
    httpd.server_close()
    thread.join(5)


def test_capgemini_keeps_idf_jobs_and_drops_internships(server) -> None:
    base, calls, _status = server
    collector = CapgeminiCollector(
        queries=["IA", "data"], locations=["Paris", "Issy"], api_url=f"{base}/api/job-search",
    )
    offers = collector.fetch()
    # Une offre par URL même quand deux mots-clés la renvoient ; Pau et le stage écartés.
    assert [(offer.title, offer.company, offer.location, offer.contract) for offer in offers] == [
        ("Ingénieur IA générative", "Capgemini Invent", "Issy-les-Moulineaux", "permanent"),
    ]
    assert parse_qs(urlsplit(calls[0]).query) == {
        "country_code": ["fr-fr"], "size": ["1000"], "search": ["IA"],
    }
    assert len(calls) == 2 and collector.failed_requests == 0


def test_capgemini_counts_http_failures(server) -> None:
    base, _calls, status = server
    status[0] = 500
    collector = CapgeminiCollector(queries=["IA"], api_url=f"{base}/api/job-search")
    assert collector.fetch() == [] and collector.failed_requests == 1


def test_run_collects_successfactors_offers_in_idf(server, tmp_path) -> None:
    """Parcours CLI : flux RSS, ville lue dans le titre, URL sans suivi, match créé."""
    base, calls, _status = server
    db = tmp_path / "jobwatch.db"
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "db": str(db),
        "searches": [{"name": "IA", "include": ["IA", "innovation"]}],
        "sources": {"successfactors": {
            "sites": [{"url": base, "name": "Atos"}],
            "keywords": ["IA"],
            "locations": ["Bezons", "Paris"],
        }},
    }))
    result = CliRunner().invoke(cli, ["run", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert parse_qs(urlsplit(calls[0]).query) == {"locale": ["fr_FR"], "keywords": ["(IA)"]}
    assert "1 nouvelles offres collectées, 1 nouveaux matchs" in result.output
    listed = CliRunner().invoke(cli, ["list", "--config", str(config)])
    assert "Consultant IA générative H/F" in listed.output
    assert "Nantes" not in listed.output


def test_esn_sources_config_and_naming(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""db: {tmp_path / 'db.sqlite'}
searches:
  - name: ai
    include: [AI]
sources:
  capgemini:
    queries: [IA]
    locations: [Paris]
  successfactors:
    keywords: [IA]
    sites:
      - url: https://jobs.atos.net/
        name: Atos
"""
    )
    collectors = build_collectors(load_config(path).sources)
    assert [collector.name for collector in collectors] == [
        "capgemini", "successfactors:jobs.atos.net",
    ]


@pytest.mark.parametrize(
    "block, message",
    [
        ("capgemini: {}", "queries"),
        ("capgemini:\n    queries: [IA]\n    country: France", "fr-fr"),
        ("successfactors:\n    keywords: [IA]", "sites"),
        ("successfactors:\n    sites:\n      - url: https://a.example\n        name: A", "keywords"),
        ("successfactors:\n    keywords: [IA]\n    sites:\n      - url: ftp://a\n        name: A",
         "URL HTTP\\(S\\)"),
    ],
)
def test_esn_sources_reject_invalid_config(tmp_path, block: str, message: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"db: {tmp_path / 'db.sqlite'}\nsearches:\n  - name: ai\n    include: [AI]\n"
        f"sources:\n  {block}\n"
    )
    with pytest.raises(ConfigError, match=message):
        load_config(path)


def _hellowork_card(job_id: int, title: str, company: str, location: str, contract: str) -> str:
    """Carte de résultat Hellowork, réduite à la structure réelle du site."""
    return f"""
<div data-controller="intersect" class="flex" data-cy="serpCard">
  <header><div><a data-turbo="false" href="/fr-fr/emplois/{job_id}.html"
     title="{title} - {company}" data-cy="offerTitle">
    <h3 class="inline">
      <span class="block typo-l sm:typo-xl">{title}</span>
      <span class="inline typo-s">{company}</span>
    </h3></a></div></header>
  <div class="mt-6"><div class="flex gap-3">
    <div class="readonly tag-secondary-s w-fit border-0" data-cy="localisationCard">
      {location}
    </div>
    <div class="readonly tag-secondary-s w-fit border-0" data-cy="contractCard">
{contract}
    </div>
  </div></div>
</div>"""


def test_hellowork_collects_cards_until_an_empty_page() -> None:
    import httpx

    from jobwatch.collectors.hellowork import HelloworkCollector

    pages = {
        "1": _hellowork_card(1, "Data Scientist H/F", "Cr&#xE9;dit Mutuel", "Paris 9e - 75", "CDI")
        + _hellowork_card(2, "Stage - Data Scientist H/F", "Acme", "Paris - 75", "Stage")
        + _hellowork_card(3, "Développeur Python H/F", "Beta", "Île-de-France", "CDD"),
        "2": "<html><body>Aucune offre</body></html>",
    }
    requested: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        requested.append(params)
        return httpx.Response(200, text=pages.get(params["p"], ""))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    offers = HelloworkCollector(["data scientist"], pages=5, client=client).fetch()

    assert [(o.title, o.company, o.location, o.contract) for o in offers] == [
        ("Data Scientist H/F", "Crédit Mutuel", "Paris 9e - 75", "permanent"),
        ("Développeur Python H/F", "Beta", "Île-de-France", "fixed_term"),
    ]
    assert offers[0].url == "https://www.hellowork.com/fr-fr/emplois/1.html"
    # Page vide : inutile de demander les suivantes.
    assert [r["p"] for r in requested] == ["1", "2"]
    assert requested[0]["l"] == "Île-de-France"


def test_hellowork_source_is_configured_and_built(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"db: {tmp_path / 'jw.db'}\nsearches: [{{name: ai, include: [AI]}}]\n"
        "sources:\n  hellowork:\n    queries: [data scientist, développeur python]\n"
        "    pages: 3\n"
    )
    config = load_config(path)
    (collector,) = build_collectors(config.sources)
    assert collector.name == "hellowork"
    assert (collector.queries, collector.location, collector.pages) == (
        ["data scientist", "développeur python"], "Île-de-France", 3,
    )
    path.write_text(path.read_text().replace("pages: 3", "pages: 50"))
    with pytest.raises(ConfigError, match="pages"):
        load_config(path)
