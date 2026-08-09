"""Collecteur Welcome to the Jungle via son index Algolia public."""

from __future__ import annotations

import logging
import time
import unicodedata

import httpx

from jobwatch.collectors.base import RawOffer

log = logging.getLogger(__name__)

PLATFORM = "WTTJ"
SOURCE_TYPE = "wttj"
HUMAN_URL = "https://www.welcometothejungle.com/fr/companies/{company}/jobs/{job}"


def _normalized(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in text if not unicodedata.combining(char)).lower()


def _geo_ok(office: dict, cities: dict[str, list[str]]) -> bool:
    country = str(office.get("country_code") or "")
    if country == "FR":
        return "ile-de-france" in _normalized(office.get("state")).replace(" ", "-")
    city = _normalized(office.get("city"))
    return any(_normalized(target) in city for target in cities.get(country, []))


def _offer_from_hit(hit: dict, cities: dict[str, list[str]]) -> RawOffer | None:
    title = hit.get("name")
    organization = hit.get("organization")
    if not isinstance(title, str) or not title or not isinstance(organization, dict):
        return None
    company = organization.get("name")
    company_slug = organization.get("slug")
    job_slug = hit.get("slug")
    if not all(isinstance(value, str) and value for value in (company, company_slug, job_slug)):
        return None
    offices_raw = hit.get("offices")
    offices = [office for office in offices_raw if isinstance(office, dict)] if isinstance(offices_raw, list) else []
    remote = hit.get("remote") == "full"
    matching = [office for office in offices if _geo_ok(office, cities)]
    if not remote and not matching:
        return None
    office = matching[0] if matching else (offices[0] if offices else {})
    location = ", ".join(
        str(value) for value in (office.get("city"), office.get("country_code")) if value
    )
    published = hit.get("published_at")
    return RawOffer(
        title=title.strip(),
        url=HUMAN_URL.format(company=company_slug, job=job_slug),
        company=company,
        platform=PLATFORM,
        location=location or ("Télétravail complet" if remote else None),
        contract="permanent",
        published_at=published if isinstance(published, str) and published else None,
    )


class WttjCollector:
    """Interroge l'index de recherche public utilisé par le site WTTJ."""

    name = "wttj"
    source_type = SOURCE_TYPE
    platform = PLATFORM

    def __init__(
        self,
        queries: list[str],
        countries: list[str],
        cities: dict[str, list[str]],
        app_id: str,
        api_key: str,
        index: str,
        hours: int = 48,
        client: httpx.Client | None = None,
    ) -> None:
        self.queries = queries
        self.countries = countries
        self.cities = cities
        self.app_id = app_id
        self.api_key = api_key
        self.index = index
        self.hours = hours
        self._client = client

    @property
    def search_url(self) -> str:
        return f"https://{self.app_id}-dsn.algolia.net/1/indexes/{self.index}/query"

    def _request_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30.0)
        return self._client

    def fetch(self) -> list[RawOffer]:
        offers: list[RawOffer] = []
        since = int(time.time()) - self.hours * 3600
        country_filter = " OR ".join(
            f"offices.country_code:{country}" for country in self.countries
        )
        headers = {
            "X-Algolia-Application-Id": self.app_id,
            "X-Algolia-API-Key": self.api_key,
            "Referer": "https://www.welcometothejungle.com/",
        }
        for query in self.queries:
            payload = {
                "query": query,
                "hitsPerPage": 50,
                "filters": f"contract_type:full_time AND ({country_filter})",
                "numericFilters": f"published_at_timestamp>{since}",
                "attributesToRetrieve": [
                    "name",
                    "organization.name",
                    "organization.slug",
                    "offices",
                    "published_at",
                    "slug",
                    "remote",
                ],
            }
            try:
                response = self._request_client().post(
                    self.search_url, json=payload, headers=headers
                )
            except httpx.HTTPError as exc:
                log.warning("wttj request for %r failed: %s", query, exc)
                continue
            if response.status_code != 200:
                log.warning("wttj request for %r returned status %s", query, response.status_code)
                continue
            try:
                data = response.json()
            except ValueError:
                log.warning("wttj request for %r returned invalid JSON", query)
                continue
            hits = data.get("hits", []) if isinstance(data, dict) else []
            for hit in hits if isinstance(hits, list) else []:
                if isinstance(hit, dict):
                    offer = _offer_from_hit(hit, self.cities)
                    if offer is not None:
                        offers.append(offer)
        return offers
