"""Collecteur SuccessFactors : flux RSS de recherche des sites carrières SAP.

Les sites « Career Site Builder » (jobs.atos.net, etc.) exposent
`/services/rss/job/?locale=fr_FR&keywords=(<mot-clé>)` : les 20 offres les plus
récentes pour ce mot-clé. La ville figure entre parenthèses à la fin du titre,
« Consultant IA H/F (Bezons, FR) » ; `locations` filtre sur cette ville.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit, urlunsplit

import httpx

from jobwatch.collectors.base import RawOffer
from jobwatch.collectors.talentsoft import _published_at
from jobwatch.geography import location_matches

log = logging.getLogger(__name__)

PLATFORM = "SuccessFactors"
RSS_PATH = "/services/rss/job/"
TITLE_LOCATION_RE = re.compile(r"^(?P<title>.+?)\s*\((?P<location>[^()]+,\s*[A-Z]{2})\)\s*$")


class SuccessFactorsCollector:
    """Collecte le flux RSS d'un site carrières SuccessFactors, mot-clé par mot-clé."""

    name = "successfactors"
    source_type = "successfactors"
    platform = PLATFORM

    def __init__(
        self,
        base_url: str,
        company: str,
        keywords: list[str],
        locations: list[str] | None = None,
        client: httpx.Client | None = None,
        interval_days: int = 1,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.company = company
        self.keywords = keywords
        self.locations = locations or []
        self._client = client
        self.interval_days = interval_days
        self.failed_requests = 0

    def _request_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30.0, headers={"User-Agent": "Mozilla/5.0"})
        return self._client

    def fetch(self) -> list[RawOffer]:
        self.failed_requests = 0
        offers: dict[str, RawOffer] = {}
        for keyword in self.keywords:
            params = {"locale": "fr_FR", "keywords": f"({keyword})"}
            try:
                response = self._request_client().get(f"{self.base_url}{RSS_PATH}", params=params)
                response.raise_for_status()
                root = ET.fromstring(response.text)
            except (httpx.HTTPError, ET.ParseError) as exc:
                self.failed_requests += 1
                log.warning("successfactors request for '%s' (%s) failed: %s",
                            self.base_url, keyword, exc)
                continue
            for item in root.findall(".//item"):
                offer = _offer_from_item(item, self.company)
                if offer is not None and location_matches(offer.location, self.locations):
                    offers.setdefault(offer.url, offer)
        return list(offers.values())


def _offer_from_item(item: ET.Element, company: str) -> RawOffer | None:
    raw_title = (item.findtext("title") or "").strip()
    url = (item.findtext("link") or "").strip()
    if not raw_title or not url.startswith(("http://", "https://")):
        return None
    match = TITLE_LOCATION_RE.match(raw_title)
    title, location = (match["title"], match["location"]) if match else (raw_title, None)
    return RawOffer(
        title=title,
        # Sans les paramètres de suivi du flux : la même offre garde la même URL.
        url=urlunsplit(urlsplit(url)._replace(query="", fragment="")),
        company=company,
        platform=PLATFORM,
        location=location,
        published_at=_published_at(item.findtext("pubDate")),
    )


def site_slug(base_url: str) -> str:
    return urlsplit(base_url).netloc.casefold()
