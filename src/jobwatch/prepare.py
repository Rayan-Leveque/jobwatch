"""Préparation quotidienne des candidatures assistées : `jw prepare` (bloc autoapply).

Choisit les meilleurs matchs neufs (fit high, mêmes filtres que la section
Priorité haute du dashboard), puis les matchs « À candidater » gardés au swipe
sans dossier (fit puis fraîcheur), écarte ceux dont l'offre a disparu (404, 410)
sans entamer la limite du jour, passe les autres « À candidater », génère leur lettre
par le pipeline de draft.py et crée un dossier `submission_job` en attente de
validation. Sans `auto_send`, rien ne part chez un employeur avant le tap
« Envoyer » du dashboard ; avec, le dossier est mis en file pour le poste
d'envoi (voir jobwatch.submit).
"""

from __future__ import annotations

import base64
import fcntl
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from jobwatch import draft
from jobwatch.config import Config
from jobwatch.digest import NTFY_URL
from jobwatch.enrich import TERMINAL_HTTP_STATUSES, _source_http_headers
from jobwatch.serve_queries import PROJECT_TITLE_PATTERNS, _later_matches, _priority_matches
from jobwatch.submit import queue_submission

log = logging.getLogger(__name__)

_PROJECT_TITLE_RES = tuple(
    re.compile(re.escape(pattern).replace("%", ".*"), re.IGNORECASE)
    for pattern in PROJECT_TITLE_PATTERNS
)


class PrepareError(Exception):
    """Préparation impossible : profil sans CV, bloc absent..."""


class PrepareBusy(PrepareError):
    """Une autre préparation tourne déjà (minuterie, cron ou lancement manuel)."""


@dataclass
class PrepareResult:
    prepared: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)


def draft_track(title: str) -> str:
    """Piste de lettres exemples, même règle que les onglets du dashboard."""
    folded = title.replace("’", "'")
    return "project" if any(r.search(folded) for r in _PROJECT_TITLE_RES) else "engineer"


def _profile(conn: sqlite3.Connection) -> sqlite3.Row:
    rows = conn.execute(
        "SELECT account_id, cv_library_id FROM candidate_profile "
        "WHERE completed_at IS NOT NULL ORDER BY account_id LIMIT 2"
    ).fetchall()
    if len(rows) != 1:
        raise PrepareError("autoapply exige un unique profil confirmé")
    if rows[0]["cv_library_id"] is None:
        raise PrepareError("aucun CV dans le profil : choisissez-en un dans Options")
    return rows[0]


def _prepared_today(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM submission_job "
        "WHERE date(created_at, 'localtime') = date('now', 'localtime')"
    ).fetchone()
    return int(row["n"])


def _candidates(conn: sqlite3.Connection, account_id: int) -> list[sqlite3.Row]:
    already = {
        int(row["match_id"])
        for row in conn.execute("SELECT DISTINCT match_id FROM submission_job")
    }
    priority = [
        row
        for row in _priority_matches(conn, "all", account_id)
        if row["state"] == "new" and int(row["id"]) not in already
    ]
    # Déjà choisis par l'humain : leur ancienne lettre éventuelle est réécrite au
    # format court du pipeline actuel.
    return priority + _later_matches(conn, "all")


def offer_status(url: str, client: httpx.Client) -> int | None:
    """Code HTTP de la page de l'offre ; None si le site ne répond pas."""
    try:
        return client.get(url, follow_redirects=True, headers=_source_http_headers(url)).status_code
    except httpx.HTTPError:
        return None


def _offer_gone(conn: sqlite3.Connection, row: sqlite3.Row, client: httpx.Client) -> bool:
    """Offre retirée : déjà vue en 404/410 par enrich, ou constatée maintenant.

    Un refus (403 anti-robot, WTTJ depuis le VPS) ou un site muet ne compte pas :
    seul le poste d'envoi, dans un vrai navigateur, peut en juger.
    """
    known = conn.execute(
        "SELECT failure_reason FROM offer_content WHERE offer_id = ?", (row["offer_id"],)
    ).fetchone()
    if known is not None and known["failure_reason"] in ("http_404", "http_410"):
        return True
    return offer_status(str(row["url"]), client) in TERMINAL_HTTP_STATUSES


