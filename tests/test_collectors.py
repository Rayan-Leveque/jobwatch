"""Tests for collectors, using httpx.MockTransport."""

from __future__ import annotations

import httpx

from jobwatch.collectors.france_travail import (
    TOKEN_URL,
    FranceTravailCollector,
)
from jobwatch.collectors.smartrecruiters import SmartRecruitersCollector

SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ft_item(
    title: str = "Ingénieur IA",
    url: str = "https://example.com/offre/1",
    company: str = "Acme",
    location: str = "Paris",
    contrat: str = "CDI",
    date: str = "2026-01-02T10:00:00Z",
) -> dict:
    return {
        "intitule": title,
        "origineOffre": {"urlOrigine": url},
        "entreprise": {"nom": company},
        "lieuTravail": {"libelle": location},
        "typeContrat": contrat,
        "dateCreation": date,
    }


def _ft_collector(handler, department: str | None = None) -> FranceTravailCollector:
    return FranceTravailCollector(
        client_id="cid",
        client_secret="csecret",
        keywords="IA",
        department=department,
        client=_client(handler),
    )


def test_france_travail_fetch_maps_offers() -> None:
    item = _ft_item(contrat="CDD")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "POST":
            assert str(request.url) == TOKEN_URL
            assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
            return httpx.Response(200, json={"access_token": "tok123"})
        return httpx.Response(
            206,
            json={"resultats": [item, _ft_item(url="https://example.com/offre/2", contrat="MIS")]},
        )

    offers = _ft_collector(handler).fetch()

    assert len(offers) == 2
    first = offers[0]
    assert first.title == "Ingénieur IA"
    assert first.url == "https://example.com/offre/1"
    assert first.company == "Acme"
    assert first.location == "Paris"
    assert first.contract == "fixed_term"
    assert first.platform == "France Travail"
    assert first.published_at == "2026-01-02T10:00:00+00:00"
    assert offers[1].contract == "other"


def test_france_travail_contract_mapping() -> None:
    from jobwatch.collectors.france_travail import map_contract

    assert map_contract("CDI") == "permanent"
    assert map_contract("CDD") == "fixed_term"
    assert map_contract("MIS") == "other"
    assert map_contract("SAI") == "other"
    assert map_contract("CUI-CAE") is None
    assert map_contract(None) is None


def test_france_travail_search_params() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"access_token": "tok"})
        captured.update(dict(request.url.params))
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(204)

    offers = _ft_collector(handler, department="75").fetch()
    assert offers == []
    assert captured["motsCles"] == "IA"
    assert captured["departement"] == "75"
    assert captured["range"] == "0-49"


def test_france_travail_fetch_returns_empty_on_token_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad credentials"})

    assert _ft_collector(handler).fetch() == []


def test_france_travail_fetch_returns_empty_on_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    assert _ft_collector(handler).fetch() == []


def test_france_travail_search_returns_empty_on_server_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(500)

    assert _ft_collector(handler).fetch() == []


def test_france_travail_skips_items_without_title_or_url() -> None:
    items = [
        {"intitule": "No URL", "entreprise": {"nom": "Acme"}, "typeContrat": "CDI"},
        {"origineOffre": {"urlOrigine": "https://example.com/x"}, "entreprise": {"nom": "Acme"}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(200, json={"resultats": items})

    assert _ft_collector(handler).fetch() == []


def _sr_item(
    name: str = "Backend Engineer",
    posting_id: str = "abc123",
    company: str = "Acme",
    city: str = "Paris",
    experience: str = "professional",
    employment: str = "Full-Time",
) -> dict:
    return {
        "name": name,
        "id": posting_id,
        "company": {"identifier": company},
        "location": {"city": city},
        "experienceLevel": {"id": experience},
        "typeOfEmployment": {"label": employment},
        "releasedDate": "2026-01-02",
    }


def _sr_collector(handler, companies=("Acme",)) -> SmartRecruitersCollector:
    return SmartRecruitersCollector(companies=list(companies), client=_client(handler))


def test_smartrecruiters_fetch_builds_human_url_and_maps_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "companies/Acme" in str(request.url)
        assert request.url.params["limit"] == "100"
        return httpx.Response(200, json={"content": [_sr_item()]})

    offers = _sr_collector(handler).fetch()
    assert len(offers) == 1
    offer = offers[0]
    assert offer.title == "Backend Engineer"
    assert offer.url == "https://jobs.smartrecruiters.com/Acme/abc123"
    assert offer.company == "Acme"
    assert offer.location == "Paris"
    assert offer.contract == "permanent"
    assert offer.platform == "SmartRecruiters"
    assert offer.published_at == "2026-01-02"


def test_smartrecruiters_skips_internships() -> None:
    items = [_sr_item(posting_id="1", experience="internship"), _sr_item(posting_id="2")]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": items})

    offers = _sr_collector(handler).fetch()
    assert len(offers) == 1
    assert offers[0].url.endswith("/2")


def test_smartrecruiters_fetches_all_companies() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [_sr_item(posting_id=str(request.url.path))]})

    offers = _sr_collector(handler, companies=("Acme", "Beta")).fetch()
    assert len(offers) == 2


def test_smartrecruiters_skips_items_without_name_or_id() -> None:
    items = [{"company": {"identifier": "Acme"}}, {"name": "No id"}]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": items})

    assert _sr_collector(handler).fetch() == []


def test_smartrecruiters_falls_back_to_slug_for_company() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [_sr_item(company=None, posting_id="x")]})

    offers = _sr_collector(handler).fetch()
    assert offers[0].company == "Acme"


def test_smartrecruiters_contract_mapping() -> None:
    from jobwatch.collectors.smartrecruiters import _contract

    assert _contract({"typeOfEmployment": {"label": "Full-Time"}}) == "permanent"
    assert _contract({"typeOfEmployment": {"label": "Temporary"}}) == "fixed_term"
    assert _contract({"typeOfEmployment": {"label": "Internship"}}) == "internship"
    assert _contract({"typeOfEmployment": {"label": "Unknown"}}) is None
    assert _contract({}) is None


def test_smartrecruiters_returns_empty_on_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    assert _sr_collector(handler).fetch() == []
