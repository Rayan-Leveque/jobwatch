"""Récupération et résumé automatique des pages d'offres : `jw enrich`.

Pour chaque offre active (au moins un match new/seen/later ou une
candidature, quelle que soit sa source, bridge compris), récupère la page de
l'offre si elle manque, en extrait le bloc utile (voir `extraction.py` : la
page entière est surtout du décor payé en tokens) et l'enregistre avec son
HTML brut compressé, puis génère un résumé structuré via LLM : quatre champs
fixes (expérience souhaitée, salaire, télétravail, stack - table
summary_field) suivis de puces mission.

Chaque champ est accompagné d'une **citation littérale de l'annonce**, et
cette citation n'est conservée que si le code la retrouve mot pour mot dans
le texte stocké. C'est ce qui remplace les étiquettes vagues ('niveau
mid-senior') par ce que l'annonce dit vraiment, et c'est un ancrage
anti-hallucination qui ne coûte aucun appel supplémentaire. Un champ dont la
citation est invérifiable garde sa valeur mais perd sa citation : on
n'affiche jamais comme cité ce qui ne l'est pas.

Les puces d'un résumé manuel importé ne sont jamais écrasées ; les champs
fixes, eux, s'ajoutent à tout résumé qui n'en a pas. Un échec (réseau ou LLM)
est consigné en avertissement et n'interrompt jamais le traitement des offres
suivantes.
"""

from __future__ import annotations

import gzip
import json
import logging
import random
import re
import sqlite3
import time
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from dataclasses import field as dataclasses_field
from urllib.parse import urlsplit

import httpx
from playwright.sync_api import sync_playwright

from jobwatch import llm_runner
from jobwatch.config import EnrichConfig
from jobwatch.extraction import Extraction, extract
from jobwatch.llm_runner import LLMRunnerError, run_codex, run_opencode, run_pi

OPENCODE_TOOLS = llm_runner.OPENCODE_TOOLS
opencode_sandbox = llm_runner.opencode_sandbox

log = logging.getLogger(__name__)

MIN_MARKDOWN_LENGTH = 200
SLEEP_MIN_SECONDS = 1.0
SLEEP_MAX_SECONDS = 2.0
MAX_FETCH_ATTEMPTS = 3
FETCH_RETRY_SQL_DELAY = "-1 day"
TERMINAL_HTTP_STATUSES = frozenset({404, 410})
MAX_SUMMARY_ATTEMPTS = 3
SUMMARY_RETRY_SQL_DELAY = "-1 hour"
WTTJ_RECOVERY_VERSION = 1
WTTJ_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

# Champs fixes du résumé structuré, dans l'ordre d'affichage.
FIELD_KEYS = ("experience", "salary", "remote", "stack")
_PROMPT_LABEL_TO_KEY = {
    "EXPERIENCE": "experience",
    "SALAIRE": "salary",
    "TELETRAVAIL": "remote",
    "STACK": "stack",
}
FIELD_UNKNOWN = "non précisé"

# Chaque champ est doublé d'une ligne _CITATION portant le texte de l'annonce.
_PROMPT_LABEL_TO_QUOTE_KEY = {f"{label}_CITATION": key for label, key in _PROMPT_LABEL_TO_KEY.items()}

_SUMMARY_FORMAT = (
    "Pour chacun des quatre champs, donne deux lignes : la valeur courte, puis la "
    "CITATION, c'est-à-dire le passage de l'annonce copié MOT POUR MOT (30 à 200 "
    "caractères) sur lequel tu t'appuies. Ne reformule jamais une citation, ne la "
    "traduis pas, n'y ajoute aucun mot : elle est vérifiée automatiquement contre le "
    "texte de l'annonce et rejetée à la moindre différence. Si l'annonce ne dit rien "
    "sur un champ, écris 'non précisé' aux deux lignes plutôt que de déduire quoi que "
    "ce soit.\n"
    "Réponds exactement au format suivant, sans introduction ni conclusion :\n"
    "EXPERIENCE: <expérience souhaitée (années, séniorité), ou 'non précisé'>\n"
    "EXPERIENCE_CITATION: <passage exact de l'annonce, ou 'non précisé'>\n"
    "SALAIRE: <salaire ou fourchette, ou 'non précisé'>\n"
    "SALAIRE_CITATION: <passage exact de l'annonce, ou 'non précisé'>\n"
    "TELETRAVAIL: <politique de télétravail (full remote, hybride N jours, sur site), "
    "ou 'non précisé'>\n"
    "TELETRAVAIL_CITATION: <passage exact de l'annonce, ou 'non précisé'>\n"
    "STACK: <technos et compétences clés condensées en une ligne, ou 'non précisé'>\n"
    "STACK_CITATION: <passage exact de l'annonce, ou 'non précisé'>\n"
    "- <puis 3 à 6 puces courtes et factuelles : poste, mission, lieu, contrat>"
)

