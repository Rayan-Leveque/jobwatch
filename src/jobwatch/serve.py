"""Tableau de bord web local servi par `jw serve`.

La page est regénérée à chaque requête GET depuis l'état actuel de la base,
en deux onglets étanches par piste métier : GET / (Ingénieur IA) et GET /po
(Chef de projet / PO, titres matchant PROJECT_TITLE_PATTERNS).
Le rendu est pur (`render_page`) et le serveur HTTP n'utilise que la
bibliothèque standard. Les actions HTTP (POST /match/<id>/later,
/match/<id>/discard, /match/<id>/restore pour l'annulation et
/match/<id>/apply pour enregistrer une candidature avec des documents
choisis dans la bibliothèque) mutent l'état d'un match ; POST /documents
uploade un nouveau document (JSON base64, voir jobwatch.library) sans
mutation de match. POST /match/<id>/draft lance une génération de lettre de
motivation en arrière-plan (voir jobwatch.draft) quand le bloc 'draft' de la
config est renseigné ; GET /match/<id>/draft/status la sonde, et
GET /match/<id>/letter.pdf|.tex|/letter/<n>.png servent les fichiers produits.
Une instance nommée peut activer un compte propriétaire par invitation. Dans
ce cas, toutes les pages, actions et pièces jointes exigent une session.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from dataclasses import field as dataclasses_field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import click

from jobwatch import draft
from jobwatch.applications import ApplicationError, record_application
from jobwatch.auth import (
    AuthError,
    Session,
    accept_invite,
    auth_required,
    clear_login_failures,
    create_session,
    delete_session,
    invite_status,
    login_allowed,
    login_throttle_key,
    record_login_failure,
    resolve_session,
)
from jobwatch.auth_http import (
    CSRF_HEADER,
    csrf_valid,
    expired_session_cookie,
    security_headers,
    session_cookie,
    session_token,
)
from jobwatch.config import DraftConfig
from jobwatch.db import connect
from jobwatch.library import LibraryError, list_library, resolve_path, save_upload
from jobwatch.onboarding import (
    OnboardingError,
    analyze_cvs,
    complete_profile,
    profile_complete,
    profile_cv_library_ids,
    profile_intents,
)
from jobwatch.onboarding_ui import render_onboarding

RESTORE_STATES = ("new", "seen", "later")
_MATCH_ACTION_RE = re.compile(r"^/match/(\d+)/(later|discard|restore|apply)$")
_DRAFT_POST_RE = re.compile(r"^/match/(\d+)/draft$")
_DRAFT_STATUS_RE = re.compile(r"^/match/(\d+)/draft/status$")
_LETTER_FILE_RE = re.compile(r"^/match/(\d+)/letter\.(pdf|tex)$")
_LETTER_PAGE_RE = re.compile(r"^/match/(\d+)/letter/(\d+)\.png$")
_DOCUMENT_FILE_RE = re.compile(r"^/documents/(\d+)$")
_UPLOAD_PATH = "/documents"
MAX_JSON_BODY_BYTES = 15 * 1024 * 1024

_PREVIEW_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

STATUS_LABELS = {
    "applied": "Candidature envoyée",
    "follow_up": "Relance",
    "interview": "Entretien",
    "rejected": "Refus",
    "offer": "Offre reçue",
}
STATUS_UNKNOWN = "Statut inconnu"
CONTRACT_LABELS = {
    "permanent": "CDI",
    "fixed_term": "CDD",
    "internship": "Stage",
    "other": "Autre",
}

_MONTHS = {
    1: "janv.", 2: "févr.", 3: "mars", 4: "avr.", 5: "mai", 6: "juin",
    7: "juil.", 8: "août", 9: "sept.", 10: "oct.", 11: "nov.", 12: "déc.",
}
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


class ServeError(Exception):
    """Échec attendu de l'amorçage du serveur. La CLI affiche un message clair et sort."""


def _short_date(value: str) -> str:
    """2026-08-05 12:00 -> « 5 août » ; garde tel quel si non ISO."""
    m = _DATE_RE.match(value)
    if m:
        return f"{int(m.group(3))} {_MONTHS.get(int(m.group(2)), m.group(2))}"
    return value


# Deux onglets étanches : la piste « Chef de projet / PO » (track « project »)
# regroupe offres et candidatures dont le titre contient un des motifs LIKE
# ci-dessous (insensibles à la casse ASCII) ; l'onglet « Ingénieur IA »
# (track « engineer », racine /) montre tout le reste.
PROJECT_TITLE_PATTERNS = (
    "%chef%de projet%",
    "%chef%de produit%",
    "%product owner%",
    "%product manager%",
)
_PROJECT_TITLE_SQL = "(" + " OR ".join("o.title LIKE ?" for _ in PROJECT_TITLE_PATTERNS) + ")"
TRACKS = ("engineer", "project", "all")


def _track_filter(track: str) -> str:
    """Clause SQL restreignant une requête (alias offre `o`) à la piste demandée."""
    if track == "all":
        return ""
    if track == "project":
        return f"AND {_PROJECT_TITLE_SQL} "
    return f"AND NOT {_PROJECT_TITLE_SQL} "


def _track_params(track: str) -> tuple[str, ...]:
    return PROJECT_TITLE_PATTERNS if track in ("engineer", "project") else ()


def _matches(conn: sqlite3.Connection, state: str, track: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT m.id AS id, o.id AS offer_id, m.state AS state, m.fit AS fit, "
        "       s.name AS search_name, "
        "       c.name AS company, o.title AS title, o.location AS location, "
        "       o.contract AS contract, o.platform AS platform, o.url AS url, "
        "       o.collected_at AS collected_at, o.deadline AS deadline "
        "FROM match m "
        "JOIN search s ON s.id = m.search_id AND s.active = 1 "
        "JOIN offer o ON o.id = m.offer_id "
        "LEFT JOIN company c ON c.id = o.company_id "
        "WHERE m.state = ? AND (m.fit IS NULL OR m.fit != 'high') AND NOT EXISTS "
        "    (SELECT 1 FROM application a WHERE a.match_id = m.id) "
        f"{_track_filter(track)}"
        "ORDER BY CASE m.fit WHEN 'high' THEN 0 WHEN 'medium' THEN 1 "
        "         WHEN 'low' THEN 2 ELSE 3 END, "
        "         o.collected_at DESC, m.id DESC",
        (state, *_track_params(track)),
    ).fetchall()


def _priority_matches(conn: sqlite3.Connection, track: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT m.id AS id, o.id AS offer_id, m.state AS state, m.fit AS fit, "
        "       s.name AS search_name, "
        "       c.name AS company, o.title AS title, o.location AS location, "
        "       o.contract AS contract, o.platform AS platform, o.url AS url, "
        "       o.collected_at AS collected_at, o.deadline AS deadline "
        "FROM match m "
        "JOIN search s ON s.id = m.search_id AND s.active = 1 "
        "JOIN offer o ON o.id = m.offer_id "
        "LEFT JOIN company c ON c.id = o.company_id "
        "WHERE m.fit = 'high' AND m.state IN ('new', 'seen') AND NOT EXISTS "
        "    (SELECT 1 FROM application a WHERE a.match_id = m.id) "
        f"{_track_filter(track)}"
        "ORDER BY o.collected_at DESC, m.id DESC",
        _track_params(track),
    ).fetchall()


def _later_matches(conn: sqlite3.Connection, track: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT m.id AS id, o.id AS offer_id, m.state AS state, m.fit AS fit, "
        "       s.name AS search_name, "
        "       c.name AS company, o.title AS title, o.location AS location, "
        "       o.contract AS contract, o.platform AS platform, o.url AS url, "
        "       o.collected_at AS collected_at, o.deadline AS deadline "
        "FROM match m "
        "JOIN search s ON s.id = m.search_id AND s.active = 1 "
        "JOIN offer o ON o.id = m.offer_id "
        "LEFT JOIN company c ON c.id = o.company_id "
        "WHERE m.state = 'later' AND NOT EXISTS "
        "    (SELECT 1 FROM application a WHERE a.match_id = m.id) "
        f"{_track_filter(track)}"
        "ORDER BY CASE m.fit WHEN 'high' THEN 0 WHEN 'medium' THEN 1 "
        "         WHEN 'low' THEN 2 ELSE 3 END, "
        "         o.collected_at DESC, m.id DESC",
        _track_params(track),
    ).fetchall()


def _discarded_matches(conn: sqlite3.Connection, track: str) -> list[sqlite3.Row]:
    """Matchs écartés depuis moins de 30 jours ; filtre d'affichage pur, jamais de suppression."""
    return conn.execute(
        "SELECT m.id AS id, o.id AS offer_id, m.state AS state, m.fit AS fit, "
        "       s.name AS search_name, "
        "       c.name AS company, o.title AS title, o.location AS location, "
        "       o.contract AS contract, o.platform AS platform, o.url AS url, "
        "       o.collected_at AS collected_at, o.deadline AS deadline "
        "FROM match m "
        "JOIN search s ON s.id = m.search_id AND s.active = 1 "
        "JOIN offer o ON o.id = m.offer_id "
        "LEFT JOIN company c ON c.id = o.company_id "
        "WHERE m.state = 'discarded' AND m.discarded_at > datetime('now', '-30 days') "
        f"{_track_filter(track)}"
        "ORDER BY m.discarded_at DESC, m.id DESC",
        _track_params(track),
    ).fetchall()


def _applications(conn: sqlite3.Connection, track: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT a.id AS id, o.id AS offer_id, m.id AS match_id, m.fit AS fit, "
        "       c.name AS company, o.title AS title, "
        "       o.location AS location, o.contract AS contract, "
        "       o.platform AS platform, o.url AS url, a.note AS note, "
        "       s.name AS search_name, "
        "       a.created_at AS created_at, "
        "       (SELECT e.type FROM event e WHERE e.application_id = a.id "
        "        ORDER BY e.at DESC, e.id DESC LIMIT 1) AS status, "
        "       (SELECT e.at FROM event e WHERE e.application_id = a.id "
        "        ORDER BY e.at DESC, e.id DESC LIMIT 1) AS status_at "
        "FROM application a "
        "JOIN offer o ON o.id = a.offer_id "
        "LEFT JOIN company c ON c.id = o.company_id "
        "LEFT JOIN match m ON m.id = a.match_id "
        "LEFT JOIN search s ON s.id = m.search_id AND s.active = 1 "
        f"WHERE 1=1 {_track_filter(track)}"
        "ORDER BY a.created_at DESC, a.id DESC",
        _track_params(track),
    ).fetchall()


FIELD_LABELS = {
    "experience": "Expérience souhaitée",
    "salary": "Salaire",
    "remote": "Télétravail",
    "stack": "Stack",
}


@dataclass
class Summary:
    """Résumé affichable d'une offre : champs structurés + puces mission."""

    bullets: list[str] = dataclasses_field(default_factory=list)
    fields: list[tuple[str, str]] = dataclasses_field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.bullets or self.fields)


def _summary_bullets(
    conn: sqlite3.Connection, offer_ids: list[int]
) -> dict[int, Summary]:
    if not offer_ids:
        return {}
    summaries: dict[int, Summary] = {}
    for start in range(0, len(offer_ids), 500):
        chunk = offer_ids[start : start + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT os.offer_id AS offer_id, sb.text AS text "
            "FROM offer_summary os "
            "JOIN summary_bullet sb ON sb.summary_id = os.id "
            f"WHERE os.offer_id IN ({placeholders}) "
            "ORDER BY os.offer_id, sb.position",
            chunk,
        ).fetchall()
        for row in rows:
            summaries.setdefault(int(row["offer_id"]), Summary()).bullets.append(
                str(row["text"])
            )
        field_rows = conn.execute(
            "SELECT os.offer_id AS offer_id, sf.key AS key, sf.value AS value "
            "FROM offer_summary os "
            "JOIN summary_field sf ON sf.summary_id = os.id "
            f"WHERE os.offer_id IN ({placeholders})",
            chunk,
        ).fetchall()
        by_offer: dict[int, dict[str, str]] = {}
        for row in field_rows:
            by_offer.setdefault(int(row["offer_id"]), {})[str(row["key"])] = str(row["value"])
        for offer_id, values in by_offer.items():
            summaries.setdefault(offer_id, Summary()).fields = [
                (key, values[key]) for key in FIELD_LABELS if key in values
            ]
    return summaries


def _offer_contents(conn: sqlite3.Connection, offer_ids: list[int]) -> dict[int, str]:
    if not offer_ids:
        return {}
    contents: dict[int, str] = {}
    for start in range(0, len(offer_ids), 500):
        chunk = offer_ids[start : start + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT offer_id AS offer_id, markdown AS markdown FROM offer_content "
            f"WHERE offer_id IN ({placeholders}) AND status = 'ok'",
            chunk,
        ).fetchall()
        for row in rows:
            contents[int(row["offer_id"])] = str(row["markdown"])
    return contents


def _draft_rows(conn: sqlite3.Connection, match_ids: list[int]) -> dict[int, sqlite3.Row]:
    """Dernier job de génération de lettre par match (le plus grand id gagne)."""
    if not match_ids:
        return {}
    drafts: dict[int, sqlite3.Row] = {}
    for start in range(0, len(match_ids), 500):
        chunk = match_ids[start : start + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT * FROM draft_job "
            f"WHERE match_id IN ({placeholders}) ORDER BY match_id, id",
            chunk,
        ).fetchall()
        for row in rows:
            drafts[int(row["match_id"])] = row
    return drafts


def _swipe_deck(conn: sqlite3.Connection, track: str) -> list[sqlite3.Row]:
    """Offres 'new' de la piste à trier : fit high d'abord, puis par date de collecte."""
    return conn.execute(
        "SELECT m.id AS id, o.id AS offer_id, m.state AS state, m.fit AS fit, "
        "       s.name AS search_name, c.name AS company, o.title AS title, "
        "       o.location AS location, o.contract AS contract, o.platform AS platform, "
        "       o.url AS url, o.collected_at AS collected_at, o.deadline AS deadline "
        "FROM match m "
        "JOIN search s ON s.id = m.search_id AND s.active = 1 "
        "JOIN offer o ON o.id = m.offer_id "
        "LEFT JOIN company c ON c.id = o.company_id "
        "WHERE m.state = 'new' AND NOT EXISTS "
        "    (SELECT 1 FROM application a WHERE a.match_id = m.id) "
        f"{_track_filter(track)}"
        "ORDER BY CASE WHEN m.fit = 'high' THEN 0 ELSE 1 END, "
        "         o.collected_at DESC, m.id DESC",
        _track_params(track),
    ).fetchall()


# Cibles de la génération groupée : offres « À candidater » sans lettre générée
# ni génération en cours (les échecs précédents sont réessayés).
_BATCH_ELIGIBLE_SQL = (
    "FROM match m "
    "JOIN search s ON s.id = m.search_id AND s.active = 1 "
    "JOIN offer o ON o.id = m.offer_id "
    "WHERE m.state = 'later' AND NOT EXISTS "
    "    (SELECT 1 FROM application a WHERE a.match_id = m.id) "
    "AND NOT EXISTS (SELECT 1 FROM draft_job dj WHERE dj.match_id = m.id "
    "                AND dj.status IN ('ok', 'running', 'queued')) "
)


_BATCH_BADGE_HTML = (
    '<div class="batch-badge-wrap" id="batch-badge-wrap" hidden>'
    '<button class="batch-badge" id="batch-badge" type="button" aria-expanded="false" '
    'aria-controls="batch-panel" aria-label="Avancement des lettres">'
    '<span class="batch-ring" id="batch-ring" aria-hidden="true"></span>'
    '<span class="batch-badge-count" id="batch-badge-count"></span>'
    "</button>"
    '<div class="batch-panel" id="batch-panel" hidden aria-live="polite">'
    '<p class="batch-panel-title" id="batch-panel-line1"></p>'
    '<p class="batch-panel-note" id="batch-panel-line2"></p></div></div>'
)


