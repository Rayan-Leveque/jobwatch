"""Point d'entrée HTTP commun vers des instances Jobwatch isolées."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from jobwatch.auth import AuthError, normalize_email
from jobwatch.auth_http import security_headers
from jobwatch.paths import validate_instance_name

SESSION_COOKIE = "id"
_MAX_BODY_BYTES = 15 * 1024 * 1024
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class SharedError(Exception):
    """Configuration commune invalide."""


@dataclass(frozen=True)
class Instance:
    slug: str
    email: str
    port: int


def load_registry(path: Path) -> tuple[Instance, ...]:
    """Charge la correspondance publique email vers instance privée."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SharedError(f"registre illisible {path} : {exc}") from exc
    rows = payload.get("instances") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise SharedError("le registre doit contenir une liste 'instances' non vide")
    instances: list[Instance] = []
    for row in rows:
        if not isinstance(row, dict):
            raise SharedError("entrée d'instance invalide")
        try:
            slug = validate_instance_name(row["slug"])
            email = normalize_email(row["email"])
            port = int(row["port"])
        except (AuthError, KeyError, TypeError, ValueError) as exc:
            raise SharedError(f"entrée d'instance invalide : {exc}") from exc
        if isinstance(row["port"], bool) or not 1 <= port <= 65535:
            raise SharedError(f"port invalide pour {slug}")
        instances.append(Instance(slug, email, port))
    if len({item.slug for item in instances}) != len(instances):
        raise SharedError("slug d'instance dupliqué")
    if len({item.email for item in instances}) != len(instances):
        raise SharedError("email d'instance dupliqué")
    if len({item.port for item in instances}) != len(instances):
        raise SharedError("port d'instance dupliqué")
    return tuple(instances)


