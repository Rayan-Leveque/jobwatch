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
import subprocess
import tempfile
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

from jobwatch.config import EnrichConfig
from jobwatch.extraction import Extraction, extract

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


def _fetch_and_extract(
    url: str, client: httpx.Client
) -> tuple[Extraction | None, str | None, str | None]:
    """Renvoie (extraction, fetch_method, html), ou (None, None, None) si tout échoue.

    Le seuil de repli vers Playwright reste mesuré sur la page entière : une
    page trop courte est une page morte ou un mur de connexion, indépendamment
    de la qualité de l'extraction qu'on en tirera.
    """
    fetchers = (
        ("http", lambda: _fetch_http(url, client)),
        ("playwright", lambda: _fetch_playwright(url)),
    )
    for method, fetch in fetchers:
        html = fetch()
        if not html:
            continue
        result = extract(html)
        if len(result.markdown) >= MIN_MARKDOWN_LENGTH:
            return result, method, html
    return None, None, None


def _store_content(
    conn: sqlite3.Connection,
    offer_id: int,
    result: Extraction | None,
    fetch_method: str | None,
    html: str | None,
) -> None:
    """Enregistre le bloc utile, sa provenance et le HTML brut compressé.

    Garder le HTML permet de ré-extraire tout le corpus quand l'extracteur
    s'améliore, sans refetcher les sites ni redemander la moindre page.
    """
    conn.execute(
        "INSERT INTO offer_content (offer_id, markdown, fetch_method, extract_method, "
        "html_gz, status) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(offer_id) DO UPDATE SET markdown = excluded.markdown, "
        "fetch_method = excluded.fetch_method, extract_method = excluded.extract_method, "
        "html_gz = excluded.html_gz, status = excluded.status, fetched_at = datetime('now')",
        (
            offer_id,
            result.markdown if result is not None else None,
            fetch_method,
            result.method if result is not None else None,
            gzip.compress(html.encode("utf-8")) if html else None,
            "ok" if result is not None else "failed",
        ),
    )
    conn.commit()


SummaryParts = tuple[dict[str, str], dict[str, str], list[str]]


def _summarize(config: EnrichConfig, markdown: str) -> SummaryParts | None:
    if config.runner == "codex":
        return _summarize_codex(config, markdown)
    return _summarize_opencode(config, markdown)


def _summarize_codex(
    config: EnrichConfig, markdown: str
) -> SummaryParts | None:
    # Le texte de l'offre passe par stdin (bloc <stdin> côté codex) : pas de
    # limite d'argument ni de fichier temporaire à faire lire au modèle. La
    # réponse finale est écrite par codex dans un fichier (-o), donc aucun
    # parsing de log. Le texte vient d'une page tierce, donc non fiable :
    # lecture seule, config utilisateur ignorée et outils désactivés.
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = Path(tmp_dir) / "reponse.txt"
        command = [
            config.codex_bin, "exec",
            "--ignore-user-config",
            "--disable", "shell_tool",
            "--disable", "code_mode_host",
            "--disable", "apps",
            "--disable", "plugins",
            "--model", config.model,
            "-s", "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "-o", str(out_path),
        ]
        if config.variant:
            command += ["-c", f"model_reasoning_effort={config.variant}"]
        command.append(CODEX_SUMMARY_PROMPT)
        try:
            completed = subprocess.run(
                command,
                input=markdown,
                capture_output=True,
                text=True,
                timeout=CODEX_TIMEOUT_SECONDS,
                check=False,
                cwd=tmp_dir,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("enrich: codex subprocess failed: %s", exc)
            return None
        if completed.returncode != 0:
            log.warning("enrich: codex exited with status %s", completed.returncode)
            return None
        try:
            text = out_path.read_text(encoding="utf-8")
        except OSError:
            log.warning("enrich: codex did not write its final message")
            return None
    fields, quotes, bullets = _parse_summary(text)
    if not fields and not bullets:
        return None
    return fields, _verified_quotes(quotes, markdown), bullets


def write_opencode_denials(tmp_dir: Path) -> Path:
    """Écrit un opencode.json qui refuse tout outil, à côté du prompt.

    Le texte vient d'une page tierce : le modèle ne doit pouvoir ni lire, ni
    écrire, ni exécuter quoi que ce soit, seulement répondre. Avec --pure, la
    configuration utilisateur est ignorée et seul ce fichier s'applique.
    """
    path = tmp_dir / "opencode.json"
    path.write_text(
        json.dumps(
            {"$schema": "https://opencode.ai/config.json", "permission": {"*": "deny"}}
        ),
        encoding="utf-8",
    )
    return path


def _summarize_opencode(
    config: EnrichConfig, markdown: str
) -> SummaryParts | None:
    # Le Markdown est passé en pièce jointe (--file) et non en argument CLI :
    # le noyau Linux limite chaque argument individuel à ~128 Ko (MAX_ARG_STRLEN).
    with tempfile.TemporaryDirectory() as tmp_dir:
        content_path = Path(tmp_dir) / "offer.md"
        content_path.write_text(markdown, encoding="utf-8")
        write_opencode_denials(Path(tmp_dir))
        command = [config.opencode_bin, "run", "--pure", "--model", config.model]
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
        fetch_remaining = sum(1 for offer in offers if offer["content_status"] is None)
        for offer in offers:
            offer_id = int(offer["id"])
            url = str(offer["url"])
            if offer["content_status"] is None:
                fetch_remaining -= 1
                extracted, fetch_method, html = _fetch_and_extract(url, http_client)
                _store_content(conn, offer_id, extracted, fetch_method, html)
                if extracted is None:
                    result.fetched_failed += 1
                    continue
                result.fetched_ok += 1
                if extracted.fields:
                    jsonld_fields[offer_id] = extracted.fields
                markdown = extracted.markdown
                futures[pool.submit(_summarize, config, markdown)] = offer_id
                # Le sommeil ne s'applique qu'entre deux fetchs réels : résumer
                # un texte déjà en base ne martèle personne.
                if fetch_remaining > 0:
                    sleep(random.uniform(SLEEP_MIN_SECONDS, SLEEP_MAX_SECONDS))
            else:
                row = conn.execute(
                    "SELECT markdown FROM offer_content WHERE offer_id = ?", (offer_id,)
                ).fetchone()
                markdown = str(row["markdown"]) if row and row["markdown"] else None
                if markdown is not None:
                    futures[pool.submit(_summarize, config, markdown)] = offer_id
        for future in as_completed(futures):
            offer_id = futures[future]
            try:
                summarized = future.result()
            except Exception:  # un résumé qui plante ne doit pas emporter le run
                log.exception("enrich: summary worker failed for offer %d", offer_id)
                continue
            if summarized is None:
                continue
            fields, quotes, bullets = summarized
            # Le JSON-LD ne sert qu'à combler : ce que le modèle a lu dans
            # l'annonce prime sur une donnée structurée souvent générique.
            for key, value in jsonld_fields.get(offer_id, {}).items():
                if fields.get(key, FIELD_UNKNOWN) == FIELD_UNKNOWN:
                    fields[key] = value
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