def _batch_eligible_ids(conn: sqlite3.Connection, track: str) -> list[int]:
    rows = conn.execute(
        f"SELECT m.id AS id {_BATCH_ELIGIBLE_SQL}{_track_filter(track)}ORDER BY m.id",
        _track_params(track),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def _batch_status(conn: sqlite3.Connection, track: str) -> dict[str, int]:
    """État du dernier job de chaque offre « À candidater » de la piste."""
    rows = conn.execute(
        "SELECT (SELECT dj.status FROM draft_job dj WHERE dj.match_id = m.id "
        "        ORDER BY dj.id DESC LIMIT 1) AS status "
        "FROM match m "
        "JOIN search s ON s.id = m.search_id AND s.active = 1 "
        "JOIN offer o ON o.id = m.offer_id "
        "WHERE m.state = 'later' AND NOT EXISTS "
        "    (SELECT 1 FROM application a WHERE a.match_id = m.id) "
        f"{_track_filter(track)}",
        _track_params(track),
    ).fetchall()
    counts = {"queued": 0, "running": 0, "ok": 0, "failed": 0, "none": 0}
    for row in rows:
        status = str(row["status"]) if row["status"] in counts else "none"
        counts[status] += 1
    return counts


def _link(url: object) -> str:
    """Renvoie le lien de l'offre si son schéma est http/https, sinon rien."""
    value = str(url or "")
    if not value:
        return ""
    try:
        scheme = urlsplit(value).scheme.lower()
    except ValueError:
        return ""
    if scheme not in ("http", "https"):
        return ""
    escaped = html.escape(value)
    return f'<a href="{escaped}" target="_blank" rel="noopener noreferrer">offre ↗</a>'


def _meta(
    row: sqlite3.Row,
    date_label: str,
    date: object,
    search_name: object = None,
    deadline: object = None,
) -> str:
    """Ligne de métadonnées : plateforme, lieu, contrat, deadline, recherche, date, lien."""
    parts: list[str] = []
    if row["platform"]:
        parts.append(f'<span class="platform">{html.escape(str(row["platform"]))}</span>')
    text: list[str] = []
    if row["location"]:
        text.append(str(row["location"]))
    if row["contract"]:
        contract = str(row["contract"])
        text.append(CONTRACT_LABELS.get(contract, contract))
    if deadline:
        text.append(f"échéance {_short_date(str(deadline))}")
    if search_name:
        text.append(f"via {search_name}")
    if date:
        text.append(f"{date_label} {_short_date(str(date))}")
    if text:
        parts.append(html.escape(" · ".join(text)))
    link = _link(row["url"])
    if link:
        parts.append(link)
    return " · ".join(parts)


def _fit_pill(fit: object) -> str:
    """Pill high/medium/low quand un fit est connu, sinon rien."""
    if fit is None:
        return ""
    value = str(fit).lower()
    if value not in ("high", "medium", "low"):
        return ""
    return f'<span class="pill fit {value}">{html.escape(value)}</span>'


def _summary_fields_html(fields: list[tuple[str, str]]) -> str:
    if not fields:
        return ""
    rows = []
    for key, value in fields:
        label = FIELD_LABELS.get(key, key)
        empty = " sf-empty" if value.strip().lower() == "non précisé" else ""
        rows.append(
            f'<div class="summary-field{empty}">'
            f'<span class="sf-label">{html.escape(label)}</span>'
            f'<span class="sf-value">{html.escape(value)}</span></div>'
        )
    return f'<div class="summary-fields">{"".join(rows)}</div>'


def _summary_panel(row: sqlite3.Row, summary: Summary, prefix: str) -> tuple[str, str]:
    if not summary:
        return "", ""
    panel_id = f"summary-{prefix}-{int(row['id'])}"
    label = html.escape(f"Afficher le résumé de {row['title'] or 'cette offre'}", quote=True)
    button = (
        f'<button class="reader-tab summary-toggle" type="button" aria-expanded="false" '
        f'aria-controls="{panel_id}" aria-label="{label}">En bref</button>'
    )
    items = "".join(f"<li>{html.escape(bullet)}</li>" for bullet in summary.bullets)
    bullets_html = f"<ul>{items}</ul>" if items else ""
    panel = (
        f'<div class="summary-panel" id="{panel_id}" hidden>'
        f'<div class="summary-title">En bref</div>'
        f"{_summary_fields_html(summary.fields)}{bullets_html}</div>"
    )
    return button, panel


def _markdown_to_html(markdown: str) -> str:
    """Rendu minimal et échappé : un <p> par paragraphe, <br> pour les retours à la ligne.

    Pas de bibliothèque Markdown supplémentaire pour le dashboard : la syntaxe
    Markdown (titres, gras, listes...) apparaît telle quelle, échappée.
    """
    paragraphs = re.split(r"\n\s*\n", markdown.strip())
    rendered = (
        f"<p>{html.escape(paragraph).replace(chr(10), '<br>')}</p>"
        for paragraph in paragraphs
        if paragraph.strip()
    )
    return "".join(rendered)


def _content_panel(row: sqlite3.Row, markdown: str | None, prefix: str) -> tuple[str, str]:
    if not markdown:
        return "", ""
    panel_id = f"content-{prefix}-{int(row['id'])}"
    label = html.escape(f"Afficher l'annonce complète de {row['title'] or 'cette offre'}", quote=True)
    button = (
        f'<button class="reader-tab offer-toggle" type="button" aria-expanded="false" '
        f'aria-controls="{panel_id}" aria-label="{label}">Annonce</button>'
    )
    panel = f'<div class="content-panel" id="{panel_id}" hidden>{_markdown_to_html(markdown)}</div>'
    return button, panel


def _row_class(state: object) -> str:
    value = str(state)
    return value if value in ("new", "seen", "later", "discarded") else "seen"


def _document_options(rows: list[sqlite3.Row]) -> str:
    options = ['<option value="">Aucun</option>']
    options.extend(
        f'<option value="{int(r["id"])}">{html.escape(str(r["label"]))}</option>' for r in rows
    )
    return "".join(options)


_EYE_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M2.5 12S6 5.8 12 5.8 21.5 12 21.5 12 18 18.2 12 18.2 2.5 12 2.5 12Z"/>'
    '<circle cx="12" cy="12" r="2.8"/></svg>'
)
_UPLOAD_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M12 4v11M7.5 11l4.5 4.5L16.5 11"/><path d="M5 20h14"/></svg>'
)


def _preview_button(label: str) -> str:
    escaped = html.escape(f"Prévisualiser : {label}", quote=True)
    return (
        f'<button class="doc-icon-btn doc-preview-btn" type="button" '
        f'aria-label="{escaped}" title="Prévisualiser">{_EYE_SVG}</button>'
    )


def _document_field(match_id: int, doc_type: str, name: str, label: str, rows: list[sqlite3.Row]) -> str:
    select_id = f"{doc_type}-select-{match_id}"
    options = _document_options(rows)
    upload_label = html.escape(f"Uploader : {label}", quote=True)
    return (
        f'<div class="doc-field" data-doc-type="{doc_type}">'
        f'<label class="doc-label" for="{select_id}">{html.escape(label)}</label>'
        '<div class="doc-row">'
        f'<select class="apply-input doc-select" id="{select_id}" name="{name}" '
        f'data-doc-type="{doc_type}">{options}</select>'
        f"{_preview_button(label)}"
        f'<button class="doc-icon-btn doc-upload-btn" type="button" data-doc-type="{doc_type}" '
        f'aria-label="{upload_label}" title="Uploader">{_UPLOAD_SVG}</button>'
        "</div>"
        f'<input class="doc-file-input" type="file" data-doc-type="{doc_type}" hidden>'
        '<div class="doc-label-prompt" hidden>'
        '<input class="apply-input doc-label-input" type="text" autocomplete="off" '
        'placeholder="Nom du document (optionnel)" aria-label="Nom du document">'
        '<button class="card-action doc-label-confirm" type="button">Ajouter à la bibliothèque</button>'
        "</div>"
        "</div>"
    )


def _card_actions(
    row: sqlite3.Row,
    library: dict[str, list[sqlite3.Row]],
) -> str:
    match_id = int(row["id"])
    prev_state = html.escape(str(row["state"]), quote=True)
    form_id = f"apply-form-{match_id}"
    return (
        '<div class="card-actions">'
        f'<button class="card-action action-later" type="button" data-match-id="{match_id}" '
        f'data-prev-state="{prev_state}" data-action="later">Plus tard</button>'
        f'<button class="card-action action-apply" type="button" aria-expanded="false" '
        f'aria-controls="{form_id}">Candidater</button>'
        f'<button class="card-action action-discard" type="button" data-match-id="{match_id}" '
        f'data-prev-state="{prev_state}" data-action="discard">Écarter</button>'
        "</div>"
        f'<form class="apply-form" id="{form_id}" data-match-id="{match_id}" hidden>'
        f'{_document_field(match_id, "cv", "cv_library_id", "CV", library["cv"])}'
        f'{_document_field(match_id, "cover_letter", "cover_letter_library_id", "Lettre de motivation", library["cover_letter"])}'
        '<button class="card-action apply-submit" type="submit">Enregistrer la candidature</button>'
        "</form>"
    )


def _draft_button(match_id: int, label: str, *, hidden: bool = False) -> str:
    hidden_attr = " hidden" if hidden else ""
    return (
        f'<button class="card-action action-draft" type="button" aria-expanded="false" '
        f'aria-controls="draft-form-{match_id}"{hidden_attr}>{html.escape(label)}</button>'
    )


def _draft_form(match_id: int, track: str, cv_rows: list[sqlite3.Row]) -> str:
    form_id = f"draft-form-{match_id}"
    if not cv_rows:
        inner = (
            '<p class="empty-note">Uploadez d\'abord un CV dans la bibliothèque '
            "(formulaire Candidater).</p>"
        )
    else:
        select_id = f"draft-cv-{match_id}"
        options = "".join(
            f'<option value="{int(r["id"])}">{html.escape(str(r["label"]))}</option>'
            for r in cv_rows
        )
        inner = (
            f'<label class="doc-label" for="{select_id}">CV</label>'
            '<div class="doc-row">'
            f'<select class="apply-input draft-cv-select" id="{select_id}" '
            f'name="cv_library_id">{options}</select>'
            f'{_preview_button("CV")}'
            "</div>"
            '<input class="apply-input" type="text" name="instruction" autocomplete="off" '
            'placeholder="Consigne (optionnel)" aria-label="Consigne pour le modèle">'
            '<button class="card-action draft-submit" type="submit">Générer la lettre</button>'
        )
    return (
        f'<form class="draft-form" id="{form_id}" data-match-id="{match_id}" '
        f'data-track="{html.escape(track, quote=True)}" hidden>{inner}</form>'
    )


def _draft_status_html(match_id: int, job: sqlite3.Row | None) -> str:
    """Fragment d'état de la génération : réutilisé au rendu et par /draft/status."""
    if job is None:
        return ""
    status = str(job["status"])
    if status == "queued":
        return (
            '<span class="draft-spinner" aria-hidden="true"></span>'
            "<span>Lettre en file d'attente…</span>"
        )
    if status == "running":
        return (
            '<span class="draft-spinner" aria-hidden="true"></span>'
            "<span>Génération de la lettre en cours…</span>"
        )
    if status == "failed":
        error = html.escape(str(job["error"] or "échec inconnu"))
        tex_link = (
            f' · <a href="/match/{match_id}/letter.tex">source .tex</a>'
            if job["tex_path"]
            else ""
        )
        return f'<p class="draft-error">Échec de la génération : {error}{tex_link}</p>'
    pages = int(job["png_pages"] or 1)
    version = int(job["id"])
    images = "".join(
        f'<img class="letter-page" src="/match/{match_id}/letter/{page}.png?v={version}" '
        f'alt="Lettre de motivation, page {page}" loading="lazy">'
        for page in range(1, pages + 1)
    )
    warning = (
        f'<p class="draft-warning">{html.escape(str(job["warning"]))}</p>'
        if job["warning"]
        else ""
    )
    return (
        f"{warning}{images}"
        f'<p class="letter-links"><a href="/match/{match_id}/letter.pdf">Télécharger le PDF</a>'
        f' · <a href="/match/{match_id}/letter.tex">Source .tex</a></p>'
    )


def _letter_reader(
    match_id: int,
    job: sqlite3.Row | None,
    track: str,
    cv_rows: list[sqlite3.Row],
) -> tuple[str, str]:
    status = str(job["status"]) if job is not None else ""
    labels = {
        "queued": "Lettre · en cours",
        "running": "Lettre · en cours",
        "failed": "Lettre · échec",
        "ok": "Lettre · prête",
    }
    compose_labels = {
        "failed": "Réessayer",
        "ok": "Régénérer la lettre",
    }
    panel_id = f"letter-panel-{match_id}"
    button = (
        f'<button class="reader-tab letter-toggle letter-{html.escape(status or "empty", quote=True)}" '
        f'type="button" aria-expanded="false" aria-controls="{panel_id}">'
        f'<span class="reader-tab-label">{html.escape(labels.get(status, "Lettre"))}</span></button>'
    )
    area = (
        f'<div class="draft-area" id="draft-area-{match_id}" data-match-id="{match_id}" '
        f'data-status="{status}">{_draft_status_html(match_id, job)}</div>'
    )
    compose = _draft_button(
        match_id,
        compose_labels.get(status, "Générer la lettre"),
        hidden=status in ("queued", "running"),
    )
    panel = (
        f'<div class="letter-panel" id="{panel_id}" hidden>{area}{compose}'
        f'{_draft_form(match_id, track, cv_rows)}</div>'
    )
    return button, panel


def _card_reader(buttons: list[str], panels: list[str]) -> str:
    available_buttons = "".join(button for button in buttons if button)
    if not available_buttons:
        return ""
    return (
        '<div class="card-reader"><div class="reader-tabs" role="tablist" '
        f'aria-label="Contenu de l’offre">{available_buttons}</div>'
        f'{"".join(panel for panel in panels if panel)}</div>'
    )


def _match_card(
    row: sqlite3.Row,
    summary: Summary,
    content: str | None,
    library: dict[str, list[sqlite3.Row]],
    job: sqlite3.Row | None,
    track: str,
    draft_enabled: bool,
    actions: bool = False,
) -> str:
    cls = _row_class(row["state"])
    company = html.escape(str(row["company"] or "Société inconnue"))
    title = html.escape(str(row["title"] or ""))
    pill = _fit_pill(row["fit"])
    meta = _meta(row, "collecté le", row["collected_at"], row["search_name"], row["deadline"])
    summary_button, summary_panel = _summary_panel(row, summary, "match")
    content_button, content_panel = _content_panel(row, content, "match")
    letter_button = ""
    letter_panel = ""
    if actions and draft_enabled:
        letter_button, letter_panel = _letter_reader(
            int(row["id"]), job, track, library["cv"]
        )
    reader = _card_reader(
        [summary_button, content_button, letter_button],
        [summary_panel, content_panel, letter_panel],
    )
    actions_html = _card_actions(row, library) if actions else ""
    return (
        f'<article class="row row-{cls}"><div class="body">'
        f'<div class="card-topline"><div class="company">{company}</div>'
        f'<div class="card-badges">{pill}</div></div>'
        f'<div class="role">{title}</div>'
        f'<div class="meta">{meta}</div></div>{actions_html}{reader}'
        "</article>"
    )


def _application_card(
    row: sqlite3.Row,
    summary: Summary,
    content: str | None,
    library: dict[str, list[sqlite3.Row]],
    job: sqlite3.Row | None,
    track: str,
    draft_enabled: bool,
) -> str:
    status = str(row["status"] or "")
    cls = status if status in STATUS_LABELS else "unknown"
    label = STATUS_LABELS.get(status, STATUS_UNKNOWN)
    company = html.escape(str(row["company"] or "Société inconnue"))
    title = html.escape(str(row["title"] or ""))
    pill = f'<span class="pill {cls}">{html.escape(label)}</span>'
    meta = _meta(row, "candidature le", row["created_at"], row["search_name"])
    note = f'<p class="note">{html.escape(str(row["note"]))}</p>' if row["note"] else ""
    summary_button, summary_panel = _summary_panel(row, summary, "application")
    content_button, content_panel = _content_panel(row, content, "application")
    letter_button = ""
    letter_panel = ""
    if draft_enabled and row["match_id"] is not None:
        match_id = int(row["match_id"])
        letter_button, letter_panel = _letter_reader(
            match_id, job, track, library["cv"]
        )
    reader = _card_reader(
        [summary_button, content_button, letter_button],
        [summary_panel, content_panel, letter_panel],
    )
    return (
        f'<article class="row row-applied"><div class="body">'
        f'<div class="card-topline"><div class="company">{company}</div>'
        f'<div class="card-badges">{pill}</div></div>'
        f'<div class="role">{title}</div>'
        f'<div class="meta">{meta}</div>{note}</div>{reader}</article>'
    )


ACTIONABLE_SECTIONS = {"priority", "new", "seen", "later"}


def _card(
    row: sqlite3.Row,
    key: str,
    summaries: dict[int, Summary],
    contents: dict[int, str],
    library: dict[str, list[sqlite3.Row]],
    drafts: dict[int, sqlite3.Row],
    track: str,
    draft_enabled: bool,
) -> str:
    summary = summaries.get(int(row["offer_id"])) or Summary()
    content = contents.get(int(row["offer_id"]))
    if key == "applied":
        job = drafts.get(int(row["match_id"])) if row["match_id"] is not None else None
        return _application_card(row, summary, content, library, job, track, draft_enabled)
    job = drafts.get(int(row["id"]))
    return _match_card(
        row, summary, content, library, job, track, draft_enabled,
        actions=key in ACTIONABLE_SECTIONS,
    )


def _section(
    key: str,
    label: str,
    subtitle: str,
    rows,
    empty_text: str,
    open_default: bool,
    summaries: dict[int, Summary],
    contents: dict[int, str],
    library: dict[str, list[sqlite3.Row]],
    drafts: dict[int, sqlite3.Row],
    track: str,
    draft_enabled: bool,
) -> str:
    if rows:
        cards = "\n".join(
            _card(row, key, summaries, contents, library, drafts, track, draft_enabled)
            for row in rows
        )
    else:
        cards = f'<p class="empty-note">{empty_text}</p>'
    open_attr = " open" if open_default else ""
    default = "1" if open_default else "0"
    return (
        f'<details class="section section-{key}"{open_attr} data-section="{key}" '
        f'data-default="{default}">'
        f'<summary><span class="summary-copy"><span class="section-dot"></span>'
        f'<span><span class="section-title">{html.escape(label)}</span>'
        f'<span class="section-subtitle">{html.escape(subtitle)}</span></span></span>'
        f'<span class="summary-tail"><span class="count">{len(rows)}</span>'
        f'<span class="chevron" aria-hidden="true"></span></span></summary>'
        f'<div class="card-list">{cards}</div></details>'
    )


