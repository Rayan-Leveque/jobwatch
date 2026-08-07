"""Récupération et résumé automatique des pages d'offres : `jw enrich`.

Pour chaque offre collectée par les collecteurs jobwatch (France Travail,
SmartRecruiters) sans `offer_content`, récupère la page de l'offre, la
convertit en Markdown et l'enregistre, puis génère un résumé court via un LLM
bon marché (deepseek-v4-flash par OpenCode) quand aucun résumé manuel
n'existe déjà. Un échec (réseau ou LLM) est consigné en avertissement et
n'interrompt jamais le traitement des offres suivantes.
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from markdownify import markdownify
from playwright.sync_api import sync_playwright

from jobwatch.config import EnrichConfig

log = logging.getLogger(__name__)

COLLECTOR_SOURCE_TYPES = ("france_travail", "smartrecruiters")

MIN_MARKDOWN_LENGTH = 200
SLEEP_MIN_SECONDS = 1.0
SLEEP_MAX_SECONDS = 2.0

SUMMARY_PROMPT = (
    "Le fichier attaché contient le texte Markdown d'une offre d'emploi (converti "
    "automatiquement depuis sa page web, peut contenir du bruit de navigation/pied de "
    "page). Résume les points clés en 3 à 6 puces courtes et factuelles (poste, mission, "
    "stack/compétences, lieu, contrat, salaire si mentionné). Réponds uniquement avec des "
    "lignes commençant par '- ', sans introduction ni conclusion."
)


class EnrichError(Exception):
    """Échec attendu (config manquante). La CLI affiche un message clair et sort."""


@dataclass
class EnrichResult:
    """Bilan déterministe d'un `jw enrich`."""

    fetched_ok: int = 0
    fetched_failed: int = 0
    summaries_written: int = 0


def _pending_offers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    placeholders = ",".join("?" * len(COLLECTOR_SOURCE_TYPES))
    return conn.execute(
        "SELECT o.id AS id, o.url AS url "
        "FROM offer o "
        "JOIN source s ON s.id = o.source_id "
        f"WHERE s.type IN ({placeholders}) "
        "AND NOT EXISTS (SELECT 1 FROM offer_content oc WHERE oc.offer_id = o.id) "
        "ORDER BY o.id",
        COLLECTOR_SOURCE_TYPES,
    ).fetchall()


def _fetch_http(url: str, client: httpx.Client) -> str | None:
    try:
        response = client.get(url, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("enrich: http fetch failed for %s: %s", url, exc)
        return None
    return response.text


def _fetch_playwright(url: str) -> str | None:
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(url, timeout=30_000, wait_until="domcontentloaded")
                return page.content()
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - un site tiers cassé ne doit jamais arrêter le run
        log.warning("enrich: playwright fetch failed for %s: %s", url, exc)
        return None


def _fetch_and_convert(url: str, client: httpx.Client) -> tuple[str | None, str | None]:
    """Renvoie (markdown, fetch_method), ou (None, None) si tout échoue."""
    html = _fetch_http(url, client)
    markdown = markdownify(html).strip() if html else ""
    if len(markdown) >= MIN_MARKDOWN_LENGTH:
        return markdown, "http"

    html = _fetch_playwright(url)
    markdown = markdownify(html).strip() if html else ""
    if len(markdown) >= MIN_MARKDOWN_LENGTH:
        return markdown, "playwright"

    return None, None


def _store_content(
    conn: sqlite3.Connection, offer_id: int, markdown: str | None, fetch_method: str | None
) -> None:
    status = "ok" if markdown is not None else "failed"
    conn.execute(
        "INSERT INTO offer_content (offer_id, markdown, fetch_method, status) "
        "VALUES (?, ?, ?, ?)",
        (offer_id, markdown, fetch_method, status),
    )
    conn.commit()


def _has_manual_summary(conn: sqlite3.Connection, offer_id: int) -> bool:
    row = conn.execute(
        "SELECT source FROM offer_summary WHERE offer_id = ?", (offer_id,)
    ).fetchone()
    return row is not None and row["source"] == "manual"


def _summarize(config: EnrichConfig, markdown: str) -> list[str] | None:
    # Le Markdown est passé en pièce jointe (--file) et non en argument CLI :
    # le noyau Linux limite chaque argument individuel à ~128 Ko (MAX_ARG_STRLEN).
    with tempfile.TemporaryDirectory() as tmp_dir:
        content_path = Path(tmp_dir) / "offer.md"
        content_path.write_text(markdown, encoding="utf-8")
        try:
            completed = subprocess.run(
                [
                    config.opencode_bin,
                    "run",
                    "--model",
                    config.model,
                    "--format",
                    "json",
                    f"--file={content_path}",
                    "--",
                    SUMMARY_PROMPT,
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
                cwd=tmp_dir,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("enrich: opencode subprocess failed: %s", exc)
            return None
    if completed.returncode != 0:
        log.warning("enrich: opencode exited with status %s", completed.returncode)
        return None
    text = _extract_text(completed.stdout)
    bullets = _parse_bullets(text)
    return bullets or None


def _extract_text(stdout: str) -> str:
    chunks: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "text":
            continue
        part = event.get("part")
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            chunks.append(part["text"])
    return "\n".join(chunks)


def _parse_bullets(text: str) -> list[str]:
    bullets = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("- "):
            bullet = line[2:].strip()
            if bullet:
                bullets.append(bullet)
    return bullets


def _write_auto_summary(conn: sqlite3.Connection, offer_id: int, bullets: list[str]) -> None:
    cur = conn.execute(
        "INSERT INTO offer_summary (offer_id, source) VALUES (?, 'auto')", (offer_id,)
    )
    summary_id = int(cur.lastrowid)
    conn.executemany(
        "INSERT INTO summary_bullet (summary_id, position, text) VALUES (?, ?, ?)",
        ((summary_id, position, bullet) for position, bullet in enumerate(bullets)),
    )
    conn.commit()


def enrich(
    conn: sqlite3.Connection,
    config: EnrichConfig | None,
    *,
    client: httpx.Client | None = None,
    sleep=time.sleep,
) -> EnrichResult:
    """Récupère, convertit et résume les offres sans `offer_content`.

    Lève EnrichError si `config` est None (bloc `enrich` non configuré). Un
    échec de fetch ou de résumé pour une offre n'interrompt jamais le
    traitement des offres suivantes.
    """
    if config is None:
        raise EnrichError(
            "l'enrichissement n'est pas configuré ; renseignez le bloc 'enrich' "
            "de config.yaml (voir README.md)"
        )

    offers = _pending_offers(conn)
    result = EnrichResult()
    if not offers:
        return result

    owned_client = client is None
    http_client = client if client is not None else httpx.Client(timeout=30.0)
    try:
        for index, offer in enumerate(offers):
            offer_id = int(offer["id"])
            url = str(offer["url"])
            markdown, fetch_method = _fetch_and_convert(url, http_client)
            _store_content(conn, offer_id, markdown, fetch_method)
            if markdown is None:
                result.fetched_failed += 1
            else:
                result.fetched_ok += 1
                if not _has_manual_summary(conn, offer_id):
                    bullets = _summarize(config, markdown)
                    if bullets:
                        _write_auto_summary(conn, offer_id, bullets)
                        result.summaries_written += 1
            if index < len(offers) - 1:
                sleep(random.uniform(SLEEP_MIN_SECONDS, SLEEP_MAX_SECONDS))
    finally:
        if owned_client:
            http_client.close()

    return result