SUMMARY_PROMPT = (
    "Le fichier attaché contient le texte Markdown d'une offre d'emploi, extrait "
    f"automatiquement de sa page web. {_SUMMARY_FORMAT}"
)

CODEX_SUMMARY_PROMPT = (
    "Le bloc <stdin> ci-dessous contient le texte Markdown d'une offre d'emploi, "
    "extrait automatiquement de sa page web. N'exécute aucune commande et ne lis "
    f"aucun fichier : réponds directement. {_SUMMARY_FORMAT}"
)

CODEX_TIMEOUT_SECONDS = 300


class EnrichError(Exception):
    """Échec attendu (config manquante). La CLI affiche un message clair et sort."""


@dataclass
class EnrichResult:
    """Bilan déterministe d'un `jw enrich`."""

    fetched_ok: int = 0
    fetched_failed: int = 0
    summaries_written: int = 0
    fields_written: int = 0
    #: Nombre d'offres par méthode d'extraction retenue ('jsonld'/'readable'/'raw').
    extract_methods: Counter[str] = dataclasses_field(default_factory=Counter)
    #: Caractères de la page entière, et caractères réellement gardés.
    raw_chars: int = 0
    kept_chars: int = 0
    quotes_verified: int = 0
    quotes_rejected: int = 0
    wttj_recovery_attempted: int = 0
    wttj_recovery_recovered: int = 0

    def summary_line(self) -> str:
        """Bilan lisible d'un run, affiché même sans -v (donc visible en cron)."""
        parts = [
            f"{self.fetched_ok} offre(s) récupérée(s), {self.fetched_failed} échec(s)",
            f"{self.summaries_written} résumé(s) généré(s)",
            f"{self.fields_written} fiche(s) de champs écrite(s)",
        ]
        if self.extract_methods:
            methods = ", ".join(
                f"{name}:{count}" for name, count in sorted(self.extract_methods.items())
            )
            parts.append(f"extraction {methods}")
        if self.raw_chars:
            saved = 100 * (self.raw_chars - self.kept_chars) / self.raw_chars
            parts.append(
                f"{_thousands(self.raw_chars)} -> {_thousands(self.kept_chars)} caractères "
                f"({saved:.0f} % de bruit retiré)"
            )
        if self.quotes_verified or self.quotes_rejected:
            parts.append(
                f"{self.quotes_verified} citation(s) vérifiée(s), "
                f"{self.quotes_rejected} rejetée(s)"
            )
        if self.wttj_recovery_attempted:
            parts.append(
                f"reprise WTTJ {self.wttj_recovery_recovered}/"
                f"{self.wttj_recovery_attempted} récupérée(s)"
            )
        return " ; ".join(parts)


@dataclass(frozen=True)
class FetchPage:
    """Résultat d'une voie de récupération, avant extraction du texte."""

    html: str | None
    failure_reason: str | None = None
    terminal: bool = False


@dataclass(frozen=True)
class FetchOutcome:
    """Résultat complet d'un fetch, y compris sa cause d'échec persistable."""

    extraction: Extraction | None
    fetch_method: str | None
    html: str | None
    failure_reason: str | None = None


def _thousands(value: int) -> str:
    """Sépare les milliers par une espace insécable, comme en français."""
    return f"{value:,}".replace(",", " ")


