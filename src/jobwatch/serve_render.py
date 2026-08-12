"""Rendu HTML pur du tableau de bord et de la page de tri.

Fonctions extraites de jobwatch.serve : `render_page` et `render_swipe_page`
transforment l'état SQLite (via jobwatch.serve_queries) en HTML complet, sans
toucher au serveur HTTP.
"""

from __future__ import annotations

import html
import re
import sqlite3
import unicodedata
from datetime import UTC, datetime
from urllib.parse import urlsplit

from jobwatch.enrich import MAX_FETCH_ATTEMPTS
from jobwatch.library import list_library
from jobwatch.serve_queries import (
    FIELD_LABELS,
    Summary,
    _applications,
    _batch_eligible_ids,
    _discarded_matches,
    _draft_rows,
    _later_matches,
    _matches,
    _offer_content_failures,
    _offer_contents,
    _priority_matches,
    _summary_bullets,
    _swipe_deck,
)
from jobwatch.serve_templates import _page_template, _swipe_page_template

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


def _short_date(value: str) -> str:
    """2026-08-05 12:00 -> « 5 août » ; garde tel quel si non ISO."""
    m = _DATE_RE.match(value)
    if m:
        return f"{int(m.group(3))} {_MONTHS.get(int(m.group(2)), m.group(2))}"
    return value


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
    return (
        f'<a class="offer-link" href="{escaped}" target="_blank" '
        'rel="noopener noreferrer" aria-label="Voir l’offre externe">'
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" '
        'aria-hidden="true"><path d="M14 5h5v5"/><path d="m19 5-8 8"/>'
        '<path d="M18 13v5a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>'
        "</svg></a>"
    )


def _meta(
    row: sqlite3.Row,
    date_label: str,
    date: object,
    search_name: object = None,
    deadline: object = None,
) -> str:
    """Ligne informative : plateforme, lieu, contrat, deadline, recherche et date."""
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


def _summary_provenance_html(summary: Summary) -> str:
    if summary.source == "metadata":
        label = "Résumé limité - basé uniquement sur les métadonnées enregistrées"
        return f'<p class="summary-provenance limited">{label}</p>'
    if summary.source == "auto":
        return '<p class="summary-provenance grounded">Résumé basé sur le texte de l’annonce</p>'
    return '<p class="summary-provenance manual">Résumé importé ou saisi manuellement</p>'


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
        f"{_summary_provenance_html(summary)}"
        f"{_summary_fields_html(summary.fields)}{bullets_html}</div>"
    )
    return button, panel


_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")


# Marqueur de titre sans texte (ex. "#### " suivi d'un logo image que markdownify
# a réduit à rien) : rien à afficher, on l'ignore comme une ligne blanche plutôt
# que de laisser "####" apparaître littéralement.
_MD_BARE_HEADING_RE = re.compile(r"^#{1,6}$")


# Titre "souligné" (markdownify produit ce style par défaut pour les h1/h2,
# ex. "Titre\n===" ou "Titre\n---") : seulement reconnu juste après une ligne
# de texte non vide, jamais après une ligne blanche (sinon ce serait plutôt
# une séparation visuelle sans rapport avec un titre).
_MD_SETEXT_RE = re.compile(r"^(?:=+|-{2,})$")


# Séparateur horizontal (ligne de ---, *** ou ___ seule, comme sur Obsidian) :
# reconnu seulement quand la boucle l'atteint comme ligne "courante", c'est-à-
# dire quand elle n'a pas déjà été absorbée comme soulignement de titre par
# le lookahead ci-dessus (qui la consomme avant qu'elle devienne "courante").
_MD_HR_RE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")


_MD_UL_RE = re.compile(r"^[-*]\s+(.+)$")


_MD_OL_RE = re.compile(r"^\d+\.\s+(.+)$")


# Quotes du titre optionnel déjà html.escape()-ées (&quot;/&#x27;) au moment où
# ce motif s'applique : le texte est échappé avant tout formatage (voir plus bas).
_MD_TITLE = r"(?:&quot;.*?&quot;|&#x27;.*?&#x27;)"


_MD_TITLE_SUFFIX = r"(?:\s+" + _MD_TITLE + r")?"


# Une URL réelle peut contenir une paire de parenthèses (ex. une page
# gouvernementale ".../ingenieur(e)") ou des espaces bruts (ex. un lien
# mailto:?subject=... non encodé) : on tolère un niveau de parenthèses
# imbriquées et les espaces, la sécurité venant du filtre de schéma
# http(s) dans _render_anchor, pas de la forme de l'URL elle-même. Un espace
# n'est consommé dans l'URL que s'il n'amorce pas un titre optionnel (sinon
# l'URL gloutonne avalerait le titre avant que _MD_TITLE_SUFFIX ne le voie).
_MD_URL = r"(?:[^()\s]|\([^()]*\)|\s(?!" + _MD_TITLE + r"\)))*"


