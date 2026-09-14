"""Charge et valide config.yaml dans des dataclasses typées."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml

CONTRACTS = {"permanent", "fixed_term", "internship", "other"}
CONFIG_EXAMPLE = "config.example.yaml"

WORKDAY_URL_RE = re.compile(
    r"https://[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com/[A-Za-z-]+/[A-Za-z0-9_-]+"
)


class ConfigError(Exception):
    """Configuration invalide ou manquante. La CLI affiche un message clair et sort avec le code 1."""


def _resolve_llm_bin(binary: str, field_name: str, runner: str) -> str:
    """Résout un binaire LLM en chemin absolu, ou échoue avec une aide exploitable.

    Un cron a un PATH minimal qui n'inclut pas forcément le répertoire nvm où
    vit le runner : avec un nom nu, l'échec du sous-processus serait autrement
    avalé (une offre gardait son texte mais n'obtenait jamais de résumé,
    sans erreur visible avant d'éplucher les logs). Résoudre ici, une fois,
    fait échouer 'jw enrich' tout de suite avec un message actionnable
    plutôt que de dégrader silencieusement chaque appel de résumé.
    """
    path = Path(binary).expanduser()
    if path.is_absolute():
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ConfigError(f"{field_name} : '{binary}' n'est pas exécutable")
        return str(path)
    resolved = shutil.which(binary)
    if resolved is None:
        raise ConfigError(
            f"{field_name} : binaire {runner} '{binary}' introuvable dans PATH "
            "(un cron a souvent un PATH minimal) ; renseignez un chemin absolu, "
            f"par exemple ~/.nvm/versions/node/<version>/bin/{runner}"
        )
    return str(Path(resolved).resolve())


@dataclass
class SearchConfig:
    name: str
    include: list[str]
    exclude: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    contract: str | None = None


@dataclass
class FranceTravailSource:
    client_id: str
    client_secret: str
    keywords: str
    department: str | None = None
    interval_days: int = 1


@dataclass
class SmartRecruitersSource:
    companies: list[str]
    interval_days: int = 1
    company_intervals: dict[str, int] = field(default_factory=dict)


@dataclass
class LinkedInQuery:
    keywords: str
    location: str
    remote: bool = False


@dataclass
class LinkedInSource:
    queries: list[LinkedInQuery]
    hours: int = 48
    from_profile: bool = False
    interval_days: int = 1


@dataclass
class WttjAlgoliaConfig:
    app_id: str
    api_key: str
    index: str


@dataclass
class WttjSource:
    queries: list[str]
    countries: list[str]
    cities: dict[str, list[str]]
    algolia: WttjAlgoliaConfig
    hours: int = 48
    interval_days: int = 1


@dataclass
class TalentsoftSite:
    url: str
    name: str
    interval_days: int = 1


@dataclass
class WorkdaySite:
    url: str
    name: str
    interval_days: int = 1


@dataclass
class WorkdaySource:
    sites: list[WorkdaySite]
    interval_days: int = 1


@dataclass
class RenderedSite:
    url: str
    name: str
    link_pattern: str
    interval_days: int = 1


@dataclass
class RenderedSource:
    sites: list[RenderedSite]
    interval_days: int = 1


@dataclass
class TalentsoftSource:
    sites: list[TalentsoftSite]
    interval_days: int = 1


@dataclass
class SourcesConfig:
    france_travail: FranceTravailSource | None = None
    smartrecruiters: SmartRecruitersSource | None = None
    linkedin: LinkedInSource | None = None
    wttj: WttjSource | None = None
    talentsoft: TalentsoftSource | None = None
    workday: WorkdaySource | None = None
    playwright: RenderedSource | None = None


@dataclass
class NtfyConfig:
    topic: str


@dataclass
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    to: str


@dataclass
class NotifyConfig:
    ntfy: NtfyConfig | None = None
    smtp: SmtpConfig | None = None
    heartbeat: bool = False

    def enabled(self) -> bool:
        return self.ntfy is not None or self.smtp is not None


STANDARD_LLM_RUNNERS = ("opencode", "codex", "openrouter")
ENRICH_RUNNERS = (*STANDARD_LLM_RUNNERS, "openrouter", "pi")
RESEARCH_RUNNERS = (*STANDARD_LLM_RUNNERS, "openrouter")


@dataclass
class EnrichConfig:
    model: str
    # Exécuteur LLM : 'opencode', 'codex', 'openrouter' ou 'pi'.
    runner: str = "opencode"
    opencode_bin: str = "opencode"
    codex_bin: str = "codex"
    pi_bin: str = "pi"
    # Clé API OpenRouter, requise avec le runner 'openrouter'.
    api_key: str = ""
    # Fournisseur OpenRouter imposé, par exemple 'fireworks'.
    provider: str | None = None
    # Effort de raisonnement : --variant OpenCode, model_reasoning_effort Codex
    # ou --thinking Pi.
    variant: str | None = None
    # Appels LLM de résumé simultanés (les fetchs web restent séquentiels et espacés).
    concurrency: int = 4


@dataclass
class ResearchConfig:
    model: str
    runner: str = "codex"
    opencode_bin: str = "opencode"
    codex_bin: str = "codex"
    # Requise avec le runner openrouter : appel HTTP direct, la recherche web
    # passant par le plugin `web` d'OpenRouter.
    api_key: str | None = None
    # Fournisseur OpenRouter imposé, par exemple 'fireworks'.
    provider: str | None = None
    variant: str | None = None
    instructions: str = ""
    recency_days: int = 7
    max_results: int = 50


DRAFT_TRACKS = ("engineer", "project", "all")


@dataclass
class DraftConfig:
    model: str
    # Exécuteur LLM : 'opencode' (API, facturé à l'appel) ou 'codex' (CLI codex exec,
    # couvert par l'abonnement ChatGPT).
    runner: str = "opencode"
    opencode_bin: str = "opencode"
    codex_bin: str = "codex"
    # Clé API OpenRouter, requise avec le runner 'openrouter'.
    api_key: str = ""
    # Effort de raisonnement : --variant OpenCode ou model_reasoning_effort codex.
    variant: str | None = None
    # Lettres exemples .tex par piste métier ('engineer' | 'project' | 'all') : elles
    # donnent au LLM le format LaTeX et le ton des vraies lettres du candidat. Une
    # instance personnalisée utilise la piste unifiée 'all', qui retombe sur la
    # réunion des autres pistes quand elle n'est pas renseignée.
    examples: dict[str, list[Path]] = field(default_factory=dict)


@dataclass
class Config:
    db: Path
    searches: list[SearchConfig]
    sources: SourcesConfig
    notify: NotifyConfig
    research: ResearchConfig | None = None
    enrich: EnrichConfig | None = None
    draft: DraftConfig | None = None


def _string_list(value: object, field_name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{field_name} doit être une liste de chaînes")
    return list(value)


def _search_from_dict(raw: object) -> SearchConfig:
    if not isinstance(raw, dict):
        raise ConfigError("chaque recherche doit être un mapping")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ConfigError("chaque recherche doit avoir un 'name' non vide")
    include = raw.get("include")
    if not isinstance(include, list) or not all(isinstance(i, str) for i in include):
        raise ConfigError(f"recherche '{name}' : 'include' doit être une liste de chaînes")
    if not include:
        raise ConfigError(f"recherche '{name}' : 'include' ne doit pas être vide")
    exclude = _string_list(raw.get("exclude", []), f"search '{name}': 'exclude'")
    locations = _string_list(raw.get("locations", []), f"search '{name}': 'locations'")
    contract = raw.get("contract")
    if contract is not None and contract not in CONTRACTS:
        raise ConfigError(
            f"recherche '{name}' : 'contract' doit être l'une de {sorted(CONTRACTS)}, "
            f"valeur reçue : {contract!r}"
        )
    return SearchConfig(
        name=name, include=include, exclude=exclude, locations=locations, contract=contract
    )


def _interval_days(value: object, field_name: str) -> int:
    if type(value) is not int or value not in (1, 4):
        raise ConfigError(f"{field_name} doit être 1 ou 4 jours")
    return value


def _france_travail_from_dict(raw: object) -> FranceTravailSource:
    if not isinstance(raw, dict):
        raise ConfigError("sources.france_travail doit être un mapping")
    client_id = raw.get("client_id")
    client_secret = raw.get("client_secret")
    keywords = raw.get("keywords")
    if not isinstance(client_id, str) or not client_id:
        raise ConfigError("sources.france_travail.client_id est requis")
    if not isinstance(client_secret, str) or not client_secret:
        raise ConfigError("sources.france_travail.client_secret est requis")
    if client_id.startswith("YOUR_") or client_secret.startswith("YOUR_"):
        raise ConfigError(
            "sources.france_travail : identifiants factices — créez une application sur "
            "https://francetravail.io et renseignez client_id/client_secret"
        )
    if not isinstance(keywords, str):
        raise ConfigError("sources.france_travail.keywords doit être une chaîne")
    department = raw.get("department")
    if department is not None and not isinstance(department, str):
        raise ConfigError("sources.france_travail.department doit être une chaîne")
    return FranceTravailSource(
        client_id=client_id,
        client_secret=client_secret,
        keywords=keywords,
        department=department,
        interval_days=_interval_days(raw.get("interval_days", 1), "sources.france_travail.interval_days"),
    )


def _smartrecruiters_from_dict(raw: object) -> SmartRecruitersSource:
    if not isinstance(raw, dict):
        raise ConfigError("sources.smartrecruiters doit être un mapping")
    companies = raw.get("companies")
    if not isinstance(companies, list) or not all(isinstance(c, str) and c for c in companies):
        raise ConfigError(
            "sources.smartrecruiters.companies doit être une liste non vide de chaînes"
        )
    overrides = raw.get("company_intervals", {})
    if not isinstance(overrides, dict):
        raise ConfigError("sources.smartrecruiters.company_intervals doit être un mapping")
    intervals: dict[str, int] = {}
    for slug, days in overrides.items():
        if slug not in companies:
            raise ConfigError(f"sources.smartrecruiters.company_intervals société inconnue {slug!r}")
        intervals[slug] = _interval_days(days, f"sources.smartrecruiters.company_intervals.{slug}")
    if len({slug.casefold() for slug in companies}) != len(companies):
        raise ConfigError("sources.smartrecruiters.companies contient une société en double")
    return SmartRecruitersSource(
        companies=list(companies), company_intervals=intervals,
        interval_days=_interval_days(raw.get("interval_days", 1), "sources.smartrecruiters.interval_days"),
    )


def _positive_hours(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ConfigError(f"{field_name} doit être un entier >= 1")
    return value


def _linkedin_from_dict(raw: object) -> LinkedInSource:
    if not isinstance(raw, dict):
        raise ConfigError("sources.linkedin doit être un mapping")
    from_profile = raw.get("from_profile", False)
    if not isinstance(from_profile, bool):
        raise ConfigError("sources.linkedin.from_profile doit être un booléen")
    queries_raw = raw.get("queries", [])
    if not isinstance(queries_raw, list) or (not queries_raw and not from_profile):
        raise ConfigError("sources.linkedin.queries doit être une liste non vide")
    queries: list[LinkedInQuery] = []
    for index, query in enumerate(queries_raw):
        if not isinstance(query, dict):
            raise ConfigError(f"sources.linkedin.queries[{index}] doit être un mapping")
        keywords = query.get("keywords")
        location = query.get("location")
        if not isinstance(keywords, str) or not keywords:
            raise ConfigError(f"sources.linkedin.queries[{index}].keywords est requis")
        if not isinstance(location, str) or not location:
            raise ConfigError(f"sources.linkedin.queries[{index}].location est requis")
        queries.append(LinkedInQuery(keywords=keywords, location=location))
    return LinkedInSource(
        queries=queries,
        hours=_positive_hours(raw.get("hours", 48), "sources.linkedin.hours"),
        from_profile=from_profile,
        interval_days=_interval_days(raw.get("interval_days", 1), "sources.linkedin.interval_days"),
    )


def _wttj_from_dict(raw: object) -> WttjSource:
    if not isinstance(raw, dict):
        raise ConfigError("sources.wttj doit être un mapping")
    queries = _string_list(raw.get("queries"), "sources.wttj.queries")
    if not queries or not all(queries):
        raise ConfigError("sources.wttj.queries doit être une liste non vide")
    countries = _string_list(raw.get("countries", ["FR"]), "sources.wttj.countries")
    if not countries or any(len(country) != 2 or country.upper() != country for country in countries):
        raise ConfigError("sources.wttj.countries doit contenir des codes pays ISO en majuscules")
    cities_raw = raw.get("cities", {})
    if not isinstance(cities_raw, dict):
        raise ConfigError("sources.wttj.cities doit être un mapping pays -> liste de villes")
    cities: dict[str, list[str]] = {}
    for country, values in cities_raw.items():
        if not isinstance(country, str) or country not in countries:
            raise ConfigError(f"sources.wttj.cities : pays inconnu {country!r}")
        cities[country] = _string_list(values, f"sources.wttj.cities.{country}")
    algolia_raw = raw.get("algolia")
    if not isinstance(algolia_raw, dict):
        raise ConfigError("sources.wttj.algolia doit être un mapping")
    algolia_values = {}
    for key in ("app_id", "api_key", "index"):
        value = algolia_raw.get(key)
        if not isinstance(value, str) or not value:
            raise ConfigError(f"sources.wttj.algolia.{key} est requis")
        algolia_values[key] = value
    return WttjSource(
        queries=queries,
        countries=countries,
        cities=cities,
        algolia=WttjAlgoliaConfig(**algolia_values),
        hours=_positive_hours(raw.get("hours", 48), "sources.wttj.hours"),
        interval_days=_interval_days(raw.get("interval_days", 1), "sources.wttj.interval_days"),
    )


def _talentsoft_from_dict(raw: object) -> TalentsoftSource:
    if not isinstance(raw, dict):
        raise ConfigError("sources.talentsoft doit être un mapping")
    interval_days = _interval_days(
        raw.get("interval_days", 1), "sources.talentsoft.interval_days"
    )
    sites_raw = raw.get("sites")
    if not isinstance(sites_raw, list) or not sites_raw:
        raise ConfigError("sources.talentsoft.sites doit être une liste non vide")
    sites: list[TalentsoftSite] = []
    for index, site_raw in enumerate(sites_raw):
        if not isinstance(site_raw, dict):
            raise ConfigError(f"sources.talentsoft.sites[{index}] doit être un mapping")
        url = site_raw.get("url")
        name = site_raw.get("name")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ConfigError(f"sources.talentsoft.sites[{index}].url doit être une URL HTTP(S)")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"sources.talentsoft.sites[{index}].name est requis")
        sites.append(
            TalentsoftSite(
                url=url.rstrip("/"),
                name=name,
                interval_days=_interval_days(
                    site_raw.get("interval_days", interval_days),
                    f"sources.talentsoft.sites[{index}].interval_days",
                ),
            )
        )
    if len({site.url.casefold() for site in sites}) != len(sites):
        raise ConfigError("sources.talentsoft.sites contient une URL en double")
    return TalentsoftSource(sites=sites, interval_days=interval_days)


def _workday_from_dict(raw: object) -> WorkdaySource:
    if not isinstance(raw, dict):
        raise ConfigError("sources.workday doit être un mapping")
    interval_days = _interval_days(
        raw.get("interval_days", 1), "sources.workday.interval_days"
    )
    sites_raw = raw.get("sites")
    if not isinstance(sites_raw, list) or not sites_raw:
        raise ConfigError("sources.workday.sites doit être une liste non vide")
    sites: list[WorkdaySite] = []
    for index, site_raw in enumerate(sites_raw):
        if not isinstance(site_raw, dict):
            raise ConfigError(f"sources.workday.sites[{index}] doit être un mapping")
        url = site_raw.get("url")
        name = site_raw.get("name")
        if not isinstance(url, str) or not WORKDAY_URL_RE.fullmatch(url.rstrip("/")):
            raise ConfigError(
                f"sources.workday.sites[{index}].url doit être une URL Workday "
                "(https://<tenant>.wd<N>.myworkdayjobs.com/<locale>/<site>)"
            )
        if not isinstance(name, str) or not name:
            raise ConfigError(f"sources.workday.sites[{index}].name est requis")
        sites.append(
            WorkdaySite(
                url=url.rstrip("/"),
                name=name,
                interval_days=_interval_days(
                    site_raw.get("interval_days", interval_days),
                    f"sources.workday.sites[{index}].interval_days",
                ),
            )
        )
    if len({site.url.casefold() for site in sites}) != len(sites):
        raise ConfigError("sources.workday.sites contient une URL en double")
    return WorkdaySource(sites=sites, interval_days=interval_days)


def _rendered_from_dict(raw: object) -> RenderedSource:
    if not isinstance(raw, dict):
        raise ConfigError("sources.playwright doit être un mapping")
    interval_days = _interval_days(
        raw.get("interval_days", 1), "sources.playwright.interval_days"
    )
    sites_raw = raw.get("sites")
    if not isinstance(sites_raw, list) or not sites_raw:
        raise ConfigError("sources.playwright.sites doit être une liste non vide")
    sites: list[RenderedSite] = []
    for index, site_raw in enumerate(sites_raw):
        if not isinstance(site_raw, dict):
            raise ConfigError(f"sources.playwright.sites[{index}] doit être un mapping")
        url = site_raw.get("url")
        name = site_raw.get("name")
        link_pattern = site_raw.get("link_pattern")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ConfigError(f"sources.playwright.sites[{index}].url doit être une URL HTTP(S)")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"sources.playwright.sites[{index}].name est requis")
        if not isinstance(link_pattern, str) or not link_pattern:
            raise ConfigError(f"sources.playwright.sites[{index}].link_pattern est requis")
        try:
            re.compile(link_pattern)
        except re.error as exc:
            raise ConfigError(
                f"sources.playwright.sites[{index}].link_pattern est une regex invalide : {exc}"
            ) from exc
        sites.append(
            RenderedSite(
                url=url.rstrip("/"),
                name=name,
                link_pattern=link_pattern,
                interval_days=_interval_days(
                    site_raw.get("interval_days", interval_days),
                    f"sources.playwright.sites[{index}].interval_days",
                ),
            )
        )
    if len({site.url.casefold() for site in sites}) != len(sites):
        raise ConfigError("sources.playwright.sites contient une URL en double")
    return RenderedSource(sites=sites, interval_days=interval_days)


def _sources_from_dict(raw: object) -> SourcesConfig:
    if raw is None:
        return SourcesConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'sources' doit être un mapping")
    known = {
        "france_travail", "smartrecruiters", "linkedin", "wttj",
        "talentsoft", "workday", "playwright",
    }
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"type(s) de source inconnu(s) : {sorted(unknown)}")
    return SourcesConfig(
        france_travail=_france_travail_from_dict(raw.get("france_travail"))
        if "france_travail" in raw
        else None,
        smartrecruiters=_smartrecruiters_from_dict(raw.get("smartrecruiters"))
        if "smartrecruiters" in raw
        else None,
        linkedin=_linkedin_from_dict(raw.get("linkedin")) if "linkedin" in raw else None,
        wttj=_wttj_from_dict(raw.get("wttj")) if "wttj" in raw else None,
        talentsoft=_talentsoft_from_dict(raw.get("talentsoft")) if "talentsoft" in raw else None,
        workday=_workday_from_dict(raw.get("workday")) if "workday" in raw else None,
        playwright=_rendered_from_dict(raw.get("playwright")) if "playwright" in raw else None,
    )


def _ntfy_from_dict(raw: object) -> NtfyConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("notify.ntfy doit être un mapping")
    topic = raw.get("topic")
    if not isinstance(topic, str) or not topic:
        raise ConfigError("notify.ntfy.topic est requis quand notify.ntfy est présent")
    return NtfyConfig(topic=topic)


def _smtp_from_dict(raw: object) -> SmtpConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("notify.smtp doit être un mapping")
    host = raw.get("host")
    port = raw.get("port")
    user = raw.get("user")
    password = raw.get("password")
    to = raw.get("to")
    if not isinstance(host, str) or not host:
        raise ConfigError("notify.smtp.host est requis quand notify.smtp est présent")
    if not isinstance(port, int):
        raise ConfigError("notify.smtp.port doit être un entier")
    if not isinstance(user, str) or not user:
        raise ConfigError("notify.smtp.user est requis quand notify.smtp est présent")
    if not isinstance(password, str):
        raise ConfigError("notify.smtp.password doit être une chaîne")
    if not isinstance(to, str) or not to:
        raise ConfigError("notify.smtp.to est requis quand notify.smtp est présent")
    return SmtpConfig(host=host, port=port, user=user, password=password, to=to)


def _notify_from_dict(raw: object) -> NotifyConfig:
    if raw is None:
        return NotifyConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'notify' doit être un mapping")
    heartbeat = raw.get("heartbeat", False)
    if not isinstance(heartbeat, bool):
        raise ConfigError("notify.heartbeat doit être un booléen")
    return NotifyConfig(
        ntfy=_ntfy_from_dict(raw.get("ntfy")),
        smtp=_smtp_from_dict(raw.get("smtp")),
        heartbeat=heartbeat,
    )


def _research_from_dict(raw: object) -> ResearchConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("'research' doit être un mapping")
    if not raw:
        return None
    model = raw.get("model")
    if not isinstance(model, str) or not model:
        raise ConfigError("research.model est requis quand research est présent")
    runner = raw.get("runner", "codex")
    if runner not in RESEARCH_RUNNERS:
        raise ConfigError(
            "research.runner doit être l'un de "
            f"{list(RESEARCH_RUNNERS)}, reçu : {runner!r}"
        )
    api_key = raw.get("api_key")
    if api_key is not None and (not isinstance(api_key, str) or not api_key):
        raise ConfigError("research.api_key doit être une chaîne non vide")
    if runner == "openrouter" and not api_key:
        raise ConfigError("research.api_key est requise avec le runner openrouter")
    provider = raw.get("provider")
    if provider is not None and (not isinstance(provider, str) or not provider.strip()):
        raise ConfigError("research.provider doit être une chaîne non vide")
    opencode_bin = raw.get("opencode_bin", "opencode")
    if not isinstance(opencode_bin, str) or not opencode_bin:
        raise ConfigError("research.opencode_bin doit être une chaîne non vide")
    codex_bin = raw.get("codex_bin", "codex")
    if not isinstance(codex_bin, str) or not codex_bin:
        raise ConfigError("research.codex_bin doit être une chaîne non vide")
    variant = raw.get("variant")
    if variant is not None and (not isinstance(variant, str) or not variant):
        raise ConfigError("research.variant doit être une chaîne non vide")
    instructions = raw.get("instructions", "")
    if not isinstance(instructions, str):
        raise ConfigError("research.instructions doit être une chaîne")
    recency_days = _positive_hours(raw.get("recency_days", 7), "research.recency_days")
    max_results = _positive_hours(raw.get("max_results", 50), "research.max_results")
    return ResearchConfig(
        model=model,
        runner=runner,
        opencode_bin=opencode_bin,
        codex_bin=codex_bin,
        api_key=api_key,
        provider=provider.strip() if provider else None,
        variant=variant,
        instructions=instructions.strip(),
        recency_days=recency_days,
        max_results=max_results,
    )


def _enrich_from_dict(raw: object) -> EnrichConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("'enrich' doit être un mapping")
    if not raw:
        return None
    model = raw.get("model")
    if not isinstance(model, str) or not model:
        raise ConfigError("enrich.model est requis quand enrich est présent")
    runner = raw.get("runner", "opencode")
    if runner not in ENRICH_RUNNERS:
        raise ConfigError(
            f"enrich.runner doit être l'un de {list(ENRICH_RUNNERS)}, reçu : {runner!r}"
        )
    opencode_bin = raw.get("opencode_bin", "opencode")
    if not isinstance(opencode_bin, str) or not opencode_bin:
        raise ConfigError("enrich.opencode_bin doit être une chaîne non vide")
    if runner == "opencode" and "opencode_bin" not in raw:
        raise ConfigError("enrich.opencode_bin est requis avec le runner opencode")
    codex_bin = raw.get("codex_bin", "codex")
    if not isinstance(codex_bin, str) or not codex_bin:
        raise ConfigError("enrich.codex_bin doit être une chaîne non vide")
    if runner == "codex":
        codex_bin = _resolve_llm_bin(codex_bin, "enrich.codex_bin", "codex")
    pi_bin = raw.get("pi_bin", "pi")
    if not isinstance(pi_bin, str) or not pi_bin:
        raise ConfigError("enrich.pi_bin doit être une chaîne non vide")
    if runner == "pi":
        pi_bin = _resolve_llm_bin(pi_bin, "enrich.pi_bin", "pi")
    api_key = raw.get("api_key", "")
    if not isinstance(api_key, str):
        raise ConfigError("enrich.api_key doit être une chaîne")
    if runner == "openrouter" and not api_key.strip():
        raise ConfigError("enrich.api_key est requis avec le runner openrouter")
    provider = raw.get("provider")
    if provider is not None and (not isinstance(provider, str) or not provider.strip()):
        raise ConfigError("enrich.provider doit être une chaîne non vide")
    variant = raw.get("variant")
    if variant is not None and (not isinstance(variant, str) or not variant):
        raise ConfigError("enrich.variant doit être une chaîne non vide")
    concurrency = raw.get("concurrency", 4)
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency < 1:
        raise ConfigError("enrich.concurrency doit être un entier >= 1")
    return EnrichConfig(
        model=model, runner=runner, opencode_bin=opencode_bin, codex_bin=codex_bin,
        pi_bin=pi_bin, api_key=api_key,
        provider=provider.strip() if provider else None,
        variant=variant, concurrency=concurrency,
    )


def _draft_from_dict(raw: object) -> DraftConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("'draft' doit être un mapping")
    if not raw:
        return None
    model = raw.get("model")
    if not isinstance(model, str) or not model:
        raise ConfigError("draft.model est requis quand draft est présent")
    runner = raw.get("runner", "opencode")
    if runner not in STANDARD_LLM_RUNNERS:
        raise ConfigError(
            f"draft.runner doit être l'un de {list(STANDARD_LLM_RUNNERS)}, reçu : {runner!r}"
        )
    opencode_bin = raw.get("opencode_bin", "opencode")
    if not isinstance(opencode_bin, str) or not opencode_bin:
        raise ConfigError("draft.opencode_bin doit être une chaîne non vide")
    if runner == "opencode" and "opencode_bin" not in raw:
        raise ConfigError("draft.opencode_bin est requis avec le runner opencode")
    api_key = str(raw.get("api_key", "") or "")
    if runner == "openrouter" and not api_key.strip():
        raise ConfigError("draft.api_key est requise avec le runner openrouter")
    codex_bin = raw.get("codex_bin", "codex")
    if not isinstance(codex_bin, str) or not codex_bin:
        raise ConfigError("draft.codex_bin doit être une chaîne non vide")
    variant = raw.get("variant")
    if variant is not None and (not isinstance(variant, str) or not variant):
        raise ConfigError("draft.variant doit être une chaîne non vide")
    examples_raw = raw.get("examples", {})
    if not isinstance(examples_raw, dict):
        raise ConfigError("draft.examples doit être un mapping piste -> liste de chemins .tex")
    unknown = set(examples_raw) - set(DRAFT_TRACKS)
    if unknown:
        raise ConfigError(
            f"draft.examples : piste(s) inconnue(s) {sorted(unknown)} "
            f"(pistes valides : {list(DRAFT_TRACKS)})"
        )
    examples: dict[str, list[Path]] = {}
    for track, paths in examples_raw.items():
        entries = _string_list(paths, f"draft.examples.{track}")
        examples[track] = [Path(os.path.expanduser(p)) for p in entries]
    return DraftConfig(
        model=model, runner=runner, opencode_bin=opencode_bin, codex_bin=codex_bin,
        api_key=api_key, variant=variant, examples=examples,
    )


def load_config(path: Path) -> Config:
    """Analyse et valide un fichier de config, levant ConfigError en cas de problème."""
    try:
        text = path.read_text()
    except OSError as exc:
        raise ConfigError(f"impossible de lire le fichier de config {path} : {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML invalide dans {path} : {exc}") from exc
    if raw is None:
        raise ConfigError(f"le fichier de config {path} est vide")
    if not isinstance(raw, dict):
        raise ConfigError(f"le fichier de config {path} doit contenir un mapping")

    db = raw.get("db")
    if not isinstance(db, str) or not db:
        raise ConfigError("'db' doit être un chemin non vide")
    db_path = Path(os.path.expanduser(db))

    searches_raw = raw.get("searches")
    if not isinstance(searches_raw, list):
        raise ConfigError("'searches' doit être une liste")
    sources = _sources_from_dict(raw.get("sources"))
    if not searches_raw and (sources.linkedin is None or not sources.linkedin.from_profile):
        raise ConfigError("'searches' vide nécessite sources.linkedin.from_profile")
    searches = [_search_from_dict(s) for s in searches_raw]

    return Config(
        db=db_path,
        searches=searches,
        sources=sources,
        notify=_notify_from_dict(raw.get("notify")),
        research=_research_from_dict(raw.get("research")),
        enrich=_enrich_from_dict(raw.get("enrich")),
        draft=_draft_from_dict(raw.get("draft")),
    )


def example_config_text() -> str:
    """Renvoie le contenu du config.example.yaml fourni avec le paquet."""
    return resources.files("jobwatch").joinpath(CONFIG_EXAMPLE).read_text()