def render_page(
    conn: sqlite3.Connection,
    track: str = "engineer",
    draft_enabled: bool = False,
    csrf_token: str = "",
) -> str:
    """Rend la page HTML complète d'un onglet depuis l'état actuel de la base."""
    priority = _priority_matches(conn, track)
    new = _matches(conn, "new", track)
    seen = _matches(conn, "seen", track)
    later = _later_matches(conn, track)
    discarded = _discarded_matches(conn, track)
    applied = _applications(conn, track)
    offer_ids = sorted(
        {int(row["offer_id"]) for row in (*priority, *new, *seen, *later, *discarded, *applied)}
    )
    summaries = _summary_bullets(conn, offer_ids)
    contents = _offer_contents(conn, offer_ids)
    library = {"cv": list_library(conn, "cv"), "cover_letter": list_library(conn, "cover_letter")}
    match_ids = sorted(
        {int(row["id"]) for row in (*priority, *new, *seen, *later)}
        | {int(row["match_id"]) for row in applied if row["match_id"] is not None}
    )
    drafts = _draft_rows(conn, match_ids) if draft_enabled else {}
    extra = (drafts, track, draft_enabled)
    body = "\n".join(
        (
            _section(
                "priority", "Priorité haute", "À regarder en premier", priority,
                "Aucune offre prioritaire pour l'instant.", True, summaries, contents, library, *extra,
            ),
            _section(
                "new", "Nouveaux matchs", "À découvrir", new,
                "Aucun nouveau match pour l'instant.", True, summaries, contents, library, *extra,
            ),
            _section(
                "seen", "Vus", "Déjà parcourus", seen,
                "Aucun match parcouru pour l'instant.", False, summaries, contents, library, *extra,
            ),
            _section(
                "later", "À candidater", "Mis de côté pour plus tard", later,
                "Aucune offre à candidater plus tard pour l'instant.", False, summaries, contents, library, *extra,
            ),
            _section(
                "discarded", "Corbeille", "Écartées dans les 30 derniers jours", discarded,
                "Aucune offre écartée récemment.", False, summaries, contents, library, *extra,
            ),
            _section(
                "applied", "Candidatures", "Dernier statut connu", applied,
                "Aucune candidature pour l'instant.", False, summaries, contents, library, *extra,
            ),
        )
    )
    priority_new = sum(row["state"] == "new" for row in priority)
    priority_seen = len(priority) - priority_new
    deck_count = priority_new + len(new)
    swipe_fab, swipe_popup = _swipe_invites(track, deck_count)
    total = len(priority) + len(new) + len(seen) + len(later) + len(discarded) + len(applied)
    stamp = datetime.now(UTC).astimezone().strftime("%d/%m/%Y %H:%M")
    return _page_template(
        body=body, total=total,
        new_count=len(new) + priority_new,
        seen_count=len(seen) + priority_seen,
        applied_count=len(applied),
        stamp=stamp,
        track=track,
        category_link=(
            '<a class="manage-link" href="/onboarding?edit=1">'
            "Modifier mes catégories →</a>"
            if track == "all"
            else ""
        ),
        swipe_fab=swipe_fab,
        swipe_popup=swipe_popup,
        batch_badge=_BATCH_BADGE_HTML if draft_enabled else "",
        csrf_token=csrf_token,
    )


_CARDS_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<rect x="7.2" y="4.2" width="12" height="16" rx="2.4" '
    'transform="rotate(8 13.2 12.2)"/>'
    '<path d="M8.6 6.5 6.2 7.1a2.4 2.4 0 0 0-1.7 2.9l2.6 9.8"/></svg>'
)


def _swipe_invites(track: str, deck_count: int) -> tuple[str, str]:
    """Bouton badge de la barre du haut + popup d'accueil « c'est le moment de swiper »."""
    href = "/swipe" if track in ("engineer", "all") else "/po/swipe"
    plural = "s" if deck_count > 1 else ""
    label = html.escape(
        f"Trier {deck_count} nouvelle{plural} offre{plural}" if deck_count else "Ouvrir le tri des offres",
        quote=True,
    )
    count_html = (
        f'<span class="swipe-fab-count">{deck_count}</span>' if deck_count else ""
    )
    fab = (
        f'<a class="swipe-fab" href="{href}" aria-label="{label}">{_CARDS_SVG}'
        f'<span class="swipe-fab-label">Swiper</span>'
        f"{count_html}</a>"
    )
    if not deck_count:
        return fab, ""
    popup = (
        f'<div class="swipe-popup" id="swipe-popup" data-track="{track}" hidden>'
        '<div class="swipe-popup-card" role="dialog" aria-modal="true" '
        'aria-label="Nouvelles offres à trier">'
        f'<div class="swipe-popup-icon" aria-hidden="true">{_CARDS_SVG}</div>'
        f'<h2>{deck_count} nouvelle{plural} offre{plural}</h2>'
        "<p>C'est le moment de swiper.</p>"
        '<div class="swipe-popup-actions">'
        f'<a class="swipe-popup-go" href="{href}">Swiper →</a>'
        '<button class="swipe-popup-later" type="button">Plus tard</button>'
        "</div></div></div>"
    )
    return fab, popup


def _swipe_card(row: sqlite3.Row, summary: Summary, content: str | None) -> str:
    company = html.escape(str(row["company"] or "Société inconnue"))
    title = html.escape(str(row["title"] or ""))
    pill = _fit_pill(row["fit"])
    meta = _meta(row, "collecté le", row["collected_at"], row["search_name"], row["deadline"])
    summary_html = ""
    if summary:
        items = "".join(f"<li>{html.escape(bullet)}</li>" for bullet in summary.bullets)
        bullets_html = f"<ul>{items}</ul>" if items else ""
        summary_html = (
            '<div class="swipe-summary"><div class="summary-title">En bref</div>'
            f"{_summary_fields_html(summary.fields)}{bullets_html}</div>"
        )
    content_html = ""
    if content:
        panel_id = f"swipe-content-{int(row['id'])}"
        content_html = (
            f'<button class="content-toggle swipe-content-toggle" type="button" '
            f'aria-expanded="false" aria-controls="{panel_id}">Annonce complète'
            '<span class="summary-chevron" aria-hidden="true"></span></button>'
            f'<div class="content-panel" id="{panel_id}" hidden>{_markdown_to_html(content)}</div>'
        )
    return (
        f'<article class="swipe-card" data-match-id="{int(row["id"])}">'
        '<div class="swipe-card-scroll">'
        f'<div class="card-topline"><div class="company">{company}</div>'
        f'<div class="card-badges">{pill}</div></div>'
        f'<div class="role">{title}</div>'
        f'<div class="meta">{meta}</div>'
        f"{summary_html}{content_html}"
        "</div>"
        '<div class="swipe-stamp stamp-right" aria-hidden="true">À candidater</div>'
        '<div class="swipe-stamp stamp-left" aria-hidden="true">Écartée</div>'
        "</article>"
    )


def render_swipe_page(
    conn: sqlite3.Connection,
    track: str = "engineer",
    draft_enabled: bool = False,
    csrf_token: str = "",
) -> str:
    """Rend la page de tri type swipe : une carte 'new' à la fois, bilan à la fin."""
    deck = _swipe_deck(conn, track)
    offer_ids = sorted({int(row["offer_id"]) for row in deck})
    summaries = _summary_bullets(conn, offer_ids)
    contents = _offer_contents(conn, offer_ids)
    cards = "\n".join(
        _swipe_card(
            row,
            summaries.get(int(row["offer_id"])) or Summary(),
            contents.get(int(row["offer_id"])),
        )
        for row in deck
    )
    pending = len(_batch_eligible_ids(conn, track)) if draft_enabled else 0
    cv_rows = list_library(conn, "cv") if draft_enabled else []
    if draft_enabled and cv_rows:
        options = "".join(
            f'<option value="{int(r["id"])}">{html.escape(str(r["label"]))}</option>'
            for r in cv_rows
        )
        batch = (
            '<div class="batch-form">'
            '<label class="doc-label" for="batch-cv">CV pour toutes les lettres</label>'
            f'<select class="apply-input" id="batch-cv">{options}</select>'
            '<button class="card-action batch-btn" id="batch-btn" type="button">'
            f'Générer <span id="batch-count">{pending}</span> lettre(s)</button>'
            "</div>"
        )
    elif draft_enabled:
        batch = (
            '<p class="empty-note">Uploadez d\'abord un CV dans la bibliothèque '
            "(formulaire Candidater du tableau de bord) pour générer les lettres.</p>"
        )
    else:
        batch = (
            '<p class="empty-note">Génération de lettres non configurée '
            "(bloc 'draft' de config.yaml).</p>"
        )
    back_href = "/" if track in ("engineer", "all") else "/po"
    return _swipe_page_template(
        track=track, cards=cards, total=len(deck), pending=pending,
        batch=batch, back_href=back_href,
        batch_badge=_BATCH_BADGE_HTML if draft_enabled else "",
        csrf_token=csrf_token,
    )


def _spawn_draft_job(db_path: Path, config: DraftConfig, job_id: int) -> None:
    """Lance le job de génération dans un thread ; point d'accroche des tests."""
    threading.Thread(
        target=draft.run_job, args=(db_path, config, job_id), daemon=True
    ).start()


