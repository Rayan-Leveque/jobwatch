"""Extraction du bloc utile d'une page d'offre, en cascade.

Une page d'offre est surtout du décor : navigation, bandeau cookies, pied de
page, « offres similaires », discours d'entreprise. Convertir la page entière
en Markdown (ce que faisait `enrich` jusqu'ici) fait payer ce décor en tokens
à chaque résumé et noie le modèle, qui rend alors des étiquettes vagues
plutôt que ce que l'annonce dit vraiment.

La cascade essaie, dans l'ordre :

1. les données structurées `schema.org/JobPosting` (`ld+json`) quand la page
   en publie - c'est la description officielle, plus quelques champs déjà
   normalisés (expérience, salaire, télétravail) qu'on récupère sans aucun
   appel LLM ;
2. l'extraction de contenu principal par trafilatura ;
3. la conversion brute de la page, comportement historique.

Un garde-fou décide du repli : si le texte brut porte un marqueur à forte
valeur (années d'expérience, salaire, télétravail) que le texte extrait a
perdu, l'extraction a mangé de l'information utile et on garde le brut. Le
garde-fou ne se trompe donc jamais qu'en faveur de l'ancien comportement :
son coût est en tokens, jamais en qualité.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

import trafilatura
from lxml import etree
from lxml import html as lxml_html
from markdownify import markdownify

log = logging.getLogger(__name__)

# En dessous, un texte extrait est trop court pour être une annonce crédible.
MIN_EXTRACT_LENGTH = 400

# Marqueurs à forte valeur pour les quatre champs du résumé. Ils servent
# uniquement au garde-fou de rétention : perdre un marqueur présent dans le
# brut disqualifie l'extraction.
MARKER_PATTERNS = {
    "experience": re.compile(
        r"\b\d+\s*(?:[-–à/]\s*\d+\s*)?(?:ans?|années?|years?)\b|\bexp[ée]rience\b|\bseniorit",
        re.IGNORECASE,
    ),
    "salary": re.compile(
        r"\b\d{2,3}\s?[kK]\s?[€$£]|\b\d{2,3}[\s.]?000\s?(?:€|\$|EUR|USD)"
        r"|\bsalaire\b|\br[ée]mun[ée]ration\b|\bsalary\b",
        re.IGNORECASE,
    ),
    "remote": re.compile(
        r"\bt[ée]l[ée]travail\b|\bremote\b|\bhybride\b|\bpr[ée]sentiel\b|\bsur site\b|\bon-?site\b",
        re.IGNORECASE,
    ),
}

# Faits chiffrés que les sites placent souvent HORS du corps de l'annonce, donc
# hors de portée d'un extracteur de contenu principal : LinkedIn affiche par
# exemple « Base pay range €38,000.00/yr - €65,000.00/yr » dans son bandeau
# latéral. Les perdre serait une régression ; retomber sur la page entière pour
# si peu en serait une autre, en tokens. On les repêche donc ligne à ligne.
SALVAGE_PATTERNS = (
    re.compile(r"[€$£]\s?\d[\d\s.,]{2,}|\d[\d\s.,]{2,}\s?(?:€|\$|£|EUR|USD)\b|\b\d{2,3}\s?[kK]\s?[€$£]"),
    re.compile(r"\b\d+\s*(?:[-–à/]\s*\d+\s*)?(?:ans|années|years)\b\s*(?:d['’]|of\s)?\s*"
               r"(?:exp[ée]rience|experience)", re.IGNORECASE),
)
MAX_SALVAGED_LINES = 5
MAX_SALVAGED_LINE_LENGTH = 200
SALVAGE_HEADING = "\n\n**Repéré hors du corps de l'annonce**\n"

_JOBPOSTING_FIELD_LABELS = {
    "experience": ("experienceRequirements",),
    "salary": ("baseSalary", "estimatedSalary"),
    "remote": ("jobLocationType",),
    "stack": ("skills", "qualifications"),
}


@dataclass
class Extraction:
    """Texte retenu pour une page, et d'où il vient."""

    markdown: str
    #: "jsonld", "readable" ou "raw" (repli, comportement historique).
    method: str
    #: Champs déjà normalisés lus dans le JSON-LD, sans aucun appel LLM.
    fields: dict[str, str] = field(default_factory=dict)
    #: Marqueurs présents dans le brut qu'une extraction candidate a perdus.
    lost_markers: tuple[str, ...] = ()
    #: Taille de la page entière, pour chiffrer le bruit retiré dans les logs.
    raw_chars: int = 0
    #: Lignes repêchées hors du corps de l'annonce (salaires isolés, etc.).
    salvaged_lines: int = 0

    @property
    def degraded(self) -> bool:
        """Vrai quand on a dû garder la page entière faute de mieux."""
        return self.method == "raw"


