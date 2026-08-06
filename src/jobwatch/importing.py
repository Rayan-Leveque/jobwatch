"""Ingestion : artefacts quotidiens (JSON + digest LLM) et import du suivi Markdown historique."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import urllib.parse
from dataclasses import dataclass
from datetime import date
from pathlib import Path


class ImportError(Exception):
    """Erreur d'import attendue (validation ou SQLite). Aucune écriture partielle n'est conservée."""


@dataclass
class DailyOffer:
    """Une offre issue d'un artefact quotidien, avant écriture en base."""

    url: str
    title: str
    company: str
    platform: str
    source_name: str
    source_type: str
    location: str | None = None
    published_at: str | None = None
    collected_at: str | None = None
    fit: str | None = None


@dataclass
class IngestResult:
    """Bilan déterministe d'un import quotidien."""

    offers_created: int = 0
    offers_already_present: int = 0
    matches_created: int = 0
    fits_updated: int = 0


@dataclass(frozen=True)
class OfferSummary:
    """Résumé Markdown validé, associé à une offre par son URL exacte."""

    url: str
    bullets: tuple[str, ...]


@dataclass
class SummaryImportResult:
    """Bilan déterministe d'un import de résumés."""

    summaries_created: int = 0
    summaries_updated: int = 0
    summaries_unchanged: int = 0
    bullets_written: int = 0


FITS = ("high", "medium", "low")

_JSON_OPTIONAL = ("location", "released", "source", "first_seen")

_URL_RE = re.compile(r"https?://[^\s)]+")
_SECTION_FIT_RE = re.compile(r"\b(high|medium|low)\b", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LINK_RE = re.compile(r"^\[([^\]]+)\]\([^)]+\)")
_SENT_RE = re.compile(r"\[x\]", re.IGNORECASE)
_TRACKER_PATH_RE = re.compile(
    r"(?:documents/(?:cv|cover_letters|applications)|cv|cover_letters)"
    r"/[A-Za-z0-9._\-/]+\.(?:pdf|tex|md)"
)

# Libellé normalisé de source -> (type, nom de source, plateforme).
_KNOWN_SOURCES = {
    "smartrecruiters": ("smartrecruiters", "smartrecruiters", "SmartRecruiters"),
    "sr": ("smartrecruiters", "smartrecruiters", "SmartRecruiters"),
    "linkedin": ("web", "linkedin", "LinkedIn"),
    "li": ("web", "linkedin", "LinkedIn"),
    "wttj": ("web", "wttj", "WTTJ"),
    "welcometothejungle": ("web", "wttj", "WTTJ"),
    "wttj (web)": ("web", "wttj", "WTTJ"),
    "france_travail": ("france_travail", "france_travail", "France Travail"),
    "france travail": ("france_travail", "france_travail", "France Travail"),
}


def _is_http_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in ("http", "https")
        and bool(parsed.hostname)
        and (port is None or 0 < port <= 65535)
        and not any(char.isspace() for char in value)
    )