def make_handler(
    db_path: Path,
    draft_config: DraftConfig | None = None,
    *,
    workspace_slug: str | None = None,
    secure_cookie: bool = True,
    onboarding_config: DraftConfig | None = None,
    onboarding_enabled: bool = False,
) -> type[BaseHTTPRequestHandler]:
    """Fabrique une classe de gestionnaire HTTP branchée sur render_page.

    Chaque requête ouvre sa propre connexion : ThreadingHTTPServer sert chaque
    requête dans un thread dédié, et la page relit ainsi l'état le plus récent.
    """

    onboarding_enabled = onboarding_enabled or onboarding_config is not None

    class Handler(BaseHTTPRequestHandler):
        server_version = "jobwatch"

        def _authentication(self) -> tuple[bool, Session | None, str | None]:
            conn = connect(db_path)
            try:
                required = auth_required(conn)
                token = session_token(self.headers.get("Cookie"))
                session = resolve_session(conn, token) if required and token else None
                if session is not None and workspace_slug is not None:
                    workspace = conn.execute(
                        "SELECT 1 FROM workspace WHERE id = ? AND slug = ?",
                        (session.workspace_id, workspace_slug),
                    ).fetchone()
                    if workspace is None:
                        session = None
                return required, session, token
            finally:
                conn.close()

        def _require_session(self, path: str) -> Session | None:
            required, session, _token = self._authentication()
            if not required:
                return None
            if workspace_slug is None:
                self._send_text(503, "instance nommée requise pour l'authentification\n")
                return None
            if session is not None:
                return session
            if path.startswith(("/draft/", "/match/", "/documents", "/onboarding/")):
                self._send_json(401, {"error": "authentification requise"})
            else:
                self._redirect("/login")
            return None

        def _auth_enabled_without_session(self, path: str) -> tuple[bool, Session | None]:
            required, session, _token = self._authentication()
            if required and session is None and path not in ("/login",) and not path.startswith(
                "/invite/"
            ):
                self._require_session(path)
                return True, None
            return required, session

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/login":
                required, session, _token = self._authentication()
                if not required or session is not None:
                    self._redirect("/")
                else:
                    self._send_auth_page("Connexion", _login_form())
                return
            invite = re.fullmatch(r"/invite/([^/]+)", path)
            if invite:
                required, session, _token = self._authentication()
                if required and session is not None:
                    self._redirect("/")
                    return
                if workspace_slug is None:
                    self._send_text(503, "instance nommée requise pour l'authentification\n")
                    return
                conn = connect(db_path)
                try:
                    status = invite_status(conn, invite.group(1), workspace_slug)
                finally:
                    conn.close()
                if status == "accepted":
                    self._redirect("/login")
                elif status == "valid":
                    self._send_auth_page("Créer votre compte", _invite_form(invite.group(1)))
                else:
                    self._send_auth_page(
                        "Invitation indisponible",
                        '<p class="auth-intro">Ce lien est invalide ou expiré.</p>'
                        '<a class="auth-link" href="/login">Aller à la connexion</a>',
                        status=410,
                    )
                return
            required, session = self._auth_enabled_without_session(path)
            if required and session is None:
                return
            if path == "/onboarding":
                if session is None:
                    self._redirect("/")
                    return
                editing = parse_qs(parsed.query).get("edit") == ["1"]
                conn = connect(db_path)
                try:
                    intents = profile_intents(conn, session.account_id) if editing else None
                    cv_library_ids = profile_cv_library_ids(conn, session.account_id)
                finally:
                    conn.close()
                initial_intents = (
                    [
                        {
                            "label": intent.label,
                            "keywords": intent.keywords,
                            "exclude": intent.exclude,
                        }
                        for intent in intents
                    ]
                    if intents is not None
                    else None
                )
                self._send_bytes(
                    200,
                    render_onboarding(
                        session.csrf_token,
                        initial_intents=initial_intents,
                        cv_library_ids=cv_library_ids,
                    ).encode("utf-8"),
                    "text/html; charset=utf-8",
                )
                return
            status = _DRAFT_STATUS_RE.match(path)
            if status:
                self._handle_draft_status(int(status.group(1)))
                return
            letter_file = _LETTER_FILE_RE.match(path)
            if letter_file:
                self._handle_letter_file(int(letter_file.group(1)), letter_file.group(2))
                return
            letter_page = _LETTER_PAGE_RE.match(path)
            if letter_page:
                self._handle_letter_page(
                    int(letter_page.group(1)), int(letter_page.group(2))
                )
                return
            document = _DOCUMENT_FILE_RE.match(path)
            if document:
                self._handle_document_file(int(document.group(1)))
                return
            if path == "/draft/batch/status":
                self._handle_batch_status()
                return
            swipe = path in ("/swipe", "/po/swipe")
            if (
                path in ("/", "/po", "/swipe", "/po/swipe")
                and session is not None
                and onboarding_enabled
            ):
                conn = connect(db_path)
                try:
                    needs_onboarding = not profile_complete(conn, session.account_id)
                finally:
                    conn.close()
                if needs_onboarding:
                    self._redirect("/onboarding")
                    return
            if session is not None and onboarding_enabled:
                track = "all"
            elif path in ("/", "/swipe"):
                track = "engineer"
            elif path in ("/po", "/po/swipe"):
                track = "project"
            else:
                self._send_text(404, "404 Not Found\n")
                return
            try:
                conn = connect(db_path)
                try:
                    render = render_swipe_page if swipe else render_page
                    page = render(
                        conn,
                        track,
                        draft_enabled=draft_config is not None,
                        csrf_token=session.csrf_token if session is not None else "",
                    )
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                self._send_text(500, f"erreur base de données : {exc}\n")
                return
            self._send_bytes(200, page.encode("utf-8"), "text/html; charset=utf-8")

        def _handle_batch_status(self) -> None:
            query = urlsplit(self.path).query
            track = "engineer"
            for part in query.split("&"):
                if part.startswith("track="):
                    track = part.removeprefix("track=")
            if track not in TRACKS:
                self._send_json(400, {"error": "champ track invalide"})
                return
            try:
                conn = connect(db_path)
                try:
                    counts = _batch_status(conn, track)
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                self._send_text(500, f"erreur base de données : {exc}\n")
                return
            self._send_json(200, counts)

        def _handle_batch_post(self) -> None:
            if draft_config is None:
                self._send_json(
                    503,
                    {"error": "génération non configurée : renseignez le bloc 'draft' de config.yaml"},
                )
                return
            body = self._read_json_body()
            fields = body if isinstance(body, dict) else {}
            cv_library_id = fields.get("cv_library_id")
            track = fields.get("track")
            if not isinstance(cv_library_id, int) or isinstance(cv_library_id, bool):
                self._send_json(400, {"error": "champ cv_library_id invalide"})
                return
            if track not in TRACKS:
                self._send_json(400, {"error": "champ track invalide"})
                return
            try:
                conn = connect(db_path)
                try:
                    match_ids = _batch_eligible_ids(conn, track)
                    job_ids = []
                    for match_id in match_ids:
                        cur = conn.execute(
                            "INSERT INTO draft_job (match_id, track, cv_library_id, status) "
                            "VALUES (?, ?, ?, 'queued')",
                            (match_id, track, cv_library_id),
                        )
                        job_ids.append(int(cur.lastrowid))
                    conn.commit()
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                self._send_text(500, f"erreur base de données : {exc}\n")
                return
            for job_id in job_ids:
                _spawn_draft_job(db_path, draft_config, job_id)
            self._send_json(202, {"count": len(job_ids)})

        def _latest_draft(self, conn: sqlite3.Connection, match_id: int) -> sqlite3.Row | None:
            return conn.execute(
                "SELECT * FROM draft_job WHERE match_id = ? ORDER BY id DESC LIMIT 1",
                (match_id,),
            ).fetchone()

        def _handle_draft_status(self, match_id: int) -> None:
            try:
                conn = connect(db_path)
                try:
                    job = self._latest_draft(conn, match_id)
                    entry = None
                    if job is not None and job["library_id"] is not None:
                        entry = conn.execute(
                            "SELECT id, label FROM document_library WHERE id = ?",
                            (job["library_id"],),
                        ).fetchone()
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                self._send_text(500, f"erreur base de données : {exc}\n")
                return
            if job is None:
                self._send_json(404, {"error": "aucune génération pour ce match"})
                return
            payload = {"status": str(job["status"]), "html": _draft_status_html(match_id, job)}
            if entry is not None:
                # Permet au client d'ajouter la lettre aux menus Candidater sans recharger.
                payload["library_id"] = int(entry["id"])
                payload["library_label"] = str(entry["label"])
            self._send_json(200, payload)

        def _handle_letter_file(self, match_id: int, extension: str) -> None:
            column = "pdf_path" if extension == "pdf" else "tex_path"
            try:
                conn = connect(db_path)
                try:
                    row = conn.execute(
                        f"SELECT {column} AS path FROM draft_job "
                        f"WHERE match_id = ? AND {column} IS NOT NULL "
                        "ORDER BY id DESC LIMIT 1",
                        (match_id,),
                    ).fetchone()
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                self._send_text(500, f"erreur base de données : {exc}\n")
                return
            content_type = (
                "application/pdf" if extension == "pdf" else "text/plain; charset=utf-8"
            )
            self._send_draft_file(row["path"] if row else None, content_type)

        def _handle_letter_page(self, match_id: int, page: int) -> None:
            try:
                conn = connect(db_path)
                try:
                    row = conn.execute(
                        "SELECT pdf_path, png_pages FROM draft_job "
                        "WHERE match_id = ? AND status = 'ok' ORDER BY id DESC LIMIT 1",
                        (match_id,),
                    ).fetchone()
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                self._send_text(500, f"erreur base de données : {exc}\n")
                return
            if row is None or not (1 <= page <= int(row["png_pages"] or 0)):
                self._send_text(404, "404 Not Found\n")
                return
            pdf_path = Path(str(row["pdf_path"]))
            png_path = pdf_path.parent / f"{pdf_path.stem}-{page}.png"
            self._send_draft_file(str(png_path), "image/png")

        def _handle_document_file(self, library_id: int) -> None:
            """Sert un document de la bibliothèque pour prévisualisation (œil des menus)."""
            try:
                conn = connect(db_path)
                try:
                    row = conn.execute(
                        "SELECT file_path FROM document_library WHERE id = ?",
                        (library_id,),
                    ).fetchone()
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                self._send_text(500, f"erreur base de données : {exc}\n")
                return
            if row is None:
                self._send_text(404, "404 Not Found\n")
                return
            suffix = Path(str(row["file_path"])).suffix.lower()
            content_type = _PREVIEW_CONTENT_TYPES.get(suffix, "text/plain; charset=utf-8")
            self._send_draft_file(str(row["file_path"]), content_type)

        def _send_draft_file(self, path: str | None, content_type: str) -> None:
            """Sert un fichier produit par un job de génération (chemin écrit par le serveur)."""
            if path is None:
                self._send_text(404, "404 Not Found\n")
                return
            try:
                data = Path(path).read_bytes()
            except OSError:
                self._send_text(404, "404 Not Found\n")
                return
            self._send_bytes(200, data, content_type)

        def _handle_draft_post(self, match_id: int) -> None:
            if draft_config is None:
                self._send_json(
                    503,
                    {"error": "génération non configurée : renseignez le bloc 'draft' de config.yaml"},
                )
                return
            body = self._read_json_body()
            fields = body if isinstance(body, dict) else {}
            cv_library_id = fields.get("cv_library_id")
            instruction = fields.get("instruction")
            track = fields.get("track")
            if not isinstance(cv_library_id, int) or isinstance(cv_library_id, bool):
                self._send_json(400, {"error": "champ cv_library_id invalide"})
                return
            if instruction is not None and not isinstance(instruction, str):
                self._send_json(400, {"error": "champ instruction invalide"})
                return
            if track not in TRACKS:
                self._send_json(400, {"error": "champ track invalide"})
                return
            try:
                conn = connect(db_path)
                try:
                    match_row = conn.execute(
                        "SELECT id FROM match WHERE id = ?", (match_id,)
                    ).fetchone()
                    if match_row is None:
                        self._send_text(404, "404 Not Found\n")
                        return
                    running = conn.execute(
                        "SELECT id FROM draft_job WHERE match_id = ? "
                        "AND status IN ('running', 'queued')",
                        (match_id,),
                    ).fetchone()
                    if running is not None:
                        self._send_json(409, {"error": "une génération est déjà en cours"})
                        return
                    cur = conn.execute(
                        "INSERT INTO draft_job "
                        "(match_id, track, cv_library_id, instruction, status) "
                        "VALUES (?, ?, ?, ?, 'queued')",
                        (match_id, track, cv_library_id, (instruction or "").strip() or None),
                    )
                    job_id = int(cur.lastrowid)
                    conn.commit()
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                self._send_text(500, f"erreur base de données : {exc}\n")
                return
            _spawn_draft_job(db_path, draft_config, job_id)
            self._send_json(202, {"ok": True, "job_id": job_id})

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if path == "/login":
                self._handle_login()
                return
            invite = re.fullmatch(r"/invite/([^/]+)", path)
            if invite:
                self._handle_invite(invite.group(1))
                return
            required, session = self._auth_enabled_without_session(path)
            if required and session is None:
                return
            if required and not csrf_valid(session, self.headers.get(CSRF_HEADER)):
                self._send_json(403, {"error": "jeton CSRF invalide"})
                return
            if path == "/onboarding/analyze":
                self._handle_onboarding_analyze()
                return
            if path == "/onboarding/complete":
                self._handle_onboarding_complete(session)
                return
            if path == "/logout":
                token = session_token(self.headers.get("Cookie"))
                if token:
                    conn = connect(db_path)
                    try:
                        delete_session(conn, token)
                    finally:
                        conn.close()
                self._redirect(
                    "/login", headers={"Set-Cookie": expired_session_cookie(secure=secure_cookie)}
                )
                return
            if path == _UPLOAD_PATH:
                self._handle_upload()
                return
            if path == "/draft/batch":
                self._handle_batch_post()
                return
            draft_post = _DRAFT_POST_RE.match(path)
            if draft_post:
                self._handle_draft_post(int(draft_post.group(1)))
                return
            match = _MATCH_ACTION_RE.match(path)
            if not match:
                self._send_text(404, "404 Not Found\n")
                return
            match_id = int(match.group(1))
            action = match.group(2)
            target_state: str | None = None
            cv_library_id: int | None = None
            cover_letter_library_id: int | None = None
            if action == "restore":
                body = self._read_json_body()
                target_state = body.get("state") if isinstance(body, dict) else None
                if target_state not in RESTORE_STATES:
                    self._send_json(400, {"error": "état de restauration invalide"})
                    return
            elif action == "apply":
                body = self._read_json_body()
                fields = body if isinstance(body, dict) else {}
                library_ids: list[int | None] = []
                for key in ("cv_library_id", "cover_letter_library_id"):
                    value = fields.get(key)
                    if value in (None, ""):
                        library_ids.append(None)
                    elif isinstance(value, int) and not isinstance(value, bool):
                        library_ids.append(value)
                    else:
                        self._send_json(400, {"error": f"champ {key} invalide"})
                        return
                cv_library_id, cover_letter_library_id = library_ids
            try:
                conn = connect(db_path)
                try:
                    match_row = conn.execute(
                        "SELECT state FROM match WHERE id = ?", (match_id,)
                    ).fetchone()
                    if match_row is None:
                        self._send_text(404, "404 Not Found\n")
                        return
                    if action == "apply":
                        if match_row["state"] == "discarded":
                            self._send_json(
                                409, {"error": "match écarté : restaurez-le d'abord"}
                            )
                            return
                        cv_path = (
                            resolve_path(conn, cv_library_id, "cv")
                            if cv_library_id is not None
                            else None
                        )
                        cover_letter_path = (
                            resolve_path(conn, cover_letter_library_id, "cover_letter")
                            if cover_letter_library_id is not None
                            else None
                        )
                        try:
                            record_application(
                                conn, match_id,
                                cv_path=cv_path, cover_letter_path=cover_letter_path,
                            )
                        except ApplicationError as exc:
                            self._send_json(409, {"error": str(exc)})
                            return
                    elif action == "later":
                        conn.execute(
                            "UPDATE match SET state = 'later', discarded_at = NULL WHERE id = ?",
                            (match_id,),
                        )
                    elif action == "discard":
                        conn.execute(
                            "UPDATE match SET state = 'discarded', "
                            "discarded_at = datetime('now') WHERE id = ?",
                            (match_id,),
                        )
                    else:
                        conn.execute(
                            "UPDATE match SET state = ?, discarded_at = NULL WHERE id = ?",
                            (target_state, match_id),
                        )
                    conn.commit()
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                self._send_text(500, f"erreur base de données : {exc}\n")
                return
            self._send_json(200, {"ok": True})

        def _handle_upload(self) -> None:
            body = self._read_json_body()
            fields = body if isinstance(body, dict) else {}
            filename = fields.get("filename")
            doc_type = fields.get("type")
            label = fields.get("label")
            content_base64 = fields.get("content_base64")
            if not isinstance(filename, str) or not isinstance(doc_type, str) or not isinstance(
                content_base64, str
            ):
                self._send_json(400, {"error": "champs requis manquants ou invalides"})
                return
            if label is not None and not isinstance(label, str):
                self._send_json(400, {"error": "champ label invalide"})
                return
            try:
                conn = connect(db_path)
                try:
                    entry = save_upload(conn, db_path, doc_type, label, filename, content_base64)
                except LibraryError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                self._send_text(500, f"erreur base de données : {exc}\n")
                return
            self._send_json(
                201, {"id": int(entry["id"]), "label": str(entry["label"]), "type": str(entry["type"])}
            )

        def _handle_onboarding_analyze(self) -> None:
            body = self._read_json_body()
            cv_library_ids = body.get("cv_library_ids") if isinstance(body, dict) else None
            if not isinstance(cv_library_ids, list) or not cv_library_ids or not all(
                isinstance(item, int) and not isinstance(item, bool) for item in cv_library_ids
            ):
                self._send_json(400, {"error": "CV invalide"})
                return
            conn = connect(db_path)
            try:
                try:
                    intents = analyze_cvs(
                        conn, onboarding_config or draft_config, cv_library_ids
                    )
                except OnboardingError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
            finally:
                conn.close()
            self._send_json(
                200,
                {
                    "intents": [
                        {
                            "label": intent.label,
                            "keywords": intent.keywords,
                            "exclude": intent.exclude,
                        }
                        for intent in intents
                    ]
                },
            )

        def _handle_onboarding_complete(self, session: Session | None) -> None:
            if session is None:
                self._send_json(401, {"error": "authentification requise"})
                return
            body = self._read_json_body()
            fields = body if isinstance(body, dict) else {}
            cv_library_ids = fields.get("cv_library_ids", [])
            if not isinstance(cv_library_ids, list) or not all(
                isinstance(item, int) and not isinstance(item, bool) for item in cv_library_ids
            ):
                self._send_json(400, {"error": "CV invalide"})
                return
            conn = connect(db_path)
            try:
                try:
                    intents = complete_profile(
                        conn,
                        session.account_id,
                        session.workspace_id,
                        cv_library_ids,
                        fields.get("intents"),
                    )
                except OnboardingError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
            finally:
                conn.close()
            self._send_json(200, {"ok": True, "count": len(intents)})

        def _handle_login(self) -> None:
            if not self._same_origin():
                self._send_text(403, "origine invalide\n")
                return
            fields = self._read_form_body()
            email = fields.get("email", "")
            password = fields.get("password", "")
            if workspace_slug is None:
                self._send_auth_page(
                    "Connexion", _login_form(email, "Instance nommée requise."), status=503
                )
                return
            conn = connect(db_path)
            try:
                if not auth_required(conn):
                    self._redirect("/")
                    return
                key = login_throttle_key(email, self.client_address[0])
                if not login_allowed(conn, key):
                    self._send_auth_page(
                        "Connexion",
                        _login_form(email, "Trop de tentatives. Réessayez dans 15 minutes."),
                        status=429,
                    )
                    return
                try:
                    token, _session = create_session(conn, email, password, workspace_slug)
                except AuthError:
                    record_login_failure(conn, key)
                    self._send_auth_page(
                        "Connexion", _login_form(email, "Email ou mot de passe incorrect."),
                        status=401,
                    )
                    return
                clear_login_failures(conn, key)
            finally:
                conn.close()
            self._redirect(
                "/", headers={"Set-Cookie": session_cookie(token, secure=secure_cookie)}
            )

        def _handle_invite(self, token: str) -> None:
            if not self._same_origin():
                self._send_text(403, "origine invalide\n")
                return
            fields = self._read_form_body()
            password = fields.get("password", "")
            confirmation = fields.get("password_confirmation", "")
            if workspace_slug is None:
                self._send_auth_page(
                    "Créer votre compte",
                    _invite_form(token, "Instance nommée requise."),
                    status=503,
                )
                return
            conn = connect(db_path)
            try:
                status = invite_status(conn, token, workspace_slug)
            finally:
                conn.close()
            if status == "accepted":
                self._redirect("/login")
                return
            if password != confirmation:
                self._send_auth_page(
                    "Créer votre compte",
                    _invite_form(token, "Les deux mots de passe sont différents."),
                    status=400,
                )
                return
            conn = connect(db_path)
            try:
                try:
                    account_id = accept_invite(
                        conn, token, password, workspace_slug=workspace_slug
                    )
                    account = conn.execute(
                        "SELECT email FROM account WHERE id = ?", (account_id,)
                    ).fetchone()
                    if account is None:
                        raise AuthError("instance nommée requise")
                    session_value, _session = create_session(
                        conn, str(account["email"]), password, workspace_slug
                    )
                except AuthError as exc:
                    self._send_auth_page(
                        "Créer votre compte", _invite_form(token, str(exc)), status=400
                    )
                    return
            finally:
                conn.close()
            self._redirect(
                "/", headers={"Set-Cookie": session_cookie(session_value, secure=secure_cookie)}
            )

        def _read_form_body(self) -> dict[str, str]:
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                return {}
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            return {key: values[-1] for key, values in parse_qs(raw).items() if values}

        def _same_origin(self) -> bool:
            fetch_site = self.headers.get("Sec-Fetch-Site")
            if fetch_site:
                return fetch_site == "same-origin"
            origin = self.headers.get("Origin")
            if not origin or origin == "null":
                return True
            parsed = urlsplit(origin)
            return parsed.netloc == self.headers.get("Host") and parsed.scheme in ("http", "https")

        def _read_json_body(self) -> object:
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                return None
            if length <= 0 or length > MAX_JSON_BODY_BYTES:
                return None
            raw = self.rfile.read(length)
            try:
                return json.loads(raw)
            except ValueError:
                return None

        def _send_json(self, status: int, payload: dict) -> None:
            self._send_bytes(status, json.dumps(payload).encode("utf-8"), "application/json")

        def _send_bytes(
            self,
            status: int,
            data: bytes,
            content_type: str,
            headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            for name, value in security_headers().items():
                self.send_header(name, value)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def _send_text(self, status: int, text: str) -> None:
            self._send_bytes(status, text.encode("utf-8"), "text/plain; charset=utf-8")

        def _send_auth_page(self, title: str, body: str, *, status: int = 200) -> None:
            page = _auth_page(title, body)
            self._send_bytes(status, page.encode("utf-8"), "text/html; charset=utf-8")

        def _redirect(self, location: str, *, headers: dict[str, str] | None = None) -> None:
            response_headers = {"Location": location, **(headers or {})}
            self._send_bytes(303, b"", "text/plain; charset=utf-8", response_headers)

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Handler


def url_of(host: str, port: int) -> str:
    if ":" in host:
        return f"http://[{host}]:{port}"
    return f"http://{host}:{port}"


def _fail_interrupted_draft_jobs(db_path: Path) -> None:
    """Marque en échec les jobs laissés en 'running'/'queued' par un arrêt du serveur."""
    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE draft_job SET status = 'failed', "
            "error = 'interrompu par un redémarrage du serveur', "
            "finished_at = datetime('now') "
            "WHERE status IN ('running', 'queued')"
        )
        conn.commit()
    finally:
        conn.close()


