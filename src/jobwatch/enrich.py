"""Récupération et résumé automatique des pages d'offres : `jw enrich`.

Pour chaque offre active (au moins un match new/seen/later ou une
candidature, quelle que soit sa source, bridge compris), récupère la page de
l'offre si elle manque, la convertit en Markdown et l'enregistre, puis génère
un résumé structuré via un LLM bon marché (deepseek-v4-flash par OpenCode) :
quatre champs fixes (expérience souhaitée, salaire, télétravail, stack -
table summary_field) suivis de puces mission. Les puces d'un résumé manuel
importé ne sont jamais écrasées ; les champs fixes, eux, s'ajoutent à tout
résumé qui n'en a pas. Un échec (réseau ou LLM) est consigné en avertissement
et n'interrompt jamais le traitement des offres suivantes.
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

MIN_MARKDOWN_LENGTH = 200
SLEEP_MIN_SECONDS = 1.0
SLEEP_MAX_SECONDS = 2.0

# Champs fixes du résumé structuré, dans l'ordre d'affichage.
FIELD_KEYS = ("experience", "salary", "remote", "stack")
_PROMPT_LABEL_TO_KEY = {
    "EXPERIENCE": "experience",
    "SALAIRE": "salary",
    "TELETRAVAIL": "remote",
    "STACK": "stack",
}
FIELD_UNKNOWN = "non précisé"

SUMMARY_PROMPT = (
    "Le fichier attaché contient le texte Markdown d'une offre d'emploi (converti "
    "automatiquement depuis sa page web, peut contenir du bruit de navigation/pied de "
    "page). Réponds exactement au format suivant, sans introduction ni conclusion :\n"
    "EXPERIENCE: <expérience souhaitée (années, séniorité), ou 'non précisé'>\n"
    "SALAIRE: <salaire ou fourchette, ou 'non précisé'>\n"
    "TELETRAVAIL: <politique de télétravail (full remote, hybride N jours, sur site), "
    "ou 'non précisé'>\n"
    "STACK: <technos et compétences clés condensées en une ligne, ou 'non précisé'>\n"
    "- <puis 3 à 6 puces courtes et factuelles : poste, mission, lieu, contrat>"
)


class EnrichError(Exception):
    """Échec attendu (config manquante). La CLI affiche un message clair et sort."""


@dataclass
class EnrichResult:
    """Bilan déterministe d'un `jw enrich`."""

    fetched_ok: int = 0
    fetched_failed: int = 0
    summaries_written: int = 0
    fields_written: int = 0


