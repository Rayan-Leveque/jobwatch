"""Tests des cookies et protections HTTP d'authentification."""

from jobwatch.auth import Session
from jobwatch.auth_http import (
    csrf_valid,
    expired_session_cookie,
    security_headers,
    session_cookie,
    session_token,
)


def _session() -> Session:
    return Session(1, 1, "alice@example.com", "owner", "csrf-secret", "tomorrow")


def test_secure_session_cookie_has_required_attributes() -> None:
    value = session_cookie("opaque-token", secure=True)
    assert value.startswith("id=opaque-token")
    for attribute in ("HttpOnly", "Path=/", "SameSite=Strict", "Secure"):
        assert attribute in value
    assert "Max-Age" not in value


def test_insecure_cookie_is_explicit_for_private_http_only() -> None:
    value = session_cookie("opaque-token", secure=False)
    assert "HttpOnly" in value
    assert "SameSite=Strict" in value
    assert "Secure" not in value


def test_session_cookie_round_trip_and_expiration() -> None:
    assert session_token("theme=dark; id=opaque-token; other=x") == "opaque-token"
    assert session_token(None) is None
    assert session_token("broken-cookie") is None
    expired = expired_session_cookie(secure=True)
    assert "Max-Age=0" in expired
    assert "Secure" in expired


def test_csrf_requires_exact_session_token() -> None:
    assert csrf_valid(_session(), "csrf-secret")
    assert not csrf_valid(_session(), "wrong")
    assert not csrf_valid(_session(), None)


def test_security_headers_disable_cache_sniffing_and_framing() -> None:
    headers = security_headers()
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