def register_instance(path: Path, instance: Instance) -> None:
    """Ajoute atomiquement une instance au registre commun."""
    if not 1 <= instance.port <= 65535:
        raise SharedError(f"port invalide pour {instance.slug}")
    current = list(load_registry(path)) if path.exists() else []
    if any(
        item.slug == instance.slug or item.email == instance.email or item.port == instance.port
        for item in current
    ):
        raise SharedError("slug, email ou port déjà enregistré")
    current.append(instance)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.stat() if path.exists() else None
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            json.dump(
                {
                    "instances": [
                        {"slug": item.slug, "email": item.email, "port": item.port}
                        for item in current
                    ]
                },
                temporary,
                ensure_ascii=False,
                indent=2,
            )
            temporary.write("\n")
        os.chmod(temporary_name, previous.st_mode & 0o777 if previous else 0o640)
        if previous:
            os.chown(temporary_name, previous.st_uid, previous.st_gid)
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def make_handler(registry_path: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "jobwatch"

        def do_GET(self) -> None:
            self._handle()

        def do_POST(self) -> None:
            self._handle()

        def _instances(self) -> tuple[Instance, ...]:
            return load_registry(registry_path)

        def _handle(self) -> None:
            try:
                instances = self._instances()
                body = self._read_body()
                if body is None:
                    return
                path = urlsplit(self.path).path
                scoped = self._scoped_instance(instances)
                instance = scoped[0] if scoped else None
                token = scoped[1] if scoped else None
                if path == "/healthz":
                    self._health(instances)
                    return
                if path.startswith("/invite/"):
                    instance = self._find_invite(instances)
                elif path == "/login" and self.command == "POST":
                    email = parse_qs(body.decode("utf-8", errors="replace")).get("email", [""])[-1]
                    try:
                        normalized = normalize_email(email)
                    except AuthError:
                        normalized = ""
                    instance = next(
                        (item for item in instances if item.email == normalized), instances[0]
                    )
                elif path == "/login" and instance is None:
                    instance = instances[0]
                elif instance is None:
                    self._redirect_login()
                    return
                self._proxy(instance, body, token)
            except SharedError as exc:
                print(f"point d'entrée Jobwatch indisponible - {exc}", file=sys.stderr, flush=True)
                self._send_text(503, "service indisponible\n")

        def _read_body(self) -> bytes | None:
            lengths = self.headers.get_all("Content-Length", [])
            if self.command == "GET":
                return b""
            if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
                self._send_text(400, "taille de requête invalide\n")
                return None
            try:
                length = int(lengths[0])
            except ValueError:
                length = -1
            if length < 0 or length > _MAX_BODY_BYTES:
                self._send_text(
                    413 if length > _MAX_BODY_BYTES else 400, "taille de requête invalide\n"
                )
                return None
            return self.rfile.read(length)

        def _scoped_instance(self, instances: tuple[Instance, ...]) -> tuple[Instance, str] | None:
            try:
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
            except CookieError:
                return None
            morsel = cookie.get(SESSION_COOKIE)
            if morsel is None or "." not in morsel.value:
                return None
            slug, token = morsel.value.split(".", 1)
            instance = next((item for item in instances if item.slug == slug), None)
            return (instance, token) if instance is not None and token else None

        def _find_invite(self, instances: tuple[Instance, ...]) -> Instance:
            fallback = None
            for instance in instances:
                try:
                    status, _headers, _body = self._upstream(instance, b"", None, method="GET")
                except SharedError:
                    continue
                fallback = fallback or instance
                if status in (200, 303):
                    return instance
            return fallback or instances[0]

        def _health(self, instances: tuple[Instance, ...]) -> None:
            healthy = []
            for instance in instances:
                try:
                    status, _headers, _body = self._upstream(
                        instance, b"", None, method="GET", path="/healthz"
                    )
                except SharedError:
                    status = 503
                healthy.append(status == 200)
            status = 200 if all(healthy) else 503
            self._send_json(status, {"status": "ok" if status == 200 else "unavailable"})

        def _upstream(
            self,
            instance: Instance,
            body: bytes,
            token: str | None,
            *,
            method: str | None = None,
            path: str | None = None,
        ) -> tuple[int, list[tuple[str, str]], bytes]:
            headers = {
                name: value
                for name, value in self.headers.items()
                if name.casefold() not in _HOP_BY_HOP and name.casefold() != "cookie"
            }
            if token:
                headers["Cookie"] = f"{SESSION_COOKIE}={token}"
            headers["Connection"] = "close"
            conn = http.client.HTTPConnection("127.0.0.1", instance.port, timeout=35)
            try:
                conn.request(method or self.command, path or self.path, body=body, headers=headers)
                response = conn.getresponse()
                return response.status, response.getheaders(), response.read()
            except (OSError, http.client.HTTPException) as exc:
                raise SharedError(f"instance {instance.slug} indisponible") from exc
            finally:
                conn.close()

        def _proxy(self, instance: Instance, body: bytes, token: str | None) -> None:
            status, headers, payload = self._upstream(instance, body, token)
            self.send_response(status)
            for name, value in headers:
                lowered = name.casefold()
                if lowered in _HOP_BY_HOP or lowered in {"server", "date", "content-length"}:
                    continue
                if lowered == "set-cookie":
                    value = self._scope_cookie(value, instance.slug)
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        @staticmethod
        def _scope_cookie(header: str, slug: str) -> str:
            try:
                cookie = SimpleCookie()
                cookie.load(header)
                morsel = cookie.get(SESSION_COOKIE)
                if morsel is not None and morsel.value:
                    morsel.set(SESSION_COOKIE, f"{slug}.{morsel.value}", f"{slug}.{morsel.value}")
                return morsel.OutputString() if morsel is not None else header
            except CookieError:
                return header

        def _redirect_login(self) -> None:
            self.send_response(303)
            self.send_header("Location", "/login")
            for name, value in security_headers().items():
                self.send_header(name, value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send_json(self, status: int, payload: dict[str, str]) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for name, value in security_headers().items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_text(self, status: int, text: str) -> None:
            data = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            for name, value in security_headers().items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Handler


def serve(registry: Path, host: str, port: int) -> None:
    load_registry(registry)
    server = ThreadingHTTPServer((host, port), make_handler(registry))
    print(f"application Jobwatch commune sur http://{host}:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Point d'entrée commun Jobwatch")
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8810)
    parser.add_argument("--register", nargs=3, metavar=("SLUG", "EMAIL", "PORT"))
    args = parser.parse_args()
    try:
        if args.register:
            slug, email, port = args.register
            register_instance(
                args.registry,
                Instance(validate_instance_name(slug), normalize_email(email), int(port)),
            )
            return
        serve(args.registry, args.host, args.port)
    except (AuthError, SharedError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
