"""Primitives HTTP d'authentification indépendantes du serveur jobwatch."""

from __future__ import annotations

import hmac
import ipaddress
from http.cookies import CookieError, SimpleCookie

from jobwatch.auth import REMEMBER_SESSION_SECONDS, Session

SESSION_COOKIE = "id"
CSRF_HEADER = "X-CSRF-Token"
CLIENT_HEADER = "X-Real-IP"


def client_address(peer: str, forwarded: str | None, *, trust_proxy: bool) -> str:
    """Adresse du visiteur : celle annoncée par le proxy local, sinon celle du pair.

    L'en-tête n'est cru que sur demande explicite et venant de la boucle locale :
    sans cela, n'importe quel visiteur choisirait l'adresse qui le fait limiter.
    """
    if not trust_proxy or not forwarded:
        return peer
    try:
        if not ipaddress.ip_address(peer).is_loopback:
            return peer
        return str(ipaddress.ip_address(forwarded.strip()))
    except ValueError:
        return peer


def session_cookie(token: str, *, secure: bool, remember: bool = False) -> str:
    """Construit un cookie HttpOnly, persistant 30 jours sur demande."""
    cookie = SimpleCookie()
    cookie[SESSION_COOKIE] = token
    morsel = cookie[SESSION_COOKIE]
    morsel["path"] = "/"
    morsel["httponly"] = True
    morsel["samesite"] = "Strict"
    if remember:
        morsel["max-age"] = REMEMBER_SESSION_SECONDS
    if secure:
        morsel["secure"] = True
    return morsel.OutputString()


def expired_session_cookie(*, secure: bool) -> str:
    cookie = SimpleCookie()
    cookie[SESSION_COOKIE] = ""
    morsel = cookie[SESSION_COOKIE]
    morsel["path"] = "/"
    morsel["httponly"] = True
    morsel["samesite"] = "Strict"
    morsel["max-age"] = 0
    if secure:
        morsel["secure"] = True
    return morsel.OutputString()


def session_token(cookie_header: str | None) -> str | None:
    if not cookie_header:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except CookieError:
        return None
    morsel = cookie.get(SESSION_COOKIE)
    return morsel.value if morsel is not None and morsel.value else None


def csrf_valid(session: Session, provided: str | None) -> bool:
    return provided is not None and hmac.compare_digest(session.csrf_token, provided)


def security_headers() -> dict[str, str]:
    """En-têtes communs compatibles avec les styles et scripts inline existants."""
    return {
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }
