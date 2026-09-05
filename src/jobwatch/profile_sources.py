"""Requêtes de collecte issues des catégories confirmées par le propriétaire."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from jobwatch.config import LinkedInQuery, SourcesConfig
from jobwatch.onboarding import profile_geography, profile_intents


def sources_for_profile(conn: sqlite3.Connection, sources: SourcesConfig) -> SourcesConfig:
    if sources.linkedin is None or not sources.linkedin.from_profile:
        return sources
    owner = conn.execute(
        "SELECT account_id FROM candidate_profile WHERE completed_at IS NOT NULL LIMIT 1"
    ).fetchone()
    queries: list[LinkedInQuery] = []
    if owner is not None:
        account_id = int(owner["account_id"])
        locations, include_remote = profile_geography(conn, account_id)
        keywords = dict.fromkeys(
            keyword for intent in profile_intents(conn, account_id) for keyword in intent.keywords
        )
        for keyword in keywords:
            for location in locations or ["France"]:
                queries.append(LinkedInQuery(keyword, location))
            if include_remote and locations:
                queries.append(LinkedInQuery(keyword, "France", remote=True))
    return replace(sources, linkedin=replace(sources.linkedin, queries=queries))
