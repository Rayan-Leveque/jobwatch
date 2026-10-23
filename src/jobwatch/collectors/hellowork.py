"""Collecteur Hellowork : pages de résultats de recherche, rendues côté serveur.

Une page par mot-clé et par numéro de page (30 offres chacune). Le lieu passe
dans le paramètre `l` (une région comme « Île-de-France » est comprise par le
site). Chaque carte donne titre, entreprise, lieu et contrat ; les stages et
alternances sont écartés ici.
"""

from __future__ import annotations

import html
import logging
import re

import httpx

from jobwatch.collectors.base import RawOffer

log = logging.getLogger(__name__)

PLATFORM = "Hellowork"
BASE_URL = "https://www.hellowork.com"
SEARCH_URL = f"{BASE_URL}/fr-fr/emploi/recherche.html"
# Sans en-tête de navigateur, le site sert une page vide.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9",
}
EXCLUDED_CONTRACTS = {"stage", "alternance", "apprentissage", "contrat pro", "intérim"}
CONTRACTS = {"cdi": "permanent", "cdd": "fixed_term"}

_CARD_RE = re.compile(r'data-cy="serpCard"(.*?)(?=data-cy="serpCard"|\Z)', re.DOTALL)
_HREF_RE = re.compile(r'href="(/fr-fr/emplois/\d+\.html)"')
_TITLE_RE = re.compile(r'<span class="block[^"]*">([^<]+)</span>')
_COMPANY_RE = re.compile(r'<span class="inline typo-s">([^<]+)</span>')
_LOCATION_RE = re.compile(r'data-cy="localisationCard"\s*>\s*([^<]+?)\s*<')
_CONTRACT_RE = re.compile(r'data-cy="contractCard"\s*>\s*([^<]+?)\s*<')


class HelloworkCollector:
    """Collecte les offres Hellowork d'un lieu pour une liste de mots-clés."""

    name = "hellowork"
    source_type = "hellowork"
    platform = PLATFORM

    def __init__(
        self,
        queries: list[str],
        location: str = "Île-de-France",
        pages: int = 2,
        client: httpx.Client | None = None,
        interval_days: int = 1,
    ) -> None:
        self.queries = queries
        self.location = location
        self.pages = pages
        self._client = client
        self.interval_days = interval_days
        self.failed_requests = 0

    def _request_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30.0, headers=HEADERS, follow_redirects=True)
        return self._client

    def fetch(self) -> list[RawOffer]:
        self.failed_requests = 0
        offers: dict[str, RawOffer] = {}
        for query in self.queries:
            for page in range(1, self.pages + 1):
                params = {"k": query, "l": self.location, "p": page}
                try:
                    response = self._request_client().get(SEARCH_URL, params=params)
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    self.failed_requests += 1
                    log.warning("hellowork request for '%s' p%d failed: %s", query, page, exc)
                    break
                found = offers_from_html(response.text)
                for offer in found:
                    offers.setdefault(offer.url, offer)
                if not found:
                    break
        return list(offers.values())


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return html.unescape(match.group(1)).strip() if match else None


def offers_from_html(page: str) -> list[RawOffer]:
    """Offres des cartes d'une page de résultats, hors stages et alternances."""
    offers = []
    for card in _CARD_RE.findall(page):
        href, title = _first(_HREF_RE, card), _first(_TITLE_RE, card)
        if not href or not title:
            continue
        contract = (_first(_CONTRACT_RE, card) or "").casefold()
        if contract in EXCLUDED_CONTRACTS:
            continue
        offers.append(RawOffer(
            title=title,
            url=BASE_URL + href,
            company=_first(_COMPANY_RE, card) or "",
            platform=PLATFORM,
            location=_first(_LOCATION_RE, card),
            contract=CONTRACTS.get(contract, "other") if contract else None,
        ))
    return offers
