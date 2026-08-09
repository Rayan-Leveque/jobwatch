"""Charge et valide config.yaml dans des dataclasses typées."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml

CONTRACTS = {"permanent", "fixed_term", "internship", "other"}
CONFIG_EXAMPLE = "config.example.yaml"


class ConfigError(Exception):
    """Configuration invalide ou manquante. La CLI affiche un message clair et sort avec le code 1."""


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


@dataclass
class SmartRecruitersSource:
    companies: list[str]


@dataclass
class SourcesConfig:
    france_travail: FranceTravailSource | None = None
    smartrecruiters: SmartRecruitersSource | None = None

    def configured(self) -> list[tuple[str, object]]:
        """Renvoie les paires (source_type, config) pour chaque source configurée."""
        pairs = []
        if self.france_travail is not None:
            pairs.append(("france_travail", self.france_travail))
        if self.smartrecruiters is not None:
            pairs.append(("smartrecruiters", self.smartrecruiters))
        return pairs


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

    def enabled(self) -> bool:
        return self.ntfy is not None or self.smtp is not None


@dataclass
class EnrichConfig:
    opencode_bin: str
    model: str
    # Variante de raisonnement OpenCode (--variant : minimal, high, max...), optionnelle.
    variant: str | None = None
    # Appels LLM de résumé simultanés (les fetchs web restent séquentiels et espacés).
    concurrency: int = 4


DRAFT_TRACKS = ("engineer", "project")


@dataclass
class DraftConfig:
    opencode_bin: str
    model: str
    # Lettres exemples .tex par piste métier ('engineer' | 'project') : elles
    # donnent au LLM le format LaTeX et le ton des vraies lettres du candidat.
    examples: dict[str, list[Path]] = field(default_factory=dict)


@dataclass
class Config:
    db: Path
    searches: list[SearchConfig]
    sources: SourcesConfig
    notify: NotifyConfig
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
    )


def _smartrecruiters_from_dict(raw: object) -> SmartRecruitersSource:
    if not isinstance(raw, dict):
        raise ConfigError("sources.smartrecruiters doit être un mapping")
    companies = raw.get("companies")
    if not isinstance(companies, list) or not all(isinstance(c, str) and c for c in companies):
        raise ConfigError(
            "sources.smartrecruiters.companies doit être une liste non vide de chaînes"
        )
    return SmartRecruitersSource(companies=list(companies))


def _sources_from_dict(raw: object) -> SourcesConfig:
    if raw is None:
        return SourcesConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'sources' doit être un mapping")
    known = {"france_travail", "smartrecruiters"}
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
    return NotifyConfig(
        ntfy=_ntfy_from_dict(raw.get("ntfy")), smtp=_smtp_from_dict(raw.get("smtp"))
    )


def _enrich_from_dict(raw: object) -> EnrichConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("'enrich' doit être un mapping")
    if not raw:
        return None
    opencode_bin = raw.get("opencode_bin")
    model = raw.get("model")
    if not isinstance(opencode_bin, str) or not opencode_bin:
        raise ConfigError("enrich.opencode_bin est requis quand enrich est présent")
    if not isinstance(model, str) or not model:
        raise ConfigError("enrich.model est requis quand enrich est présent")
    variant = raw.get("variant")
    if variant is not None and (not isinstance(variant, str) or not variant):
        raise ConfigError("enrich.variant doit être une chaîne non vide")
    concurrency = raw.get("concurrency", 4)
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency < 1:
        raise ConfigError("enrich.concurrency doit être un entier >= 1")
    return EnrichConfig(
        opencode_bin=opencode_bin, model=model, variant=variant, concurrency=concurrency
    )


def _draft_from_dict(raw: object) -> DraftConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("'draft' doit être un mapping")
    if not raw:
        return None
    opencode_bin = raw.get("opencode_bin")
    model = raw.get("model")
    if not isinstance(opencode_bin, str) or not opencode_bin:
        raise ConfigError("draft.opencode_bin est requis quand draft est présent")
    if not isinstance(model, str) or not model:
        raise ConfigError("draft.model est requis quand draft est présent")
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
    return DraftConfig(opencode_bin=opencode_bin, model=model, examples=examples)


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
    if not searches_raw:
        raise ConfigError("'searches' ne doit pas être vide")
    searches = [_search_from_dict(s) for s in searches_raw]

    return Config(
        db=db_path,
        searches=searches,
        sources=_sources_from_dict(raw.get("sources")),
        notify=_notify_from_dict(raw.get("notify")),
        enrich=_enrich_from_dict(raw.get("enrich")),
        draft=_draft_from_dict(raw.get("draft")),
    )


def example_config_text() -> str:
    """Renvoie le contenu du config.example.yaml fourni avec le paquet."""
    return resources.files("jobwatch").joinpath(CONFIG_EXAMPLE).read_text()
