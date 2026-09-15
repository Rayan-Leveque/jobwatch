"""Collecteur SmartRecruiters : API publique des offres par slug de société."""

from __future__ import annotations

import logging

import httpx

from jobwatch.collectors.base import RawOffer

log = logging.getLogger(__name__)

PLATFORM = "SmartRecruiters"

BASE_URL = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
HUMAN_URL = "https://jobs.smartrecruiters.com/{slug}/{id}"
INTERNSHIP_EXPERIENCE = "internship"


class SmartRecruitersCollector:
    """Collecte les offres pour une liste de slugs de sociétés depuis SmartRecruiters."""

    name = "smartrecruiters"
    source_type = "smartrecruiters"
    platform = PLATFORM

    def __init__(
        self,
        companies: list[str],
        client: httpx.Client | None = None,
        interval_days: int = 1,
        countries: list[str] | None = None,
        regions: list[str] | None = None,
    ) -> None:
        self.companies = companies
        self.countries = {country.casefold() for country in countries or []}
        self.regions = {region.casefold() for region in regions or []}
        self._client = client
        self.interval_days = interval_days
        self.failed_requests = 0

    def _request_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30.0)
        return self._client

    def _postings_for(self, slug: str) -> list[dict]:
        client = self._request_client()
        postings: list[dict] = []
        offset = 0
        total = 1
        while offset < total:
            try:
                response = client.get(
                    BASE_URL.format(slug=slug), params={"limit": 100, "offset": offset}
                )
            except httpx.HTTPError as exc:
                self.failed_requests += 1
                log.warning("smartrecruiters request for '%s' failed: %s", slug, exc)
                break
            if response.status_code != 200:
                self.failed_requests += 1
                log.warning(
                    "smartrecruiters request for '%s' returned status %s",
                    slug,
                    response.status_code,
                )
                break
            try:
                payload = response.json()
            except ValueError:
                self.failed_requests += 1
                log.warning("smartrecruiters response for '%s' was not valid JSON", slug)
                break
            content = payload.get("content") if isinstance(payload, dict) else None
            if not isinstance(content, list) or any(not isinstance(item, dict) for item in content):
                self.failed_requests += 1
                log.warning("smartrecruiters response for '%s' had invalid content", slug)
                break
            if not content:
                if offset < total and (offset or payload.get("totalFound", 0)):
                    self.failed_requests += 1
                    log.warning("smartrecruiters response for '%s' ended before totalFound", slug)
                break
            postings.extend(item for item in content if isinstance(item, dict))
            offset += len(content)
            found = payload.get("totalFound")
            total = found if isinstance(found, int) and found >= 0 else offset
        return postings

    def fetch(self) -> list[RawOffer]:
        self.failed_requests = 0
        offers = []
        for slug in self.companies:
            for item in self._postings_for(slug):
                if self.countries and _country(item) not in self.countries:
                    continue
                if self.regions and _region(item) not in self.regions:
                    continue
                offer = _offer_from_json(slug, item)
                if offer is not None:
                    offers.append(offer)
        return offers


def _offer_from_json(slug: str, item: dict) -> RawOffer | None:
    title = item.get("name")
    posting_id = item.get("id")
    if not isinstance(title, str) or not title or not isinstance(posting_id, str) or not posting_id:
        return None
    experience = item.get("experienceLevel")
    if isinstance(experience, dict) and experience.get("id") == INTERNSHIP_EXPERIENCE:
        return None
    company = _company_name(item)
    location = _location(item)
    contract = _contract(item)
    return RawOffer(
        title=title,
        url=HUMAN_URL.format(slug=slug, id=posting_id),
        company=company or slug,
        platform=PLATFORM,
        location=location,
        contract=contract,
        published_at=_released_date(item),
    )


def _company_name(item: dict) -> str | None:
    company = item.get("company")
    if isinstance(company, dict):
        name = company.get("name")
        if isinstance(name, str) and name:
            return name
        identifier = company.get("identifier")
        if isinstance(identifier, str) and identifier:
            return identifier
    return None


def _location(item: dict) -> str | None:
    location = item.get("location")
    if not isinstance(location, dict):
        return None
    city = location.get("city")
    if isinstance(city, str) and city:
        return city
    return None


def _country(item: dict) -> str | None:
    location = item.get("location")
    if not isinstance(location, dict):
        return None
    country = location.get("country")
    return country.casefold() if isinstance(country, str) else None


def _region(item: dict) -> str | None:
    location = item.get("location")
    if not isinstance(location, dict):
        return None
    region = location.get("region")
    return region.casefold() if isinstance(region, str) else None


def _contract(item: dict) -> str | None:
    employment = item.get("typeOfEmployment")
    if not isinstance(employment, dict):
        return None
    label = employment.get("label")
    if not isinstance(label, str) or not label:
        return None
    normalized = label.lower()
    if "full" in normalized and ("time" in normalized or "temps" in normalized):
        return "permanent"
    if "intern" in normalized or "stage" in normalized or "apprenticeship" in normalized:
        return "internship"
    if "contract" in normalized or "temporary" in normalized:
        return "fixed_term"
    return None


def _released_date(item: dict) -> str | None:
    value = item.get("releasedDate")
    if isinstance(value, str) and value:
        return value
    return None
