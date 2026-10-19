"""Préparation quotidienne des candidatures assistées : `jw prepare` (bloc autoapply).

Choisit les meilleurs matchs neufs (fit high, mêmes filtres que la section
Priorité haute du dashboard), les passe « À candidater », génère leur lettre
par le pipeline de draft.py et crée un dossier `submission_job` en attente de
validation. Sans `auto_send`, rien ne part chez un employeur avant le tap
« Envoyer » du dashboard ; avec, le dossier est mis en file pour le poste
d'envoi (voir jobwatch.submit).
"""

from __future__ import annotations

import base64
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from jobwatch import draft
from jobwatch.config import Config
from jobwatch.digest import NTFY_URL
from jobwatch.serve_queries import PROJECT_TITLE_PATTERNS, _priority_matches
from jobwatch.submit import queue_submission

log = logging.getLogger(__name__)

_PROJECT_TITLE_RES = tuple(
    re.compile(re.escape(pattern).replace("%", ".*"), re.IGNORECASE)
    for pattern in PROJECT_TITLE_PATTERNS
)


class PrepareError(Exception):
    """Préparation impossible : profil sans CV, bloc absent..."""


@dataclass
class PrepareResult:
    prepared: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


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
    return [
        row
        for row in _priority_matches(conn, "all", account_id)
        if row["state"] == "new" and int(row["id"]) not in already
    ]


def _label(row: sqlite3.Row) -> str:
    return f"{row['company'] or 'Société inconnue'} - {row['title']}"


def prepare(conn: sqlite3.Connection, db_path: Path, config: Config) -> PrepareResult:
    """Prépare jusqu'à `daily_limit` dossiers par jour ; idempotent sur la journée."""
    if config.autoapply is None or config.draft is None:
        raise PrepareError("bloc 'autoapply' absent de config.yaml")
    profile = _profile(conn)
    remaining = config.autoapply.daily_limit - _prepared_today(conn)
    result = PrepareResult()
    if remaining <= 0:
        return result
    for row in _candidates(conn, int(profile["account_id"]))[:remaining]:
        match_id = int(row["id"])
        conn.execute("UPDATE match SET state = 'later' WHERE id = ?", (match_id,))
        cur = conn.execute(
            "INSERT INTO draft_job (match_id, track, cv_library_id, status) "
            "VALUES (?, ?, ?, 'queued')",
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
    if not result.prepared or config.notify.ntfy is None:
        return False
    action = "en cours d'envoi" if config.autoapply and config.autoapply.auto_send else "à valider"
    title = f"jobwatch : {len(result.prepared)} candidature(s) {action}"
    # httpx encode les en-têtes en ASCII ; ntfy décode l'encodage RFC 2047.
    headers = {"Title": f"=?UTF-8?B?{base64.b64encode(title.encode()).decode()}?="}
    if config.autoapply is not None and config.autoapply.dashboard_url:
        headers["Click"] = config.autoapply.dashboard_url
    try:
        response = client.post(
            NTFY_URL.format(topic=config.notify.ntfy.topic),
            content="\n".join(result.prepared),
            headers=headers,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("prepare: notification ntfy échouée : %s", exc)
        return False
    return True