def extract(html: str) -> Extraction:
    """Renvoie le meilleur texte disponible pour cette page, jamais moins que le brut."""
    raw = markdownify(html).strip() if html else ""
    document = _parse(html)

    posting = _job_posting(document) if document is not None else None
    fields = _jsonld_fields(posting) if posting else {}

    jsonld = _jsonld_markdown(posting) if posting else ""
    if _accepted(raw, jsonld, "jsonld"):
        markdown, salvaged = _with_salvage(jsonld, raw)
        return Extraction(
            markdown=markdown,
            method="jsonld",
            fields=fields,
            raw_chars=len(raw),
            salvaged_lines=salvaged,
        )

    # trafilatura est l'étape la plus coûteuse : elle n'est lancée que si
    # JSON-LD n'a pas gagné, et son résultat sert aussi au repli.
    readable = _readable_markdown(html)
    if _accepted(raw, readable, "readable"):
        markdown, salvaged = _with_salvage(readable, raw)
        return Extraction(
            markdown=markdown,
            method="readable",
            fields=fields,
            raw_chars=len(raw),
            salvaged_lines=salvaged,
        )

    return Extraction(
        markdown=raw,
        method="raw",
        fields=fields,
        lost_markers=_lost_markers(raw, readable),
        raw_chars=len(raw),
    )


def _accepted(raw: str, candidate: str, method: str) -> bool:
    """Vrai quand le candidat est assez long et ne perd aucun marqueur du brut."""
    if len(candidate) < MIN_EXTRACT_LENGTH:
        return False
    lost = _lost_markers(raw, candidate)
    if lost:
        log.debug("extraction: %s rejetée, marqueurs perdus: %s", method, ", ".join(lost))
        return False
    return True


def _parse(html: str) -> lxml_html.HtmlElement | None:
    if not html:
        return None
    try:
        return lxml_html.fromstring(html)
    except (etree.ParserError, ValueError) as exc:
        log.debug("extraction: HTML illisible: %s", exc)
        return None


def _lost_markers(raw: str, candidate: str) -> tuple[str, ...]:
    """Marqueurs présents dans le brut et absents du candidat."""
    return tuple(
        name
        for name, pattern in MARKER_PATTERNS.items()
        if pattern.search(raw) and not pattern.search(candidate)
    )


def _with_salvage(candidate: str, raw: str) -> tuple[str, int]:
    """Réinjecte les faits chiffrés du brut que l'extraction a laissés de côté.

    Quelques lignes courtes coûtent une poignée de tokens, là où garder la page
    entière pour ne pas les perdre en coûterait des milliers. Renvoie le texte
    et le nombre de lignes repêchées, que les logs affichent.
    """
    salvaged: list[str] = []
    seen = {_squeeze(line) for line in candidate.splitlines()}
    for line in raw.splitlines():
        line = line.strip(" \t*#>-")
        if not line or len(line) > MAX_SALVAGED_LINE_LENGTH:
            continue
        squeezed = _squeeze(line)
        if not squeezed or squeezed in seen:
            continue
        if not any(pattern.search(line) for pattern in SALVAGE_PATTERNS):
            continue
        if squeezed in _squeeze(candidate):
            continue
        salvaged.append(line)
        seen.add(squeezed)
        if len(salvaged) >= MAX_SALVAGED_LINES:
            break
    if not salvaged:
        return candidate, 0
    lines = "\n".join(f"- {line}" for line in salvaged)
    return candidate + SALVAGE_HEADING + lines, len(salvaged)