def serve_http(
    db_path: Path,
    host: str,
    port: int,
    draft_config: DraftConfig | None = None,
    *,
    workspace_slug: str | None = None,
    secure_cookie: bool = True,
    onboarding_enabled: bool = False,
) -> None:
    """Crée le serveur HTTP et le sert jusqu'à Ctrl-C."""
    _fail_interrupted_draft_jobs(db_path)
    try:
        server = ThreadingHTTPServer(
            (host, port),
            make_handler(
                db_path,
                draft_config,
                workspace_slug=workspace_slug,
                secure_cookie=secure_cookie,
                onboarding_config=draft_config,
                onboarding_enabled=onboarding_enabled,
            ),
        )
    except (OSError, OverflowError) as exc:
        raise ServeError(f"impossible d'écouter sur {host}:{port} : {exc}") from exc
    bound_port = int(server.server_address[1])
    click.echo(f"tableau de bord jobwatch : {url_of(host, bound_port)} (Ctrl-C pour arrêter)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        click.echo("arrêt du serveur")
    finally:
        server.server_close()


def _csrf_head(token: str) -> str:
    if not token:
        return ""
    escaped = html.escape(token, quote=True)
    return f"""<meta name="csrf-token" content="{escaped}">
<script>
(function () {{
  const originalFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {{
    const options = Object.assign({{}}, init || {{}});
    const method = String(options.method || 'GET').toUpperCase();
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {{
      const headers = new Headers(options.headers || {{}});
      headers.set('X-CSRF-Token', document.querySelector('meta[name="csrf-token"]').content);
      options.headers = headers;
    }}
    return originalFetch(input, options);
  }};
}})();
</script>"""


def _login_form(email: str = "", error: str = "") -> str:
    error_html = f'<p class="auth-error">{html.escape(error)}</p>' if error else ""
    return f"""{error_html}
<form method="post" action="/login">
  <label>Email<input type="email" name="email" autocomplete="username" required
    value="{html.escape(email, quote=True)}"></label>
  {_password_field("password", "Mot de passe", "current-password")}
  <button type="submit">Se connecter</button>
</form>"""


def _invite_form(token: str, error: str = "") -> str:
    error_html = f'<p class="auth-error">{html.escape(error)}</p>' if error else ""
    action = f"/invite/{html.escape(token, quote=True)}"
    return f"""{error_html}
<p class="auth-intro">Choisissez un mot de passe d'au moins 8 caractères.</p>
<form method="post" action="{action}">
  {_password_field("password", "Mot de passe", "new-password", minlength=8)}
  {_password_field("password_confirmation", "Confirmation", "new-password", minlength=8)}
  <button type="submit">Créer mon compte</button>
</form>"""


def _password_field(
    name: str, label: str, autocomplete: str, *, minlength: int | None = None
) -> str:
    minimum = f' minlength="{minlength}"' if minlength is not None else ""
    field_id = f"auth-{name}"
    return f"""<label>{html.escape(label)}<span class="password-field">
    <input id="{field_id}" type="password" name="{name}" autocomplete="{autocomplete}"
      {minimum} required>
    <button class="password-toggle" type="button" data-password-target="{field_id}"
      aria-label="Afficher le mot de passe" aria-pressed="false">
      <svg class="eye-show" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="1.8" aria-hidden="true"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"/>
        <circle cx="12" cy="12" r="2.5"/></svg>
      <svg class="eye-hide" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="1.8" aria-hidden="true"><path d="m3 3 18 18M10.6 6.2A10.8 10.8 0 0 1 12 6c6 0 9.5 6 9.5 6a16 16 0 0 1-2.3 3M6.3 6.3C3.9 8 2.5 12 2.5 12s3.5 6 9.5 6c1.7 0 3.2-.5 4.5-1.2"/></svg>
    </button></span></label>"""


def _auth_page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="fr" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#f3f1eb"><title>jobwatch · {html.escape(title)}</title>
<style>
:root {{ color-scheme:light; font-family:Inter,ui-sans-serif,system-ui,sans-serif; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; min-height:100vh; display:grid; place-items:center; padding:24px;
  color:#191b1f; background:radial-gradient(circle at 20% 0,#e5def7 0,transparent 35%),#f3f1eb; }}
.auth-card {{ width:min(100%,430px); padding:32px; border:1px solid rgba(29,31,35,.12);
  border-radius:24px; background:#fffefa; box-shadow:0 24px 70px rgba(52,46,34,.13); }}
.auth-brand {{ color:#42752d; font-size:.78rem; font-weight:800; letter-spacing:.16em;
  text-transform:uppercase; }}
h1 {{ margin:12px 0 24px; font-size:clamp(1.8rem,8vw,2.5rem); line-height:1.05; }}
form {{ display:grid; gap:18px; }}
label {{ display:grid; gap:8px; color:#686d76; font-size:.86rem; font-weight:650; }}
input {{ width:100%; padding:13px 14px; border:1px solid rgba(29,31,35,.17);
  border-radius:12px; color:#191b1f; background:#f8f6ef; font:inherit; }}
input:focus {{ outline:2px solid #42752d; outline-offset:2px; }}
.password-field {{ position:relative; display:block; }}
.password-field input {{ padding-right:48px; }}
button {{ margin-top:4px; padding:14px 18px; border:0; border-radius:12px;
  color:#fff; background:#42752d; font:inherit; font-weight:800; cursor:pointer; }}
.password-toggle {{ position:absolute; top:50%; right:5px; width:38px; height:38px;
  margin:0; padding:9px; transform:translateY(-50%); color:#686d76; background:transparent; }}
.password-toggle:hover {{ color:#191b1f; background:rgba(29,31,35,.06); }}
.password-toggle svg {{ width:20px; height:20px; }}
.password-toggle .eye-hide {{ display:none; }}
.password-toggle[aria-pressed="true"] .eye-show {{ display:none; }}
.password-toggle[aria-pressed="true"] .eye-hide {{ display:block; }}
.auth-intro {{ color:#686d76; line-height:1.5; }}
.auth-error {{ padding:12px 14px; border-radius:12px; color:#8f2940;
  background:rgba(182,60,84,.10); }}
.auth-link {{ display:inline-flex; padding:12px 16px; border-radius:12px; color:#fff;
  background:#42752d; font-weight:750; text-decoration:none; }}
</style></head><body><main class="auth-card"><div class="auth-brand">jobwatch</div>
<h1>{html.escape(title)}</h1>{body}</main><script>
document.querySelectorAll('[data-password-target]').forEach(button => {{
  button.addEventListener('click', () => {{
    const input = document.getElementById(button.dataset.passwordTarget);
    const visible = input.type === 'text';
    input.type = visible ? 'password' : 'text';
    button.setAttribute('aria-pressed', visible ? 'false' : 'true');
    button.setAttribute('aria-label', visible ? 'Afficher le mot de passe' : 'Masquer le mot de passe');
  }});
}});
</script></body></html>"""


_CSS = """\
:root {
  color-scheme:dark;
  --bg:#090b10; --bg-deep:#06070a; --surface:#11141c; --surface-2:#171b25;
  --surface-hover:#1c2130; --fg:#f3f5f8; --muted:#9198a8; --muted-2:#6e7585;
  --line:rgba(255,255,255,.085); --line-strong:rgba(255,255,255,.16);
  --accent:#b9f46f; --accent-ink:#17210d; --accent-soft:rgba(185,244,111,.12);
  --violet:#ab91ff; --violet-soft:rgba(171,145,255,.12);
  --amber:#ffbe63; --amber-soft:rgba(255,190,99,.12);
  --blue:#72b7ff; --blue-soft:rgba(114,183,255,.13);
  --danger:#ff879b; --danger-soft:rgba(255,135,155,.12);
  --shadow:0 22px 60px rgba(0,0,0,.30);
  --card-shadow:0 8px 28px rgba(0,0,0,.16);
  --radius-xl:24px; --radius-lg:18px; --radius-md:14px;
}
html[data-theme="light"] {
  color-scheme:light;
  --bg:#f3f1eb; --bg-deep:#ebe7de; --surface:#fffefa; --surface-2:#f8f6ef;
  --surface-hover:#f4f1e8; --fg:#191b1f; --muted:#686d76; --muted-2:#858990;
  --line:rgba(29,31,35,.095); --line-strong:rgba(29,31,35,.17);
  --accent:#42752d; --accent-ink:#fff; --accent-soft:rgba(66,117,45,.10);
  --violet:#7052c8; --violet-soft:rgba(112,82,200,.09);
  --amber:#9a5d08; --amber-soft:rgba(154,93,8,.09);
  --blue:#1269ad; --blue-soft:rgba(18,105,173,.09);
  --danger:#b63c54; --danger-soft:rgba(182,60,84,.10);
  --shadow:0 22px 55px rgba(52,46,34,.12);
  --card-shadow:0 7px 24px rgba(52,46,34,.07);
}
* { box-sizing:border-box }
html { min-height:100%; background:var(--bg); scroll-behavior:smooth }
body { min-height:100vh; margin:0; background:var(--bg); color:var(--fg);
  font:15px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif;
  -webkit-font-smoothing:antialiased; -webkit-tap-highlight-color:transparent }
button, input { font:inherit }
button, summary, a { -webkit-tap-highlight-color:transparent }
.ambient { position:fixed; inset:0; overflow:hidden; pointer-events:none; z-index:0 }
.ambient::before { content:""; position:absolute; width:480px; height:480px;
  top:-250px; left:50%; transform:translateX(-50%); border-radius:50%;
  background:radial-gradient(circle, rgba(171,145,255,.22), transparent 67%);
  filter:blur(12px) }
.ambient::after { content:""; position:absolute; width:320px; height:320px;
  top:260px; right:-240px; border-radius:50%;
  background:radial-gradient(circle, rgba(185,244,111,.11), transparent 70%) }
html[data-theme="light"] .ambient::before { background:radial-gradient(circle, rgba(112,82,200,.13), transparent 67%) }
html[data-theme="light"] .ambient::after { background:radial-gradient(circle, rgba(66,117,45,.10), transparent 70%) }
.shell { position:relative; z-index:1; width:min(100%, 760px); margin:0 auto;
  padding:max(18px, env(safe-area-inset-top)) calc(16px + env(safe-area-inset-right))
  calc(40px + env(safe-area-inset-bottom)) calc(16px + env(safe-area-inset-left)) }
.topbar { min-height:48px; display:flex; align-items:center; justify-content:space-between;
  gap:16px; margin-bottom:30px }
.identity { display:flex; align-items:center; gap:11px; min-width:0 }
.monogram { width:38px; height:38px; display:grid; place-items:center; border-radius:12px;
  color:var(--accent-ink); background:var(--accent); font-size:.9rem; font-weight:850;
  letter-spacing:-.04em; box-shadow:0 0 0 5px var(--accent-soft) }
.identity-copy { display:flex; flex-direction:column; line-height:1.18 }
.identity-name { font-weight:750; letter-spacing:-.015em }
.identity-sub { margin-top:3px; color:var(--muted); font-size:.73rem; letter-spacing:.02em }
.theme-toggle { position:relative; width:48px; height:48px; flex:0 0 48px; border:1px solid var(--line);
  border-radius:15px; color:var(--fg); background:color-mix(in srgb, var(--surface) 86%, transparent);
  box-shadow:var(--card-shadow); cursor:pointer; transition:transform .2s ease, background .2s ease,
  border-color .2s ease }
.theme-toggle:active { transform:scale(.94) }
.theme-toggle svg { position:absolute; inset:0; margin:auto; width:20px; height:20px;
  transition:opacity .2s ease, transform .35s cubic-bezier(.2,.8,.2,1) }
.icon-sun { opacity:0; transform:rotate(-70deg) scale(.6) }
.icon-moon { opacity:1; transform:rotate(0) scale(1) }
html[data-theme="light"] .icon-sun { opacity:1; transform:rotate(0) scale(1) }
html[data-theme="light"] .icon-moon { opacity:0; transform:rotate(70deg) scale(.6) }
.theme-toggle:focus-visible, #q:focus-visible, .clear-search:focus-visible,
summary:focus-visible, a:focus-visible { outline:3px solid var(--violet); outline-offset:3px }
.card-toggle:focus-visible { outline:3px solid var(--violet); outline-offset:-4px }
.hero { margin-bottom:22px }
.eyebrow { margin:0 0 10px; color:var(--accent); font-size:.69rem; font-weight:800;
  letter-spacing:.16em; text-transform:uppercase }
h1 { max-width:560px; margin:0; font-size:clamp(2.25rem, 10vw, 4.6rem); line-height:.98;
  font-weight:810; letter-spacing:-.065em }
h1 span { color:var(--muted-2); font-weight:620 }
.hero-meta { display:flex; align-items:center; flex-wrap:wrap; gap:8px; margin:18px 0 0;
  color:var(--muted); font-size:.78rem }
.live-dot { width:7px; height:7px; border-radius:50%; background:var(--accent);
  box-shadow:0 0 0 5px var(--accent-soft) }
.track-tabs { display:grid; grid-template-columns:repeat(2, 1fr); gap:6px; margin:20px 0 0;
  padding:5px; border:1px solid var(--line); border-radius:var(--radius-md);
  background:var(--surface) }
.track-tab { display:grid; place-items:center; min-height:40px; padding:0 10px;
  border-radius:calc(var(--radius-md) - 5px); color:var(--muted); font-size:.73rem;
  font-weight:790; letter-spacing:.05em; text-transform:uppercase; text-decoration:none;
  transition:color .18s ease, background .18s ease }
.track-tab.active { color:var(--accent-ink); background:var(--accent);
  box-shadow:0 0 0 4px var(--accent-soft) }
.manage-link { display:inline-flex; margin-top:18px; color:var(--accent); font-size:.76rem;
  font-weight:780; text-decoration:none }
.stats { display:grid; grid-template-columns:repeat(3, 1fr); gap:8px; margin:24px 0 22px }
.stat { min-width:0; padding:14px 13px 13px; border:1px solid var(--line); border-radius:var(--radius-md);
  background:linear-gradient(145deg, var(--surface-2), var(--surface)); box-shadow:var(--card-shadow) }
.stat-value { display:block; font-size:1.55rem; line-height:1; font-weight:790; letter-spacing:-.05em }
.stat-label { display:block; margin-top:8px; color:var(--muted); font-size:.67rem; line-height:1.25;
  letter-spacing:.045em; text-transform:uppercase }
.stat-new .stat-value { color:var(--accent) }
.stat-seen .stat-value { color:var(--blue) }
.stat-applied .stat-value { color:var(--violet) }
.search-dock { position:sticky; top:calc(env(safe-area-inset-top) + 8px); z-index:20;
  margin:0 -6px 26px; padding:6px; border:1px solid transparent; border-radius:20px;
  transition:background .2s ease, border-color .2s ease, box-shadow .2s ease }
.search-dock.stuck { border-color:var(--line); background:color-mix(in srgb, var(--bg) 84%, transparent);
  box-shadow:var(--shadow); -webkit-backdrop-filter:blur(18px); backdrop-filter:blur(18px) }
.search-box { position:relative; display:flex; align-items:center; min-height:54px;
  border:1px solid var(--line-strong); border-radius:16px; background:var(--surface);
  box-shadow:var(--card-shadow); overflow:hidden; transition:border-color .2s ease, box-shadow .2s ease }
.search-box:focus-within { border-color:color-mix(in srgb, var(--violet) 70%, transparent);
  box-shadow:0 0 0 4px var(--violet-soft), var(--card-shadow) }
.search-icon { width:21px; height:21px; flex:none; margin-left:16px; color:var(--muted) }
#q { width:100%; height:52px; min-width:0; padding:0 8px 0 11px; border:0; outline:0;
  color:var(--fg); background:transparent; font-size:16px; -webkit-appearance:none; appearance:none }
#q::placeholder { color:var(--muted-2); opacity:1 }
#q::-webkit-search-cancel-button { display:none }
.clear-search { display:none; width:44px; height:44px; flex:0 0 44px; margin-right:4px;
  border:0; border-radius:12px; color:var(--muted); background:transparent; cursor:pointer }
.clear-search.visible { display:grid; place-items:center }
.clear-search svg { width:18px; height:18px }
.search-status { min-height:18px; margin:7px 11px 0; color:var(--muted); font-size:.72rem }
.section { margin:0 0 14px; border:1px solid var(--line); border-radius:var(--radius-xl);
  background:color-mix(in srgb, var(--surface) 82%, transparent); box-shadow:var(--card-shadow);
  overflow:hidden }
.section > summary { min-height:72px; display:flex; align-items:center; justify-content:space-between;
  gap:14px; padding:12px 16px; list-style:none; cursor:pointer; user-select:none }
.section > summary::-webkit-details-marker { display:none }
.summary-copy { min-width:0; display:flex; align-items:center; gap:12px }
.section-dot { width:10px; height:10px; flex:0 0 10px; border-radius:50%;
  background:var(--muted-2); box-shadow:0 0 0 5px rgba(145,152,168,.09) }
.section-new .section-dot { background:var(--accent); box-shadow:0 0 0 5px var(--accent-soft) }
.section-priority .section-dot { background:var(--amber); box-shadow:0 0 0 5px var(--amber-soft) }
.section-seen .section-dot { background:var(--blue); box-shadow:0 0 0 5px var(--blue-soft) }
.section-later .section-dot { background:var(--amber); box-shadow:0 0 0 5px var(--amber-soft) }
.section-discarded .section-dot { background:var(--danger); box-shadow:0 0 0 5px var(--danger-soft) }
.section-applied .section-dot { background:var(--violet); box-shadow:0 0 0 5px var(--violet-soft) }
.section-title, .section-subtitle { display:block }
.section-title { overflow:hidden; color:var(--fg); font-size:.94rem; font-weight:740;
  letter-spacing:-.015em; text-overflow:ellipsis; white-space:nowrap }
.section-subtitle { margin-top:2px; color:var(--muted); font-size:.7rem }
.summary-tail { display:flex; align-items:center; gap:11px; flex:none }
.count { min-width:30px; height:30px; padding:0 8px; display:inline-grid; place-items:center;
  border:1px solid var(--line); border-radius:10px; color:var(--muted); background:var(--surface-2);
  font-size:.71rem; font-weight:750; font-variant-numeric:tabular-nums }
.chevron { width:9px; height:9px; border-right:2px solid var(--muted); border-bottom:2px solid var(--muted);
  transform:rotate(45deg) translate(-2px,-2px); transition:transform .25s ease }
.section:not([open]) .chevron { transform:rotate(-45deg) translate(-1px,-1px) }
.card-list { display:grid; gap:8px; padding:0 8px 8px }
.row { position:relative; min-height:88px; padding:15px 14px 14px 17px; border:1px solid var(--line);
  border-radius:var(--radius-lg); background:var(--surface-2); overflow:hidden;
  box-shadow:0 1px 0 rgba(255,255,255,.025) inset; transition:transform .18s ease,
  border-color .18s ease, background .18s ease, opacity .22s ease }
.row::before { content:""; position:absolute; inset:13px auto 13px 0; width:3px; border-radius:0 4px 4px 0;
  background:var(--muted-2) }
.row.row-new::before { background:var(--accent) }
.row.row-seen::before { background:var(--blue) }
.row.row-later::before { background:var(--amber) }
.row.row-discarded::before { background:var(--danger) }
.row.row-applied::before { background:var(--violet) }
.row.row-removing { opacity:0; transform:translateX(18px) }
.row .body { position:relative; z-index:2; min-width:0 }
.card-topline { display:flex; align-items:flex-start; justify-content:space-between; gap:10px }
.card-badges { display:flex; align-items:center; gap:10px }
.summary-chevron { width:9px; height:9px; margin:0 4px 4px 0; border-right:2px solid var(--muted);
  border-bottom:2px solid var(--muted); transform:rotate(45deg); transition:transform .2s ease }
.company { min-width:0; overflow-wrap:anywhere; color:var(--fg); font-size:.74rem; line-height:1.35;
  font-weight:810; letter-spacing:.065em; text-transform:uppercase }
.role { max-width:620px; margin-top:5px; overflow-wrap:anywhere; color:var(--fg); font-size:.98rem;
  line-height:1.34; font-weight:590; letter-spacing:-.018em }
.pill { flex:none; min-height:22px; display:inline-flex; align-items:center; padding:2px 8px;
  border:1px solid var(--line); border-radius:999px; font-size:.6rem; line-height:1;
  font-weight:800; letter-spacing:.07em; text-transform:uppercase }
.pill.applied { color:var(--blue); border-color:color-mix(in srgb, var(--blue) 38%, transparent);
  background:var(--blue-soft) }
.pill.follow_up { color:var(--amber); border-color:color-mix(in srgb, var(--amber) 38%, transparent);
  background:var(--amber-soft) }
.pill.interview { color:var(--violet); border-color:color-mix(in srgb, var(--violet) 35%, transparent);
  background:var(--violet-soft) }
.pill.offer { color:var(--accent); border-color:color-mix(in srgb, var(--accent) 38%, transparent);
  background:var(--accent-soft) }
.pill.rejected { color:var(--danger); border-color:color-mix(in srgb, var(--danger) 38%, transparent);
  background:var(--danger-soft) }
.pill.unknown { color:var(--muted); background:var(--surface) }
.pill.fit.high { color:var(--accent); border-color:color-mix(in srgb, var(--accent) 38%, transparent);
  background:var(--accent-soft) }
.pill.fit.medium { color:var(--amber); border-color:color-mix(in srgb, var(--amber) 38%, transparent);
  background:var(--amber-soft) }
.pill.fit.low { color:var(--muted); border-color:var(--line); background:var(--surface) }
.meta { display:flex; align-items:center; flex-wrap:wrap; gap:2px 6px; margin-top:11px;
  color:var(--muted); font-size:.72rem; line-height:1.4; overflow-wrap:anywhere }
.platform { min-height:24px; display:inline-flex; align-items:center; padding:2px 8px; border-radius:999px;
  color:var(--blue); background:var(--blue-soft); font-size:.64rem; font-weight:760; letter-spacing:.015em }
.meta a { position:relative; z-index:3; min-height:44px; display:inline-flex; align-items:center;
  padding:0 10px; border:1px solid var(--line); pointer-events:auto;
  border-radius:11px; color:var(--fg); background:var(--surface); font-size:.69rem; font-weight:680;
  text-decoration:none; transition:border-color .15s ease, background .15s ease }
.note { margin:12px 0 0; padding:11px 12px; border:1px dashed var(--line-strong);
  border-radius:12px; color:var(--muted); background:color-mix(in srgb, var(--surface) 55%, transparent);
  font-size:.75rem; overflow-wrap:anywhere }
.card-reader { position:relative; z-index:3; margin:13px 13px 0; pointer-events:auto }
.reader-tabs { display:flex; gap:5px;
  padding:4px; border:1px solid var(--line); border-radius:12px; background:var(--surface) }
.reader-tab { flex:1 1 0; min-width:0; min-height:36px; padding:0 8px; overflow:hidden; border:0;
  border-radius:8px; color:var(--muted); background:transparent; font-size:.68rem; font-weight:740;
  letter-spacing:.01em; text-overflow:ellipsis; white-space:nowrap; cursor:pointer;
  transition:color .15s ease, background .15s ease, box-shadow .15s ease }
.reader-tab[aria-expanded="true"] { color:var(--fg); background:var(--surface-hover);
  box-shadow:0 1px 4px rgba(0,0,0,.09) }
.reader-tab.letter-ok { color:var(--violet) }
.reader-tab.letter-failed { color:var(--danger) }
.reader-tab.letter-running, .reader-tab.letter-queued { color:var(--violet) }
.reader-tab:focus-visible { outline:3px solid var(--violet); outline-offset:2px }
.summary-panel { position:relative; z-index:2; margin:10px 0 1px; padding:13px 0 12px;
  border-top:1px solid var(--line) }
.summary-panel[hidden] { display:none }
.summary-title { color:var(--accent); font-size:.68rem; font-weight:820; letter-spacing:.09em;
  text-transform:uppercase }
.summary-panel ul { margin:9px 0 0; padding-left:19px; color:var(--muted); font-size:.76rem }
.summary-panel li + li { margin-top:6px }
.summary-fields { display:grid; gap:5px; margin:10px 0 2px }
.summary-field { display:flex; gap:8px; align-items:baseline; font-size:.76rem }
.sf-label { flex:none; min-width:132px; color:var(--muted-2); font-size:.63rem;
  font-weight:800; letter-spacing:.07em; text-transform:uppercase }
.sf-value { color:var(--fg); overflow-wrap:anywhere }
.summary-field.sf-empty .sf-value { color:var(--muted-2); font-style:italic }
@media (max-width:370px) { .sf-label { min-width:104px } }
.content-toggle { position:relative; z-index:3; margin:12px 13px 0; padding:9px 12px;
  display:inline-flex; align-items:center; gap:7px; border:1px solid var(--line);
  border-radius:11px; color:var(--fg); background:var(--surface); font-size:.71rem;
  font-weight:700; letter-spacing:.02em; cursor:pointer; pointer-events:auto;
  transition:border-color .15s ease, background .15s ease }
.content-toggle .summary-chevron { margin:0 }
.content-toggle[aria-expanded="true"] .summary-chevron { transform:rotate(225deg) }
.content-panel { position:relative; z-index:2; margin:10px 0 1px; padding:12px 0;
  border-top:1px solid var(--line); color:var(--muted); font-size:.76rem;
  line-height:1.55; overflow-wrap:anywhere }
.content-panel[hidden] { display:none }
.content-panel p { margin:0 0 10px }
.content-panel p:last-child { margin-bottom:0 }
.card-actions { position:relative; z-index:3; display:flex; flex-wrap:wrap; gap:8px;
  margin:12px 13px 0; pointer-events:auto }
.card-action { min-height:38px; padding:0 14px; display:inline-flex; align-items:center;
  border:1px solid var(--line); border-radius:11px; background:var(--surface); color:var(--fg);
  font-size:.71rem; font-weight:700; letter-spacing:.02em; cursor:pointer;
  transition:border-color .15s ease, background .15s ease }
.action-later { color:var(--amber) }
.action-discard { color:var(--danger) }
.action-apply { color:var(--accent) }
.card-action:focus-visible { outline:3px solid var(--violet); outline-offset:2px }
@media (hover:hover) {
  .card-action:hover { border-color:var(--line-strong); background:var(--surface-hover) }
}
.apply-form { position:relative; z-index:3; display:grid; grid-template-columns:minmax(0, 1fr);
  gap:8px; margin:12px 13px 0; pointer-events:auto }
.apply-form[hidden] { display:none }
.apply-input { min-height:44px; padding:0 12px; border:1px solid var(--line-strong);
  border-radius:11px; color:var(--fg); background:var(--surface); font-size:16px }
.apply-input::placeholder { color:var(--muted-2); opacity:1 }
.apply-input:focus-visible { outline:3px solid var(--violet); outline-offset:2px }
.apply-submit { justify-self:start }
.doc-field { display:grid; grid-template-columns:minmax(0, 1fr); gap:6px; min-width:0;
  padding:8px; border:1px dashed var(--line-strong);
  border-radius:11px; transition:border-color .15s ease, background .15s ease }
.doc-field.doc-dragover { border-color:var(--accent); background:var(--accent-soft) }
.doc-label { color:var(--muted); font-size:.68rem; font-weight:700; letter-spacing:.03em;
  text-transform:uppercase }
.doc-row { display:flex; gap:8px; min-width:0 }
.doc-select { flex:1; min-width:0 }
.doc-icon-btn { flex:0 0 44px; width:44px; min-height:44px; display:grid; place-items:center;
  padding:0; border:1px solid var(--line); border-radius:11px; color:var(--fg);
  background:var(--surface); cursor:pointer;
  transition:border-color .15s ease, background .15s ease, opacity .15s ease }
.doc-icon-btn svg { width:19px; height:19px }
.doc-icon-btn:disabled { opacity:.4; cursor:default }
.doc-icon-btn:focus-visible { outline:3px solid var(--violet); outline-offset:2px }
@media (hover:hover) {
  .doc-icon-btn:not(:disabled):hover { border-color:var(--line-strong); background:var(--surface-hover) }
}
.doc-label-prompt { display:flex; gap:8px }
.doc-label-prompt[hidden] { display:none }
.action-draft { color:var(--violet) }
.draft-form { position:relative; z-index:3; display:grid; grid-template-columns:minmax(0, 1fr);
  gap:8px; margin:10px 0 0; pointer-events:auto }
.draft-form[hidden] { display:none }
.draft-submit { justify-self:start }
.draft-area { position:relative; z-index:3; display:flex; align-items:center; flex-wrap:wrap;
  gap:9px; pointer-events:auto; color:var(--muted); font-size:.75rem }
.draft-area:not(:empty) { margin-bottom:10px }
.letter-panel { margin:10px 0 1px; padding:12px 0 2px; border-top:1px solid var(--line) }
.letter-panel[hidden] { display:none }
.letter-panel > .action-draft { margin-top:2px }
/* avancement des lettres : ancré dans la barre du haut, jamais posé sur le
   contenu - un panneau flottant paraissait perdu à droite sur grand écran et
   mordait la carte dès que la fenêtre rétrécissait */
.batch-badge-wrap { position:relative; display:flex }
.batch-badge-wrap[hidden] { display:none }
.batch-badge { display:flex; align-items:center; gap:7px; height:38px; padding:0 12px 0 9px;
  border:1px solid var(--line-strong); border-radius:999px; background:var(--surface);
  color:var(--fg); font-size:.8rem; font-weight:650; cursor:pointer;
  transition:background .15s ease }
.batch-badge:hover { background:var(--surface-hover) }
.batch-ring { width:19px; height:19px; flex:none; border-radius:50%;
  background:conic-gradient(var(--violet) calc(var(--batch-progress, 0) * 1%),
    var(--line-strong) 0) }
/* l'anneau reste gris tant qu'aucune lettre n'est finie : la pulsation dit
   que le lot tourne, là où un pourcentage à 0 paraîtrait figé */
.batch-ring:not(.batch-ring-done) { animation:batch-pulse 1.7s ease-in-out infinite }
@keyframes batch-pulse { 50% { opacity:.45 } }
.batch-ring::after { content:""; display:block; width:11px; height:11px; margin:4px;
  border-radius:50%; background:var(--surface) }
.batch-badge:hover .batch-ring::after { background:var(--surface-hover) }
.batch-ring-done { background:var(--accent) }
.batch-panel { position:absolute; z-index:30; right:0; top:calc(100% + 9px);
  width:max-content; max-width:min(250px, calc(100vw - 32px)); padding:13px 15px;
  border:1px solid var(--line); border-radius:var(--radius-md);
  background:var(--surface-2); box-shadow:var(--shadow); text-align:left }
.batch-panel[hidden] { display:none }
.batch-panel p { margin:0; font-size:.82rem; line-height:1.4 }
.batch-panel-note { margin-top:3px; color:var(--muted) }
.draft-spinner { width:14px; height:14px; flex:none; border:2px solid var(--line-strong);
  border-top-color:var(--violet); border-radius:50%; animation:draft-spin .8s linear infinite }
@keyframes draft-spin { to { transform:rotate(360deg) } }
.draft-error { margin:0; color:var(--danger); overflow-wrap:anywhere }
.draft-error a, .letter-links a { position:relative; z-index:3; pointer-events:auto }
.draft-warning { margin:0 0 8px; color:var(--amber); overflow-wrap:anywhere }
.letter-page { display:block; width:100%; max-width:560px; margin:0 auto 10px;
  border:1px solid var(--line-strong); border-radius:10px; background:#fff }
.letter-links { margin:4px 0 8px; text-align:center; font-size:.72rem }
.swipe-fab { height:48px; flex:none; display:flex; align-items:center; justify-content:center;
  gap:8px; padding:0 13px; border:1px solid var(--line); border-radius:15px; color:var(--violet);
  background:color-mix(in srgb, var(--surface) 86%, transparent); box-shadow:var(--card-shadow);
  font-weight:760; text-decoration:none;
  transition:transform .2s ease, background .2s ease, border-color .2s ease }
.swipe-fab svg { width:20px; height:20px }
.swipe-fab:active { transform:scale(.94) }
.swipe-fab-count { min-width:20px; height:20px;
  display:grid; place-items:center; padding:0 5px; border-radius:999px;
  color:var(--accent-ink); background:var(--accent); font-size:.62rem; font-weight:820;
  font-variant-numeric:tabular-nums; }
@media (hover:hover) { .swipe-fab:hover { background:var(--surface-hover) } }
.topbar-tools { display:flex; align-items:center; gap:10px }
.logout-button { height:48px; display:flex; align-items:center; justify-content:center; gap:8px;
  padding:0 13px; border:1px solid var(--line); border-radius:15px; color:var(--fg);
  background:color-mix(in srgb, var(--surface) 86%, transparent); box-shadow:var(--card-shadow);
  font-weight:700; cursor:pointer; }
.logout-button svg { width:19px; height:19px; }
.swipe-popup { position:fixed; inset:0; z-index:60; display:flex; align-items:flex-end;
  justify-content:center; padding:16px; background:rgba(0,0,0,.45);
  -webkit-backdrop-filter:blur(6px); backdrop-filter:blur(6px);
  opacity:0; transition:opacity .25s ease }
.swipe-popup[hidden] { display:none }
.swipe-popup.visible { opacity:1 }
.swipe-popup-card { width:min(100%, 420px);
  margin-bottom:max(8px, env(safe-area-inset-bottom));
  padding:26px 22px 20px; border:1px solid var(--line-strong);
  border-radius:var(--radius-xl); background:var(--surface); box-shadow:var(--shadow);
  text-align:center; transform:translateY(24px); transition:transform .28s cubic-bezier(.2,.8,.2,1) }
.swipe-popup.visible .swipe-popup-card { transform:translateY(0) }
.swipe-popup-icon { width:54px; height:54px; margin:0 auto 12px; display:grid;
  place-items:center; border-radius:17px; color:var(--violet); background:var(--violet-soft);
  box-shadow:0 0 0 6px color-mix(in srgb, var(--violet) 6%, transparent) }
.swipe-popup-icon svg { width:26px; height:26px }
.swipe-popup-card h2 { margin:0 0 4px; font-size:1.3rem; font-weight:790; letter-spacing:-.03em }
.swipe-popup-card p { margin:0 0 18px; color:var(--muted); font-size:.86rem }
.swipe-popup-actions { display:grid; gap:8px }
.swipe-popup-go { display:flex; align-items:center; justify-content:center; min-height:50px;
  border-radius:14px; color:var(--accent-ink); background:var(--accent); font-size:.9rem;
  font-weight:790; text-decoration:none; box-shadow:0 0 0 5px var(--accent-soft) }
.swipe-popup-later { min-height:44px; border:0; border-radius:12px; color:var(--muted);
  background:transparent; font-size:.8rem; font-weight:700; cursor:pointer }
.swipe-popup-later:focus-visible, .swipe-popup-go:focus-visible {
  outline:3px solid var(--violet); outline-offset:2px }
@media (min-width:620px) { .swipe-popup { align-items:center } }
.undo-toast { display:flex; align-items:center; justify-content:space-between; gap:12px;
  color:var(--muted); font-size:.78rem }
.undo-toast .undo-btn { flex:none; min-height:38px; padding:0 14px; border:1px solid var(--line);
  border-radius:11px; color:var(--fg); background:var(--surface); font-size:.72rem; font-weight:700;
  cursor:pointer; transition:border-color .15s ease, background .15s ease }
.undo-toast .undo-btn:focus-visible { outline:3px solid var(--violet); outline-offset:2px }
@media (hover:hover) {
  .undo-toast .undo-btn:hover { border-color:var(--line-strong); background:var(--surface-hover) }
}
.empty-note { margin:0; padding:14px 13px; border:1px dashed var(--line-strong);
  border-radius:13px; color:var(--muted); background:var(--surface-2);
  font-size:.75rem; overflow-wrap:anywhere }
.no-results { margin:0 0 14px; padding:26px 18px; border:1px dashed var(--line-strong);
  border-radius:var(--radius-lg); color:var(--muted); text-align:center }
.no-results strong { display:block; margin-bottom:3px; color:var(--fg) }
.footer { margin-top:24px; color:var(--muted-2); font-size:.68rem; text-align:center }
a { color:var(--blue) }
@media (hover:hover) {
  .theme-toggle:hover, .clear-search:hover { background:var(--surface-hover) }
  .row:hover { transform:translateY(-1px); border-color:var(--line-strong); background:var(--surface-hover) }
  .meta a:hover { border-color:var(--line-strong); background:var(--surface-hover) }
}
@media (min-width:620px) {
  .shell { padding-left:24px; padding-right:24px }
  .topbar { margin-bottom:42px }
  .stats { gap:12px }
  .stat { padding:18px }
  .section > summary { padding-left:20px; padding-right:20px }
  .card-list { padding:0 10px 10px; gap:10px }
  .row { padding:18px 18px 17px 21px }
}
@media (max-width:619px) {
  .topbar { flex-wrap:wrap; }
  .topbar-tools { width:100%; }
  .swipe-fab { flex:1; }
  .logout-button { margin-left:auto; }
}
@media (max-width:370px) {
  .stat { padding:12px 9px }
  .stat-value { font-size:1.35rem }
  .stat-label { font-size:.59rem }
  .section-subtitle { display:none }
}
@media (prefers-reduced-motion:reduce) {
  *, *::before, *::after { scroll-behavior:auto !important; animation:none !important; transition:none !important }
}
"""

_BATCH_BADGE_JS = """\
(function () {
  const wrap = document.getElementById('batch-badge-wrap');
  if (!wrap) return;
  const badge = document.getElementById('batch-badge');
  const ring = document.getElementById('batch-ring');
  const count = document.getElementById('batch-badge-count');
  const panel = document.getElementById('batch-panel');
  const line1 = document.getElementById('batch-panel-line1');
  const line2 = document.getElementById('batch-panel-line2');
  const track = document.body.dataset.track || 'engineer';
  let timer = null;

  const stop = () => { if (timer) { clearInterval(timer); timer = null; } };
  const poll = () => fetch(`/draft/batch/status?track=${track}`)
    .then(resp => resp.ok ? resp.json() : null)
    .then(payload => {
      if (!payload) return;
      const active = payload.queued + payload.running;
      const done = payload.ok + payload.failed;
      const failed = payload.failed ? ` · ${payload.failed} échec(s)` : '';
      if (active > 0) {
        wrap.hidden = false;
        ring.classList.remove('batch-ring-done');
        ring.style.setProperty('--batch-progress', Math.round(100 * done / (done + active)));
        count.textContent = String(active);
        line1.textContent = `${active} lettre(s) en cours`;
        line2.textContent = `${payload.ok} prête(s)${failed}`;
        if (!timer) timer = setInterval(poll, 3000);
        return;
      }
      stop();
      if (wrap.hidden) return;          // rien n'a tourné pendant cette visite
      ring.classList.add('batch-ring-done');
      ring.style.setProperty('--batch-progress', 100);
      count.textContent = String(payload.ok);
      line1.textContent = `Terminé · ${payload.ok} lettre(s) prête(s)`;
      line2.textContent = `à joindre depuis les cartes${failed}`;
    })
    .catch(() => {});

  badge.addEventListener('click', () => {
    const open = badge.getAttribute('aria-expanded') === 'true';
    badge.setAttribute('aria-expanded', open ? 'false' : 'true');
    panel.hidden = open;
  });
  document.addEventListener('click', event => {
    if (panel.hidden || wrap.contains(event.target)) return;
    badge.setAttribute('aria-expanded', 'false');
    panel.hidden = true;
  });

  window.jwBatchBadge = {
    poll,
    start: () => {
      wrap.hidden = false;
      ring.classList.remove('batch-ring-done');
      ring.style.setProperty('--batch-progress', 0);
      count.textContent = '…';
      line1.textContent = 'Génération lancée…';
      line2.textContent = '';
      if (!timer) timer = setInterval(poll, 3000);
      poll();
    },
  };
  poll();
})();
"""


_JS = """\
(function () {
  const root = document.documentElement;
  const themeToggle = document.getElementById('theme-toggle');
  const themeColor = document.getElementById('theme-color');
  const syncThemeUI = () => {
    const isLight = root.dataset.theme === 'light';
    themeToggle.setAttribute('aria-label', isLight ? 'Passer au thème sombre' : 'Passer au thème clair');
    themeToggle.setAttribute('aria-pressed', isLight ? 'true' : 'false');
    themeColor.setAttribute('content', isLight ? '#f3f1eb' : '#090b10');
  };
  syncThemeUI();
  themeToggle.addEventListener('click', () => {
    root.dataset.theme = root.dataset.theme === 'light' ? 'dark' : 'light';
    try { localStorage.setItem('jw-theme', root.dataset.theme); } catch (_) {}
    syncThemeUI();
  });

  const q = document.getElementById('q');
  const clearSearch = document.getElementById('clear-search');
  const searchStatus = document.getElementById('search-status');
  const noResults = document.getElementById('no-results');
  const searchDock = document.getElementById('search-dock');
  const details = [...document.querySelectorAll('.section')];
  const rows = [...document.querySelectorAll('.row')];
  // Résumé, annonce et lettre partagent un lecteur : une seule vue reste ouverte.
  document.addEventListener('click', event => {
    const button = event.target.closest('.reader-tab');
    if (!button) return;
    const reader = button.closest('.card-reader');
    const expanded = button.getAttribute('aria-expanded') === 'true';
    reader.querySelectorAll('.reader-tab').forEach(other => {
      const panel = document.getElementById(other.getAttribute('aria-controls'));
      const keepOpen = other === button && !expanded;
      other.setAttribute('aria-expanded', keepOpen ? 'true' : 'false');
      if (panel) panel.hidden = !keepOpen;
    });
  });
  const readSession = (key, fallback) => {
    try { return sessionStorage.getItem(key) ?? fallback; } catch (_) { return fallback; }
  };
  const writeSession = (key, value) => {
    try { sessionStorage.setItem(key, value); } catch (_) {}
  };
  const normalize = value => value.toLocaleLowerCase('fr').normalize('NFD')
    .replace(/[\\u0300-\\u036f]/g, '');
  let saved = {};
  try { saved = JSON.parse(readSession('jw-open', '{}')) || {}; } catch (_) {}
  q.value = readSession('jw-q', '');
  details.forEach((d, i) => {
    const key = d.dataset.section;
    const stored = saved[key] !== undefined ? saved[key] : saved[i];
    d.dataset.open = stored !== undefined ? String(stored)
      : d.dataset.default === '1' ? '1' : '0';
    d.open = d.dataset.open === '1';
    d.addEventListener('toggle', () => {
      if (q.value.trim()) return;
      d.dataset.open = d.open ? '1' : '0';
      saved[key] = d.dataset.open;
      writeSession('jw-open', JSON.stringify(saved));
    });
  });
  const apply = () => {
    const rawNeedle = q.value.trim();
    const needle = normalize(rawNeedle);
    let shownTotal = 0;
    rows.forEach(r => {
      const visible = !needle || normalize(r.textContent).includes(needle);
      r.hidden = !visible;
      if (visible) shownTotal += 1;
    });
    details.forEach(d => {
      const sectionRows = [...d.querySelectorAll('.row')];
      const shown = sectionRows.filter(r => !r.hidden).length;
      d.open = needle ? shown > 0 : d.dataset.open === '1';
      const c = d.querySelector('.count');
      if (c) c.textContent = needle ? `${shown}/${sectionRows.length}` : `${sectionRows.length}`;
    });
    clearSearch.classList.toggle('visible', Boolean(rawNeedle));
    noResults.hidden = !needle || shownTotal > 0;
    searchStatus.textContent = needle
      ? `${shownTotal} sur ${rows.length} offre${shownTotal === 1 ? '' : 's'}`
      : `${rows.length} offres`;
  };
  q.addEventListener('input', () => {
    writeSession('jw-q', q.value);
    apply();
  });
  q.addEventListener('keydown', e => {
    if (e.key === 'Escape' && q.value) {
      e.preventDefault(); q.value = ''; writeSession('jw-q', ''); apply();
    }
  });
  clearSearch.addEventListener('click', () => {
    q.value = ''; writeSession('jw-q', ''); apply(); q.focus();
  });
  const observeDock = () => searchDock.classList.toggle('stuck', searchDock.getBoundingClientRect().top
    <= parseFloat(getComputedStyle(searchDock).top) + 1);
  document.addEventListener('scroll', observeDock, {passive:true});
  observeDock();
  apply();

  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const UNDO_WINDOW_MS = 7000;
  const REMOVE_MS = 260;
  const updateSectionCount = section => {
    if (!section) return;
    const count = section.querySelector('.count');
    if (count && !q.value.trim()) count.textContent = String(section.querySelectorAll('.row').length);
  };
  // Minuterie plutôt que transitionend : quand le système saute la transition
  // (iOS en économie d'énergie) sans exposer prefers-reduced-motion,
  // transitionend ne se déclenche jamais et la carte resterait figée.
  const removeRow = (row, done) => {
    if (reduceMotion) { done(); return; }
    row.classList.add('row-removing');
    setTimeout(done, REMOVE_MS);
  };
  const showToast = (row, message, onUndo) => {
    const toast = document.createElement('article');
    toast.className = 'row undo-toast';
    const label = document.createElement('span');
    label.textContent = message;
    toast.append(label);
    let undoBtn = null;
    if (onUndo) {
      undoBtn = document.createElement('button');
      undoBtn.type = 'button';
      undoBtn.className = 'undo-btn';
      undoBtn.textContent = 'Annuler';
      toast.append(undoBtn);
    }
    row.replaceWith(toast);
    rows.splice(rows.indexOf(row), 1, toast);
    updateSectionCount(toast.closest('.section'));
    const timer = setTimeout(() => {
      const section = toast.closest('.section');
      toast.remove();
      rows.splice(rows.indexOf(toast), 1);
      updateSectionCount(section);
    }, UNDO_WINDOW_MS);
    if (undoBtn) undoBtn.addEventListener('click', () => {
      clearTimeout(timer);
      onUndo();
    });
  };
  const showUndo = (row, matchId, prevState) => {
    showToast(row, 'Retirée du tableau de bord.', () => {
      fetch(`/match/${matchId}/restore`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({state: prevState}),
      }).then(resp => {
        if (resp.ok) location.reload();
      });
    });
  };
  const actionButtons = [...document.querySelectorAll('.action-later, .action-discard')];
  actionButtons.forEach(button => {
    button.addEventListener('click', () => {
      const row = button.closest('.row');
      const matchId = button.dataset.matchId;
      const action = button.dataset.action;
      const prevState = button.dataset.prevState;
      fetch(`/match/${matchId}/${action}`, {method: 'POST'}).then(resp => {
        if (!resp.ok) return;
        removeRow(row, () => showUndo(row, matchId, prevState));
      });
    });
  });
  // Un seul formulaire d'action reste ouvert à la fois dans une carte.
  [...document.querySelectorAll('.action-apply, .action-draft')].forEach(button => {
    button.addEventListener('click', () => {
      const expanded = button.getAttribute('aria-expanded') === 'true';
      const form = document.getElementById(button.getAttribute('aria-controls'));
      if (!expanded) {
        [...button.closest('.row').querySelectorAll('.action-apply, .action-draft')]
          .forEach(other => {
            if (other === button) return;
            other.setAttribute('aria-expanded', 'false');
            const otherForm = document.getElementById(other.getAttribute('aria-controls'));
            if (otherForm) otherForm.hidden = true;
          });
      }
      button.setAttribute('aria-expanded', expanded ? 'false' : 'true');
      if (form) form.hidden = expanded;
    });
  });
  [...document.querySelectorAll('.apply-form')].forEach(form => {
    form.addEventListener('submit', event => {
      event.preventDefault();
      const row = form.closest('.row');
      const matchId = form.dataset.matchId;
      const toId = value => value ? Number(value) : null;
      fetch(`/match/${matchId}/apply`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          cv_library_id: toId(form.elements.cv_library_id.value),
          cover_letter_library_id: toId(form.elements.cover_letter_library_id.value),
        }),
      }).then(resp => {
        if (!resp.ok) return;
        removeRow(row, () => showToast(row, 'Candidature enregistrée.'));
      });
    });
  });

  const RUNNING_HTML = '<span class="draft-spinner" aria-hidden="true"></span>'
    + '<span>Génération de la lettre en cours…</span>';
  const DRAFT_POLL_MS = 3000;
  const draftPolls = new Map();
  const setDraftArea = (matchId, html, status) => {
    const area = document.getElementById(`draft-area-${matchId}`);
    if (!area) return;
    area.innerHTML = html;
    area.dataset.status = status;
    const tab = document.querySelector(`.letter-toggle[aria-controls="letter-panel-${matchId}"]`);
    if (tab) {
      tab.classList.remove('letter-empty', 'letter-queued', 'letter-running', 'letter-failed', 'letter-ok');
      tab.classList.add(`letter-${status || 'empty'}`);
      const label = tab.querySelector('.reader-tab-label');
      if (label) label.textContent = status === 'ok' ? 'Lettre · prête'
        : status === 'failed' ? 'Lettre · échec'
        : status === 'queued' || status === 'running' ? 'Lettre · en cours' : 'Lettre';
    }
    const compose = document.querySelector(`.action-draft[aria-controls="draft-form-${matchId}"]`);
    if (compose) {
      compose.hidden = status === 'queued' || status === 'running';
      compose.textContent = status === 'ok' ? 'Régénérer la lettre'
        : status === 'failed' ? 'Réessayer' : 'Générer la lettre';
    }
  };
  const registerCoverLetter = (matchId, libraryId, label) => {
    const value = String(libraryId);
    [...document.querySelectorAll('select[data-doc-type="cover_letter"]')].forEach(select => {
      let option = [...select.options].find(o => o.value === value);
      if (!option) {
        option = document.createElement('option');
        option.value = value;
        select.append(option);
      }
      option.textContent = label;
    });
    const own = document.querySelector(`#apply-form-${matchId} select[data-doc-type="cover_letter"]`);
    if (own) {
      own.value = value;
      own.dispatchEvent(new Event('change'));
    }
  };
  const pollDraft = matchId => {
    if (draftPolls.has(matchId)) return;
    const timer = setInterval(() => {
      fetch(`/match/${matchId}/draft/status`)
        .then(resp => resp.ok ? resp.json() : null)
        .then(payload => {
          if (!payload) return;
          if (payload.status !== 'running' && payload.status !== 'queued') {
            clearInterval(timer);
            draftPolls.delete(matchId);
          }
          setDraftArea(matchId, payload.html, payload.status);
          if (payload.status === 'ok' && payload.library_id) {
            registerCoverLetter(matchId, payload.library_id, payload.library_label);
          }
        })
        .catch(() => {});
    }, DRAFT_POLL_MS);
    draftPolls.set(matchId, timer);
  };
  [...document.querySelectorAll('.draft-area[data-status="running"], .draft-area[data-status="queued"]')]
    .forEach(area => pollDraft(area.dataset.matchId));
  [...document.querySelectorAll('.draft-form')].forEach(form => {
    const select = form.elements.cv_library_id;
    const track = form.dataset.track;
    if (select) {
      let savedCv = null;
      try { savedCv = localStorage.getItem(`jw-cv-${track}`); } catch (_) {}
      if (savedCv && [...select.options].some(o => o.value === savedCv)) select.value = savedCv;
    }
    form.addEventListener('submit', event => {
      event.preventDefault();
      if (!select) return;
      const matchId = form.dataset.matchId;
      try { localStorage.setItem(`jw-cv-${track}`, select.value); } catch (_) {}
      fetch(`/match/${matchId}/draft`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          cv_library_id: Number(select.value),
          instruction: form.elements.instruction.value.trim(),
          track,
        }),
      }).then(resp => {
        if (resp.ok) {
          form.hidden = true;
          const btn = document.querySelector(`[aria-controls="draft-form-${matchId}"]`);
          if (btn) btn.setAttribute('aria-expanded', 'false');
          setDraftArea(matchId, RUNNING_HTML, 'running');
          pollDraft(matchId);
          return null;
        }
        return resp.json().catch(() => null);
      }).then(payload => {
        if (payload && payload.error) {
          const area = document.getElementById(`draft-area-${matchId}`);
          if (!area) return;
          area.dataset.status = 'failed';
          area.textContent = '';
          const p = document.createElement('p');
          p.className = 'draft-error';
          p.textContent = payload.error;
          area.append(p);
        }
      }).catch(() => {});
    });
  });

  const swipePopup = document.getElementById('swipe-popup');
  if (swipePopup) {
    const popupKey = `jw-swipe-popup-${swipePopup.dataset.track}`;
    const dismiss = () => {
      swipePopup.classList.remove('visible');
      setTimeout(() => { swipePopup.hidden = true; }, 260);
      writeSession(popupKey, '1');
    };
    if (!readSession(popupKey, '')) {
      swipePopup.hidden = false;
      requestAnimationFrame(() => swipePopup.classList.add('visible'));
    }
    swipePopup.querySelector('.swipe-popup-later').addEventListener('click', dismiss);
    swipePopup.addEventListener('click', e => { if (e.target === swipePopup) dismiss(); });
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && !swipePopup.hidden) dismiss();
    });
  }

  [...document.querySelectorAll('.doc-preview-btn')].forEach(btn => {
    const select = btn.closest('.doc-row').querySelector('select');
    const sync = () => { btn.disabled = !select.value; };
    select.addEventListener('change', sync);
    sync();
    btn.addEventListener('click', () => {
      if (select.value) window.open(`/documents/${select.value}`, '_blank', 'noopener');
    });
  });

  const readAsBase64 = file => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',', 2)[1] || '');
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
  const pendingUploads = new WeakMap();
  const startUpload = (field, file) => {
    pendingUploads.set(field, file);
    const prompt = field.querySelector('.doc-label-prompt');
    const labelInput = field.querySelector('.doc-label-input');
    labelInput.value = '';
    prompt.hidden = false;
    labelInput.focus();
  };
  const finishUpload = async field => {
    const file = pendingUploads.get(field);
    if (!file) return;
    const docType = field.dataset.docType;
    const labelInput = field.querySelector('.doc-label-input');
    const select = field.querySelector('.doc-select');
    const contentBase64 = await readAsBase64(file);
    const resp = await fetch('/documents', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        filename: file.name,
        type: docType,
        label: labelInput.value.trim(),
        content_base64: contentBase64,
      }),
    });
    if (!resp.ok) return;
    const entry = await resp.json();
    const option = document.createElement('option');
    option.value = String(entry.id);
    option.textContent = entry.label;
    select.append(option);
    select.value = String(entry.id);
    select.dispatchEvent(new Event('change'));
    pendingUploads.delete(field);
    field.querySelector('.doc-label-prompt').hidden = true;
  };
  [...document.querySelectorAll('.doc-field')].forEach(field => {
    const fileInput = field.querySelector('.doc-file-input');
    field.querySelector('.doc-upload-btn').addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', () => {
      if (fileInput.files[0]) startUpload(field, fileInput.files[0]);
      fileInput.value = '';
    });
    field.querySelector('.doc-label-confirm').addEventListener('click', () => finishUpload(field));
    field.querySelector('.doc-label-input').addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); finishUpload(field); }
    });
    field.addEventListener('dragover', e => { e.preventDefault(); field.classList.add('doc-dragover'); });
    field.addEventListener('dragleave', () => field.classList.remove('doc-dragover'));
    field.addEventListener('drop', e => {
      e.preventDefault();
      field.classList.remove('doc-dragover');
      const file = e.dataTransfer.files[0];
      if (file) startUpload(field, file);
    });
  });
})();
"""


_TRACK_TABS = (
    ("engineer", "/", "Ingénieur IA"),
    ("project", "/po", "Chef de projet / PO"),
)


def _track_nav(track: str) -> str:
    if track == "all":
        return ""
    links = []
    for key, href, label in _TRACK_TABS:
        current = ' aria-current="page"' if key == track else ""
        links.append(
            f'<a class="track-tab{" active" if key == track else ""}" '
            f'href="{href}"{current}>{html.escape(label)}</a>'
        )
    return f'<nav class="track-tabs" aria-label="Piste métier">{"".join(links)}</nav>'


def _page_template(
    *, body, total, new_count, seen_count, applied_count, stamp, track,
    category_link="", swipe_fab="", swipe_popup="", batch_badge="", csrf_token="",
) -> str:
    logout_button = (
        '<button class="logout-button" type="button" aria-label="Déconnexion" '
        'onclick="fetch(\'/logout\',{method:\'POST\'}).then(()=>location.href=\'/login\')">'
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<path d="M10 5H6a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h4"/>'
        '<path d="m15 8 4 4-4 4M9 12h10"/></svg><span>Déconnexion</span></button>'
        if csrf_token
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="fr" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#f3f1eb" id="theme-color">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>jobwatch · tableau de bord</title>
{_csrf_head(csrf_token)}
<script>
(function () {{
  try {{
    const saved = localStorage.getItem('jw-theme');
    document.documentElement.dataset.theme = saved === 'dark' ? 'dark' : 'light';
  }} catch (_) {{
    document.documentElement.dataset.theme = 'light';
  }}
}})();
</script>
<style>
{_CSS}</style></head><body data-track="{track}">
<div class="ambient" aria-hidden="true"></div>
<div class="shell">
  <header>
    <div class="topbar">
      <div class="identity">
        <div class="monogram" aria-hidden="true">JW</div>
        <div class="identity-copy"><span class="identity-name">jobwatch</span>
          <span class="identity-sub">Suivi de vos offres</span></div>
      </div>
      <div class="topbar-tools">
      {swipe_fab}
      {batch_badge}
      <button class="theme-toggle" id="theme-toggle" type="button" aria-label="Passer au thème clair">
        <svg class="icon-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
          <circle cx="12" cy="12" r="3.7"/><path d="M12 2v2.1M12 19.9V22M4.93 4.93l1.49 1.49M17.58 17.58l1.49 1.49M2 12h2.1M19.9 12H22M4.93 19.07l1.49-1.49M17.58 6.42l1.49-1.49"/>
        </svg>
        <svg class="icon-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
          <path d="M20.2 15.1A8.4 8.4 0 0 1 8.9 3.8 8.5 8.5 0 1 0 20.2 15.1Z"/>
        </svg>
      </button>
      {logout_button}
      </div>
    </div>
    <div class="hero">
      <p class="eyebrow">Tableau de bord</p>
      <h1>Vos offres,<br><span>sous contrôle.</span></h1>
      <p class="hero-meta"><span class="live-dot" aria-hidden="true"></span>
        Mis à jour le {stamp}</p>
    </div>
    {_track_nav(track)}
    {category_link}
    <div class="stats" aria-label="Vue d'ensemble">
      <div class="stat stat-new"><span class="stat-value">{new_count}</span><span class="stat-label">Nouveaux matchs</span></div>
      <div class="stat stat-seen"><span class="stat-value">{seen_count}</span><span class="stat-label">Vus</span></div>
      <div class="stat stat-applied"><span class="stat-value">{applied_count}</span><span class="stat-label">Candidatures</span></div>
    </div>
  </header>
  <div class="search-dock" id="search-dock">
    <div class="search-box">
      <svg class="search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
        <circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/>
      </svg>
      <input id="q" type="search" aria-label="Filtrer les offres"
        placeholder="Entreprise, poste, lieu, recherche…" autocomplete="off" enterkeyhint="search">
      <button class="clear-search" id="clear-search" type="button" aria-label="Effacer la recherche">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
          <circle cx="12" cy="12" r="8"/><path d="m9 9 6 6M15 9l-6 6"/>
        </svg>
      </button>
    </div>
    <div class="search-status" id="search-status" aria-live="polite">{total} offres</div>
  </div>
  <main>
{body}
    <div class="no-results" id="no-results" hidden><strong>Aucune offre trouvée</strong>
      Essayez un autre mot-clé.</div>
  </main>
  <footer class="footer">Lecture seule · données locales · base SQLite jobwatch</footer>
</div>
{swipe_popup}
<script>
{_JS}
{_BATCH_BADGE_JS}</script></body></html>
"""