def _pending_offers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Offres actives à traiter : texte à récupérer et/ou champs structurés absents.

    Une offre est active si un match new/seen/later ou une candidature la
    référence (la corbeille et les offres sans match ne coûtent aucun token).
    Une offre dont le fetch a déjà échoué n'est jamais retentée.
    """
    return conn.execute(
        "SELECT o.id AS id, o.url AS url, "
        "       (SELECT oc.status FROM offer_content oc WHERE oc.offer_id = o.id) "
        "       AS content_status "
        "FROM offer o "
        "WHERE (EXISTS (SELECT 1 FROM match m WHERE m.offer_id = o.id "
        "               AND m.state IN ('new', 'seen', 'later')) "
        "   OR EXISTS (SELECT 1 FROM application a WHERE a.offer_id = o.id)) "
        "AND (NOT EXISTS (SELECT 1 FROM offer_content oc WHERE oc.offer_id = o.id) "
        "  OR (EXISTS (SELECT 1 FROM offer_content oc WHERE oc.offer_id = o.id "
        "              AND oc.status = 'ok') "
        "      AND NOT EXISTS (SELECT 1 FROM offer_summary os "
        "                      JOIN summary_field sf ON sf.summary_id = os.id "
        "                      WHERE os.offer_id = o.id))) "
        "ORDER BY o.id"
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


def _summarize(config: EnrichConfig, markdown: str) -> tuple[dict[str, str], list[str]] | None:
    # Le Markdown est passé en pièce jointe (--file) et non en argument CLI :
    # le noyau Linux limite chaque argument individuel à ~128 Ko (MAX_ARG_STRLEN).
    with tempfile.TemporaryDirectory() as tmp_dir:
        content_path = Path(tmp_dir) / "offer.md"
        content_path.write_text(markdown, encoding="utf-8")
        command = [config.opencode_bin, "run", "--model", config.model]
        if config.variant:
            command += ["--variant", config.variant]
        command += ["--format", "json", f"--file={content_path}", "--", SUMMARY_PROMPT]
        try:
            completed = subprocess.run(
                command,
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
    fields, bullets = _parse_summary(text)
    if not fields and not bullets:
        return None
    return fields, bullets


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


def _parse_summary(text: str) -> tuple[dict[str, str], list[str]]:
    """Sépare les lignes 'LABEL: valeur' des champs fixes et les puces '- '."""
    fields: dict[str, str] = {}
    bullets: list[str] = []
    for line in text.splitlines():
        line = line.strip().lstrip("*").strip()
        if line.startswith("- "):
            bullet = line[2:].strip()
            if bullet:
                bullets.append(bullet)
            continue
        label, sep, value = line.partition(":")
        key = _PROMPT_LABEL_TO_KEY.get(label.strip().strip("*").upper())
        if sep and key and key not in fields:
            fields[key] = value.strip().strip("*").strip() or FIELD_UNKNOWN
    return fields, bullets


def _write_summary(
    conn: sqlite3.Connection, offer_id: int, fields: dict[str, str], bullets: list[str]
) -> tuple[bool, bool]:
    """Écrit champs et puces sans jamais écraser des puces existantes.

    Renvoie (summary_written, fields_written). Un résumé existant (manuel ou
    auto) garde ses puces telles quelles ; les champs fixes sont ajoutés à
    tout résumé qui n'en a pas encore.
    """
    row = conn.execute(
        "SELECT id FROM offer_summary WHERE offer_id = ?", (offer_id,)
    ).fetchone()
    summary_written = False
    if row is not None:
        summary_id = int(row["id"])
    else:
        cur = conn.execute(
            "INSERT INTO offer_summary (offer_id, source) VALUES (?, 'auto')", (offer_id,)
        )
        summary_id = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO summary_bullet (summary_id, position, text) VALUES (?, ?, ?)",
            ((summary_id, position, bullet) for position, bullet in enumerate(bullets)),
        )
        summary_written = bool(bullets)
    fields_written = False
    if fields:
        conn.executemany(
            "INSERT OR REPLACE INTO summary_field (summary_id, key, value) VALUES (?, ?, ?)",
            ((summary_id, key, fields.get(key, FIELD_UNKNOWN)) for key in FIELD_KEYS),
        )
        fields_written = True
    conn.commit()
    return summary_written, fields_written


def enrich(
    conn: sqlite3.Connection,
    config: EnrichConfig | None,
    *,
    client: httpx.Client | None = None,
    sleep=time.sleep,
) -> EnrichResult:
    """Récupère et résume les offres actives sans texte ou sans champs structurés.

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
            if offer["content_status"] is None:
                markdown, fetch_method = _fetch_and_convert(url, http_client)
                _store_content(conn, offer_id, markdown, fetch_method)
                if markdown is None:
                    result.fetched_failed += 1
                    continue
                result.fetched_ok += 1
                # Le sommeil ne s'applique qu'aux offres réellement récupérées
                # sur le web ; résumer un texte déjà en base ne martèle personne.
                if index < len(offers) - 1:
                    sleep(random.uniform(SLEEP_MIN_SECONDS, SLEEP_MAX_SECONDS))
            else:
                row = conn.execute(
                    "SELECT markdown FROM offer_content WHERE offer_id = ?", (offer_id,)
                ).fetchone()
                markdown = str(row["markdown"]) if row and row["markdown"] else None
                if markdown is None:
                    continue
            summarized = _summarize(config, markdown)
            if summarized is None:
                continue
            fields, bullets = summarized
            summary_written, fields_written = _write_summary(conn, offer_id, fields, bullets)
            if summary_written:
                result.summaries_written += 1
            if fields_written:
                result.fields_written += 1
    finally:
        if owned_client:
            http_client.close()

    return result
