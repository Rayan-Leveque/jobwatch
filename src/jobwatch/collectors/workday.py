"""Collecteur Workday : API cxs publique des portails carrières myworkdayjobs.com.

L'URL du site dans la config est celle du navigateur, par exemple
https://onepoint.wd3.myworkdayjobs.com/en-US/OnepointFR ; l'API cxs en est
déduite (POST /wday/cxs/{tenant}/{site}/jobs). Les 20 offres les plus
récentes sont renvoyées par appel, sans date ISO exploitable : la fenêtre
temporelle n'est pas filtrable côté serveur, le calendrier et la
déduplication suffisent.
"""

from __future__ import annotations

import logging
import re

import httpx

from jobwatch.collectors.base import RawOffer

log = logging.getLogger(__name__)

PLATFORM = "Workday"
WORKDAY_URL_RE = re.compile(
    r"https://[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com/[A-Za-z-]+/[A-Za-z0-9_-]+"
)
POSTINGS_LIMIT = 20


def cxs_jobs_url(base_url: str) -> str:
    """Déduit l'URL cxs de l'URL navigateur d'un site Workday."""
    host, _locale, site = base_url.split("://", 1)[1].split("/")
    tenant = host.split(".", 1)[0]
    return f"https://{host}/wday/cxs/{tenant}/{site}/jobs"


def cxs_site_name(base_url: str) -> str:
    return base_url.rstrip("/").split("/")[-1]


class WorkdayCollector:
    """Collecte les offres d'un site carrières Workday."""

    name = "workday"
    source_type = "workday"
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
        self.jobs_url = cxs_jobs_url(self.base_url)
        self._client = client
        self.interval_days = interval_days
        self.failed_requests = 0

    def _request_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30.0)
        return self._client

    def fetch(self) -> list[RawOffer]:
        self.failed_requests = 0
        try:
            response = self._request_client().post(
                self.jobs_url,
                json={"appliedFacets": {}, "limit": POSTINGS_LIMIT, "offset": 0, "searchText": ""},
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            self.failed_requests += 1
            log.warning("workday request for '%s' failed: %s", self.base_url, exc)
            return []
        if response.status_code != 200:
            self.failed_requests += 1
            log.warning(
                "workday request for '%s' returned status %s", self.base_url, response.status_code
            )
            return []
        try:
            payload = response.json()
        except ValueError:
            self.failed_requests += 1
            log.warning("workday response for '%s' was not valid JSON", self.base_url)
            return []
        postings = payload.get("jobPostings") if isinstance(payload, dict) else None
        if not isinstance(postings, list) or any(not isinstance(p, dict) for p in postings):
            self.failed_requests += 1
            log.warning("workday response for '%s' had invalid jobPostings", self.base_url)
            return []
        offers = []
        for posting in postings:
            offer = _offer_from_posting(posting, self.base_url, self.company)
            if offer is not None:
                offers.append(offer)
        return offers


def _offer_from_posting(posting: dict, base_url: str, company: str) -> RawOffer | None:
    title = posting.get("title")
    path = posting.get("externalPath")
    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    location = posting.get("locationsText")
    return RawOffer(
        title=title.strip(),
        url=f"https://{base_url.split('://', 1)[1].split('/')[0]}{path}",
        company=company,
        platform=PLATFORM,
        location=location if isinstance(location, str) and location.strip() else None,
    )