def parse_summaries_markdown(path: Path) -> list[OfferSummary]:
    """Analyse intégralement un fichier de sections ``## URL`` et de bullets."""
    name = str(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ImportError(f"fichier introuvable : {name}") from exc
    except OSError as exc:
        raise ImportError(f"impossible de lire {name} : {exc}") from exc
    return _parse_summaries_text(text, name)


def _parse_summaries_text(text: str, name: str) -> list[OfferSummary]:
    summaries: list[OfferSummary] = []
    seen_urls: set[str] = set()
    current_url: str | None = None
    bullets: list[str] = []

    def finish_section() -> None:
        if current_url is None:
            return
        if not bullets:
            raise ImportError(f"{name} : aucun bullet pour {current_url}")
        summaries.append(OfferSummary(current_url, tuple(bullets)))

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if line.startswith("## "):
            finish_section()
            url = line[3:].strip()
            if not _is_http_url(url):
                raise ImportError(f"{name} : ligne {lineno} : URL HTTP(S) invalide")
            if url in seen_urls:
                raise ImportError(f"{name} : ligne {lineno} : URL dupliquée : {url}")
            seen_urls.add(url)
            current_url = url
            bullets = []
            continue
        if line.startswith("-"):
            if current_url is None:
                raise ImportError(f"{name} : ligne {lineno} : bullet hors section")
            if not line.startswith("- ") or not line[2:].strip():
                raise ImportError(f"{name} : ligne {lineno} : bullet vide")
            bullets.append(line[2:].strip())
            continue
        if current_url is not None and line:
            raise ImportError(f"{name} : ligne {lineno} : contenu inattendu dans la section")

    finish_section()
    if not summaries:
        raise ImportError(f"aucun résumé valide dans {name}")
    return summaries


def import_summaries(conn: sqlite3.Connection, path: Path) -> SummaryImportResult:
    """Importe des résumés atomiquement, sans créer d'offre absente.

    Un résumé inchangé reste intact. Si ses bullets ont changé, ils remplacent
    intégralement la version stockée tout en conservant leur ordre.
    """
    summaries = parse_summaries_markdown(path)
    urls = [summary.url for summary in summaries]
    offers: dict[str, int] = {}
    for chunk in _chunks(urls):
        placeholders = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT id, url FROM offer WHERE url IN ({placeholders})", chunk
        ):
            offers[str(row["url"])] = int(row["id"])
    missing = [url for url in urls if url not in offers]
    if missing:
        rendered = "\n".join(f"- {url}" for url in missing)
        raise ImportError(f"offre(s) absente(s) de la base :\n{rendered}")

    try:
        result = _write_summaries(conn, summaries, offers)
    except sqlite3.Error as exc:
        conn.rollback()
        raise ImportError(f"erreur SQLite pendant l'import : {exc}") from exc
    conn.commit()
    return result


def _write_summaries(
    conn: sqlite3.Connection,
    summaries: list[OfferSummary],
    offers: dict[str, int],
) -> SummaryImportResult:
    result = SummaryImportResult()
    for summary in summaries:
        offer_id = offers[summary.url]
        stored = conn.execute(
            "SELECT id FROM offer_summary WHERE offer_id = ?", (offer_id,)
        ).fetchone()
        if stored is None:
            cur = conn.execute("INSERT INTO offer_summary (offer_id) VALUES (?)", (offer_id,))
            summary_id = int(cur.lastrowid)
            result.summaries_created += 1
        else:
            summary_id = int(stored["id"])
            existing = tuple(
                str(row["text"])
                for row in conn.execute(
                    "SELECT text FROM summary_bullet WHERE summary_id = ? ORDER BY position",
                    (summary_id,),
                )
            )
            if existing == summary.bullets:
                result.summaries_unchanged += 1
                continue
            conn.execute("DELETE FROM summary_bullet WHERE summary_id = ?", (summary_id,))
            result.summaries_updated += 1
        conn.executemany(
            "INSERT INTO summary_bullet (summary_id, position, text) VALUES (?, ?, ?)",
            ((summary_id, position, bullet) for position, bullet in enumerate(summary.bullets)),
        )
        result.bullets_written += len(summary.bullets)
    return result


