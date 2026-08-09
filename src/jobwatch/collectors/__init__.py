"""Construit les instances de collecteurs depuis la configuration."""

from __future__ import annotations

import httpx

from jobwatch.collectors.base import Collector
from jobwatch.collectors.france_travail import FranceTravailCollector
from jobwatch.collectors.linkedin import LinkedInCollector
from jobwatch.collectors.smartrecruiters import SmartRecruitersCollector
from jobwatch.collectors.wttj import WttjCollector
from jobwatch.config import (
    FranceTravailSource,
    LinkedInSource,
    SmartRecruitersSource,
    SourcesConfig,
    WttjSource,
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
    if sources.linkedin is not None:
        linkedin: LinkedInSource = sources.linkedin
        collectors.append(
            LinkedInCollector(queries=linkedin.queries, hours=linkedin.hours, client=client)
        )
    if sources.wttj is not None:
        wttj: WttjSource = sources.wttj
        collectors.append(
            WttjCollector(
                queries=wttj.queries,
                countries=wttj.countries,
                cities=wttj.cities,
                app_id=wttj.algolia.app_id,
                api_key=wttj.algolia.api_key,
                index=wttj.algolia.index,
                hours=wttj.hours,
                client=client,
            )
        )
    return collectors
