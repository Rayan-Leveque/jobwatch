"""Send a digest of new matches via ntfy and/or SMTP, then stamp notified_at."""

from __future__ import annotations

import logging
import smtplib
import sqlite3
from email.message import EmailMessage

import httpx

from jobwatch.config import Config, SmtpConfig

log = logging.getLogger(__name__)

NTFY_URL = "https://ntfy.sh/{topic}"
SUBJECT = "jobwatch: {count} new offers"


def _collect_unnotified(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    rows = conn.execute(
        "SELECT m.id AS match_id, s.name AS search_name, c.name AS company, o.title AS title, "
        "       o.location AS location, o.url AS url "
        "FROM match m "
        "JOIN search s ON s.id = m.search_id "
        "JOIN offer o ON o.id = m.offer_id "
        "JOIN company c ON c.id = o.company_id "
        "WHERE m.notified_at IS NULL AND m.state = 'new' "
        "ORDER BY s.name, m.id"
    ).fetchall()
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(str(row["search_name"]), []).append(row)
    return groups


def format_digest(groups: dict[str, list[sqlite3.Row]]) -> str:
    """Build a plain-text digest grouped by search, one line per offer."""
    lines: list[str] = []
    for search_name in sorted(groups):
        lines.append(f"[{search_name}]")
        for row in groups[search_name]:
            company = row["company"]
            title = row["title"]
            location = row["location"]
            url = row["url"]
            where = f" ({location})" if location else ""
            lines.append(f"{company} - {title}{where} {url}")
    return "\n".join(lines) + "\n"


def _send_ntfy(topic: str, body: str, count: int, client: httpx.Client) -> bool:
    try:
        response = client.post(
            NTFY_URL.format(topic=topic),
            content=body,
            headers={"Title": SUBJECT.format(count=count)},
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("ntfy notification failed: %s", exc)
        return False
    return True


def _send_smtp(cfg: SmtpConfig, body: str, count: int) -> bool:
    message = EmailMessage()
    message["Subject"] = SUBJECT.format(count=count)
    message["From"] = cfg.user
    message["To"] = cfg.to
    message.set_content(body)
    try:
        with smtplib.SMTP(cfg.host, cfg.port, timeout=30) as smtp:
            try:
                smtp.starttls()
            except smtplib.SMTPException:
                log.debug("smtp server does not support STARTTLS, continuing without")
            smtp.login(cfg.user, cfg.password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        log.warning("smtp notification failed: %s", exc)
        return False
    return True


def send_digest(
    conn: sqlite3.Connection, config: Config, client: httpx.Client | None = None
) -> list[str]:
    """Send digests of unnotified new matches.

    Returns the list of channel names that were used successfully. Matches are
    stamped notified_at only when at least one channel succeeded.
    """
    groups = _collect_unnotified(conn)
    count = sum(len(rows) for rows in groups.values())
    if count == 0:
        log.info("0 new matches")
        return []

    body = format_digest(groups)
    used: list[str] = []

    if config.notify.ntfy is not None:
        owned_client = client is None
        http_client = client if client is not None else httpx.Client(timeout=30.0)
        try:
            if _send_ntfy(config.notify.ntfy.topic, body, count, http_client):
                used.append("ntfy")
        finally:
            if owned_client:
                http_client.close()

    if config.notify.smtp is not None and _send_smtp(config.notify.smtp, body, count):
        used.append("smtp")

    if used:
        match_ids = [int(r["match_id"]) for rows in groups.values() for r in rows]
        for match_id in match_ids:
            conn.execute("UPDATE match SET notified_at = datetime('now') WHERE id = ?", (match_id,))
        conn.commit()
        log.info("notified %d new matches via %s", count, ", ".join(used))

    return used