def _is_iso_date(value: str | None) -> bool:
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _host_of(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")


def _platform_from_host(host: str) -> str:
    if "smartrecruiters" in host:
        return "SmartRecruiters"
    if "linkedin" in host:
        return "LinkedIn"
    if "welcometothejungle" in host:
        return "WTTJ"
    if "choisirleservicepublic" in host:
        return "Service public"
    if "lever" in host:
        return "Lever"
    if "greenhouse" in host:
        return "Greenhouse"
    return host


def _link_label(cell: str) -> str:
    """Renvoie le texte d'un lien Markdown en tête de cellule, sinon la cellule telle quelle."""
    match = _LINK_RE.match(cell.strip())
    return match.group(1) if match else cell


def _source_for(label: str, url: str) -> tuple[str, str, str]:
    """Renvoie (type, nom de source, plateforme) pour un libellé de source et une URL."""
    key = (label or "").strip().lower()
    known = _KNOWN_SOURCES.get(key)
    if known is not None:
        return known
    host = _host_of(url)
    return ("web", host, _platform_from_host(host))


def parse_daily_json(path: Path) -> list[DailyOffer]:
    """Analyse et valide le JSON quotidien d'offres, en levant ImportError en cas de problème."""
    name = str(path)
    try:
        text = path.read_text()
    except OSError as exc:
        raise ImportError(f"impossible de lire {name} : {exc}") from exc
    return _parse_json_text(text, name)


def _parse_json_text(text: str, name: str) -> list[DailyOffer]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ImportError(f"JSON invalide dans {name} : {exc}") from exc
    if not isinstance(data, dict):
        raise ImportError(f"{name} : la racine doit être un objet JSON")
    offers: list[DailyOffer] = []
    for key, entry in data.items():
        if not isinstance(entry, dict):
            raise ImportError(f"{name} : l'entrée {key!r} doit être un objet")
        title = entry.get("title")
        company = entry.get("company")
        url = entry.get("url")
        if not isinstance(title, str) or not title:
            raise ImportError(f"{name} : {key!r} : 'title' doit être une chaîne non vide")
        if not isinstance(company, str) or not company:
            raise ImportError(f"{name} : {key!r} : 'company' doit être une chaîne non vide")
        if not isinstance(url, str) or not _is_http_url(url):
            raise ImportError(f"{name} : {key!r} : 'url' doit être une URL HTTP(S) absolue")
        for field_name in _JSON_OPTIONAL:
            value = entry.get(field_name)
            if value is not None and not isinstance(value, str):
                raise ImportError(f"{name} : {key!r} : '{field_name}' doit être une chaîne")
        collected_at = entry.get("first_seen")
        if not _is_iso_date(collected_at):
            collected_at = None
        source_type, source_name, platform = _source_for(entry.get("source") or "", url)
        offers.append(
            DailyOffer(
                url=url,
                title=title,
                company=company,
                platform=platform,
                source_name=source_name,
                source_type=source_type,
                location=entry.get("location") or None,
                published_at=entry.get("released"),
                collected_at=collected_at,
            )
        )
    return offers


def parse_daily_digest(path: Path) -> list[DailyOffer]:
    """Analyse le digest LLM Markdown, en levant ImportError quand aucune offre valide n'est trouvée."""
    name = str(path)
    try:
        text = path.read_text()
    except OSError as exc:
        raise ImportError(f"impossible de lire {name} : {exc}") from exc
    return _parse_digest_text(text, name)


def _parse_digest_text(text: str, name: str) -> list[DailyOffer]:
    offers: list[DailyOffer] = []
    current_fit: str | None = None
    headers: list[str] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            match = _SECTION_FIT_RE.search(line[3:])
            current_fit = match.group(1).lower() if match else None
            headers = None
            continue
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        if headers is None:
            headers = cells
            continue
        row = _repair_row(headers, cells)
        offer = _offer_from_digest_row(row, current_fit)
        if offer is not None:
            offers.append(offer)
    if not offers:
        raise ImportError(f"aucune offre valide dans le digest {name}")
    return offers


def _repair_row(headers: list[str], cells: list[str]) -> dict[str, str]:
    """Réinjecte les cellules excédentaires dans la colonne Poste quand un titre contient '|'."""
    if len(cells) > len(headers):
        poste = next(
            (i for i, header in enumerate(headers) if header.startswith("Poste")),
            len(headers) - 1,
        )
        extra = len(cells) - len(headers)
        joined = " | ".join(cells[poste : poste + extra + 1])
        cells = cells[:poste] + [joined] + cells[poste + extra + 1 :]
    cells += [""] * max(0, len(headers) - len(cells))
    return dict(zip(headers, cells[: len(headers)]))


def _clean_cell(value: str | None) -> str:
    if value is None:
        return ""
    return re.sub(r"[*`]", "", value).strip()


def _first_url(*cells: str) -> str | None:
    for cell in cells:
        match = _URL_RE.search(cell)
        if match:
            return match.group(0).rstrip(".,;:'\")]}%")
    return None


def _fit_of(cell: str | None, section_fit: str | None) -> str | None:
    value = (cell or "").strip().lower()
    if value in FITS:
        return value
    return section_fit


def _offer_from_digest_row(row: dict[str, str], section_fit: str | None) -> DailyOffer | None:
    title = _clean_cell(row.get("Poste"))
    if not title:
        return None
    company = _clean_cell(row.get("Entreprise") or row.get("Employeur") or "")
    if not company:
        return None
    url = _first_url(row.get("URL") or "", row.get("Source") or "")
    if not url or not _is_http_url(url):
        return None
    label = _link_label(row.get("Source") or "")
    source_type, source_name, platform = _source_for(label, url)
    location = _clean_cell(row.get("Lieu")) or None
    return DailyOffer(
        url=url,
        title=title,
        company=company,
        platform=platform,
        source_name=source_name,
        source_type=source_type,
        location=location,
        fit=_fit_of(row.get("Fit"), section_fit),
    )


def merge_offers(
    json_offers: list[DailyOffer], digest_offers: list[DailyOffer]
) -> list[DailyOffer]:
    """Fusionne par URL : les métadonnées structurées du JSON priment, le digest apporte le fit."""
    merged: dict[str, DailyOffer] = {}
    for offer in json_offers:
        merged[offer.url] = offer
    for offer in digest_offers:
        existing = merged.get(offer.url)
        if existing is None:
            merged[offer.url] = offer
        elif offer.fit is not None:
            existing.fit = offer.fit
    return sorted(merged.values(), key=lambda offer: offer.url)


def ingest_daily(
    conn: sqlite3.Connection,
    api_path: Path | None,
    digest_path: Path | None,
    search_name: str,
) -> IngestResult:
    """Importe atomiquement les artefacts quotidiens associés à une recherche.

    Les offres sont dédupliquées par URL. L'import est idempotent : relancer les
    mêmes artefacts ne crée aucun doublon et ne rétrograde jamais l'état d'un match.
    """
    if api_path is None and digest_path is None:
        raise ImportError("au moins l'un de api_path ou digest_path est requis")
    json_offers = parse_daily_json(api_path) if api_path is not None else []
    digest_offers = parse_daily_digest(digest_path) if digest_path is not None else []
    offers = merge_offers(json_offers, digest_offers)
    if not offers:
        raise ImportError("aucune offre à importer")
    try:
        result = _write_offers(conn, offers, search_name)
    except sqlite3.Error as exc:
        conn.rollback()
        raise ImportError(f"erreur SQLite pendant l'import : {exc}") from exc
    conn.commit()
    return result


def _write_offers(
    conn: sqlite3.Connection, offers: list[DailyOffer], search_name: str
) -> IngestResult:
    search_id = _ensure_search(conn, search_name)
    result = IngestResult()
    for offer in offers:
        source_id = _ensure_source(conn, offer.source_name, offer.source_type)
        company_id = _ensure_company(conn, offer.company)
        existing = conn.execute("SELECT id FROM offer WHERE url = ?", (offer.url,)).fetchone()
        if existing is None:
            cur = conn.execute(
                "INSERT INTO offer "
                "(source_id, company_id, title, url, platform, location, contract, "
                "published_at, collected_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, COALESCE(?, datetime('now')))",
                (
                    source_id,
                    company_id,
                    offer.title,
                    offer.url,
                    offer.platform,
                    offer.location,
                    offer.published_at,
                    offer.collected_at,
                ),
            )
            offer_id = int(cur.lastrowid)
            result.offers_created += 1
        else:
            offer_id = int(existing["id"])
            result.offers_already_present += 1
        match = conn.execute(
            "SELECT id, fit FROM match WHERE search_id = ? AND offer_id = ?",
            (search_id, offer_id),
        ).fetchone()
        if match is None:
            conn.execute(
                "INSERT INTO match (search_id, offer_id, fit) VALUES (?, ?, ?)",
                (search_id, offer_id, offer.fit),
            )
            result.matches_created += 1
        elif offer.fit is not None and offer.fit != match["fit"]:
            conn.execute("UPDATE match SET fit = ? WHERE id = ?", (offer.fit, match["id"]))
            result.fits_updated += 1
    return result


def _ensure_search(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM search WHERE name = ?", (name,)).fetchone()
    if row is not None:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO search (name, include_json, exclude_json, locations_json, contract, active) "
        "VALUES (?, '[]', '[]', '[]', NULL, 1)",
        (name,),
    )
    return int(cur.lastrowid)


def _ensure_source(conn: sqlite3.Connection, name: str, source_type: str) -> int:
    conn.execute("INSERT OR IGNORE INTO source (type, name) VALUES (?, ?)", (source_type, name))
    row = conn.execute("SELECT id FROM source WHERE name = ?", (name,)).fetchone()
    return int(row["id"])


def _ensure_company(conn: sqlite3.Connection, name: str) -> int:
    conn.execute("INSERT OR IGNORE INTO company (name) VALUES (?)", (name,))
    row = conn.execute("SELECT id FROM company WHERE name = ?", (name,)).fetchone()
    return int(row["id"])


@dataclass
class TrackerRow:
    """Une ligne de données du suivi Markdown historique, avant écriture en base."""

    section: str
    line: int
    company: str
    title: str
    url: str
    sent: bool
    applied_at: str | None
    fit: str | None
    deadline: str | None
    seen_on: str | None
    cv_paths: list[str]
    cover_paths: list[str]


@dataclass
class TrackerImportResult:
    """Bilan déterministe d'un import du suivi Markdown."""

    rows_imported: int = 0
    offers_created: int = 0
    matches_created: int = 0
    applications_created: int = 0
    events_created: int = 0
    documents_created: int = 0
    rows_already_present: int = 0
    fits_updated: int = 0


def parse_tracker_markdown(path: Path) -> list[TrackerRow]:
    """Analyse et valide le suivi Markdown, en levant ImportError en cas de ligne invalide."""
    name = str(path)
    try:
        text = path.read_text()
    except OSError as exc:
        raise ImportError(f"impossible de lire {name} : {exc}") from exc
    return _parse_tracker_text(text, name)


def _parse_tracker_text(text: str, name: str) -> list[TrackerRow]:
    rows: list[TrackerRow] = []
    current_section: str | None = None
    headers: list[str] | None = None
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if line.startswith("## "):
            current_section = line[3:]
            headers = None
            continue
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        if headers is None:
            headers = cells
            continue
        if not _is_tracker_table(headers):
            continue
        row = _repair_row(headers, cells)
        rows.append(
            _tracker_row_from(row, name, current_section or "(hors section)", lineno)
        )
    return rows


def _is_tracker_table(headers: list[str]) -> bool:
    """Vrai si les en-têtes décrivent un tableau de candidatures du suivi."""
    lower = [header.lower() for header in headers]
    has_company = any(header in ("employeur", "entreprise") for header in lower)
    has_title = any(header == "poste" or header.startswith("poste ") for header in lower)
    has_sent = any(header == "envoyé" for header in lower)
    return has_sent or (has_company and has_title)


def _tracker_row_from(
    row: dict[str, str], name: str, section: str, lineno: int
) -> TrackerRow:
    company = _clean_cell(row.get("Entreprise") or row.get("Employeur"))
    title = _clean_cell(row.get("Poste") or row.get("Poste (réf.)"))
    if not company or not title:
        raise ImportError(
            f"{name} : section {section!r}, ligne {lineno} : entreprise et titre requis"
        )
    url = _first_url(row.get("URL") or "") or _synthetic_url(section, company, title)
    return TrackerRow(
        section=section,
        line=lineno,
        company=company,
        title=title,
        url=url,
        sent=bool(_SENT_RE.search(row.get("Envoyé") or "")),
        applied_at=_normalize_date(row.get("Date")),
        fit=_fit_of_tracker(row.get("Fit")),
        deadline=_clean_deadline(row.get("Deadline")),
        seen_on=_iso_or_none(row.get("Vu le")),
        cv_paths=_doc_paths(row.get("CV")),
        cover_paths=_doc_paths(row.get("LDM")),
    )


def _synthetic_url(section: str, company: str, title: str) -> str:
    """URL synthétique stable, non cliquable, pour une offre du tracker sans URL."""
    payload = f"{section}\x1f{company}\x1f{title}"
    return f"jobwatch:{hashlib.sha1(payload.encode('utf-8')).hexdigest()}"


def _normalize_date(value: str | None) -> str | None:
    """Normalise une date approximative '~2026-07-15' en '2026-07-15'."""
    cleaned = (value or "").strip()
    if cleaned.startswith("~"):
        cleaned = cleaned[1:].strip()
    return cleaned if _is_iso_date(cleaned) else None


def _iso_or_none(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned if _is_iso_date(cleaned) else None


def _fit_of_tracker(cell: str | None) -> str | None:
    value = _clean_cell(cell).lower()
    return value if value in FITS else None


def _clean_deadline(cell: str | None) -> str | None:
    value = (cell or "").strip()
    if not value or value.lower() in ("n.r.", "nr"):
        return None
    return value


def _doc_paths(cell: str | None) -> list[str]:
    """Extrait les chemins réels documents/{cv,cover_letters,applications}/... en pdf/tex/md."""
    if not cell:
        return []
    paths: list[str] = []
    for path in _TRACKER_PATH_RE.findall(cell):
        if path not in paths:
            paths.append(path)
    return paths


def import_tracker(
    conn: sqlite3.Connection, path: Path, search_name: str
) -> TrackerImportResult:
    """Importe atomiquement le suivi Markdown associé à une recherche.

    La validation complète a lieu avant toute écriture. L'import est idempotent :
    relancer le même fichier ne crée aucun doublon d'offre, match, candidature,
    événement ou document, et ne rétrograde jamais un état existant.
    """
    rows = parse_tracker_markdown(path)
    if not rows:
        raise ImportError(f"aucune ligne de suivi valide dans {path}")
    try:
        result = _write_tracker(conn, rows, search_name)
    except sqlite3.Error as exc:
        conn.rollback()
        raise ImportError(f"erreur SQLite pendant l'import : {exc}") from exc
    conn.commit()
    return result


def _chunks(seq: list, size: int = 500):
    for index in range(0, len(seq), size):
        yield seq[index : index + size]


def _write_tracker(
    conn: sqlite3.Connection, rows: list[TrackerRow], search_name: str
) -> TrackerImportResult:
    result = TrackerImportResult(rows_imported=len(rows))
    search_id = _ensure_search(conn, search_name)
    source_id = _ensure_source(conn, "markdown-import", "import")

    company_names = sorted({row.company for row in rows})
    for name in company_names:
        conn.execute("INSERT OR IGNORE INTO company (name) VALUES (?)", (name,))
    placeholders = ",".join("?" * len(company_names))
    company_rows = conn.execute(
        f"SELECT id, name FROM company WHERE name IN ({placeholders})", company_names
    ).fetchall()
    company_ids = {row["name"]: int(row["id"]) for row in company_rows}

    offer_map: dict[str, tuple[int, str | None]] = {}
    for chunk in _chunks([row.url for row in rows]):
        query = ",".join("?" * len(chunk))
        for offer_row in conn.execute(
            f"SELECT id, url, deadline FROM offer WHERE url IN ({query})", chunk
        ):
            offer_map[offer_row["url"]] = (int(offer_row["id"]), offer_row["deadline"])

    match_rows = conn.execute(
        "SELECT id, offer_id, state, fit FROM match WHERE search_id = ?", (search_id,)
    ).fetchall()
    match_map: dict[int, tuple[int, str, str | None]] = {
        int(match_row["offer_id"]): (
            int(match_row["id"]),
            match_row["state"],
            match_row["fit"],
        )
        for match_row in match_rows
    }

    processed: list[tuple[TrackerRow, int, int, bool, bool]] = []
    for row in rows:
        entry = offer_map.get(row.url)
        if entry is None:
            platform = (
                _platform_from_host(_host_of(row.url))
                if _is_http_url(row.url)
                else "Import Markdown"
            )
            cur = conn.execute(
                "INSERT INTO offer "
                "(source_id, company_id, title, url, platform, location, contract, "
                "published_at, collected_at, deadline) "
                "VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, COALESCE(?, datetime('now')), ?)",
                (
                    source_id,
                    company_ids[row.company],
                    row.title,
                    row.url,
                    platform,
                    row.seen_on,
                    row.deadline,
                ),
            )
            offer_id = int(cur.lastrowid)
            offer_map[row.url] = (offer_id, row.deadline)
            offer_new = True
            result.offers_created += 1
        else:
            offer_id, existing_deadline = entry
            offer_new = False
            if row.deadline is not None and row.deadline != existing_deadline:
                conn.execute(
                    "UPDATE offer SET deadline = ? WHERE id = ?", (row.deadline, offer_id)
                )
                offer_map[row.url] = (offer_id, row.deadline)

        match = match_map.get(offer_id)
        if match is None:
            state = "applied" if row.sent else "seen"
            cur = conn.execute(
                "INSERT INTO match (search_id, offer_id, state, fit) VALUES (?, ?, ?, ?)",
                (search_id, offer_id, state, row.fit),
            )
            match_id = int(cur.lastrowid)
            match_map[offer_id] = (match_id, state, row.fit)
            match_new = True
            result.matches_created += 1
        else:
            match_id, existing_state, existing_fit = match
            match_new = False
            if row.fit is not None and row.fit != existing_fit:
                conn.execute("UPDATE match SET fit = ? WHERE id = ?", (row.fit, match_id))
                match_map[offer_id] = (match_id, existing_state, row.fit)
                result.fits_updated += 1
            if row.sent and existing_state in ("new", "seen"):
                conn.execute(
                    "UPDATE match SET state = 'applied' WHERE id = ?", (match_id,)
                )
                match_map[offer_id] = (
                    match_id,
                    "applied",
                    row.fit if row.fit is not None else existing_fit,
                )

        processed.append((row, offer_id, match_id, offer_new, match_new))

    pre_existing_apps: set[int] = set()
    sent_rows = [entry for entry in processed if entry[0].sent]
    if sent_rows:
        app_map = _existing_applications(conn, [entry[2] for entry in sent_rows])
        pre_existing_apps = set(app_map)
        for row, offer_id, match_id, _offer_new, _match_new in sent_rows:
            if match_id not in app_map:
                cur = conn.execute(
                    "INSERT INTO application (match_id, offer_id, created_at) "
                    "VALUES (?, ?, COALESCE(?, datetime('now')))",
                    (match_id, offer_id, row.applied_at),
                )
                app_map[match_id] = int(cur.lastrowid)
                result.applications_created += 1
        app_ids = list(app_map.values())
        applied_events = _existing_applied_events(conn, app_ids)
        documents = _existing_documents(conn, app_ids)
        for row, _offer_id, match_id, _offer_new, _match_new in sent_rows:
            app_id = app_map[match_id]
            if app_id not in applied_events:
                if row.applied_at is not None:
                    conn.execute(
                        "INSERT INTO event (application_id, type, at) VALUES (?, 'applied', ?)",
                        (app_id, row.applied_at),
                    )
                else:
                    conn.execute(
                        "INSERT INTO event (application_id, type) VALUES (?, 'applied')",
                        (app_id,),
                    )
                applied_events.add(app_id)
                result.events_created += 1
            for path in row.cv_paths:
                if (app_id, "cv", path) not in documents:
                    conn.execute(
                        "INSERT INTO document (application_id, type, path, sent_at) "
                        "VALUES (?, 'cv', ?, ?)",
                        (app_id, path, row.applied_at),
                    )
                    documents.add((app_id, "cv", path))
                    result.documents_created += 1
            for path in row.cover_paths:
                if (app_id, "cover_letter", path) not in documents:
                    conn.execute(
                        "INSERT INTO document (application_id, type, path, sent_at) "
                        "VALUES (?, 'cover_letter', ?, ?)",
                        (app_id, path, row.applied_at),
                    )
                    documents.add((app_id, "cover_letter", path))
                    result.documents_created += 1

    result.rows_already_present = sum(
        1
        for row, _offer_id, match_id, offer_new, match_new in processed
        if not offer_new
        and not match_new
        and (not row.sent or match_id in pre_existing_apps)
    )
    return result


def _existing_applications(conn: sqlite3.Connection, match_ids: list[int]) -> dict[int, int]:
    found: dict[int, int] = {}
    for chunk in _chunks(sorted(set(match_ids))):
        query = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT id, match_id FROM application WHERE match_id IN ({query})", chunk
        ):
            found[int(row["match_id"])] = int(row["id"])
    return found


def _existing_applied_events(conn: sqlite3.Connection, app_ids: list[int]) -> set[int]:
    found: set[int] = set()
    for chunk in _chunks(sorted(set(app_ids))):
        query = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT application_id FROM event WHERE type = 'applied' "
            f"AND application_id IN ({query})",
            chunk,
        ):
            found.add(int(row["application_id"]))
    return found


def _existing_documents(
    conn: sqlite3.Connection, app_ids: list[int]
) -> set[tuple[int, str, str]]:
    found: set[tuple[int, str, str]] = set()
    for chunk in _chunks(sorted(set(app_ids))):
        query = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT application_id, type, path FROM document "
            f"WHERE application_id IN ({query})",
            chunk,
        ):
            found.add((int(row["application_id"]), row["type"], row["path"]))
    return found
