"""Tests for collectors, using httpx.MockTransport."""

from __future__ import annotations

import httpx
import pytest

from jobwatch.collectors import build_collectors
from jobwatch.collectors.france_travail import (
    TOKEN_URL,
    FranceTravailCollector,
)
from jobwatch.collectors.linkedin import LinkedInCollector
from jobwatch.collectors.smartrecruiters import SmartRecruitersCollector
from jobwatch.collectors.wttj import WttjCollector
from jobwatch.config import ConfigError, LinkedInQuery, load_config

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
    company_name: str | None = None,
    city: str = "Paris",
    experience: str = "professional",
    employment: str = "Full-Time",
) -> dict:
    return {
        "name": name,
        "id": posting_id,
        "company": {"name": company_name, "identifier": company},
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
        item = _sr_item(company="Wavestone1", company_name="Wavestone")
        return httpx.Response(200, json={"content": [item]})

    offers = _sr_collector(handler).fetch()
    assert len(offers) == 1
    offer = offers[0]
    assert offer.title == "Backend Engineer"
    assert offer.url == "https://jobs.smartrecruiters.com/Acme/abc123"
    assert offer.company == "Wavestone"
    assert offer.location == "Paris"
    assert offer.contract == "permanent"
    assert offer.platform == "SmartRecruiters"
    assert offer.published_at == "2026-01-02"


def test_smartrecruiters_company_falls_back_to_identifier() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        item = _sr_item(company_name=None, company="Wavestone1")
        return httpx.Response(200, json={"content": [item]})

    offers = _sr_collector(handler).fetch()
    assert offers[0].company == "Wavestone1"


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


def test_smartrecruiters_paginates_all_postings() -> None:
    offsets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        offset = request.url.params["offset"]
        offsets.append(offset)
        if offset == "0":
            return httpx.Response(
                200,
                json={"totalFound": 101, "content": [_sr_item(posting_id=str(i)) for i in range(100)]},
            )
        return httpx.Response(200, json={"totalFound": 101, "content": [_sr_item(posting_id="100")]})

    offers = _sr_collector(handler).fetch()
    assert len(offers) == 101
    assert offsets == ["0", "100"]


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


LINKEDIN_HTML = """
<ul>
  <li>
    <div data-entity-urn="urn:li:jobPosting:12345">
      <h3 class="base-search-card__title"> Ingénieur IA &amp; LLM </h3>
      <h4 class="base-search-card__subtitle"><a> Acme France </a></h4>
      <span class="job-search-card__location"> Paris </span>
      <time datetime="2026-08-09"></time>
    </div>
  </li>
</ul>
"""


def test_linkedin_fetch_maps_guest_cards_and_query_params() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, text=LINKEDIN_HTML)

    collector = LinkedInCollector(
        queries=[LinkedInQuery(keywords="LLM engineer", location="Paris, France")],
        hours=72,
        client=_client(handler),
    )
    offers = collector.fetch()
    assert captured == {
        "keywords": "LLM engineer",
        "location": "Paris, France",
        "f_TPR": "r259200",
        "start": "0",
    }
    assert len(offers) == 1
    assert offers[0].title == "Ingénieur IA & LLM"
    assert offers[0].company == "Acme France"
    assert offers[0].location == "Paris"
    assert offers[0].published_at == "2026-08-09"
    assert offers[0].url == "https://www.linkedin.com/jobs/view/12345"


def test_linkedin_continues_after_failed_query() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, text=LINKEDIN_HTML)

    queries = [LinkedInQuery("first", "Paris"), LinkedInQuery("second", "Paris")]
    assert len(LinkedInCollector(queries, client=_client(handler)).fetch()) == 1


def _wttj_hit(
    *,
    title: str = "AI Engineer",
    city: str = "Paris",
    country: str = "FR",
    state: str = "Île-de-France",
    remote: str = "partial",
) -> dict:
    return {
        "objectID": "job-1",
        "name": title,
        "organization": {"name": "Acme", "slug": "acme"},
        "offices": [{"city": city, "country_code": country, "state": state}],
        "published_at": "2026-08-09T08:00:00Z",
        "slug": "ai-engineer_paris",
        "remote": remote,
    }


def _wttj_collector(handler) -> WttjCollector:
    return WttjCollector(
        queries=["AI engineer"],
        countries=["FR", "DE"],
        cities={"DE": ["berlin"]},
        app_id="APP",
        api_key="public-key",
        index="jobs",
        client=_client(handler),
    )


def test_wttj_fetch_maps_hit_and_request() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["body"] = request.read().decode()
        return httpx.Response(200, json={"hits": [_wttj_hit()]})

    offers = _wttj_collector(handler).fetch()
    assert len(offers) == 1
    offer = offers[0]
    assert offer.company == "Acme"
    assert offer.location == "Paris, FR"
    assert offer.contract == "permanent"
    assert offer.url.endswith("/companies/acme/jobs/ai-engineer_paris")
    assert captured["url"] == "https://app-dsn.algolia.net/1/indexes/jobs/query"
    assert captured["headers"]["x-algolia-application-id"] == "APP"
    assert "offices.country_code:FR OR offices.country_code:DE" in captured["body"]


def test_wttj_filters_geo_but_keeps_full_remote() -> None:
    hits = [
        _wttj_hit(title="Munich", city="Munich", country="DE", state="Bavaria"),
        _wttj_hit(title="Remote", city="Munich", country="DE", state="Bavaria", remote="full"),
        _wttj_hit(title="Berlin", city="Berlin", country="DE", state="Berlin"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hits": hits})

    assert [offer.title for offer in _wttj_collector(handler).fetch()] == ["Remote", "Berlin"]


def test_wttj_returns_empty_on_error_or_invalid_json() -> None:
    def network_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    def invalid_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    assert _wttj_collector(network_error).fetch() == []
    assert _wttj_collector(invalid_json).fetch() == []


def test_config_parses_linkedin_and_wttj_sources(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""db: {tmp_path / 'db.sqlite'}
searches:
  - name: ai
    include: [AI]
sources:
  linkedin:
    hours: 72
    queries:
      - keywords: LLM engineer
        location: Paris
  wttj:
    queries: [AI engineer]
    countries: [FR, DE]
    cities:
      DE: [berlin]
    algolia:
      app_id: APP
      api_key: public-key
      index: jobs
"""
    )
    sources = load_config(path).sources
    assert sources.linkedin is not None
    assert sources.linkedin.hours == 72
    assert sources.linkedin.queries == [LinkedInQuery("LLM engineer", "Paris")]
    assert sources.wttj is not None
    assert sources.wttj.cities == {"DE": ["berlin"]}
    assert [collector.name for collector in build_collectors(sources, _client(lambda r: None))] == [
        "linkedin",
        "wttj",
    ]


@pytest.mark.parametrize(
    "source_yaml, message",
    [
        ("linkedin:\n    queries: []", "linkedin.queries"),
        ("linkedin:\n    hours: 0\n    queries:\n      - keywords: IA\n        location: Paris", "linkedin.hours"),
        ("wttj:\n    queries: [IA]\n    countries: [fr]\n    algolia: {}", "countries"),
    ],
)
def test_config_rejects_invalid_public_sources(tmp_path, source_yaml: str, message: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"db: {tmp_path / 'db.sqlite'}\nsearches:\n  - name: ai\n    include: [AI]\n"
        f"sources:\n  {source_yaml}\n"
    )
    with pytest.raises(ConfigError, match=message):
        load_config(path)