def _squeeze(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _readable_markdown(html: str) -> str:
    if not html:
        return ""
    try:
        text = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
            output_format="markdown",
        )
    except Exception as exc:  # noqa: BLE001 - une page tordue ne doit jamais casser le run
        log.debug("extraction: trafilatura a échoué: %s", exc)
        return ""
    return (text or "").strip()


# --- schema.org/JobPosting ------------------------------------------------


def _job_posting(document: lxml_html.HtmlElement) -> dict | None:
    """Premier objet JobPosting trouvé dans les blocs ld+json de la page."""
    for script in document.xpath('//script[@type="application/ld+json"]'):
        try:
            data = json.loads(script.text_content())
        except ValueError:
            continue
        for obj in _walk(data):
            types = obj.get("@type") or obj.get("@Type") or ""
            if "JobPosting" in (types if isinstance(types, str) else " ".join(map(str, types))):
                return obj
    return None


def _walk(data: object) -> list[dict]:
    """Aplatit listes et `@graph` en une liste d'objets."""
    found: list[dict] = []
    if isinstance(data, list):
        for item in data:
            found += _walk(item)
    elif isinstance(data, dict):
        found.append(data)
        found += _walk(data.get("@graph"))
    return found


def _get(obj: dict, name: str) -> object:
    """Lecture insensible à la casse : certaines pages écrivent `Description`."""
    lowered = name.lower()
    for key, value in obj.items():
        if isinstance(key, str) and key.lower() == lowered:
            return value
    return None


def _jsonld_markdown(posting: dict) -> str:
    """Description officielle de l'offre, précédée de son en-tête factuel."""
    description = _get(posting, "description")
    if not isinstance(description, str) or not description.strip():
        return ""
    header = []
    for label, key in (("Titre", "title"), ("Contrat", "employmentType"), ("Lieu", "jobLocation")):
        value = _flatten(_get(posting, key))
        if value:
            header.append(f"**{label}** : {value}")
    company = _flatten(_get(_as_dict(_get(posting, "hiringOrganization")) or {}, "name"))
    if company:
        header.insert(1, f"**Société** : {company}")
    body = markdownify(description).strip() if "<" in description else description.strip()
    return "\n".join([*header, "", body]).strip()


def _jsonld_fields(posting: dict) -> dict[str, str]:
    """Champs du résumé déjà normalisés par la page, donc gratuits et sûrs."""
    fields: dict[str, str] = {}
    for key, names in _JOBPOSTING_FIELD_LABELS.items():
        for name in names:
            value = _get(posting, name)
            rendered = _salary(value) if key == "salary" else _flatten(value)
            if rendered:
                fields[key] = rendered
                break
    if fields.get("remote", "").upper() == "TELECOMMUTE":
        fields["remote"] = "Télétravail (jobLocationType : TELECOMMUTE)"
    return fields


def _as_dict(value: object) -> dict | None:
    return value if isinstance(value, dict) else None


def _flatten(value: object, depth: int = 0) -> str:
    """Rend lisible une valeur JSON-LD, qui peut être imbriquée ou multiple."""
    if depth > 3 or value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return re.sub(r"\s+", " ", markdownify(value) if "<" in value else value).strip()
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        parts = [_flatten(item, depth + 1) for item in value]
        return ", ".join(part for part in parts if part)
    if isinstance(value, dict):
        for name in ("name", "value", "monthsOfExperience", "description", "addressLocality"):
            rendered = _flatten(_get(value, name), depth + 1)
            if rendered:
                if name == "monthsOfExperience":
                    return f"{rendered} mois d'expérience"
                return rendered
    return ""


def _salary(value: object) -> str:
    """Rend une fourchette `MonetaryAmount` sous forme lisible."""
    amount = _as_dict(value)
    if amount is None:
        return _flatten(value)
    currency = _flatten(_get(amount, "currency")) or _flatten(_get(amount, "salaryCurrency"))
    inner = _as_dict(_get(amount, "value")) or amount
    low = _flatten(_get(inner, "minValue"))
    high = _flatten(_get(inner, "maxValue"))
    single = _flatten(_get(inner, "value")) if inner is not amount else ""
    unit = _flatten(_get(inner, "unitText"))
    figure = f"{low} - {high}" if low and high else (low or high or single)
    if not figure:
        return ""
    return " ".join(part for part in (figure, currency, unit.lower() if unit else "") if part)