_SWIPE_CSS = """\
.swipe-shell { position:relative; z-index:1; display:flex; flex-direction:column;
  width:min(100%, 560px); min-height:100vh; min-height:100dvh; margin:0 auto;
  padding:max(14px, env(safe-area-inset-top)) calc(14px + env(safe-area-inset-right))
  calc(14px + env(safe-area-inset-bottom)) calc(14px + env(safe-area-inset-left)) }
.swipe-top { display:flex; align-items:center; justify-content:space-between; gap:10px;
  margin-bottom:12px }
.swipe-back { display:inline-flex; align-items:center; min-height:44px; padding:0 13px;
  border:1px solid var(--line); border-radius:11px; color:var(--fg); background:var(--surface);
  font-size:.76rem; font-weight:700; text-decoration:none }
.swipe-count { color:var(--muted); font-size:.8rem; font-weight:700;
  font-variant-numeric:tabular-nums }
.swipe-stage { position:relative; flex:1; min-height:340px }
.swipe-stage[hidden] { display:none }
.swipe-card { position:absolute; inset:0; display:none; flex-direction:column;
  padding:18px 16px 14px; border:1px solid var(--line-strong); border-radius:var(--radius-xl);
  background:var(--surface); box-shadow:var(--shadow); overflow:hidden; touch-action:pan-y }
.swipe-card.top { display:flex; z-index:2 }
.swipe-card.next { display:flex; z-index:1; transform:scale(.955) translateY(10px);
  opacity:.55; pointer-events:none }
.swipe-card.leaving { display:flex; z-index:3; pointer-events:none;
  transition:transform .28s ease, opacity .28s ease }
.swipe-card-scroll { flex:1; min-height:0; overflow-y:auto; -webkit-overflow-scrolling:touch }
.swipe-card .content-panel { margin:10px 0 0; padding:12px 0 0; pointer-events:auto }
.swipe-card .content-toggle { margin:12px 0 0 }
.swipe-summary { margin:14px 0 0; padding:12px 12px 11px; border:1px dashed var(--line-strong);
  border-radius:12px }
.swipe-summary ul { margin:8px 0 0; padding-left:18px; color:var(--muted); font-size:.8rem }
.swipe-summary li + li { margin-top:5px }
.swipe-stamp { position:absolute; top:16px; padding:6px 12px; border:2px solid;
  border-radius:10px; font-size:.78rem; font-weight:850; letter-spacing:.06em;
  text-transform:uppercase; opacity:0; pointer-events:none; background:var(--surface) }
.stamp-right { left:12px; color:var(--accent); border-color:var(--accent);
  transform:rotate(-12deg) }
.stamp-left { right:12px; color:var(--danger); border-color:var(--danger);
  transform:rotate(12deg) }
.swipe-controls { display:flex; align-items:center; justify-content:center; gap:16px;
  padding:16px 0 4px }
.swipe-controls[hidden] { display:none }
.swipe-btn { width:64px; height:64px; display:grid; place-items:center; padding:0;
  border:1px solid var(--line-strong); border-radius:50%; background:var(--surface);
  box-shadow:var(--card-shadow); cursor:pointer;
  transition:transform .15s ease, background .15s ease, opacity .15s ease }
.swipe-btn svg { width:26px; height:26px }
.swipe-btn:active { transform:scale(.9) }
.swipe-btn:disabled { opacity:.35; cursor:default }
.swipe-btn:focus-visible { outline:3px solid var(--violet); outline-offset:3px }
.swipe-btn-no { color:var(--danger) }
.swipe-btn-yes { color:var(--accent) }
.swipe-btn-undo { width:50px; height:50px; color:var(--muted) }
.swipe-btn-undo svg { width:20px; height:20px }
@media (hover:hover) { .swipe-btn:not(:disabled):hover { background:var(--surface-hover) } }
.swipe-done { padding:22px 18px; border:1px solid var(--line); border-radius:var(--radius-xl);
  background:var(--surface); box-shadow:var(--card-shadow) }
.swipe-done[hidden] { display:none }
.swipe-done h2 { margin:0 0 6px; font-size:1.35rem; font-weight:790; letter-spacing:-.03em }
.done-stats { margin:0 0 16px; color:var(--muted); font-size:.85rem }
.batch-form { display:grid; grid-template-columns:minmax(0, 1fr); gap:10px }
.batch-btn { justify-self:start; gap:5px; color:var(--violet) }
.batch-btn:disabled { opacity:.45; cursor:default }
.done-back { display:inline-flex; margin-top:18px; text-decoration:none }
"""

