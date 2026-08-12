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

Ce module contient le serveur HTTP (make_handler, serve_http) ; les requêtes
SQLite vivent dans jobwatch.serve_queries, le rendu HTML des données dans
jobwatch.serve_render et le chrome de page (CSS/JS statique) dans
jobwatch.serve_templates.
"""

from __future__ import annotations

import functools
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import click

from jobwatch import draft
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
from jobwatch.library import LibraryError, save_upload
from jobwatch.match_actions import (
    MatchActionResult,
    apply_match_action,
    parse_match_action,
)
from jobwatch.onboarding import (
    OnboardingError,
    analyze_cvs,
    complete_profile,
    profile_complete,
    profile_cv_library_ids,
    profile_intents,
)
from jobwatch.onboarding_ui import render_onboarding
from jobwatch.profile import ProfileError, profile_details, save_profile_details
from jobwatch.profile_ui import render_profile
from jobwatch.serve_queries import (
    TRACKS,
    _batch_eligible_ids,
    _batch_status,
    _swipe_deck,  # noqa: F401  (ré-export : utilisé par le bac à sable jobwatch_demo)
)
from jobwatch.serve_render import (
    _draft_status_html,
    _markdown_to_html,  # noqa: F401  (ré-export : surface publique historique de jobwatch.serve)
    render_page,
    render_swipe_page,
)
from jobwatch.serve_templates import _auth_page, _invite_form, _login_form

_MATCH_ACTION_RE = re.compile(r"^/match/(\d+)/(later|discard|restore|apply)$")


_DRAFT_POST_RE = re.compile(r"^/match/(\d+)/draft$")


_DRAFT_STATUS_RE = re.compile(r"^/match/(\d+)/draft/status$")


_LETTER_FILE_RE = re.compile(r"^/match/(\d+)/letter\.(pdf|tex)$")


_LETTER_PAGE_RE = re.compile(r"^/match/(\d+)/letter/(\d+)\.png$")


_LETTER_BODY_RE = re.compile(r"^/match/(\d+)/letter/body$")


_DOCUMENT_FILE_RE = re.compile(r"^/documents/(\d+)$")


_UPLOAD_PATH = "/documents"


_BUG_REPORT_PATH = "/bug-report"


MAX_JSON_BODY_BYTES = 15 * 1024 * 1024


MAX_BUG_REPORT_LENGTH = 4_000


MAX_BUG_CONTEXT_LENGTH = 500


_PREVIEW_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


class ServeError(Exception):
    """Échec attendu de l'amorçage du serveur. La CLI affiche un message clair et sort."""


def _spawn_draft_job(db_path: Path, config: DraftConfig, job_id: int) -> None:
    """Lance le job de génération dans un thread ; point d'accroche des tests."""
    threading.Thread(
        target=draft.run_job, args=(db_path, config, job_id), daemon=True
    ).start()