# Logo/illustration cliquable : [![alt](image)](lien) -> lien texté avec l'alt.
_MD_LINKED_IMAGE_RE = re.compile(
    r"\[!\[([^\]]*)\]\(" + _MD_URL + r"\)\]\((" + _MD_URL + r")" + _MD_TITLE_SUFFIX + r"\)"
)


# Image seule, jamais affichée (pas de support image) : on ne garde que l'alt.
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(" + _MD_URL + _MD_TITLE_SUFFIX + r"\)")


_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((" + _MD_URL + r")" + _MD_TITLE_SUFFIX + r"\)")


_MD_BOLD_RE = re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*")


_MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)")


def _render_anchor(label: str, url: str) -> str:
    """Lien réel seulement en http(s) ; sinon juste le texte, sans crochets/URL.

    Le contenu réel des offres regorge de liens de navigation relatifs ou en
    ancre (`#main-content`, `/fr/companies`...) issus de la page complète
    scrapée : les garder cliquables serait inutile (URL relative à nulle
    part) et les laisser en syntaxe Markdown littérale ([texte](url)) donne
    une impression de rendu cassé. Le texte seul est le repli le plus propre.
    """
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return label
    if scheme not in ("http", "https"):
        return label
    return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{label}</a>'


def _format_inline(text: str) -> str:
    """Échappe une ligne puis applique gras/italique/liens Markdown dessus.

    L'échappement précède le formatage : les caractères Markdown (*, [, ], (,
    )) traversent html.escape intacts, donc les regex ci-dessous opèrent en
    toute sécurité sur du texte déjà échappé sans jamais réinjecter de HTML
    fourni par l'offre. Les images (seules ou cliquables, ex. logo d'entreprise
    lié à son site) ne sont jamais affichées, faute de support image : un logo
    cliquable devient un lien texté avec son alt, une image isolée devient son
    alt en texte brut. Doit tourner avant _MD_LINK_RE, qui matcherait sinon
    à l'intérieur des crochets imbriqués et laisserait des fragments cassés.
    """
    escaped = html.escape(text)
    escaped = _MD_LINKED_IMAGE_RE.sub(
        lambda match: _render_anchor(match.group(1), match.group(2)), escaped
    )
    escaped = _MD_IMAGE_RE.sub(lambda match: match.group(1), escaped)
    escaped = _MD_LINK_RE.sub(
        lambda match: _render_anchor(match.group(1), match.group(2)), escaped
    )
    escaped = _MD_BOLD_RE.sub(r"<strong>\1</strong>", escaped)
    escaped = _MD_ITALIC_RE.sub(r"<em>\1</em>", escaped)
    return escaped


def _markdown_to_html(markdown: str) -> str:
    """Rendu minimal d'un sous-ensemble Markdown : titres, gras, italique, listes, liens.

    Pas de bibliothèque Markdown supplémentaire pour le dashboard : toute
    syntaxe non reconnue (tableaux, citations, code...) reste affichée telle
    quelle, échappée. Les titres deviennent des paragraphes stylés (pas de
    vraies balises <h1>-<h6>, pour ne pas percuter la hiérarchie de titres de
    la carte) et les listes restent à plat, sans imbrication.
    """
    blocks: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    list_tag: str | None = None

    def flush_paragraph() -> None:
        if paragraph:
            content = "<br>".join(_format_inline(line) for line in paragraph)
            blocks.append(f"<p>{content}</p>")
            paragraph.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if list_items:
            items = "".join(f"<li>{item}</li>" for item in list_items)
            blocks.append(f"<{list_tag}>{items}</{list_tag}>")
            list_items.clear()
        list_tag = None

    lines = markdown.strip().splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line or _MD_BARE_HEADING_RE.match(line):
            flush_paragraph()
            flush_list()
            continue
        heading_match = _MD_HEADING_RE.match(line)
        if heading_match:
            flush_paragraph()
            flush_list()
            blocks.append(f'<p class="md-heading">{_format_inline(heading_match.group(2))}</p>')
            continue
        if _MD_HR_RE.match(line):
            flush_paragraph()
            flush_list()
            blocks.append("<hr>")
            continue
        ul_match = _MD_UL_RE.match(line)
        ol_match = _MD_OL_RE.match(line) if not ul_match else None
        if not (ul_match or ol_match) and index < len(lines) and _MD_SETEXT_RE.match(lines[index].strip()):
            flush_paragraph()
            flush_list()
            blocks.append(f'<p class="md-heading">{_format_inline(line)}</p><hr>')
            index += 1
            continue
        if ul_match or ol_match:
            flush_paragraph()
            tag = "ul" if ul_match else "ol"
            if list_tag and list_tag != tag:
                flush_list()
            list_tag = tag
            item_text = ul_match.group(1) if ul_match else ol_match.group(1)
            list_items.append(_format_inline(item_text))
            continue
        flush_list()
        paragraph.append(line)

    flush_paragraph()
    flush_list()
    return "".join(blocks)


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