def _pending_offers(
    conn: sqlite3.Connection, *, recover_wttj: bool = False
) -> list[sqlite3.Row]:
    """Offres actives à traiter : texte à récupérer et/ou champs structurés absents.

    Une offre est active si un match new/seen/later ou une candidature la
    référence (la corbeille et les offres sans match ne coûtent aucun token).
    Les échecs retryables sont repris après 24 h, jusqu'à trois tentatives.
    Les anciennes lignes sans classification ont droit à un retry immédiat.
    """
    return conn.execute(
        "WITH candidates AS ("
        " SELECT o.id AS id, o.url AS url, oc.status AS content_status, "
        "        oc.fetch_attempts AS fetch_attempts, oc.failure_reason AS failure_reason, "
        "        os.id AS summary_id, os.source AS summary_source, "
        "        os.status AS summary_status, os.attempt_count AS summary_attempts, "
        "        CASE WHEN oc.id IS NULL OR "
        "          (oc.status = 'failed' AND oc.fetch_attempts < ? "
        "           AND (oc.failure_reason IS NULL "
        "                OR (oc.failure_reason NOT IN ('http_404', 'http_410') "
        "                    AND oc.fetched_at <= datetime('now', ?)))) "
        "        THEN 1 ELSE 0 END AS regular_fetch_due, "
        "        CASE WHEN ? = 1 AND oc.status = 'failed' "
        "          AND oc.wttj_recovery_version < ? "
        "          AND (oc.failure_reason IS NULL "
        "               OR oc.failure_reason NOT IN ('http_404', 'http_410')) "
        "          AND (src.type = 'wttj' OR lower(src.name) = 'wttj' "
        "               OR lower(o.platform) = 'wttj') "
        "        THEN 1 ELSE 0 END AS recovery_due "
        " FROM offer o JOIN source src ON src.id = o.source_id "
        " LEFT JOIN offer_content oc ON oc.offer_id = o.id "
        " LEFT JOIN offer_summary os ON os.offer_id = o.id "
        " WHERE EXISTS (SELECT 1 FROM match m WHERE m.offer_id = o.id "
        "               AND m.state IN ('new', 'seen', 'later')) "
        "    OR EXISTS (SELECT 1 FROM application a WHERE a.offer_id = o.id)"
        ") "
        "SELECT *, CASE WHEN regular_fetch_due = 1 OR recovery_due = 1 "
        "               THEN 1 ELSE 0 END AS fetch_due "
        "FROM candidates c WHERE regular_fetch_due = 1 OR recovery_due = 1 "
        "  OR summary_id IS NULL "
        "  OR (NOT EXISTS (SELECT 1 FROM summary_bullet sb WHERE sb.summary_id = c.summary_id) "
        "      AND NOT EXISTS (SELECT 1 FROM summary_field sf WHERE sf.summary_id = c.summary_id)) "
        "  OR (content_status = 'ok' AND summary_source = 'metadata' "
        "      AND summary_attempts < ? "
        "      AND (summary_attempts = 0 OR EXISTS ("
        "          SELECT 1 FROM offer_summary retry_summary "
        "          WHERE retry_summary.id = c.summary_id "
        "            AND retry_summary.attempted_at <= datetime('now', ?)))) "
        "  OR (content_status = 'ok' AND summary_source != 'metadata' "
        "      AND NOT EXISTS (SELECT 1 FROM summary_field sf "
        "                      WHERE sf.summary_id = c.summary_id)) "
        "ORDER BY id",
        (
            MAX_FETCH_ATTEMPTS,
            FETCH_RETRY_SQL_DELAY,
            int(recover_wttj),
            WTTJ_RECOVERY_VERSION,
            MAX_SUMMARY_ATTEMPTS,
            SUMMARY_RETRY_SQL_DELAY,
        ),
    ).fetchall()


def _source_http_headers(url: str) -> dict[str, str] | None:
    """Imite un navigateur uniquement pour le domaine WTTJ qui bloque le client brut."""
    try:
        hostname = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return None
    if hostname in ("welcometothejungle.com", "www.welcometothejungle.com"):
        return WTTJ_HTTP_HEADERS
    return None


