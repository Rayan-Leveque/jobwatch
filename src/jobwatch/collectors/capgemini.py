"""Collecteur Capgemini : API JSON publique du site carrières.

Une requête par mot-clé renvoie toutes les offres d'un pays d'un coup (marques
Capgemini, Capgemini Engineering, Invent, Sogeti, frog). L'API ne filtre ni la
ville ni le contrat : `locations` (sous-chaînes de la ville) et l'exclusion des
stages et alternances se font ici.
"""

from __future__ import annotations

import logging

import httpx

from jobwatch.collectors.base import RawOffer
from jobwatch.geography import location_matches

log = logging.getLogger(__name__)

PLATFORM = "Capgemini"
API_URL = "https://cg-job-search-microservices.azurewebsites.net/api/job-search"
PAGE_SIZE = 1000
EXCLUDED_CONTRACTS = {"stage", "alternance", "apprentissage", "vie"}
CONTRACTS = {"cdi": "permanent", "cdd": "fixed_term"}


class CapgeminiCollector:
    """Collecte les offres Capgemini d'un pays pour une liste de mots-clés."""

    name = "capgemini"
    source_type = "capgemini"
    platform = PLATFORM

    def __init__(
        self,
        queries: list[str],
        country: str = "fr-fr",
        locations: list[str] | None = None,
        client: httpx.Client | None = None,
        interval_days: int = 1,
        api_url: str = API_URL,
    ) -> None:
        self.queries = queries
        self.country = country
        self.locations = locations or []
        self._client = client
        self.interval_days = interval_days
        self.api_url = api_url
        self.failed_requests = 0

    def _request_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30.0)
        return self._client

    def fetch(self) -> list[RawOffer]:
        self.failed_requests = 0
        offers: dict[str, RawOffer] = {}
        for query in self.queries:
            params = {"country_code": self.country, "size": PAGE_SIZE, "search": query}
            try:
                response = self._request_client().get(self.api_url, params=params)
                response.raise_for_status()
                items = response.json()["data"]
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                self.failed_requests += 1
                log.warning("capgemini request for '%s' failed: %s", query, exc)
                continue
            for item in items:
                offer = _offer_from_item(item)
                if offer is not None and location_matches(offer.location, self.locations):
                    offers.setdefault(offer.url, offer)
        return list(offers.values())


def _offer_from_item(item: object) -> RawOffer | None:
    if not isinstance(item, dict):
        return None
    title = str(item.get("title") or "").strip()
    url = str(item.get("apply_job_url") or "").strip()
    contract = str(item.get("contract_type") or "").strip().casefold()
    if not title or not url.startswith(("http://", "https://")):
        return None
    if contract in EXCLUDED_CONTRACTS:
        return None
    return RawOffer(
        title=title,
        url=url,
        company=str(item.get("brand") or "Capgemini"),
        platform=PLATFORM,
        location=str(item.get("location") or "").strip() or None,
        contract=CONTRACTS.get(contract, "other") if contract else None,
        published_at=str(item.get("updated_at") or "") or None,
    )
