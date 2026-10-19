"""Lecture des emails de vérification récents pour l'agent de candidature (`jw mail-code`).

Les ATS (Workday, Taleo...) envoient un code ou un lien avant d'ouvrir le
formulaire. L'agent ne voit jamais les identifiants IMAP : il lance cette
commande, qui ne rend que les messages reçus dans une courte fenêtre.
"""

from __future__ import annotations

import datetime
import email
import html
import imaplib
import re
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime

from jobwatch.config import ImapConfig

MAX_MESSAGES = 5
MAX_BODY_CHARS = 2_000

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


@dataclass
class MailSummary:
    received: datetime.datetime
    sender: str
    subject: str
    text: str


def _header(value: str | None) -> str:
    return str(make_header(decode_header(value or "")))


def _body_text(message: Message) -> str:
    """Texte brut du message ; la partie HTML sert de repli, sans balises."""
    plain, rich = "", ""
    for part in message.walk():
        if part.is_multipart():
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_type() == "text/plain" and not plain:
            plain = text
        elif part.get_content_type() == "text/html" and not rich:
            # Les liens de vérification vivent dans href : on les garde visibles.
            rich = re.sub(r'<a\s[^>]*href="([^"]+)"[^>]*>', r" \1 ", text, flags=re.IGNORECASE)
            rich = html.unescape(_TAG_RE.sub(" ", rich))
    return _SPACE_RE.sub(" ", plain or rich).strip()[:MAX_BODY_CHARS]


def parse_message(raw: bytes) -> MailSummary | None:
    message = email.message_from_bytes(raw)
    try:
        received = parsedate_to_datetime(message.get("Date"))
    except (TypeError, ValueError):
        return None
    if received.tzinfo is None:
        received = received.replace(tzinfo=datetime.UTC)
    return MailSummary(
        received=received,
        sender=_header(message.get("From")),
        subject=_header(message.get("Subject")),
        text=_body_text(message),
    )


def recent_messages(
    config: ImapConfig, minutes: int, now: datetime.datetime | None = None
) -> list[MailSummary]:
    """Messages de la boîte de réception reçus depuis `minutes`, du plus récent au plus ancien."""
    now = now or datetime.datetime.now(tz=datetime.UTC)
    cutoff = now - datetime.timedelta(minutes=minutes)
    since = (cutoff - datetime.timedelta(days=1)).strftime("%d-%b-%Y")
    with imaplib.IMAP4_SSL(config.host, config.port) as client:
        client.login(config.user, config.password)
        client.select("INBOX", readonly=True)
        _status, data = client.search(None, "SINCE", since)
        ids = data[0].split()[-30:] if data and data[0] else []
        found = []
        for message_id in reversed(ids):
            _status, parts = client.fetch(message_id, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            summary = parse_message(raw) if isinstance(raw, bytes) else None
            if summary is not None and summary.received >= cutoff:
                found.append(summary)
            if len(found) >= MAX_MESSAGES:
                break
    return sorted(found, key=lambda m: m.received, reverse=True)


def format_messages(messages: list[MailSummary]) -> str:
    if not messages:
        return "aucun email reçu dans la fenêtre demandée\n"
    blocks = [
        f"## {m.received.astimezone():%H:%M:%S} - {m.sender}\nObjet : {m.subject}\n\n{m.text}"
        for m in messages
    ]
    return "\n\n".join(blocks) + "\n"