def _fetch_http(url: str, client: httpx.Client) -> FetchPage:
    try:
        response = client.get(
            url, follow_redirects=True, headers=_source_http_headers(url)
        )
        response.raise_for_status()
    except httpx.TimeoutException as exc:
        log.warning("enrich: http fetch failed for %s: %s", url, exc)
        return FetchPage(None, "http_timeout")
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        log.warning("enrich: http fetch failed for %s: %s", url, exc)
        return FetchPage(
            None,
            f"http_{status}",
            terminal=status in TERMINAL_HTTP_STATUSES,
        )
    except httpx.HTTPError as exc:
        log.warning("enrich: http fetch failed for %s: %s", url, exc)
        return FetchPage(None, "http_error")
    return FetchPage(response.text)


def _fetch_playwright(url: str) -> FetchPage:
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                response = page.goto(url, timeout=30_000, wait_until="domcontentloaded")
                if response is not None and response.status in TERMINAL_HTTP_STATUSES:
                    return FetchPage(None, f"http_{response.status}", terminal=True)
                return FetchPage(page.content())
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - un site tiers cassé ne doit jamais arrêter le run
        log.warning("enrich: playwright fetch failed for %s: %s", url, exc)
        return FetchPage(None, "browser_error")


def _fetch_and_extract_result(url: str, client: httpx.Client) -> FetchOutcome:
    """Récupère puis extrait une offre, avec une cause d'échec classifiable."""
    fetchers = (
        ("http", lambda: _fetch_http(url, client)),
        ("playwright", lambda: _fetch_playwright(url)),
    )
    failure_reasons: list[str] = []
    for method, fetch in fetchers:
        page = fetch()
        if page.terminal:
            return FetchOutcome(None, None, None, page.failure_reason)
        if not page.html:
            failure_reasons.append(page.failure_reason or f"{method}_empty")
            continue
        result = extract(page.html)
        if len(result.markdown) >= MIN_MARKDOWN_LENGTH:
            return FetchOutcome(result, method, page.html)
        failure_reasons.append(f"{method}_too_short")
    return FetchOutcome(None, None, None, "+".join(failure_reasons))


def _fetch_and_extract(
    url: str, client: httpx.Client
) -> tuple[Extraction | None, str | None, str | None]:
    """Renvoie (extraction, fetch_method, html), ou (None, None, None) si tout échoue.

    Le seuil de repli vers Playwright reste mesuré sur la page entière : une
    page trop courte est une page morte ou un mur de connexion, indépendamment
    de la qualité de l'extraction qu'on en tirera.
    """
    outcome = _fetch_and_extract_result(url, client)
    return outcome.extraction, outcome.fetch_method, outcome.html


def _store_content(
    conn: sqlite3.Connection,
    offer_id: int,
    result: Extraction | None,
    fetch_method: str | None,
    html: str | None,
    failure_reason: str | None,
    *,
    wttj_recovery: bool = False,
) -> None:
    """Enregistre le bloc utile, sa provenance et le HTML brut compressé.

    Garder le HTML permet de ré-extraire tout le corpus quand l'extracteur
    s'améliore, sans refetcher les sites ni redemander la moindre page.
    """
    conn.execute(
        "INSERT INTO offer_content (offer_id, markdown, fetch_method, extract_method, "
        "html_gz, status, fetch_attempts, failure_reason, wttj_recovery_version) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?) "
        "ON CONFLICT(offer_id) DO UPDATE SET markdown = excluded.markdown, "
        "fetch_method = excluded.fetch_method, extract_method = excluded.extract_method, "
        "html_gz = excluded.html_gz, status = excluded.status, "
        "fetch_attempts = offer_content.fetch_attempts + 1, "
        "failure_reason = excluded.failure_reason, "
        "wttj_recovery_version = MAX(offer_content.wttj_recovery_version, "
        "                            excluded.wttj_recovery_version), "
        "fetched_at = datetime('now')",
        (
            offer_id,
            result.markdown if result is not None else None,
            fetch_method,
            result.method if result is not None else None,
            gzip.compress(html.encode("utf-8")) if html else None,
            "ok" if result is not None else "failed",
            failure_reason,
            WTTJ_RECOVERY_VERSION if wttj_recovery else 0,
        ),
    )
    conn.commit()


SummaryParts = tuple[dict[str, str], dict[str, str], list[str]]