def _edit_body_button(match_id: int, *, hidden: bool) -> str:
    hidden_attr = " hidden" if hidden else ""
    return (
        f'<button class="card-action action-edit-body" type="button" '
        f'data-match-id="{match_id}"{hidden_attr}>Modifier le texte</button>'
    )


def _body_editor(match_id: int) -> str:
    return (
        f'<div class="body-editor" id="body-editor-{match_id}" hidden>'
        '<textarea class="apply-input body-editor-textarea" rows="10" '
        'aria-label="Texte de la lettre"></textarea>'
        '<p class="body-editor-status" aria-live="polite"></p>'
        '<div class="body-editor-actions">'
        '<button class="card-action body-editor-save" type="button">Enregistrer</button>'
        '<button class="card-action body-editor-cancel" type="button">Annuler</button>'
        "</div></div>"
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
        f'<div class="letter-panel" id="{panel_id}" hidden>{area}'
        f'{_edit_body_button(match_id, hidden=status != "ok")}{_body_editor(match_id)}'
        f'{compose}{_draft_form(match_id, track, cv_rows)}</div>'
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
    link = _link(row["url"])
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
        f'<article class="row row-{cls}" '
        f'data-search="{html.escape(_search_haystack(row), quote=True)}"><div class="body">'
        f'<div class="card-topline"><div class="company">{company}</div>'
        f'<div class="card-badges">{pill}{link}</div></div>'
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
    link = _link(row["url"])
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
        f'<article class="row row-applied" '
        f'data-search="{html.escape(_search_haystack(row), quote=True)}"><div class="body">'
        f'<div class="card-topline"><div class="company">{company}</div>'
        f'<div class="card-badges">{pill}{link}</div></div>'
        f'<div class="role">{title}</div>'
        f'<div class="meta">{meta}</div>{note}</div>{reader}</article>'
    )


def _search_haystack(row: sqlite3.Row) -> str:
    """Attribut data-search d'une carte : ce sur quoi la recherche doit porter.

    Le filtre lisait le textContent de la carte entière, donc aussi les
    <option> des menus « Candidater » et « Générer LM ». Ces menus listent
    toute la bibliothèque : chercher une société dont on a déjà généré une
    lettre faisait ressortir toutes les cartes portant un formulaire, et pas
    l'offre voulue. On expose donc explicitement les quatre champs annoncés
    par le placeholder : entreprise, poste, lieu, recherche.
    """
    parts = [
        str(row["company"] or ""),
        str(row["title"] or ""),
        str(row["location"] or ""),
        str(row["platform"] or ""),
        str(row["search_name"] or ""),
    ]
    joined = " ".join(part for part in parts if part)
    # Même normalisation que côté JS : minuscules sans accents, pour que la
    # comparaison soit un simple includes().
    folded = unicodedata.normalize("NFD", joined.casefold())
    return "".join(c for c in folded if not unicodedata.combining(c))


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


def _unavailable_announcement_html(failure: tuple[str, int] | None) -> str:
    if failure is None:
        message = "Annonce complète pas encore récupérée. Aucun texte d’annonce n’a été inventé."
    else:
        reason, attempts = failure
        if reason in ("http_404", "http_410"):
            code = reason.removeprefix("http_")
            message = f"Annonce complète indisponible : l’offre a été retirée (HTTP {code})."
        elif attempts >= MAX_FETCH_ATTEMPTS:
            message = (
                f"Annonce complète indisponible après {attempts} tentatives. "
                "Aucun texte d’annonce n’a été inventé."
            )
        else:
            message = (
                "Annonce complète temporairement indisponible : une nouvelle tentative est prévue."
            )
    return f'<p class="content-unavailable">{html.escape(message)}</p>'


def _swipe_card(
    row: sqlite3.Row,
    summary: Summary,
    content: str | None,
    content_failure: tuple[str, int] | None,
) -> str:
    company = html.escape(str(row["company"] or "Société inconnue"))
    title = html.escape(str(row["title"] or ""))
    pill = _fit_pill(row["fit"])
    link = _link(row["url"])
    meta = _meta(row, "collecté le", row["collected_at"], row["search_name"], row["deadline"])
    summary_html = ""
    if summary:
        items = "".join(f"<li>{html.escape(bullet)}</li>" for bullet in summary.bullets)
        bullets_html = f"<ul>{items}</ul>" if items else ""
        summary_html = (
            '<div class="swipe-summary"><div class="summary-title">En bref</div>'
            f"{_summary_provenance_html(summary)}"
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
        f'<div class="card-badges">{pill}{link}</div></div>'
        f'<div class="role">{title}</div>'
        f'<div class="meta">{meta}</div>'
        f"{summary_html}{content_html}"
        f"{_unavailable_announcement_html(content_failure) if not content else ''}"
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
    failures = _offer_content_failures(conn, offer_ids)
    cards = "\n".join(
        _swipe_card(
            row,
            summaries.get(int(row["offer_id"])) or Summary(),
            contents.get(int(row["offer_id"])),
            failures.get(int(row["offer_id"])),
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