def _label(row: sqlite3.Row) -> str:
    return f"{row['company'] or 'Société inconnue'} - {row['title']}"


def prepare(
    conn: sqlite3.Connection, db_path: Path, config: Config, client: httpx.Client
) -> PrepareResult:
    """Prépare jusqu'à `daily_limit` dossiers par jour ; idempotent sur la journée.

    Lancée souvent (minuterie) : une seule préparation à la fois, les suivantes
    s'arrêtent aussitôt.
    """
    if config.autoapply is None or config.draft is None:
        raise PrepareError("bloc 'autoapply' absent de config.yaml")
    lock = db_path.with_name(db_path.name + ".prepare.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise PrepareBusy("préparation déjà en cours") from None
    try:
        return _prepare(conn, db_path, config, client)
    finally:
        lock.close()


def _prepare(
    conn: sqlite3.Connection, db_path: Path, config: Config, client: httpx.Client
) -> PrepareResult:
    # Verrou tenu : une lettre encore « en cours » vient d'une préparation tuée.
    conn.execute(
        "UPDATE draft_job SET status = 'failed', error = 'préparation interrompue', "
        "finished_at = datetime('now') "
        "WHERE owner = 'prepare' AND status IN ('running', 'queued')"
    )
    conn.commit()
    profile = _profile(conn)
    remaining = config.autoapply.daily_limit - _prepared_today(conn)
    result = PrepareResult()
    if remaining <= 0:
        return result
    for row in _candidates(conn, int(profile["account_id"])):
        if remaining <= 0:
            break
        match_id = int(row["id"])
        if _offer_gone(conn, row, client):
            conn.execute(
                "UPDATE match SET state = 'discarded', discarded_at = datetime('now') "
                "WHERE id = ?",
                (match_id,),
            )
            conn.commit()
            result.expired.append(_label(row))
            continue
        remaining -= 1
        conn.execute("UPDATE match SET state = 'later' WHERE id = ?", (match_id,))
        cur = conn.execute(
            "INSERT INTO draft_job (match_id, track, cv_library_id, status, owner) "
            "VALUES (?, ?, ?, 'queued', 'prepare')",
            (match_id, draft_track(str(row["title"] or "")), profile["cv_library_id"]),
        )
        draft_job_id = int(cur.lastrowid)
        conn.commit()
        draft.run_job(db_path, config.draft, draft_job_id)
        job = conn.execute(
            "SELECT status, error FROM draft_job WHERE id = ?", (draft_job_id,)
        ).fetchone()
        ok = job["status"] == "ok"
        conn.execute(
            "INSERT INTO submission_job (match_id, cv_library_id, draft_job_id, status, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                match_id,
                profile["cv_library_id"],
                draft_job_id,
                "prepared" if ok else "failed",
                None if ok else f"lettre non générée : {job['error']}",
            ),
        )
        conn.commit()
        if ok and config.autoapply.auto_send:
            queue_submission(conn, match_id)
        (result.prepared if ok else result.failed).append(_label(row))
    return result


def notify_prepared(config: Config, result: PrepareResult, client: httpx.Client) -> bool:
    """Une notification ntfy par préparation non vide, avec lien vers le dashboard."""
    if not result.prepared:
        return False
    action = "en cours d'envoi" if config.autoapply and config.autoapply.auto_send else "à valider"
    title = f"jobwatch : {len(result.prepared)} candidature(s) {action}"
    return notify(config, title, "\n".join(result.prepared), client)


def notify(config: Config, title: str, body: str, client: httpx.Client) -> bool:
    """Notification ntfy avec lien vers le dashboard ; False si ntfy absent ou en échec."""
    if config.notify.ntfy is None:
        return False
    # httpx encode les en-têtes en ASCII ; ntfy décode l'encodage RFC 2047.
    headers = {"Title": f"=?UTF-8?B?{base64.b64encode(title.encode()).decode()}?="}
    if config.autoapply is not None and config.autoapply.dashboard_url:
        headers["Click"] = config.autoapply.dashboard_url
    try:
        response = client.post(
            NTFY_URL.format(topic=config.notify.ntfy.topic),
            content=body,
            headers=headers,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("prepare: notification ntfy échouée : %s", exc)
        return False
    return True