def _summarize(config: EnrichConfig, markdown: str) -> SummaryParts | None:
    if config.runner == "pi":
        return _summarize_pi(config, markdown)
    if config.runner == "codex":
        return _summarize_codex(config, markdown)
    return _summarize_opencode(config, markdown)


def _summarize_pi(config: EnrichConfig, markdown: str) -> SummaryParts | None:
    try:
        text = run_pi(
            binary=config.pi_bin,
            model=config.model,
            prompt=SUMMARY_PROMPT,
            attachment=markdown,
            timeout=CODEX_TIMEOUT_SECONDS,
            thinking=config.variant,
        )
    except LLMRunnerError as exc:
        log.warning("enrich: %s", exc)
        return None
    fields, quotes, bullets = _parse_summary(text)
    if not fields and not bullets:
        return None
    return fields, _verified_quotes(quotes, markdown), bullets


def _summarize_codex(config: EnrichConfig, markdown: str) -> SummaryParts | None:
    try:
        text = run_codex(binary=config.codex_bin, model=config.model,
                         prompt=CODEX_SUMMARY_PROMPT, attachment=markdown,
                         timeout=CODEX_TIMEOUT_SECONDS, variant=config.variant)
    except LLMRunnerError as exc:
        log.warning("enrich: %s", exc)
        return None
    fields, quotes, bullets = _parse_summary(text)
    if not fields and not bullets:
        return None
    return fields, _verified_quotes(quotes, markdown), bullets


def _summarize_opencode(config: EnrichConfig, markdown: str) -> SummaryParts | None:
    try:
        stdout = run_opencode(binary=config.opencode_bin, model=config.model,
                              prompt=SUMMARY_PROMPT, attachment=markdown,
                              timeout=120, variant=config.variant, pass_variant=True,
                              attachment_name="offer.md")
    except LLMRunnerError as exc:
        log.warning("enrich: %s", exc)
        return None
    text = _extract_text(stdout)
    fields, quotes, bullets = _parse_summary(text)
    if not fields and not bullets:
        return None
    return fields, _verified_quotes(quotes, markdown), bullets


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


def _parse_summary(text: str) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Sépare les valeurs de champs, leurs citations, et les puces '- '."""
    fields: dict[str, str] = {}
    quotes: dict[str, str] = {}
    bullets: list[str] = []
    for line in text.splitlines():
        line = line.strip().lstrip("*").strip()
        if line.startswith("- "):
            bullet = line[2:].strip()
            if bullet:
                bullets.append(bullet)
            continue
        label, sep, value = line.partition(":")
        if not sep:
            continue
        label = label.strip().strip("*").upper()
        value = value.strip().strip("*").strip()
        quote_key = _PROMPT_LABEL_TO_QUOTE_KEY.get(label)
        if quote_key is not None:
            if value and value.lower() != FIELD_UNKNOWN and quote_key not in quotes:
                quotes[quote_key] = value
            continue
        key = _PROMPT_LABEL_TO_KEY.get(label)
        if key and key not in fields:
            fields[key] = value or FIELD_UNKNOWN
    return fields, quotes, bullets


def _normalize_for_match(text: str) -> str:
    """Réduit un texte à sa substance pour comparer une citation à l'annonce.

    Le modèle recopie fidèlement les mots mais pas toujours les espaces, la
    ponctuation typographique ou le balisage Markdown qui les entoure. On
    compare donc lettres et chiffres uniquement : assez tolérant pour ne pas
    rejeter une vraie citation, assez strict pour qu'une phrase inventée ne
    passe pas.
    """
    return re.sub(r"[^0-9a-zà-ÿ]+", " ", text.casefold()).strip()


def _verified_quotes(quotes: dict[str, str], markdown: str) -> dict[str, str]:
    """Ne garde que les citations réellement présentes dans le texte de l'annonce."""
    haystack = _normalize_for_match(markdown)
    verified: dict[str, str] = {}
    for key, quote in quotes.items():
        needle = _normalize_for_match(quote)
        if len(needle) >= 12 and needle in haystack:
            verified[key] = quote.strip('"« »')
        else:
            log.debug("enrich: citation rejetée pour %s: %r", key, quote[:80])
    return verified


