"""Construit les instances de collecteurs depuis la configuration."""

from __future__ import annotations

import httpx

from jobwatch.collectors.base import Collector
from jobwatch.collectors.france_travail import FranceTravailCollector
from jobwatch.collectors.smartrecruiters import SmartRecruitersCollector
from jobwatch.config import (
    FranceTravailSource,
    SmartRecruitersSource,
    SourcesConfig,
)


def build_collectors(sources: SourcesConfig, client: httpx.Client | None = None) -> list[Collector]:
    """Construit un collecteur par type de source configuré."""
    collectors: list[Collector] = []
    if sources.france_travail is not None:
        ft: FranceTravailSource = sources.france_travail
        collectors.append(
            FranceTravailCollector(
                client_id=ft.client_id,
                client_secret=ft.client_secret,
                keywords=ft.keywords,
                department=ft.department,
                client=client,
            )
        )
    if sources.smartrecruiters is not None:
        sr: SmartRecruitersSource = sources.smartrecruiters
        collectors.append(SmartRecruitersCollector(companies=sr.companies, client=client))
    return collectors
