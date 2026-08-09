"""Collecteur LinkedIn via l'API publique des offres invitées."""

from __future__ import annotations

import html
import logging
import re

import httpx

from jobwatch.collectors.base import RawOffer
from jobwatch.config import LinkedInQuery

log = logging.getLogger(__name__)

PLATFORM = "LinkedIn"
SOURCE_TYPE = "linkedin"
SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
HUMAN_URL = "https://www.linkedin.com/jobs/view/{id}"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_CARD_RE = re.compile(r"<li(?P<body>.*?)(?=<li|\Z)", re.DOTALL | re.IGNORECASE)
_ID_RE = re.compile(r'data-entity-urn="urn:li:jobPosting:(\d+)"')
_TITLE_RE = re.compile(
    r"base-search-card__title[^>]*>\s*(.*?)\s*</", re.DOTALL | re.IGNORECASE
)
_COMPANY_RE = re.compile(
    r"base-search-card__subtitle[^>]*>\s*(?:<a[^>]*>)?\s*(.*?)\s*</",
    re.DOTALL | re.IGNORECASE,
)
_LOCATION_RE = re.compile(
    r"job-search-card__location[^>]*>\s*(.*?)\s*</", re.DOTALL | re.IGNORECASE
)
_POSTED_RE = re.compile(r'<time[^>]*datetime="([^"]+)"', re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(fragment: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", _TAG_RE.sub("", fragment))).strip()


def _offers_from_html(page: str, fallback_location: str) -> list[RawOffer]:
    offers: list[RawOffer] = []
    for card_match in _CARD_RE.finditer(page):
        card = card_match.group("body")
        posting_id = _ID_RE.search(card)
        title = _TITLE_RE.search(card)
        if posting_id is None or title is None:
            continue
        company = _COMPANY_RE.search(card)
        location = _LOCATION_RE.search(card)
        posted = _POSTED_RE.search(card)
        offers.append(
            RawOffer(
                title=_clean(title.group(1)),
                url=HUMAN_URL.format(id=posting_id.group(1)),
                company=_clean(company.group(1)) if company else "Entreprise non précisée",
                platform=PLATFORM,
                location=_clean(location.group(1)) if location else fallback_location,
                published_at=posted.group(1) if posted else None,
            )
        )
    return offers


class LinkedInCollector:
    """Collecte les cartes publiques LinkedIn sans compte utilisateur."""

    name = "linkedin"
    source_type = SOURCE_TYPE
    platform = PLATFORM

    def __init__(
        self,
        queries: list[LinkedInQuery],
        hours: int = 48,
        client: httpx.Client | None = None,
    ) -> None:
        self.queries = queries
        self.hours = hours
        self._client = client

    def _request_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30.0, headers={"User-Agent": USER_AGENT})
        return self._client

    def fetch(self) -> list[RawOffer]:
        offers: list[RawOffer] = []
        seconds = self.hours * 3600
        for query in self.queries:
            try:
                response = self._request_client().get(
                    SEARCH_URL,
                    params={
                        "keywords": query.keywords,
                        "location": query.location,
                        "f_TPR": f"r{seconds}",
                        "start": 0,
                    },
                )
            except httpx.HTTPError as exc:
                log.warning(
                    "linkedin request for %r in %r failed: %s",
                    query.keywords,
                    query.location,
                    exc,
                )
                continue
            if response.status_code != 200:
                log.warning("linkedin request returned status %s", response.status_code)
                continue
            offers.extend(_offers_from_html(response.text, query.location))
        return offers