def _write_summary(
    conn: sqlite3.Connection,
    offer_id: int,
    fields: dict[str, str],
    quotes: dict[str, str],
    bullets: list[str],
) -> tuple[bool, bool]:
    """Écrit champs, citations et puces sans jamais écraser des puces existantes.

    Renvoie (summary_written, fields_written). Un résumé existant (manuel ou
    auto) garde ses puces telles quelles ; les champs fixes sont ajoutés à
    tout résumé qui n'en a pas encore.
    """
    row = conn.execute(
        "SELECT id, source, attempt_count FROM offer_summary WHERE offer_id = ?", (offer_id,)
    ).fetchone()
    summary_written = False
    if row is not None:
        summary_id = int(row["id"])
        if row["source"] == "metadata":
            if bullets:
                conn.execute("DELETE FROM summary_bullet WHERE summary_id = ?", (summary_id,))
                conn.execute("DELETE FROM summary_field WHERE summary_id = ?", (summary_id,))
                conn.execute(
                    "UPDATE offer_summary SET source = 'auto', status = 'ready', "
                    "attempt_count = attempt_count + 1, attempted_at = datetime('now') "
                    "WHERE id = ?",
                    (summary_id,),
                )
            else:
                conn.execute(
                    "UPDATE offer_summary SET "
                    "attempt_count = attempt_count + 1, attempted_at = datetime('now') "
                    "WHERE id = ?",
                    (summary_id,),
                )
        elif row["source"] != "manual":
            conn.execute(
                "UPDATE offer_summary SET status = 'ready', "
                "attempt_count = attempt_count + 1, attempted_at = datetime('now') "
                "WHERE id = ?",
                (summary_id,),
            )
    else:
        cur = conn.execute(
            "INSERT INTO offer_summary "
            "(offer_id, source, status, attempt_count, attempted_at) "
            "VALUES (?, 'auto', 'ready', 1, datetime('now'))",
            (offer_id,),
        )
        summary_id = int(cur.lastrowid)
    has_bullets = conn.execute(
        "SELECT 1 FROM summary_bullet WHERE summary_id = ? LIMIT 1", (summary_id,)
    ).fetchone()
    if not has_bullets and bullets:
        conn.executemany(
            "INSERT INTO summary_bullet (summary_id, position, text) VALUES (?, ?, ?)",
            ((summary_id, position, bullet) for position, bullet in enumerate(bullets)),
        )
        summary_written = bool(bullets)
    fields_written = False
    if fields:
        conn.executemany(
            "INSERT OR REPLACE INTO summary_field (summary_id, key, value, quote) "
            "VALUES (?, ?, ?, ?)",
            (
                (summary_id, key, fields.get(key, FIELD_UNKNOWN), quotes.get(key))
                for key in FIELD_KEYS
            ),
        )
        fields_written = True
    conn.commit()
    return summary_written, fields_written


def _metadata_summary_bullets(conn: sqlite3.Connection, offer_id: int) -> list[str]:
    """Résumé déterministe limité aux métadonnées fiables déjà stockées."""
    row = conn.execute(
        "SELECT o.title AS title, c.name AS company, o.location AS location, "
        "       o.contract AS contract, o.platform AS platform, src.name AS source_name, "
        "       o.published_at AS published_at, o.deadline AS deadline, "
        "       (SELECT s.name FROM match m JOIN search s ON s.id = m.search_id "
        "        WHERE m.offer_id = o.id AND s.archived_at IS NULL ORDER BY m.id LIMIT 1) "
        "       AS search_name, "
        "       (SELECT m.fit FROM match m WHERE m.offer_id = o.id "
        "        AND m.fit IS NOT NULL ORDER BY m.id LIMIT 1) AS fit "
        "FROM offer o LEFT JOIN company c ON c.id = o.company_id "
        "JOIN source src ON src.id = o.source_id WHERE o.id = ?",
        (offer_id,),
    ).fetchone()
    if row is None:
        return ["Informations limitées : métadonnées de l'offre indisponibles."]
    bullets = [
        "Résumé limité aux métadonnées enregistrées ; le texte de l'annonce n'est pas disponible.",
        f"Poste enregistré : {row['title']} chez {row['company'] or 'société inconnue'}.",
    ]
    if row["location"]:
        bullets.append(f"Lieu enregistré : {row['location']}.")
    if row["contract"]:
        bullets.append(f"Contrat enregistré : {row['contract']}.")
    source = row["platform"] or row["source_name"]
    if source:
        bullets.append(f"Source enregistrée : {source}.")
    category = row["search_name"]
    fit = row["fit"]
    if category or fit:
        detail = " · ".join(
            part
            for part in (
                f"catégorie {category}" if category else "",
                f"fit {fit}" if fit else "",
            )
            if part
        )
        bullets.append(f"Classement Jobwatch : {detail}.")
    if row["published_at"]:
        bullets.append(f"Publication enregistrée : {row['published_at']}.")
    if row["deadline"]:
        bullets.append(f"Échéance enregistrée : {row['deadline']}.")
    return bullets


