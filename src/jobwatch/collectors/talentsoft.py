"""Collecteur Talentsoft : flux RSS public des offres, un collecteur par site.

Les portails carrières Talentsoft (ministères, CEA, Arkéa, MAIF, Passerelles...)
exposent tous `/handlers/offerRss.ashx?LCID=1036` : les 20 offres les plus
récentes du site, avec titre, lien, catégories et date de publication.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx

from jobwatch.collectors.base import RawOffer

log = logging.getLogger(__name__)

PLATFORM = "Talentsoft"
RSS_PATH = "/handlers/offerRss.ashx"
LOCATION_RE = r"\d{5}\s"


class TalentsoftCollector:
    """Collecte le flux RSS d'un portail Talentsoft."""

    name = "talentsoft"
    source_type = "talentsoft"
    platform = PLATFORM

    def __init__(
        self,
        base_url: str,
        company: str,
        client: httpx.Client | None = None,
        interval_days: int = 1,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.company = company
        self._client = client
        self.interval_days = interval_days
        self.failed_requests = 0

    @property
    def feed_url(self) -> str:
        return f"{self.base_url}{RSS_PATH}"

    def _request_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30.0)
        return self._client

    def fetch(self) -> list[RawOffer]:
        self.failed_requests = 0
        try:
            response = self._request_client().get(self.feed_url)
        except httpx.HTTPError as exc:
            self.failed_requests += 1
            log.warning("talentsoft request for '%s' failed: %s", self.base_url, exc)
            return []
        if response.status_code != 200:
            self.failed_requests += 1
            log.warning(
                "talentsoft request for '%s' returned status %s",
                self.base_url,
                response.status_code,
            )
            return []
        try:
            root = ET.fromstring(response.text)
        except ET.ParseError:
            self.failed_requests += 1
            log.warning("talentsoft feed for '%s' was not valid XML", self.base_url)
            return []
        return [
            offer
            for item in root.findall(".//item")
            if (offer := _offer_from_item(item, self.company)) is not None
        ]


def _offer_from_item(item: ET.Element, company: str) -> RawOffer | None:
    title = (item.findtext("title") or "").strip()
    url = (item.findtext("link") or "").strip()
    if not title or not url.startswith(("http://", "https://")):
        return None
    location = next(
        (
            category.text.strip()
            for category in item.findall("category")
            if category.text and re.search(LOCATION_RE, category.text)
        ),
        None,
    )
    return RawOffer(
        title=title,
        url=url,
        company=company,
        platform=PLATFORM,
        location=location,
        published_at=_published_at(item.findtext("pubDate")),
    )


def _published_at(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith(" Z"):
        text = text[:-2] + " +0000"
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return value
    if parsed.tzinfo is None:
        return parsed.isoformat()
    return parsed.astimezone(UTC).isoformat()


def site_slug(base_url: str) -> str:
    return urlparse(base_url).netloc.casefold()