def _db_error_response(response_format: str = "text"):
    """Traduit un `sqlite3.Error` levé par la méthode décorée en réponse 500 standard.

    Ne gère ni l'ouverture ni la fermeture de la connexion (voir `Handler._db`) :
    seule la traduction de l'erreur est mutualisée ici, pour rester composable
    avec les méthodes qui commitent ou non selon leur propre logique.
    """

    def decorator(method):
        @functools.wraps(method)
        def wrapper(self, *args, **kwargs):
            try:
                return method(self, *args, **kwargs)
            except sqlite3.Error as exc:
                message = f"erreur base de données : {exc}"
                if response_format == "json":
                    self._send_json(500, {"error": message})
                else:
                    self._send_text(500, message + "\n")
                return None

        return wrapper

    return decorator


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

    class Handler(BaseHTTPRequestHandler):
        server_version = "jobwatch"

        @contextmanager
        def _db(self):
            """Ouvre une connexion pour la requête et la ferme dans tous les cas.

            Ne commite jamais : chaque appelant reste responsable de son
            `conn.commit()` s'il mute des données.
            """
            conn = connect(db_path)
            try:
                yield conn
            finally:
                conn.close()

        def _authentication(self) -> tuple[bool, Session | None, str | None]:
            with self._db() as conn:
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

        def _require_session(self, path: str) -> Session | None:
            required, session, _token = self._authentication()
            if not required:
                return None
            if workspace_slug is None:
                self._send_text(503, "instance nommée requise pour l'authentification\n")
                return None
            if session is not None:
                return session
            if path.startswith(
                (
                    "/draft/",
                    "/match/",
                    "/documents",
                    "/onboarding/",
                    "/profile",
                    _BUG_REPORT_PATH,
                )
            ):
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
                with self._db() as conn:
                    status = invite_status(conn, invite.group(1), workspace_slug)
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
                with self._db() as conn:
                    intents = profile_intents(conn, session.account_id) if editing else None
                    cv_library_ids = profile_cv_library_ids(conn, session.account_id)
                initial_intents = (
                    [
                        {
                            "id": intent.intent_id,
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
            if path == "/profile":
                if session is None or workspace_slug is None:
                    self._redirect("/")
                    return
                with self._db() as conn:
                    details = profile_details(conn, session.account_id)
                self._send_bytes(
                    200,
                    render_profile(
                        details,
                        session.csrf_token,
                        email=session.email,
                        workspace_slug=workspace_slug,
                        welcome=parse_qs(parsed.query).get("welcome") == ["1"],
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
            letter_body = _LETTER_BODY_RE.match(path)
            if letter_body:
                self._handle_letter_body_get(int(letter_body.group(1)))
                return
            document = _DOCUMENT_FILE_RE.match(path)
            if document:
                self._handle_document_file(int(document.group(1)))
                return
            if path == "/draft/batch/status":
                self._handle_batch_status()
                return
            if path in ("/", "/swipe"):
                track = "engineer"
            elif path in ("/po", "/po/swipe"):
                track = "project"
            else:
                self._send_text(404, "404 Not Found\n")
                return
            swipe = path in ("/swipe", "/po/swipe")
            if session is not None and onboarding_enabled:
                with self._db() as conn:
                    needs_onboarding = not profile_complete(conn, session.account_id)
                if needs_onboarding:
                    self._redirect("/onboarding")
                    return
                track = "all"
            self._render_track_page(track, swipe, session)

        @_db_error_response()
        def _render_track_page(self, track: str, swipe: bool, session: Session | None) -> None:
            with self._db() as conn:
                common = {
                    "draft_enabled": draft_config is not None,
                    "csrf_token": session.csrf_token if session is not None else "",
                }
                if swipe:
                    page = render_swipe_page(conn, track, **common)
                else:
                    page = render_page(
                        conn,
                        track,
                        account_id=session.account_id if session is not None else None,
                        identity_sub=(
                            f"{session.email} · espace {workspace_slug}"
                            if session is not None and workspace_slug is not None
                            else ""
                        ),
                        **common,
                    )
            self._send_bytes(200, page.encode("utf-8"), "text/html; charset=utf-8")

        @_db_error_response()
        def _handle_batch_status(self) -> None:
            query = urlsplit(self.path).query
            track = "engineer"
            for part in query.split("&"):
                if part.startswith("track="):
                    track = part.removeprefix("track=")
            if track not in TRACKS:
                self._send_json(400, {"error": "champ track invalide"})
                return
            with self._db() as conn:
                counts = _batch_status(conn, track)
            self._send_json(200, counts)

        @_db_error_response()
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
            with self._db() as conn:
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
            for job_id in job_ids:
                _spawn_draft_job(db_path, draft_config, job_id)
            self._send_json(202, {"count": len(job_ids)})

        def _latest_draft(self, conn: sqlite3.Connection, match_id: int) -> sqlite3.Row | None:
            return conn.execute(
                "SELECT * FROM draft_job WHERE match_id = ? ORDER BY id DESC LIMIT 1",
                (match_id,),
            ).fetchone()

        @_db_error_response()
        def _handle_draft_status(self, match_id: int) -> None:
            with self._db() as conn:
                job = self._latest_draft(conn, match_id)
                entry = None
                if job is not None and job["library_id"] is not None:
                    entry = conn.execute(
                        "SELECT id, label FROM document_library WHERE id = ?",
                        (job["library_id"],),
                    ).fetchone()
            if job is None:
                self._send_json(404, {"error": "aucune génération pour ce match"})
                return
            payload = {"status": str(job["status"]), "html": _draft_status_html(match_id, job)}
            if entry is not None:
                # Permet au client d'ajouter la lettre aux menus Candidater sans recharger.
                payload["library_id"] = int(entry["id"])
                payload["library_label"] = str(entry["label"])
            self._send_json(200, payload)

        @_db_error_response()
        def _handle_letter_file(self, match_id: int, extension: str) -> None:
            column = "pdf_path" if extension == "pdf" else "tex_path"
            with self._db() as conn:
                row = conn.execute(
                    f"SELECT {column} AS path FROM draft_job "
                    f"WHERE match_id = ? AND {column} IS NOT NULL "
                    "ORDER BY id DESC LIMIT 1",
                    (match_id,),
                ).fetchone()
            content_type = (
                "application/pdf" if extension == "pdf" else "text/plain; charset=utf-8"
            )
            self._send_draft_file(row["path"] if row else None, content_type)

        @_db_error_response()
        def _handle_letter_page(self, match_id: int, page: int) -> None:
            with self._db() as conn:
                row = conn.execute(
                    "SELECT pdf_path, png_pages FROM draft_job "
                    "WHERE match_id = ? AND status = 'ok' ORDER BY id DESC LIMIT 1",
                    (match_id,),
                ).fetchone()
            if row is None or not (1 <= page <= int(row["png_pages"] or 0)):
                self._send_text(404, "404 Not Found\n")
                return
            pdf_path = Path(str(row["pdf_path"]))
            png_path = pdf_path.parent / f"{pdf_path.stem}-{page}.png"
            self._send_draft_file(str(png_path), "image/png")

        @_db_error_response()
        def _handle_document_file(self, library_id: int) -> None:
            """Sert un document de la bibliothèque pour prévisualisation (œil des menus)."""
            with self._db() as conn:
                row = conn.execute(
                    "SELECT file_path FROM document_library WHERE id = ?",
                    (library_id,),
                ).fetchone()
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

        @_db_error_response()
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
            with self._db() as conn:
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
            _spawn_draft_job(db_path, draft_config, job_id)
            self._send_json(202, {"ok": True, "job_id": job_id})

        @_db_error_response()
        def _handle_letter_body_get(self, match_id: int) -> None:
            try:
                with self._db() as conn:
                    body_text = draft.get_body_edit(conn, match_id)
            except draft.DraftError as exc:
                self._send_json(404, {"error": str(exc)})
                return
            self._send_json(200, {"body": body_text})

        @_db_error_response()
        def _handle_letter_body_post(self, match_id: int) -> None:
            if draft_config is None:
                self._send_json(
                    503,
                    {"error": "génération non configurée : renseignez le bloc 'draft' de config.yaml"},
                )
                return
            body = self._read_json_body()
            fields = body if isinstance(body, dict) else {}
            text = fields.get("body")
            if not isinstance(text, str) or not text.strip():
                self._send_json(400, {"error": "le texte de la lettre ne peut pas être vide"})
                return
            with self._db() as conn:
                try:
                    job_id = draft.apply_body_edit(conn, db_path, match_id, text)
                except draft.DraftError as exc:
                    self._send_json(422, {"error": str(exc)})
                    return
                job = conn.execute(
                    "SELECT * FROM draft_job WHERE id = ?", (job_id,)
                ).fetchone()
                entry = None
                if job is not None and job["library_id"] is not None:
                    entry = conn.execute(
                        "SELECT id, label FROM document_library WHERE id = ?",
                        (job["library_id"],),
                    ).fetchone()
            payload = {"status": str(job["status"]), "html": _draft_status_html(match_id, job)}
            if entry is not None:
                payload["library_id"] = int(entry["id"])
                payload["library_label"] = str(entry["label"])
            self._send_json(200, payload)

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
            if path == "/profile":
                self._handle_profile_save(session)
                return
            if path == "/logout":
                token = session_token(self.headers.get("Cookie"))
                if token:
                    with self._db() as conn:
                        delete_session(conn, token)
                self._redirect(
                    "/login", headers={"Set-Cookie": expired_session_cookie(secure=secure_cookie)}
                )
                return
            if path == _BUG_REPORT_PATH:
                self._handle_bug_report(session)
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
            letter_body_post = _LETTER_BODY_RE.match(path)
            if letter_body_post:
                self._handle_letter_body_post(int(letter_body_post.group(1)))
                return
            match = _MATCH_ACTION_RE.match(path)
            if not match:
                self._send_text(404, "404 Not Found\n")
                return
            match_id = int(match.group(1))
            action = match.group(2)
            self._handle_match_action(match_id, action)

        @_db_error_response()
        def _handle_match_action(self, match_id: int, action: str) -> None:
            body = self._read_json_body() if action in ("restore", "apply") else None
            request = parse_match_action(action, body)
            if isinstance(request, MatchActionResult):
                self._send_json(400, {"error": request.error})
                return
            with self._db() as conn:
                result = apply_match_action(conn, match_id, request)
            if result.kind == "not_found":
                self._send_text(404, "404 Not Found\n")
            elif result.kind == "conflict":
                self._send_json(409, {"error": result.error})
            else:
                self._send_json(200, {"ok": True})

        @_db_error_response()
        def _handle_bug_report(self, session: Session | None) -> None:
            body = self._read_json_body()
            fields = body if isinstance(body, dict) else {}
            message = fields.get("message")
            page = fields.get("page")
            if not isinstance(message, str) or not message.strip():
                self._send_json(400, {"error": "décrivez le problème rencontré"})
                return
            message = message.strip()
            if len(message) > MAX_BUG_REPORT_LENGTH:
                self._send_json(
                    400,
                    {"error": f"description trop longue ({MAX_BUG_REPORT_LENGTH} caractères maximum)"},
                )
                return
            if not isinstance(page, str) or not page.startswith("/"):
                page = "/"
            page = page[:MAX_BUG_CONTEXT_LENGTH]
            user_agent = (self.headers.get("User-Agent") or "")[:MAX_BUG_CONTEXT_LENGTH]
            with self._db() as conn:
                cur = conn.execute(
                    "INSERT INTO bug_report "
                    "(account_id, workspace_id, message, page, user_agent) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        session.account_id if session is not None else None,
                        session.workspace_id if session is not None else None,
                        message,
                        page,
                        user_agent or None,
                    ),
                )
                report_id = int(cur.lastrowid)
                conn.commit()
            self._send_json(201, {"ok": True, "id": report_id})

        @_db_error_response()
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
            with self._db() as conn:
                try:
                    entry = save_upload(conn, db_path, doc_type, label, filename, content_base64)
                except LibraryError as exc:
                    self._send_json(400, {"error": str(exc)})
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
            with self._db() as conn:
                try:
                    intents = analyze_cvs(
                        conn, onboarding_config or draft_config, cv_library_ids
                    )
                except OnboardingError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
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

        @_db_error_response("json")
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
            with self._db() as conn:
                try:
                    already_complete = profile_complete(conn, session.account_id)
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
            self._send_json(
                200,
                {
                    "ok": True,
                    "count": len(intents),
                    "next": "/" if already_complete else "/profile?welcome=1",
                },
            )

        @_db_error_response("json")
        def _handle_profile_save(self, session: Session | None) -> None:
            if session is None:
                self._send_json(401, {"error": "authentification requise"})
                return
            body = self._read_json_body()
            with self._db() as conn:
                try:
                    details = save_profile_details(
                        conn, session.account_id, session.workspace_id, body
                    )
                except ProfileError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
            self._send_json(200, {"ok": True, "personalized": details.has_personalization})

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
            with self._db() as conn:
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
            with self._db() as conn:
                status = invite_status(conn, token, workspace_slug)
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
            with self._db() as conn:
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
            page = _auth_page(title, body, workspace_slug=workspace_slug)
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
