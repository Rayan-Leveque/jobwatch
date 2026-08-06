"""CLI click pour jobwatch."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import click

from jobwatch import __version__, importing
from jobwatch.collectors import build_collectors
from jobwatch.collectors.base import store_offers
from jobwatch.config import Config, ConfigError, example_config_text, load_config
from jobwatch.db import connect, init_db
from jobwatch.digest import send_digest
from jobwatch.matching import run_matching, sync_searches
from jobwatch.serve import ServeError, serve_http

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = "config.yaml"
FALLBACK_CONFIG = "~/.config/jobwatch/config.yaml"

EVENT_TYPES = ("applied", "follow_up", "interview", "rejected", "offer")
MATCH_STATES = ("new", "seen", "applied", "discarded")


class CliError(Exception):
    """Échec attendu avec un message clair destiné à l'utilisateur."""


def _fatal(message: str) -> None:
    click.echo(f"erreur : {message}", err=True)
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
    raise CliError(
        f"aucune config trouvée (recherchée dans {local} et {fallback}) ; lancez 'jw init' d'abord"
    )


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
@click.version_option(version=__version__, message="jw, version %(version)s")
def cli() -> None:
    """jobwatch : observateur d'offres d'emploi auto-hébergé."""


@cli.command()
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
def init(config_path: Path | None) -> None:
    """Crée un fichier config.yaml et une base de données vide, puis affiche les prochaines étapes."""
    target = config_path or Path(DEFAULT_CONFIG)
    if target.exists():
        _fatal(f"refus d'écraser la config existante {target}")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(example_config_text())
        config = load_config(target)
    except (OSError, ConfigError) as exc:
        _fatal(str(exc))

    conn = _open_db(config)
    conn.close()

    click.echo(f"config créée : {target}")
    click.echo(f"base de données initialisée : {config.db}")
    click.echo("prochaines étapes : modifiez config.yaml, puis lancez 'jw run'")


@cli.command()
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
def run(config_path: Path | None) -> None:
    """Collecte les offres, les met en correspondance avec les recherches et envoie un digest."""
    config = _require_config(config_path)

    conn = _open_db(config)
    try:
        sync_searches(conn, config.searches)
        collected = 0
        for collector in build_collectors(config.sources):
            offers = collector.fetch()
            new_ids = store_offers(conn, collector.name, collector.source_type, offers)
            collected += len(new_ids)
            logger.info("collected %d new offers from %s", len(new_ids), collector.name)
        new_matches = run_matching(conn)
        channels = send_digest(conn, config)
    finally:
        conn.close()

    notified = f", notifié via {', '.join(channels)}" if channels else ""
    click.echo(f"{collected} nouvelles offres collectées, {len(new_matches)} nouveaux matchs{notified}")


@cli.command("ingest-daily")
@click.option(
    "--api-json",
    "api_json",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    default=None,
    help="fichier JSON quotidien d'offres",
)
@click.option(
    "--digest",
    "digest",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    default=None,
    help="digest quotidien LLM (Markdown)",
)
@click.option(
    "--search-name",
    "search_name",
    default="veille-importee",
    show_default=True,
    help="nom de la recherche à laquelle associer les offres",
)
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
def ingest_daily(config_path: Path | None, api_json: Path | None, digest: Path | None, search_name: str) -> None:
    """Importe les artefacts quotidiens (JSON et/ou digest) dans une recherche."""
    if api_json is None and digest is None:
        _fatal("au moins l'un de --api-json ou --digest est requis")
    config = _require_config(config_path)
    conn = _open_db(config)
    try:
        result = importing.ingest_daily(conn, api_json, digest, search_name)
    except importing.ImportError as exc:
        _fatal(str(exc))
    finally:
        conn.close()
    click.echo(
        f"{result.offers_created} offre(s) créée(s), {result.offers_already_present} déjà présente(s), "
        f"{result.matches_created} match(s) créé(s), {result.fits_updated} fit(s) mis à jour"
    )


