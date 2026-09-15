"""Construit les instances de collecteurs depuis la configuration."""

from __future__ import annotations

import logging
import math
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx

from jobwatch.collectors.base import Collector, store_offers
from jobwatch.collectors.france_travail import FranceTravailCollector
from jobwatch.collectors.linkedin import LinkedInCollector
from jobwatch.collectors.rendered import PlaywrightCollector
from jobwatch.collectors.smartrecruiters import SmartRecruitersCollector
from jobwatch.collectors.talentsoft import TalentsoftCollector, site_slug
from jobwatch.collectors.workday import WorkdayCollector, cxs_site_name
from jobwatch.collectors.wttj import WttjCollector
from jobwatch.config import (
    FranceTravailSource,
    LinkedInSource,
    SmartRecruitersSource,
    SourcesConfig,
    WttjSource,
)

log = logging.getLogger(__name__)


def build_collectors(sources: SourcesConfig, client: httpx.Client | None = None) -> list[Collector]:
    """Construit un collecteur par site, dont un par société SmartRecruiters."""
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
                interval_days=ft.interval_days,
            )
        )
    if sources.smartrecruiters is not None:
        sr: SmartRecruitersSource = sources.smartrecruiters
        for slug in sr.companies:
            collector = SmartRecruitersCollector(
                companies=[slug], client=client, countries=sr.countries,
                interval_days=sr.company_intervals.get(slug, sr.interval_days),
            )
            collector.name = f"smartrecruiters:{slug.casefold()}"
            collectors.append(collector)
    if sources.linkedin is not None and sources.linkedin.queries:
        linkedin: LinkedInSource = sources.linkedin
        collectors.append(
            LinkedInCollector(
                queries=linkedin.queries, hours=linkedin.hours, client=client,
                interval_days=linkedin.interval_days,
            )
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
                interval_days=wttj.interval_days,
            )
        )
    if sources.talentsoft is not None:
        for site in sources.talentsoft.sites:
            collector = TalentsoftCollector(
                base_url=site.url, company=site.name, client=client,
                interval_days=site.interval_days,
            )
            collector.name = f"talentsoft:{site_slug(site.url)}"
            collectors.append(collector)
    if sources.workday is not None:
        for site in sources.workday.sites:
            collector = WorkdayCollector(
                base_url=site.url, company=site.name, client=client,
                interval_days=site.interval_days,
            )
            collector.name = f"workday:{cxs_site_name(site.url).casefold()}"
            collectors.append(collector)
    if sources.playwright is not None:
        for site in sources.playwright.sites:
            collector = PlaywrightCollector(
                url=site.url, company=site.name, link_pattern=site.link_pattern,
                client=client, interval_days=site.interval_days,
            )
            collector.name = f"playwright:{site_slug(site.url)}"
            collectors.append(collector)
    return collectors


def collect_due(conn: sqlite3.Connection, sources: SourcesConfig) -> tuple[int, int, bool]:
    """Collecte les sites arrivés à échéance et conserve le dernier succès complet."""
    collected = skipped = 0
    failed = False
    for collector in build_collectors(sources):
        started = datetime.now(UTC)
        row = conn.execute(
            "SELECT last_success_at FROM source WHERE name = ?", (collector.name,)
        ).fetchone()
        last_success = (
            datetime.fromisoformat(row["last_success_at"]).replace(tzinfo=UTC)
            if row and row["last_success_at"] else None
        )
        elapsed = (started - last_success).total_seconds() if last_success else None
        if elapsed is not None and elapsed < timedelta(days=collector.interval_days).total_seconds():
            skipped += 1
            log.info("%s différée, intervalle de %d jour(s)", collector.name, collector.interval_days)
            continue
        if isinstance(collector, (LinkedInCollector, WttjCollector)):
            # Un jour de chevauchement couvre les décalages du cron et la précision des dates.
            collector.hours = max(
                collector.hours, collector.interval_days * 24 + 24,
                math.ceil(elapsed / 3600) + 24 if elapsed is not None else 0,
            )
        offers = collector.fetch()
        source_failed = bool(collector.failed_requests)
        failed |= source_failed
        new_ids = store_offers(
            conn, collector.name, collector.source_type, offers,
            successful_at=None if source_failed else started.strftime("%Y-%m-%d %H:%M:%S"),
        )
        collected += len(new_ids)
        log.info("collected %d new offers from %s", len(new_ids), collector.name)
    return collected, skipped, failed