def _write_metadata_summary(
    conn: sqlite3.Connection,
    offer_id: int,
    *,
    retryable: bool,
    summary_attempted: bool = False,
) -> bool:
    """Crée ou actualise un résumé limité sans écraser un résumé fiable."""
    row = conn.execute(
        "SELECT id, source FROM offer_summary WHERE offer_id = ?", (offer_id,)
    ).fetchone()
    status = "limited_retryable" if retryable else "limited_no_content"
    if row is not None and row["source"] != "metadata":
        return False
    if row is None:
        cur = conn.execute(
            "INSERT INTO offer_summary "
            "(offer_id, source, status, attempt_count, attempted_at) "
            "VALUES (?, 'metadata', ?, ?, datetime('now'))",
            (offer_id, status, int(summary_attempted)),
        )
        summary_id = int(cur.lastrowid)
        created = True
    else:
        summary_id = int(row["id"])
        conn.execute("DELETE FROM summary_bullet WHERE summary_id = ?", (summary_id,))
        conn.execute("DELETE FROM summary_field WHERE summary_id = ?", (summary_id,))
        conn.execute(
            "UPDATE offer_summary SET status = ?, "
            "attempt_count = attempt_count + ?, attempted_at = datetime('now') WHERE id = ?",
            (status, int(summary_attempted), summary_id),
        )
        created = False
    conn.executemany(
        "INSERT INTO summary_bullet (summary_id, position, text) VALUES (?, ?, ?)",
        (
            (summary_id, position, bullet)
            for position, bullet in enumerate(_metadata_summary_bullets(conn, offer_id))
        ),
    )
    conn.commit()
    return created


