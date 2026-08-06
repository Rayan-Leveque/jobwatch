"""SmartRecruiters collector: public postings API per company slug."""

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
    """Collect offers for a list of company slugs from SmartRecruiters."""

    name = "smartrecruiters"
    platform = PLATFORM

    def __init__(self, companies: list[str], client: httpx.Client | None = None) -> None:
        self.companies = companies
        self._client = client

    def _request_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30.0)
        return self._client

    def _postings_for(self, slug: str) -> list[dict]:
        client = self._request_client()
        try:
            response = client.get(BASE_URL.format(slug=slug), params={"limit": 100})
        except httpx.HTTPError as exc:
            log.warning("smartrecruiters request for '%s' failed: %s", slug, exc)
            return []
        if response.status_code != 200:
            log.warning(
                "smartrecruiters request for '%s' returned status %s", slug, response.status_code
            )
            return []
        try:
            payload = response.json()
        except ValueError:
            return []
        content = payload.get("content", []) if isinstance(payload, dict) else []
        return content if isinstance(content, list) else []

    def fetch(self) -> list[RawOffer]:
        offers = []
        for slug in self.companies:
            for item in self._postings_for(slug):
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
