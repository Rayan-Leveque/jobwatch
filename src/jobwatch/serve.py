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
mutation de match. Aucune authentification n'est ajoutée, le périmètre
Tailscale est la frontière de confiance.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import click

from jobwatch.applications import ApplicationError, record_application
from jobwatch.db import connect
from jobwatch.library import LibraryError, list_library, resolve_path, save_upload

RESTORE_STATES = ("new", "seen", "later")
_MATCH_ACTION_RE = re.compile(r"^/match/(\d+)/(later|discard|restore|apply)$")
_UPLOAD_PATH = "/documents"

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
TRACKS = ("engineer", "project")


def _track_filter(track: str) -> str:
    """Clause SQL restreignant une requête (alias offre `o`) à la piste demandée."""
    if track == "project":
        return f"AND {_PROJECT_TITLE_SQL} "
    return f"AND NOT {_PROJECT_TITLE_SQL} "


def _matches(conn: sqlite3.Connection, state: str, track: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT m.id AS id, o.id AS offer_id, m.state AS state, m.fit AS fit, "
        "       s.name AS search_name, "
        "       c.name AS company, o.title AS title, o.location AS location, "
        "       o.contract AS contract, o.platform AS platform, o.url AS url, "
        "       o.collected_at AS collected_at, o.deadline AS deadline "
        "FROM match m "
        "JOIN search s ON s.id = m.search_id "
        "JOIN offer o ON o.id = m.offer_id "
        "LEFT JOIN company c ON c.id = o.company_id "
        "WHERE m.state = ? AND (m.fit IS NULL OR m.fit != 'high') AND NOT EXISTS "
        "    (SELECT 1 FROM application a WHERE a.match_id = m.id) "
        f"{_track_filter(track)}"
        "ORDER BY CASE m.fit WHEN 'high' THEN 0 WHEN 'medium' THEN 1 "
        "         WHEN 'low' THEN 2 ELSE 3 END, "
        "         o.collected_at DESC, m.id DESC",
        (state, *PROJECT_TITLE_PATTERNS),
    ).fetchall()


def _priority_matches(conn: sqlite3.Connection, track: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT m.id AS id, o.id AS offer_id, m.state AS state, m.fit AS fit, "
        "       s.name AS search_name, "
        "       c.name AS company, o.title AS title, o.location AS location, "
        "       o.contract AS contract, o.platform AS platform, o.url AS url, "
        "       o.collected_at AS collected_at, o.deadline AS deadline "
        "FROM match m "
        "JOIN search s ON s.id = m.search_id "
        "JOIN offer o ON o.id = m.offer_id "
        "LEFT JOIN company c ON c.id = o.company_id "
        "WHERE m.fit = 'high' AND m.state IN ('new', 'seen') AND NOT EXISTS "
        "    (SELECT 1 FROM application a WHERE a.match_id = m.id) "
        f"{_track_filter(track)}"
        "ORDER BY o.collected_at DESC, m.id DESC",
        PROJECT_TITLE_PATTERNS,
    ).fetchall()


def _later_matches(conn: sqlite3.Connection, track: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT m.id AS id, o.id AS offer_id, m.state AS state, m.fit AS fit, "
        "       s.name AS search_name, "
        "       c.name AS company, o.title AS title, o.location AS location, "
        "       o.contract AS contract, o.platform AS platform, o.url AS url, "
        "       o.collected_at AS collected_at, o.deadline AS deadline "
        "FROM match m "
        "JOIN search s ON s.id = m.search_id "
        "JOIN offer o ON o.id = m.offer_id "
        "LEFT JOIN company c ON c.id = o.company_id "
        "WHERE m.state = 'later' AND NOT EXISTS "
        "    (SELECT 1 FROM application a WHERE a.match_id = m.id) "
        f"{_track_filter(track)}"
        "ORDER BY CASE m.fit WHEN 'high' THEN 0 WHEN 'medium' THEN 1 "
        "         WHEN 'low' THEN 2 ELSE 3 END, "
        "         o.collected_at DESC, m.id DESC",
        PROJECT_TITLE_PATTERNS,
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
        "JOIN search s ON s.id = m.search_id "
        "JOIN offer o ON o.id = m.offer_id "
        "LEFT JOIN company c ON c.id = o.company_id "
        "WHERE m.state = 'discarded' AND m.discarded_at > datetime('now', '-30 days') "
        f"{_track_filter(track)}"
        "ORDER BY m.discarded_at DESC, m.id DESC",
        PROJECT_TITLE_PATTERNS,
    ).fetchall()


