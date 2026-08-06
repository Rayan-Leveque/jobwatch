"""click CLI for jobwatch."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import click

from jobwatch import __version__
from jobwatch.collectors import build_collectors
from jobwatch.collectors.base import store_offers
from jobwatch.config import Config, ConfigError, example_config_text, load_config
from jobwatch.db import connect, init_db
from jobwatch.digest import send_digest
from jobwatch.matching import run_matching, sync_searches

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = "config.yaml"
FALLBACK_CONFIG = "~/.config/jobwatch/config.yaml"

EVENT_TYPES = ("applied", "follow_up", "interview", "rejected", "offer")
MATCH_STATES = ("new", "seen", "applied", "discarded")


class CliError(Exception):
    """Expected failure with a clean user-facing message."""


def _fatal(message: str) -> None:
    click.echo(f"error: {message}", err=True)
    raise SystemExit(1)


def _resolve_config_path(explicit: str | None) -> Path:
    if explicit is not None:
        return Path(explicit)
    local = Path(DEFAULT_CONFIG)
    if local.exists():
        return local
    fallback = Path.home() / ".config" / "jobwatch" / "config.yaml"
    if fallback.exists():
        return fallback
    raise CliError(f"no config found (looked for {local} and {fallback}); run 'jw init' first")


def _require_config(explicit: str | None) -> Config:
    try:
        return load_config(_resolve_config_path(explicit))
    except (ConfigError, CliError) as exc:
        _fatal(str(exc))


def _open_db(config: Config) -> sqlite3.Connection:
    config.db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(config.db)
    init_db(conn)
    return conn


@click.group()
@click.version_option(version=__version__)
def cli() -> None:
    """jobwatch: self-hosted job-posting watcher."""


@cli.command()
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
def init(config_path: Path | None) -> None:
    """Create a config.yaml and an empty database, then print next steps."""
    target = config_path or Path(DEFAULT_CONFIG)
    if target.exists():
        _fatal(f"refusing to overwrite existing config {target}")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(example_config_text())
        config = load_config(target)
    except (OSError, ConfigError) as exc:
        _fatal(str(exc))

    conn = _open_db(config)
    conn.close()

    click.echo(f"created {target}")
    click.echo(f"initialized database at {config.db}")
    click.echo("next steps: edit config.yaml, then run 'jw run'")


@cli.command()
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
def run(config_path: Path | None) -> None:
    """Collect offers, match them against searches, send a digest."""
    config = _require_config(config_path)

    conn = _open_db(config)
    try:
        sync_searches(conn, config.searches)
        collected = 0
        for collector in build_collectors(config.sources):
            offers = collector.fetch()
            new_ids = store_offers(conn, collector.name, collector.name, offers)
            collected += len(new_ids)
            logger.info("collected %d new offers from %s", len(new_ids), collector.name)
        new_matches = run_matching(conn)
        channels = send_digest(conn, config)
    finally:
        conn.close()

    notified = f", notified via {', '.join(channels)}" if channels else ""
    click.echo(f"collected {collected} new offers, {len(new_matches)} new matches{notified}")


@cli.command("list")
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
@click.option("--state", "state", type=click.Choice(MATCH_STATES), default="new")
@click.option("--search", "search_name", default=None, help="filter by search name")
@click.option("--ack", is_flag=True, help="mark listed 'new' matches as 'seen'")
def list_matches(config_path: Path | None, state: str, search_name: str | None, ack: bool) -> None:
    """List matches, optionally acknowledging them as seen."""
    config = _require_config(config_path)
    conn = _open_db(config)
    try:
        query = (
            "SELECT m.id AS id, s.name AS search_name, c.name AS company, o.title AS title, "
            "       o.location AS location, m.state AS state, o.collected_at AS collected_at "
            "FROM match m "
            "JOIN search s ON s.id = m.search_id "
            "JOIN offer o ON o.id = m.offer_id "
            "JOIN company c ON c.id = o.company_id "
            "WHERE m.state = ?"
        )
        params: list[object] = [state]
        if search_name:
            query += " AND s.name = ?"
            params.append(search_name)
        query += " ORDER BY m.id"
        rows = conn.execute(query, params).fetchall()
        if ack and rows:
            conn.execute(
                "UPDATE match SET state = 'seen' WHERE state = 'new' AND id IN ("
                + ",".join("?" * len(rows))
                + ")",
                [row["id"] for row in rows],
            )
            conn.commit()
    finally:
        conn.close()

    _print_matches(rows)
    if ack and rows:
        click.echo(f"acknowledged {len(rows)} match(es) as seen")


def _print_matches(rows) -> None:
    header = (
        f"{'id':>5}  {'search':<16} {'company':<22} {'title':<45} "
        f"{'location':<18} {'state':<9} {'collected':<10}"
    )
    click.echo(header)
    for row in rows:
        click.echo(
            f"{int(row['id']):>5}  {_clip(row['search_name'], 16):<16} "
            f"{_clip(row['company'], 22):<22} {_clip(row['title'], 45):<45} "
            f"{_clip(row['location'], 18):<18} {_clip(row['state'], 9):<9} "
            f"{_clip(str(row['collected_at']), 10):<10}"
        )


def _clip(value: object, width: int) -> str:
    text = str(value) if value is not None else ""
    if len(text) <= width:
        return text
    return text[: width - 3] + "..."


@cli.command()
@click.argument("match_id", type=int)
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
def show(config_path: Path | None, match_id: int) -> None:
    """Show full details for a match."""
    config = _require_config(config_path)
    conn = _open_db(config)
    try:
        row = conn.execute(
            "SELECT m.id AS id, s.name AS search_name, m.state AS state, "
            "       c.name AS company, o.title AS title, o.url AS url, o.platform AS platform, "
            "       o.location AS location, o.contract AS contract, "
            "       o.published_at AS published_at, o.collected_at AS collected_at "
            "FROM match m "
            "JOIN search s ON s.id = m.search_id "
            "JOIN offer o ON o.id = m.offer_id "
            "JOIN company c ON c.id = o.company_id "
            "WHERE m.id = ?",
            (match_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        _fatal(f"no match with id {match_id}")
    for label, key in (
        ("id", "id"),
        ("search", "search_name"),
        ("state", "state"),
        ("company", "company"),
        ("title", "title"),
        ("url", "url"),
        ("platform", "platform"),
        ("location", "location"),
        ("contract", "contract"),
        ("published", "published_at"),
        ("collected", "collected_at"),
    ):
        click.echo(f"{label:<10} {row[key] if row[key] is not None else ''}")


def _require_match(conn, match_id: int):
    row = conn.execute(
        "SELECT m.id AS id, m.offer_id AS offer_id, m.state AS state, "
        "       s.name AS search_name, c.name AS company, o.title AS title "
        "FROM match m "
        "JOIN search s ON s.id = m.search_id "
        "JOIN offer o ON o.id = m.offer_id "
        "JOIN company c ON c.id = o.company_id "
        "WHERE m.id = ?",
        (match_id,),
    ).fetchone()
    if row is None:
        _fatal(f"no match with id {match_id}")
    return row


@cli.command()
@click.argument("match_id", type=int)
@click.option("--note", default=None, help="note stored on the application")
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
def apply(config_path: Path | None, match_id: int, note: str | None) -> None:
    """Record an application for a match."""
    config = _require_config(config_path)
    conn = _open_db(config)
    try:
        match = _require_match(conn, match_id)
        existing = conn.execute(
            "SELECT id FROM application WHERE match_id = ?", (match_id,)
        ).fetchone()
        if existing is not None:
            _fatal(f"match {match_id} already applied (application {existing['id']})")
        cur = conn.execute(
            "INSERT INTO application (match_id, offer_id, note) VALUES (?, ?, ?)",
            (match_id, match["offer_id"], note),
        )
        application_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO event (application_id, type, comment) VALUES (?, 'applied', ?)",
            (application_id, note),
        )
        conn.execute("UPDATE match SET state = 'applied' WHERE id = ?", (match_id,))
        conn.commit()
    finally:
        conn.close()
    click.echo(f"recorded application {application_id} for match {match_id}")


@cli.command()
@click.argument("match_id", type=int)
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
def discard(config_path: Path | None, match_id: int) -> None:
    """Mark a match as discarded."""
    config = _require_config(config_path)
    conn = _open_db(config)
    try:
        _require_match(conn, match_id)
        conn.execute("UPDATE match SET state = 'discarded' WHERE id = ?", (match_id,))
        conn.commit()
    finally:
        conn.close()
    click.echo(f"discarded match {match_id}")


@cli.command()
@click.argument("application_id", type=int)
@click.argument("event_type", type=click.Choice(EVENT_TYPES))
@click.option("-m", "--comment", default=None, help="comment for the event")
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
def log(
    config_path: Path | None, application_id: int, event_type: str, comment: str | None
) -> None:
    """Add an event to an application."""
    config = _require_config(config_path)
    conn = _open_db(config)
    try:
        app = conn.execute("SELECT id FROM application WHERE id = ?", (application_id,)).fetchone()
        if app is None:
            _fatal(f"no application with id {application_id}")
        conn.execute(
            "INSERT INTO event (application_id, type, comment) VALUES (?, ?, ?)",
            (application_id, event_type, comment),
        )
        conn.commit()
    finally:
        conn.close()
    click.echo(f"logged {event_type} for application {application_id}")


@cli.command("apps")
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
def apps(config_path: Path | None) -> None:
    """List applications with their current status (latest event)."""
    config = _require_config(config_path)
    conn = _open_db(config)
    try:
        rows = conn.execute(
            "SELECT a.id AS id, c.name AS company, o.title AS title, "
            "       a.note AS note, "
            "       (SELECT e.type FROM event e WHERE e.application_id = a.id "
            "        ORDER BY e.at DESC, e.id DESC LIMIT 1) AS status, "
            "       (SELECT e.at FROM event e WHERE e.application_id = a.id "
            "        ORDER BY e.at DESC, e.id DESC LIMIT 1) AS status_at "
            "FROM application a "
            "JOIN offer o ON o.id = a.offer_id "
            "JOIN company c ON c.id = o.company_id "
            "ORDER BY a.id"
        ).fetchall()
    finally:
        conn.close()

    header = f"{'id':>3}  {'company':<22} {'title':<45} {'status':<10} {'updated':<19}"
    click.echo(header)
    for row in rows:
        click.echo(
            f"{int(row['id']):>3}  {_clip(row['company'], 22):<22} "
            f"{_clip(row['title'], 45):<45} {_clip(row['status'], 10):<10} "
            f"{_clip(row['status_at'], 19):<19}"
        )


def main() -> None:
    """Entry point for the 'jw' console script."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    cli(prog_name="jw")


if __name__ == "__main__":
    main()
