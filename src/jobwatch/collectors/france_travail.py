"""France Travail collector: OAuth2 client-credentials + offers search API."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx

from jobwatch.collectors.base import RawOffer

log = logging.getLogger(__name__)

PLATFORM = "France Travail"

TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=%2Fpartenaire"
SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"

SCOPE = "api_offresdemploiv2 o2dsoffre"
SORT_NEWEST_FIRST = 1

CONTRACT_MAP = {
    "CDI": "permanent",
    "CDD": "fixed_term",
    "MIS": "other",
    "SAI": "other",
}


def map_contract(type_contrat: str | None) -> str | None:
    """Map a France Travail contract code to the internal vocabulary."""
    if type_contrat is None:
        return None
    return CONTRACT_MAP.get(type_contrat.upper())


class FranceTravailCollector:
    """Collect offers from the France Travail partenaire API."""

    name = "france_travail"
    platform = PLATFORM

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        keywords: str,
        department: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.keywords = keywords
        self.department = department
        self._client = client

    def _request_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30.0)
        return self._client

    def _access_token(self) -> str | None:
        client = self._request_client()
        try:
            response = client.post(
                TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "scope": SCOPE,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("france_travail token request failed: %s", exc)
            return None
        try:
            payload = response.json()
        except ValueError:
            log.warning("france_travail token response was not valid JSON")
            return None
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            log.warning("france_travail token response had no access_token")
            return None
        return token

    def _search(self, token: str) -> list[dict]:
        params = {"motsCles": self.keywords, "range": "0-49", "sort": SORT_NEWEST_FIRST}
        if self.department:
            params["departement"] = self.department
        client = self._request_client()
        try:
            response = client.get(
                SEARCH_URL, params=params, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as exc:
            log.warning("france_travail search request failed: %s", exc)
            return []
        if response.status_code in (204, 206):
            if not response.content:
                return []
            try:
                return response.json().get("resultats", [])
            except ValueError:
                return []
        if response.status_code != 200:
            log.warning("france_travail search returned status %s", response.status_code)
            return []
        try:
            payload = response.json()
        except ValueError:
            return []
        return payload.get("resultats", []) if isinstance(payload, dict) else []

    def fetch(self) -> list[RawOffer]:
        token = self._access_token()
        if token is None:
            return []
        offers = []
        for item in self._search(token):
            offer = _offer_from_json(item)
            if offer is not None:
                offers.append(offer)
        return offers


def _offer_from_json(item: dict) -> RawOffer | None:
    title = item.get("intitule")
    if not isinstance(title, str) or not title:
        return None
    company = _company_from_json(item)
    url = _url_from_json(item)
    if not company or not url:
        return None
    location = _libelle(item.get("lieuTravail", {}))
    return RawOffer(
        title=title,
        url=url,
        company=company,
        platform=PLATFORM,
        location=location,
        contract=map_contract(item.get("typeContrat")),
        published_at=_published_at(item.get("dateCreation")),
    )


def _company_from_json(item: dict) -> str | None:
    entreprise = item.get("entreprise")
    if isinstance(entreprise, dict):
        name = entreprise.get("nom")
        if isinstance(name, str) and name:
            return name
    return None


def _url_from_json(item: dict) -> str | None:
    origine = item.get("origineOffre")
    if isinstance(origine, dict):
        url = origine.get("urlOrigine")
        if isinstance(url, str) and url:
            return url
    return None


def _libelle(lieu: dict) -> str | None:
    if not isinstance(lieu, dict):
        return None
    libelle = lieu.get("libelle")
    if isinstance(libelle, str) and libelle:
        return libelle
    return None


def _published_at(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return value
    if dt.tzinfo is None:
        return dt.isoformat()
    return dt.astimezone(UTC).isoformat()