_SWIPE_JS = """\
(function () {
  const stage = document.getElementById('swipe-stage');
  const cards = [...document.querySelectorAll('.swipe-card')];
  const countEl = document.getElementById('swipe-count');
  const controls = document.getElementById('swipe-controls');
  const done = document.getElementById('swipe-done');
  const undoBtn = document.getElementById('swipe-undo');
  const track = document.body.dataset.track;
  const pendingInitial = Number(document.body.dataset.pending || '0');
  const total = cards.length;
  let index = 0;
  const history = [];
  const session = {right: 0, left: 0};

  const showDone = () => {
    stage.hidden = true;
    controls.hidden = true;
    done.hidden = false;
    document.getElementById('done-right').textContent = String(session.right);
    document.getElementById('done-left').textContent = String(session.left);
    const batchBtn = document.getElementById('batch-btn');
    if (batchBtn && !batchBtn.dataset.started) {
      const pending = pendingInitial + session.right;
      document.getElementById('batch-count').textContent = String(pending);
      batchBtn.disabled = pending === 0;
    }
  };
  const hideDone = () => {
    stage.hidden = false;
    controls.hidden = false;
    done.hidden = true;
  };
  const render = () => {
    cards.forEach((card, i) => {
      card.classList.toggle('top', i === index);
      card.classList.toggle('next', i === index + 1);
    });
    countEl.textContent = `${Math.min(index + 1, total)} / ${total}`;
    undoBtn.disabled = history.length === 0;
    if (index >= total) showDone();
  };

  const act = dir => {
    if (index >= total) return;
    const card = cards[index];
    const action = dir === 'right' ? 'later' : 'discard';
    fetch(`/match/${card.dataset.matchId}/${action}`, {method: 'POST'}).then(resp => {
      if (!resp.ok) location.reload();
    });
    history.push({index, dir});
    session[dir] += 1;
    index += 1;
    card.classList.remove('top');
    card.classList.add('leaving');
    const sign = dir === 'right' ? 1 : -1;
    requestAnimationFrame(() => {
      card.style.transform = `translateX(${sign * 130}%) rotate(${sign * 14}deg)`;
      card.style.opacity = '0';
    });
    setTimeout(() => {
      card.classList.remove('leaving');
      card.style.transform = '';
      card.style.opacity = '';
    }, 300);
    render();
  };

  const undo = () => {
    const last = history.pop();
    if (!last) return;
    if (index >= total) hideDone();
    const card = cards[last.index];
    session[last.dir] -= 1;
    index = last.index;
    fetch(`/match/${card.dataset.matchId}/restore`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({state: 'new'}),
    }).then(resp => {
      if (!resp.ok) location.reload();
    });
    render();
  };

  document.getElementById('swipe-no').addEventListener('click', () => act('left'));
  document.getElementById('swipe-yes').addEventListener('click', () => act('right'));
  undoBtn.addEventListener('click', undo);
  document.addEventListener('keydown', e => {
    if (e.key === 'ArrowRight') { e.preventDefault(); act('right'); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); act('left'); }
    else if (e.key === 'ArrowUp' || e.key === 'u') { e.preventDefault(); undo(); }
  });

  [...document.querySelectorAll('.swipe-content-toggle')].forEach(button => {
    button.addEventListener('click', () => {
      const expanded = button.getAttribute('aria-expanded') === 'true';
      const panel = document.getElementById(button.getAttribute('aria-controls'));
      button.setAttribute('aria-expanded', expanded ? 'false' : 'true');
      if (panel) panel.hidden = expanded;
    });
  });

  let drag = null;
  const THRESHOLD = 80;
  stage.addEventListener('pointerdown', e => {
    if (index >= total) return;
    const card = cards[index];
    if (!card.contains(e.target) || e.target.closest('a, button')) return;
    drag = {id: e.pointerId, x: e.clientX, dx: 0, card};
    card.setPointerCapture(e.pointerId);
  });
  stage.addEventListener('pointermove', e => {
    if (!drag || e.pointerId !== drag.id) return;
    drag.dx = e.clientX - drag.x;
    drag.card.style.transform = `translateX(${drag.dx}px) rotate(${drag.dx / 22}deg)`;
    const fade = Math.min(1, Math.max(0, (Math.abs(drag.dx) - 30) / 60));
    drag.card.querySelector('.stamp-right').style.opacity = drag.dx > 0 ? fade : 0;
    drag.card.querySelector('.stamp-left').style.opacity = drag.dx < 0 ? fade : 0;
  });
  const endDrag = e => {
    if (!drag || e.pointerId !== drag.id) return;
    const {card, dx} = drag;
    drag = null;
    card.querySelector('.stamp-right').style.opacity = '';
    card.querySelector('.stamp-left').style.opacity = '';
    if (dx > THRESHOLD) act('right');
    else if (dx < -THRESHOLD) act('left');
    else {
      card.style.transition = 'transform .2s ease';
      card.style.transform = '';
      setTimeout(() => { card.style.transition = ''; }, 220);
    }
  };
  stage.addEventListener('pointerup', endDrag);
  stage.addEventListener('pointercancel', endDrag);

  const batchBtn = document.getElementById('batch-btn');
  if (batchBtn) {
    const select = document.getElementById('batch-cv');
    let savedCv = null;
    try { savedCv = localStorage.getItem(`jw-cv-${track}`); } catch (_) {}
    if (savedCv && [...select.options].some(o => o.value === savedCv)) select.value = savedCv;
    batchBtn.addEventListener('click', () => {
      if (!select.value) return;
      try { localStorage.setItem(`jw-cv-${track}`, select.value); } catch (_) {}
      batchBtn.disabled = true;
      batchBtn.dataset.started = '1';
      fetch('/draft/batch', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({track, cv_library_id: Number(select.value)}),
      }).then(resp => {
        if (!resp.ok) { batchBtn.disabled = false; delete batchBtn.dataset.started; return; }
        if (window.jwBatchBadge) window.jwBatchBadge.start();
      }).catch(() => {});
    });
  }

  render();
})();
"""