def _applications(conn: sqlite3.Connection, track: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT a.id AS id, o.id AS offer_id, m.fit AS fit, c.name AS company, o.title AS title, "
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
        "LEFT JOIN search s ON s.id = m.search_id "
        f"WHERE 1=1 {_track_filter(track)}"
        "ORDER BY a.created_at DESC, a.id DESC",
        PROJECT_TITLE_PATTERNS,
    ).fetchall()


def _summary_bullets(
    conn: sqlite3.Connection, offer_ids: list[int]
) -> dict[int, list[str]]:
    if not offer_ids:
        return {}
    summaries: dict[int, list[str]] = {}
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
            summaries.setdefault(int(row["offer_id"]), []).append(str(row["text"]))
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


def _summary_panel(row: sqlite3.Row, bullets: list[str], prefix: str) -> tuple[str, str]:
    if row["fit"] != "high" or not bullets:
        return "", ""
    panel_id = f"summary-{prefix}-{int(row['id'])}"
    label = html.escape(f"Afficher le résumé de {row['title'] or 'cette offre'}", quote=True)
    button = (
        f'<button class="card-toggle" type="button" aria-expanded="false" '
        f'aria-controls="{panel_id}" aria-label="{label}"></button>'
    )
    items = "".join(f"<li>{html.escape(bullet)}</li>" for bullet in bullets)
    panel = (
        f'<div class="summary-panel" id="{panel_id}" hidden>'
        f'<div class="summary-title">En bref</div><ul>{items}</ul></div>'
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
        f'<button class="content-toggle" type="button" aria-expanded="false" '
        f'aria-controls="{panel_id}" aria-label="{label}">Annonce complète'
        f'<span class="summary-chevron" aria-hidden="true"></span></button>'
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


def _document_field(match_id: int, doc_type: str, name: str, label: str, rows: list[sqlite3.Row]) -> str:
    select_id = f"{doc_type}-select-{match_id}"
    options = _document_options(rows)
    return (
        f'<div class="doc-field" data-doc-type="{doc_type}">'
        f'<label class="doc-label" for="{select_id}">{html.escape(label)}</label>'
        '<div class="doc-row">'
        f'<select class="apply-input doc-select" id="{select_id}" name="{name}" '
        f'data-doc-type="{doc_type}">{options}</select>'
        f'<button class="doc-upload-btn" type="button" data-doc-type="{doc_type}">Uploader</button>'
        "</div>"
        f'<input class="doc-file-input" type="file" data-doc-type="{doc_type}" hidden>'
        '<div class="doc-label-prompt" hidden>'
        '<input class="apply-input doc-label-input" type="text" autocomplete="off" '
        'placeholder="Nom du document (optionnel)" aria-label="Nom du document">'
        '<button class="card-action doc-label-confirm" type="button">Ajouter à la bibliothèque</button>'
        "</div>"
        "</div>"
    )


def _card_actions(row: sqlite3.Row, library: dict[str, list[sqlite3.Row]]) -> str:
    match_id = int(row["id"])
    prev_state = html.escape(str(row["state"]), quote=True)
    form_id = f"apply-form-{match_id}"
    return (
        '<div class="card-actions">'
        f'<button class="card-action action-later" type="button" data-match-id="{match_id}" '
        f'data-prev-state="{prev_state}" data-action="later">Plus tard</button>'
        f'<button class="card-action action-discard" type="button" data-match-id="{match_id}" '
        f'data-prev-state="{prev_state}" data-action="discard">Écarter</button>'
        f'<button class="card-action action-apply" type="button" aria-expanded="false" '
        f'aria-controls="{form_id}">Candidater</button>'
        "</div>"
        f'<form class="apply-form" id="{form_id}" data-match-id="{match_id}" hidden>'
        f'{_document_field(match_id, "cv", "cv_library_id", "CV", library["cv"])}'
        f'{_document_field(match_id, "cover_letter", "cover_letter_library_id", "Lettre de motivation", library["cover_letter"])}'
        '<button class="card-action apply-submit" type="submit">Enregistrer la candidature</button>'
        "</form>"
    )


def _match_card(
    row: sqlite3.Row,
    bullets: list[str],
    content: str | None,
    library: dict[str, list[sqlite3.Row]],
    actions: bool = False,
) -> str:
    cls = _row_class(row["state"])
    company = html.escape(str(row["company"] or "Société inconnue"))
    title = html.escape(str(row["title"] or ""))
    pill = _fit_pill(row["fit"])
    meta = _meta(row, "collecté le", row["collected_at"], row["search_name"], row["deadline"])
    button, summary = _summary_panel(row, bullets, "match")
    summary_class = " has-summary" if summary else ""
    chevron = '<span class="summary-chevron" aria-hidden="true"></span>' if summary else ""
    content_button, content_panel = _content_panel(row, content, "match")
    actions_html = _card_actions(row, library) if actions else ""
    return (
        f'<article class="row row-{cls}{summary_class}">{button}<div class="body">'
        f'<div class="card-topline"><div class="company">{company}</div>'
        f'<div class="card-badges">{pill}{chevron}</div></div>'
        f'<div class="role">{title}</div>'
        f'<div class="meta">{meta}</div></div>{actions_html}{summary}{content_button}{content_panel}'
        "</article>"
    )


def _application_card(row: sqlite3.Row, bullets: list[str], content: str | None) -> str:
    status = str(row["status"] or "")
    cls = status if status in STATUS_LABELS else "unknown"
    label = STATUS_LABELS.get(status, STATUS_UNKNOWN)
    company = html.escape(str(row["company"] or "Société inconnue"))
    title = html.escape(str(row["title"] or ""))
    pill = f'<span class="pill {cls}">{html.escape(label)}</span>'
    meta = _meta(row, "candidature le", row["created_at"], row["search_name"])
    note = f'<p class="note">{html.escape(str(row["note"]))}</p>' if row["note"] else ""
    button, summary = _summary_panel(row, bullets, "application")
    summary_class = " has-summary" if summary else ""
    chevron = '<span class="summary-chevron" aria-hidden="true"></span>' if summary else ""
    content_button, content_panel = _content_panel(row, content, "application")
    return (
        f'<article class="row row-applied{summary_class}">{button}<div class="body">'
        f'<div class="card-topline"><div class="company">{company}</div>'
        f'<div class="card-badges">{pill}{chevron}</div></div>'
        f'<div class="role">{title}</div>'
        f'<div class="meta">{meta}</div>{note}</div>{summary}{content_button}{content_panel}</article>'
    )


ACTIONABLE_SECTIONS = {"priority", "new", "seen", "later"}


def _card(
    row: sqlite3.Row,
    key: str,
    summaries: dict[int, list[str]],
    contents: dict[int, str],
    library: dict[str, list[sqlite3.Row]],
) -> str:
    bullets = summaries.get(int(row["offer_id"]), [])
    content = contents.get(int(row["offer_id"]))
    if key == "applied":
        return _application_card(row, bullets, content)
    return _match_card(row, bullets, content, library, actions=key in ACTIONABLE_SECTIONS)


def _section(
    key: str,
    label: str,
    subtitle: str,
    rows,
    empty_text: str,
    open_default: bool,
    summaries: dict[int, list[str]],
    contents: dict[int, str],
    library: dict[str, list[sqlite3.Row]],
) -> str:
    if rows:
        cards = "\n".join(_card(row, key, summaries, contents, library) for row in rows)
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


def render_page(conn: sqlite3.Connection, track: str = "engineer") -> str:
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
    body = "\n".join(
        (
            _section(
                "priority", "Priorité haute", "À regarder en premier", priority,
                "Aucune offre prioritaire pour l'instant.", True, summaries, contents, library,
            ),
            _section(
                "new", "Nouveaux matchs", "À découvrir", new,
                "Aucun nouveau match pour l'instant.", True, summaries, contents, library,
            ),
            _section(
                "seen", "Vus", "Déjà parcourus", seen,
                "Aucun match parcouru pour l'instant.", False, summaries, contents, library,
            ),
            _section(
                "later", "À candidater", "Mis de côté pour plus tard", later,
                "Aucune offre à candidater plus tard pour l'instant.", False, summaries, contents, library,
            ),
            _section(
                "discarded", "Corbeille", "Écartées dans les 30 derniers jours", discarded,
                "Aucune offre écartée récemment.", False, summaries, contents, library,
            ),
            _section(
                "applied", "Candidatures", "Dernier statut connu", applied,
                "Aucune candidature pour l'instant.", False, summaries, contents, library,
            ),
        )
    )
    priority_new = sum(row["state"] == "new" for row in priority)
    priority_seen = len(priority) - priority_new
    total = len(priority) + len(new) + len(seen) + len(later) + len(discarded) + len(applied)
    stamp = datetime.now(UTC).astimezone().strftime("%d/%m/%Y %H:%M")
    return _page_template(
        body=body, total=total,
        new_count=len(new) + priority_new,
        seen_count=len(seen) + priority_seen,
        applied_count=len(applied),
        stamp=stamp,
        track=track,
    )


def make_handler(db_path: Path) -> type[BaseHTTPRequestHandler]:
    """Fabrique une classe de gestionnaire HTTP branchée sur render_page.

    Chaque requête ouvre sa propre connexion : ThreadingHTTPServer sert chaque
    requête dans un thread dédié, et la page relit ainsi l'état le plus récent.
    """

    class Handler(BaseHTTPRequestHandler):
        server_version = "jobwatch"

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/":
                track = "engineer"
            elif path == "/po":
                track = "project"
            else:
                self._send_text(404, "404 Not Found\n")
                return
            try:
                conn = connect(db_path)
                try:
                    page = render_page(conn, track)
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                self._send_text(500, f"erreur base de données : {exc}\n")
                return
            self._send_bytes(200, page.encode("utf-8"), "text/html; charset=utf-8")

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if path == _UPLOAD_PATH:
                self._handle_upload()
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

        def _read_json_body(self) -> object:
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                return None
            if length <= 0:
                return None
            raw = self.rfile.read(length)
            try:
                return json.loads(raw)
            except ValueError:
                return None

        def _send_json(self, status: int, payload: dict) -> None:
            self._send_bytes(status, json.dumps(payload).encode("utf-8"), "application/json")

        def _send_bytes(self, status: int, data: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            if status == 200:
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _send_text(self, status: int, text: str) -> None:
            self._send_bytes(status, text.encode("utf-8"), "text/plain; charset=utf-8")

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Handler


def url_of(host: str, port: int) -> str:
    if ":" in host:
        return f"http://[{host}]:{port}"
    return f"http://{host}:{port}"


def serve_http(db_path: Path, host: str, port: int) -> None:
    """Crée le serveur HTTP et le sert jusqu'à Ctrl-C."""
    try:
        server = ThreadingHTTPServer((host, port), make_handler(db_path))
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
.row .body { position:relative; z-index:2; min-width:0; pointer-events:none }
.row.has-summary { cursor:pointer }
.card-toggle { position:absolute; z-index:1; inset:0; width:100%; padding:0; border:0;
  border-radius:var(--radius-lg); background:transparent; cursor:pointer }
.card-topline { display:flex; align-items:flex-start; justify-content:space-between; gap:10px }
.card-badges { display:flex; align-items:center; gap:10px }
.summary-chevron { width:9px; height:9px; margin:0 4px 4px 0; border-right:2px solid var(--muted);
  border-bottom:2px solid var(--muted); transform:rotate(45deg); transition:transform .2s ease }
.has-summary:has(.card-toggle[aria-expanded="true"]) .summary-chevron { transform:rotate(225deg); margin-top:7px }
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
.summary-panel { position:relative; z-index:2; margin:14px 0 1px; padding:13px 13px 12px;
  border-top:1px solid var(--line); pointer-events:none }
.summary-panel[hidden] { display:none }
.summary-title { color:var(--accent); font-size:.68rem; font-weight:820; letter-spacing:.09em;
  text-transform:uppercase }
.summary-panel ul { margin:9px 0 0; padding-left:19px; color:var(--muted); font-size:.76rem }
.summary-panel li + li { margin-top:6px }
.content-toggle { position:relative; z-index:3; margin:12px 13px 0; padding:9px 12px;
  display:inline-flex; align-items:center; gap:7px; border:1px solid var(--line);
  border-radius:11px; color:var(--fg); background:var(--surface); font-size:.71rem;
  font-weight:700; letter-spacing:.02em; cursor:pointer; pointer-events:auto;
  transition:border-color .15s ease, background .15s ease }
.content-toggle .summary-chevron { margin:0 }
.content-toggle[aria-expanded="true"] .summary-chevron { transform:rotate(225deg) }
.content-panel { position:relative; z-index:2; margin:10px 13px 1px; padding:12px 13px;
  border-top:1px solid var(--line); color:var(--muted); font-size:.76rem;
  line-height:1.55; overflow-wrap:anywhere; pointer-events:none }
.content-panel[hidden] { display:none }
.content-panel p { margin:0 0 10px }
.content-panel p:last-child { margin-bottom:0 }
.card-actions { position:relative; z-index:3; display:flex; gap:8px; margin:12px 13px 0;
  pointer-events:auto }
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
.apply-form { position:relative; z-index:3; display:grid; gap:8px; margin:12px 13px 0;
  pointer-events:auto }
.apply-form[hidden] { display:none }
.apply-input { min-height:44px; padding:0 12px; border:1px solid var(--line-strong);
  border-radius:11px; color:var(--fg); background:var(--surface); font-size:16px }
.apply-input::placeholder { color:var(--muted-2); opacity:1 }
.apply-input:focus-visible { outline:3px solid var(--violet); outline-offset:2px }
.apply-submit { justify-self:start }
.doc-field { display:grid; gap:6px; padding:8px; border:1px dashed var(--line-strong);
  border-radius:11px; transition:border-color .15s ease, background .15s ease }
.doc-field.doc-dragover { border-color:var(--accent); background:var(--accent-soft) }
.doc-label { color:var(--muted); font-size:.68rem; font-weight:700; letter-spacing:.03em;
  text-transform:uppercase }
.doc-row { display:flex; gap:8px }
.doc-select { flex:1; min-width:0 }
.doc-upload-btn { flex:none; min-height:44px; padding:0 14px; border:1px solid var(--line);
  border-radius:11px; color:var(--fg); background:var(--surface); font-size:.71rem; font-weight:700;
  letter-spacing:.02em; cursor:pointer; transition:border-color .15s ease, background .15s ease }
.doc-upload-btn:focus-visible { outline:3px solid var(--violet); outline-offset:2px }
.doc-label-prompt { display:flex; gap:8px }
.doc-label-prompt[hidden] { display:none }
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
  const cardToggles = [...document.querySelectorAll('.card-toggle, .content-toggle')];
  cardToggles.forEach(button => {
    button.addEventListener('click', () => {
      const expanded = button.getAttribute('aria-expanded') === 'true';
      const panel = document.getElementById(button.getAttribute('aria-controls'));
      button.setAttribute('aria-expanded', expanded ? 'false' : 'true');
      if (panel) panel.hidden = expanded;
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
  [...document.querySelectorAll('.action-apply')].forEach(button => {
    button.addEventListener('click', () => {
      const expanded = button.getAttribute('aria-expanded') === 'true';
      const form = document.getElementById(button.getAttribute('aria-controls'));
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
    links = []
    for key, href, label in _TRACK_TABS:
        current = ' aria-current="page"' if key == track else ""
        links.append(
            f'<a class="track-tab{" active" if key == track else ""}" '
            f'href="{href}"{current}>{html.escape(label)}</a>'
        )
    return f'<nav class="track-tabs" aria-label="Piste métier">{"".join(links)}</nav>'


def _page_template(*, body, total, new_count, seen_count, applied_count, stamp, track) -> str:
    return f"""<!DOCTYPE html>
<html lang="fr" data-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#090b10" id="theme-color">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>jobwatch · tableau de bord</title>
<script>
(function () {{
  try {{
    const saved = localStorage.getItem('jw-theme');
    document.documentElement.dataset.theme = saved === 'light' ? 'light' : 'dark';
  }} catch (_) {{
    document.documentElement.dataset.theme = 'dark';
  }}
}})();
</script>
<style>
{_CSS}</style></head><body>
<div class="ambient" aria-hidden="true"></div>
<div class="shell">
  <header>
    <div class="topbar">
      <div class="identity">
        <div class="monogram" aria-hidden="true">JW</div>
        <div class="identity-copy"><span class="identity-name">jobwatch</span>
          <span class="identity-sub">Suivi de vos offres</span></div>
      </div>
      <button class="theme-toggle" id="theme-toggle" type="button" aria-label="Passer au thème clair">
        <svg class="icon-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
          <circle cx="12" cy="12" r="3.7"/><path d="M12 2v2.1M12 19.9V22M4.93 4.93l1.49 1.49M17.58 17.58l1.49 1.49M2 12h2.1M19.9 12H22M4.93 19.07l1.49-1.49M17.58 6.42l1.49-1.49"/>
        </svg>
        <svg class="icon-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
          <path d="M20.2 15.1A8.4 8.4 0 0 1 8.9 3.8 8.5 8.5 0 1 0 20.2 15.1Z"/>
        </svg>
      </button>
    </div>
    <div class="hero">
      <p class="eyebrow">Tableau de bord</p>
      <h1>Vos offres,<br><span>sous contrôle.</span></h1>
      <p class="hero-meta"><span class="live-dot" aria-hidden="true"></span>
        Mis à jour le {stamp}</p>
    </div>
    {_track_nav(track)}
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
<script>
{_JS}</script></body></html>
"""
