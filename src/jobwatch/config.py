"""Load and validate config.yaml into typed dataclasses."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml

CONTRACTS = {"permanent", "fixed_term", "internship", "other"}
CONFIG_EXAMPLE = "config.example.yaml"


class ConfigError(Exception):
    """Invalid or missing configuration. CLI prints a clean message and exits 1."""


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
        """Return (source_type, config) pairs for every configured source."""
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
class Config:
    db: Path
    searches: list[SearchConfig]
    sources: SourcesConfig
    notify: NotifyConfig


def _string_list(value: object, field_name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{field_name} must be a list of strings")
    return list(value)


def _search_from_dict(raw: object) -> SearchConfig:
    if not isinstance(raw, dict):
        raise ConfigError("each search must be a mapping")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ConfigError("each search must have a non-empty 'name'")
    include = raw.get("include")
    if not isinstance(include, list) or not all(isinstance(i, str) for i in include):
        raise ConfigError(f"search '{name}': 'include' must be a list of strings")
    if not include:
        raise ConfigError(f"search '{name}': 'include' must not be empty")
    exclude = _string_list(raw.get("exclude", []), f"search '{name}': 'exclude'")
    locations = _string_list(raw.get("locations", []), f"search '{name}': 'locations'")
    contract = raw.get("contract")
    if contract is not None and contract not in CONTRACTS:
        raise ConfigError(
            f"search '{name}': 'contract' must be one of {sorted(CONTRACTS)}, got {contract!r}"
        )
    return SearchConfig(
        name=name, include=include, exclude=exclude, locations=locations, contract=contract
    )


def _france_travail_from_dict(raw: object) -> FranceTravailSource:
    if not isinstance(raw, dict):
        raise ConfigError("sources.france_travail must be a mapping")
    client_id = raw.get("client_id")
    client_secret = raw.get("client_secret")
    keywords = raw.get("keywords")
    if not isinstance(client_id, str) or not client_id:
        raise ConfigError("sources.france_travail.client_id is required")
    if not isinstance(client_secret, str) or not client_secret:
        raise ConfigError("sources.france_travail.client_secret is required")
    if not isinstance(keywords, str):
        raise ConfigError("sources.france_travail.keywords must be a string")
    department = raw.get("department")
    if department is not None and not isinstance(department, str):
        raise ConfigError("sources.france_travail.department must be a string")
    return FranceTravailSource(
        client_id=client_id,
        client_secret=client_secret,
        keywords=keywords,
        department=department,
    )


def _smartrecruiters_from_dict(raw: object) -> SmartRecruitersSource:
    if not isinstance(raw, dict):
        raise ConfigError("sources.smartrecruiters must be a mapping")
    companies = raw.get("companies")
    if not isinstance(companies, list) or not all(isinstance(c, str) and c for c in companies):
        raise ConfigError("sources.smartrecruiters.companies must be a non-empty list of strings")
    return SmartRecruitersSource(companies=list(companies))


def _sources_from_dict(raw: object) -> SourcesConfig:
    if raw is None:
        return SourcesConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'sources' must be a mapping")
    known = {"france_travail", "smartrecruiters"}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"unknown source type(s): {sorted(unknown)}")
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
        raise ConfigError("notify.ntfy must be a mapping")
    topic = raw.get("topic")
    if not isinstance(topic, str) or not topic:
        raise ConfigError("notify.ntfy.topic is required when notify.ntfy is present")
    return NtfyConfig(topic=topic)


def _smtp_from_dict(raw: object) -> SmtpConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("notify.smtp must be a mapping")
    host = raw.get("host")
    port = raw.get("port")
    user = raw.get("user")
    password = raw.get("password")
    to = raw.get("to")
    if not isinstance(host, str) or not host:
        raise ConfigError("notify.smtp.host is required when notify.smtp is present")
    if not isinstance(port, int):
        raise ConfigError("notify.smtp.port must be an integer")
    if not isinstance(user, str) or not user:
        raise ConfigError("notify.smtp.user is required when notify.smtp is present")
    if not isinstance(password, str):
        raise ConfigError("notify.smtp.password must be a string")
    if not isinstance(to, str) or not to:
        raise ConfigError("notify.smtp.to is required when notify.smtp is present")
    return SmtpConfig(host=host, port=port, user=user, password=password, to=to)


def _notify_from_dict(raw: object) -> NotifyConfig:
    if raw is None:
        return NotifyConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'notify' must be a mapping")
    return NotifyConfig(
        ntfy=_ntfy_from_dict(raw.get("ntfy")), smtp=_smtp_from_dict(raw.get("smtp"))
    )


def load_config(path: Path) -> Config:
    """Parse and validate a config file, raising ConfigError on any problem."""
    try:
        text = path.read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if raw is None:
        raise ConfigError(f"config file {path} is empty")
    if not isinstance(raw, dict):
        raise ConfigError(f"config file {path} must contain a mapping")

    db = raw.get("db")
    if not isinstance(db, str) or not db:
        raise ConfigError("'db' must be a non-empty string path")
    db_path = Path(os.path.expanduser(db))

    searches_raw = raw.get("searches")
    if not isinstance(searches_raw, list):
        raise ConfigError("'searches' must be a list")
    if not searches_raw:
        raise ConfigError("'searches' must not be empty")
    searches = [_search_from_dict(s) for s in searches_raw]

    return Config(
        db=db_path,
        searches=searches,
        sources=_sources_from_dict(raw.get("sources")),
        notify=_notify_from_dict(raw.get("notify")),
    )


def example_config_text() -> str:
    """Return the packaged config.example.yaml contents."""
    return resources.files("jobwatch").joinpath(CONFIG_EXAMPLE).read_text()