def enrich(
    conn: sqlite3.Connection,
    config: EnrichConfig | None,
    *,
    client: httpx.Client | None = None,
    sleep=time.sleep,
    recover_wttj: bool = False,
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

    offers = _pending_offers(conn, recover_wttj=recover_wttj)
    result = EnrichResult()
    if not offers:
        return result

    owned_client = client is None
    http_client = client if client is not None else httpx.Client(timeout=30.0)
    # Les fetchs web restent séquentiels et espacés (politesse envers les sites) ;
    # les appels LLM - qui dominent le temps total - partent en parallèle dans un
    # pool, chaque résumé étant soumis dès que son texte est disponible. Toutes
    # les écritures SQLite restent dans ce thread.
    pool = ThreadPoolExecutor(max_workers=config.concurrency)
    futures: dict[Future, int] = {}
    # Champs déjà publiés par la page en JSON-LD : ils complètent ce que le LLM
    # n'a pas trouvé, sans coûter un seul token.
    jsonld_fields: dict[int, dict[str, str]] = {}
    try:
        fetch_remaining = sum(int(offer["fetch_due"]) for offer in offers)
        for offer in offers:
            offer_id = int(offer["id"])
            url = str(offer["url"])
            content_status = offer["content_status"]
            if offer["fetch_due"]:
                fetch_remaining -= 1
                fetch = _fetch_and_extract_result(url, http_client)
                extracted = fetch.extraction
                recovery_attempt = bool(offer["recovery_due"])
                if recovery_attempt:
                    result.wttj_recovery_attempted += 1
                _store_content(
                    conn,
                    offer_id,
                    extracted,
                    fetch.fetch_method,
                    fetch.html,
                    fetch.failure_reason,
                    wttj_recovery=recovery_attempt,
                )
                if extracted is None:
                    result.fetched_failed += 1
                    attempts = int(offer["fetch_attempts"] or 0) + 1
                    terminal = fetch.failure_reason in ("http_404", "http_410")
                    if _write_metadata_summary(
                        conn,
                        offer_id,
                        retryable=not terminal and attempts < MAX_FETCH_ATTEMPTS,
                    ):
                        result.summaries_written += 1
                    log.info("enrich: offre %d ECHEC fetch %s", offer_id, url)
                    continue
                content_status = "ok"
                if recovery_attempt:
                    result.wttj_recovery_recovered += 1
                result.fetched_ok += 1
                result.extract_methods[extracted.method] += 1
                result.raw_chars += extracted.raw_chars
                result.kept_chars += len(extracted.markdown)
                log.info(
                    "enrich: offre %d %s/%s %d -> %d car.%s%s %s",
                    offer_id,
                    fetch.fetch_method,
                    extracted.method,
                    extracted.raw_chars,
                    len(extracted.markdown),
                    f" +{extracted.salvaged_lines} repêchée(s)" if extracted.salvaged_lines else "",
                    f" marqueurs perdus: {','.join(extracted.lost_markers)}"
                    if extracted.lost_markers
                    else "",
                    url,
                )
                if extracted.fields:
                    jsonld_fields[offer_id] = extracted.fields
                markdown = extracted.markdown
                futures[pool.submit(_summarize, config, markdown)] = offer_id
                # Le sommeil ne s'applique qu'entre deux fetchs réels : résumer
                # un texte déjà en base ne martèle personne.
                if fetch_remaining > 0:
                    sleep(random.uniform(SLEEP_MIN_SECONDS, SLEEP_MAX_SECONDS))
            if content_status == "ok" and not offer["fetch_due"]:
                row = conn.execute(
                    "SELECT markdown FROM offer_content WHERE offer_id = ?", (offer_id,)
                ).fetchone()
                markdown = str(row["markdown"]) if row and row["markdown"] else None
                if markdown is not None:
                    futures[pool.submit(_summarize, config, markdown)] = offer_id
            elif content_status != "ok" and not offer["fetch_due"]:
                attempts = int(offer["fetch_attempts"] or 0)
                terminal = offer["failure_reason"] in ("http_404", "http_410")
                if _write_metadata_summary(
                    conn,
                    offer_id,
                    retryable=not terminal and attempts < MAX_FETCH_ATTEMPTS,
                ):
                    result.summaries_written += 1
        for future in as_completed(futures):
            offer_id = futures[future]
            try:
                summarized = future.result()
            except Exception:  # un résumé qui plante ne doit pas emporter le run
                log.exception("enrich: summary worker failed for offer %d", offer_id)
                if _write_metadata_summary(
                    conn, offer_id, retryable=True, summary_attempted=True
                ):
                    result.summaries_written += 1
                continue
            if summarized is None:
                if _write_metadata_summary(
                    conn, offer_id, retryable=True, summary_attempted=True
                ):
                    result.summaries_written += 1
                continue
            fields, quotes, bullets = summarized
            # Le JSON-LD ne sert qu'à combler : ce que le modèle a lu dans
            # l'annonce prime sur une donnée structurée souvent générique.
            for key, value in jsonld_fields.get(offer_id, {}).items():
                if fields.get(key, FIELD_UNKNOWN) == FIELD_UNKNOWN:
                    fields[key] = value
            anchored = sum(
                1 for key, value in fields.items() if value != FIELD_UNKNOWN and key in quotes
            )
            unanchored = sum(
                1 for key, value in fields.items() if value != FIELD_UNKNOWN and key not in quotes
            )
            result.quotes_verified += anchored
            result.quotes_rejected += unanchored
            log.info(
                "enrich: offre %d résumé %d puce(s), %d champ(s) ancré(s), %d sans citation",
                offer_id,
                len(bullets),
                anchored,
                unanchored,
            )
            summary_written, fields_written = _write_summary(
                conn, offer_id, fields, quotes, bullets
            )
            if summary_written:
                result.summaries_written += 1
            if fields_written:
                result.fields_written += 1
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        if owned_client:
            http_client.close()

    return result