def _swipe_page_template(
    *, track, cards, total, pending, batch, back_href, batch_badge="", csrf_token=""
) -> str:
    return f"""<!DOCTYPE html>
<html lang="fr" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#f3f1eb">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>jobwatch · tri des offres</title>
{_csrf_head(csrf_token)}
<script>
(function () {{
  try {{
    const saved = localStorage.getItem('jw-theme');
    document.documentElement.dataset.theme = saved === 'dark' ? 'dark' : 'light';
  }} catch (_) {{
    document.documentElement.dataset.theme = 'light';
  }}
}})();
</script>
<style>
{_CSS}{_SWIPE_CSS}</style></head>
<body data-track="{track}" data-pending="{pending}">
<div class="ambient" aria-hidden="true"></div>
<div class="swipe-shell">
  <div class="swipe-top">
    <a class="swipe-back" href="{back_href}">← Tableau de bord</a>
    {batch_badge}
    <span class="swipe-count" id="swipe-count">1 / {total}</span>
  </div>
  <div class="swipe-stage" id="swipe-stage">
{cards}
  </div>
  <div class="swipe-controls" id="swipe-controls">
    <button class="swipe-btn swipe-btn-no" id="swipe-no" type="button" aria-label="Écarter (flèche gauche)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>
    </button>
    <button class="swipe-btn swipe-btn-undo" id="swipe-undo" type="button" aria-label="Annuler le dernier tri (flèche haut)" disabled>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/></svg>
    </button>
    <button class="swipe-btn swipe-btn-yes" id="swipe-yes" type="button" aria-label="À candidater (flèche droite)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m4.5 12.5 5 5 10-11"/></svg>
    </button>
  </div>
  <section class="swipe-done" id="swipe-done" hidden aria-live="polite">
    <h2>Tri terminé</h2>
    <p class="done-stats"><span id="done-right">0</span> à candidater · <span id="done-left">0</span> écartée(s)</p>
    {batch}
    <a class="card-action done-back" href="{back_href}">← Retour au tableau de bord</a>
  </section>
</div>
<script>
{_SWIPE_JS}
{_BATCH_BADGE_JS}</script></body></html>
"""
