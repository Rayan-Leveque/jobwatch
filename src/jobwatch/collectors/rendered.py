"""Collecteur Playwright : pages de listes rendues par navigateur.

Pour les sites qui bloquent le HTTP simple ou construisent leur liste en
JavaScript. La config décrit chaque site par l'URL de sa page de liste et
une expression régulière reconnaissant les liens d'offres ; le titre vient
du texte de l'ancre, avec repli sur la fin du chemin. Zéro offre trouvée
est compté comme un échec : la source ne doit pas avancer sur une page
devenue méconnaissable.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse

import httpx

from jobwatch.collectors.base import RawOffer

log = logging.getLogger(__name__)

PLATFORM = "Playwright"
ANCHOR_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
GOTO_TIMEOUT_MS = 45_000
SETTLE_MS = 2_500


def offers_from_html(html: str, base_url: str, link_pattern: str) -> list[RawOffer]:
    """Extrait les offres d'un HTML rendu, hors navigateur, pour être testable."""
    anchors = ANCHOR_RE.findall(html)
    seen: set[str] = set()
    offers: list[RawOffer] = []
    for href, text in anchors:
        url = urljoin(base_url, href.strip())
        if not urlparse(url).scheme in ("http", "https"):
            continue
        if not re.search(link_pattern, url):
            continue
        if url in seen:
            continue
        seen.add(url)
        title = re.sub(r"\s+", " ", TAG_RE.sub("", text)).strip()
        if not title:
            title = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").strip()
        if not title:
            continue
        offers.append(RawOffer(title=title, url=url, company="", platform=PLATFORM))
    return offers


class PlaywrightCollector:
    """Rend une page de liste avec Chromium et en extrait les liens d'offres."""

    name = "playwright"
    source_type = "playwright"
    platform = PLATFORM

    def __init__(
        self,
        url: str,
        company: str,
        link_pattern: str,
        client: httpx.Client | None = None,
        interval_days: int = 1,
    ) -> None:
        self.url = url.rstrip("/")
        self.company = company
        self.link_pattern = link_pattern
        # httpx.Client inutilisé : imposé par le protocole Collector, et repris
        # par la signature commune de build_collectors.
        self._client = client
        self.interval_days = interval_days
        self.failed_requests = 0

    def fetch(self) -> list[RawOffer]:
        self.failed_requests = 0
        from playwright.sync_api import sync_playwright

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    page.goto(self.url, timeout=GOTO_TIMEOUT_MS, wait_until="domcontentloaded")
                    page.wait_for_timeout(SETTLE_MS)
                    html = page.content()
                finally:
                    browser.close()
        except Exception as exc:  # noqa: BLE001 - toute erreur Playwright est un échec réseau
            self.failed_requests += 1
            log.warning("playwright render for '%s' failed: %s", self.url, exc)
            return []
        offers = offers_from_html(html, self.url, self.link_pattern)
        if not offers:
            self.failed_requests += 1
            log.warning(
                "playwright render for '%s' matched no offer link (%s)",
                self.url,
                self.link_pattern,
            )
        for offer in offers:
            offer.company = self.company
        return offers