@cli.command("import-md")
@click.argument("path", type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.option(
    "--search-name",
    "search_name",
    default="suivi-importe",
    show_default=True,
    help="nom de la recherche à laquelle associer les offres",
)
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
def import_md(config_path: Path | None, path: Path, search_name: str) -> None:
    """Importe le suivi Markdown des candidatures dans une recherche."""
    config = _require_config(config_path)
    conn = _open_db(config)
    try:
        result = importing.import_tracker(conn, path, search_name)
    except importing.ImportError as exc:
        _fatal(str(exc))
    finally:
        conn.close()
    click.echo(
        f"{result.rows_imported} ligne(s) importée(s), {result.offers_created} offre(s) créée(s), "
        f"{result.matches_created} match(s) créé(s), {result.applications_created} candidature(s) créée(s), "
        f"{result.documents_created} document(s) créé(s), {result.rows_already_present} déjà présente(s)"
    )


@cli.command("import-summaries")
@click.argument("path", type=click.Path(path_type=Path, dir_okay=False))
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
def import_summaries(config_path: Path | None, path: Path) -> None:
    """Importe des résumés Markdown pour les offres déjà en base."""
    config = _require_config(config_path)
    conn = _open_db(config)
    try:
        result = importing.import_summaries(conn, path)
    except importing.ImportError as exc:
        _fatal(str(exc))
    finally:
        conn.close()
    click.echo(
        f"{result.summaries_created} résumé(s) créé(s), "
        f"{result.summaries_updated} remplacé(s), "
        f"{result.summaries_unchanged} inchangé(s), "
        f"{result.bullets_written} puce(s) écrite(s)"
    )


@cli.command("list")
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
@click.option("--state", "state", type=click.Choice(MATCH_STATES), default="new")
@click.option("--search", "search_name", default=None, help="filtrer par nom de recherche")
@click.option("--ack", is_flag=True, help="marquer les matchs 'new' listés comme 'seen'")
def list_matches(config_path: Path | None, state: str, search_name: str | None, ack: bool) -> None:
    """Liste les matchs, avec possibilité de les marquer comme vus."""
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
        click.echo(f"{len(rows)} match(s) marqué(s) comme vus")


def _print_matches(rows) -> None:
    header = (
        f"{'id':>5}  {'recherche':<18} {'société':<22} {'titre':<45} "
        f"{'localisation':<18} {'état':<9} {'collecté':<10}"
    )
    click.echo(header)
    for row in rows:
        click.echo(
            f"{int(row['id']):>5}  {_clip(row['search_name'], 18):<18} "
            f"{_clip(row['company'], 22):<22} {_clip(row['title'], 45):<45} "
            f"{_clip(row['location'], 18):<18} {_clip(row['state'], 9):<9} "
            f"{str(row['collected_at'])[:10]:<10}"
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
    """Affiche le détail complet d'un match."""
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
        _fatal(f"aucun match avec l'id {match_id}")
    fields = (
        ("id", "id"),
        ("recherche", "search_name"),
        ("état", "state"),
        ("société", "company"),
        ("titre", "title"),
        ("url", "url"),
        ("plateforme", "platform"),
        ("localisation", "location"),
        ("contrat", "contract"),
        ("publié", "published_at"),
        ("collecté", "collected_at"),
    )
    width = max(len(label) for label, _ in fields) + 1
    for label, key in fields:
        click.echo(f"{label:<{width}} {row[key] if row[key] is not None else ''}")


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
        _fatal(f"aucun match avec l'id {match_id}")
    return row


@cli.command()
@click.argument("match_id", type=int)
@click.option("--note", default=None, help="note conservée sur la candidature")
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
def apply(config_path: Path | None, match_id: int, note: str | None) -> None:
    """Enregistre une candidature pour un match."""
    config = _require_config(config_path)
    conn = _open_db(config)
    try:
        match = _require_match(conn, match_id)
        existing = conn.execute(
            "SELECT id FROM application WHERE match_id = ?", (match_id,)
        ).fetchone()
        if existing is not None:
            _fatal(f"le match {match_id} a déjà été postulé (candidature {existing['id']})")
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
    click.echo(f"candidature {application_id} enregistrée pour le match {match_id}")


@cli.command()
@click.argument("match_id", type=int)
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
def discard(config_path: Path | None, match_id: int) -> None:
    """Marque un match comme écarté."""
    config = _require_config(config_path)
    conn = _open_db(config)
    try:
        _require_match(conn, match_id)
        conn.execute("UPDATE match SET state = 'discarded' WHERE id = ?", (match_id,))
        conn.commit()
    finally:
        conn.close()
    click.echo(f"match {match_id} écarté")


@cli.command()
@click.argument("application_id", type=int)
@click.argument("event_type", type=click.Choice(EVENT_TYPES))
@click.option("-m", "--comment", default=None, help="commentaire pour l'événement")
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
def log(
    config_path: Path | None, application_id: int, event_type: str, comment: str | None
) -> None:
    """Ajoute un événement à une candidature."""
    config = _require_config(config_path)
    conn = _open_db(config)
    try:
        app = conn.execute("SELECT id FROM application WHERE id = ?", (application_id,)).fetchone()
        if app is None:
            _fatal(f"aucune candidature avec l'id {application_id}")
        conn.execute(
            "INSERT INTO event (application_id, type, comment) VALUES (?, ?, ?)",
            (application_id, event_type, comment),
        )
        conn.commit()
    finally:
        conn.close()
    click.echo(f"événement {event_type} consigné pour la candidature {application_id}")


@cli.command("apps")
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
def apps(config_path: Path | None) -> None:
    """Liste les candidatures avec leur statut actuel (dernier événement)."""
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

    header = f"{'id':>3}  {'société':<22} {'titre':<45} {'statut':<10} {'mise à jour':<10}"
    click.echo(header)
    for row in rows:
        click.echo(
            f"{int(row['id']):>3}  {_clip(row['company'], 22):<22} "
            f"{_clip(row['title'], 45):<45} {_clip(row['status'], 10):<10} "
            f"{str(row['status_at'])[:10]:<10}"
        )


@cli.command()
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
@click.option("--host", "host", default="127.0.0.1", show_default=True, help="adresse d'écoute")
@click.option("--port", "port", type=int, default=8000, show_default=True, help="port d'écoute")
def serve(config_path: Path | None, host: str, port: int) -> None:
    """Sert un tableau de bord web local en lecture seule."""
    config = _require_config(config_path)
    conn = _open_db(config)
    conn.close()
    try:
        serve_http(config.db, host, port)
    except ServeError as exc:
        _fatal(str(exc))


def main() -> None:
    """Point d'entrée du script console 'jw'."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    cli(prog_name="jw")


if __name__ == "__main__":
    main()
