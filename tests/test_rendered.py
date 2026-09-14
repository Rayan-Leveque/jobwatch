"""Collecteur Playwright : extraction testée hors navigateur, rendu réel pour le parcours CLI."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from click.testing import CliRunner

from jobwatch.cli import cli
from jobwatch.collectors import build_collectors
from jobwatch.collectors.rendered import PlaywrightCollector, offers_from_html
from jobwatch.config import ConfigError, load_config

LISTING_HTML = """
<html><body>
  <a href="/offres/ingenieur-ia-123" class="card">  <b>Ingénieur</b> IA   </a>
  <a href="/offres/ingenieur-ia-123">Doublon même URL</a>
  <a href="/blog/article">Article hors motif</a>
  <a href="mailto:rh@example.tld">Écrire</a>
  <a href="https://autre.example.tld/offres/externe-9">Offre externe au domaine mais au motif</a>
  <a href="/offres/pwd-7"><img src="x.png" alt="logo"></a>
</body></html>
"""


def test_offers_from_html_matches_resolves_and_cleans() -> None:
    offers = offers_from_html(LISTING_HTML, "https://emplois.example.tld/liste", r"/offres/.+")
    by_title = {offer.title: offer for offer in offers}
    assert set(by_title) == {"Ingénieur IA", "Offre externe au domaine mais au motif", "pwd 7"}
    first = by_title["Ingénieur IA"]
    assert first.url == "https://emplois.example.tld/offres/ingenieur-ia-123"
    assert first.platform == "Playwright"
    assert by_title["pwd 7"].url.endswith("/offres/pwd-7")  # Repli sur le chemin.


def test_offers_from_html_rejects_javascript_links() -> None:
    offers = offers_from_html(
        '<a href="javascript:ouvre(1)">Offre JS</a>', "https://a.tld/", r"offre"
    )
    assert offers == []


def _config(tmp_path, url: str) -> None:
    (tmp_path / "config.yaml").write_text(
        f"""db: {tmp_path / 'db.sqlite'}
searches:
  - name: ai
    include: [IA]
sources:
  playwright:
    interval_days: 4
    sites:
      - url: {url}
        name: Emplois test
        link_pattern: "/offres/.+"
"""
    )


def test_playwright_config_and_build_naming(tmp_path) -> None:
    _config(tmp_path, "https://emplois.example.tld/liste/")
    sources = load_config(tmp_path / "config.yaml").sources
    assert sources.playwright is not None
    assert sources.playwright.sites[0].interval_days == 4
    assert [collector.name for collector in build_collectors(sources)] == [
        "playwright:emplois.example.tld"
    ]


@pytest.mark.parametrize(
    "block, message",
    [
        ("playwright: {}", "sites"),
        (("playwright:\n    sites:\n      - url: https://a.tld/\n        name: A\n"
          "        link_pattern: \"(\""), "regex invalide"),
        ("playwright:\n    sites:\n      - url: https://a.tld/\n        name: A", "link_pattern"),
        ("playwright:\n    sites:\n      - url: https://a.tld/\n        link_pattern: x", "name"),
        ("playwright:\n    interval_days: 2\n    sites:\n      - url: https://a.tld/\n        name: A\n        link_pattern: x", "interval_days"),
    ],
)
def test_playwright_rejects_invalid_config(tmp_path, block: str, message: str) -> None:
    (tmp_path / "config.yaml").write_text(
        f"db: {tmp_path / 'db.sqlite'}\nsearches:\n  - name: ai\n    include: [AI]\n"
        f"sources:\n  {block}\n"
    )
    with pytest.raises(ConfigError, match=message):
        load_config(tmp_path / "config.yaml")


@pytest.fixture()
def listing_server():
    served = [LISTING_HTML]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(served[0].encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, served
    server.shutdown()
    server.server_close()
    thread.join(5)


def test_playwright_collector_renders_and_reports_no_match(listing_server) -> None:
    server, served = listing_server
    collector = PlaywrightCollector(
        url=f"http://127.0.0.1:{server.server_port}/liste",
        company="Emplois test",
        link_pattern=r"/offres/.+",
    )
    offers = collector.fetch()
    assert collector.failed_requests == 0
    assert {offer.title for offer in offers} >= {"Ingénieur IA", "pwd 7"}
    assert all(offer.company == "Emplois test" for offer in offers)

    served[0] = "<html><body>Plus aucune offre</body></html>"
    assert collector.fetch() == []
    assert collector.failed_requests == 1  # Zéro lien : la page est devenue méconnaissable.


def test_run_stores_rendered_offers_and_defers_rerun(tmp_path, listing_server) -> None:
    """Parcours CLI réel avec Chromium : rendu, stockage, matching, puis différé."""
    server, served = listing_server
    # Une offre injectée par JavaScript : seul un vrai rendu peut la voir.
    served[0] = served[0].replace(
        "</body>",
        "<script>setTimeout(() => {"
        "const a = document.createElement('a');"
        "a.href = '/offres/data-engineer-77';"
        "a.textContent = 'Data Engineer';"
        "document.body.appendChild(a);"
        "}, 300);</script></body>",
    )
    db = tmp_path / "db.sqlite"
    config = tmp_path / "config.yaml"
    _config(tmp_path, f"http://127.0.0.1:{server.server_port}/liste")
    (tmp_path / "config.yaml").replace(config)
    runner = CliRunner()
    args = ["run", "--config", str(config)]
    first = runner.invoke(cli, args)
    assert first.exit_code == 0, first.output
    assert "4 nouvelles offres collectées, 1 nouveaux matchs" in first.output

    import sqlite3

    conn = sqlite3.connect(db)
    titles = {row[0] for row in conn.execute("SELECT title FROM offer")}
    matches = [row[0] for row in conn.execute(
        "SELECT o.title FROM match m JOIN offer o ON o.id=m.offer_id")]
    conn.close()
    assert titles == {"Ingénieur IA", "pwd 7", "Data Engineer",
                      "Offre externe au domaine mais au motif"}
    assert matches == ["Ingénieur IA"]  # « domaine » ne déclenche plus « AI ».

    second = runner.invoke(cli, args)
    assert second.exit_code == 0, second.output
    assert "1 source(s) différée(s)" in second.output
