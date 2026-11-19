"""
Web security for the ConflictScan app
=====================================
Everything here is configured by environment variables, so the host
company's IT can tighten it without touching code.

Cross-origin access (CORS)
    Off by default: only pages served by this app can call its API with the
    user's sign-in. CONFLICTSCAN_ALLOWED_ORIGINS="https://a.example,https://b"
    allows extra, named origins (for example an intranet portal).

Cross-site request forgery (CSRF)
    Every state-changing /api/ request must carry the header X-CSRF-Token,
    equal to the per-session token. The token is given to the page in the
    readable cookie "cs_csrf" (double-submit); other sites can neither read
    that cookie nor set the header.

Session lifetime
    CONFLICTSCAN_IDLE_MINUTES     sign out after this much inactivity (default 480)
    CONFLICTSCAN_SESSION_DAYS     longest a "remember me" sign-in lasts (default 14)
    Background polling (job status, scan progress) does not count as activity.

Sign-in throttling
    CONFLICTSCAN_LOGIN_MAX_FAILS  failed sign-ins per username in 15 minutes
                                  before it is locked for 15 minutes (default 5);
                                  4x that per client address.

HTTPS
    CONFLICTSCAN_HTTPS=1          mark cookies Secure and send HSTS (set this
                                  whenever the app is served over HTTPS)

Windows compatible.
"""

import hmac
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta

from flask import jsonify, request, session

CSRF_COOKIE = "cs_csrf"
CSRF_HEADER = "X-CSRF-Token"
UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).lower() not in ("0", "false", "no", "")


HTTPS = _env_flag("CONFLICTSCAN_HTTPS")
IDLE_SECONDS = int(os.environ.get("CONFLICTSCAN_IDLE_MINUTES", "480")) * 60
SESSION_DAYS = int(os.environ.get("CONFLICTSCAN_SESSION_DAYS", "14"))
LOGIN_MAX_FAILS = int(os.environ.get("CONFLICTSCAN_LOGIN_MAX_FAILS", "5"))
LOGIN_WINDOW = 15 * 60
ALLOWED_ORIGINS = {o.strip().rstrip("/") for o in os.environ.get("CONFLICTSCAN_ALLOWED_ORIGINS", "").split(",")
                   if o.strip()}

# Requests that poll in the background; they must not keep an idle session alive.
_PASSIVE_SUFFIXES = ("/progress",)
_PASSIVE_PATHS = ("/api/jobs", "/api/auth/status")

_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",       # one inline script applies the theme before first paint
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
])


# ---------------------------------------------------------------------------
# Sign-in throttling (in memory; moves to the database with the job queue)
# ---------------------------------------------------------------------------

class LoginThrottle:
    def __init__(self, max_fails: int = LOGIN_MAX_FAILS, window: int = LOGIN_WINDOW):
        self.max_fails = max_fails
        self.window = window
        self._fails = defaultdict(deque)
        self._lock = threading.Lock()

    def _recent(self, key, now):
        q = self._fails[key]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def retry_after(self, username: str, address: str) -> int:
        """Seconds until another attempt is allowed (0 = allowed now)."""
        now = time.time()
        with self._lock:
            wait = 0
            for key, limit in ((("u", username.lower()), self.max_fails), (("ip", address), self.max_fails * 4)):
                q = self._recent(key, now)
                if len(q) >= limit:
                    wait = max(wait, int(self.window - (now - q[0])) + 1)
            return wait

    def failed(self, username: str, address: str) -> None:
        now = time.time()
        with self._lock:
            self._fails[("u", username.lower())].append(now)
            self._fails[("ip", address)].append(now)

    def succeeded(self, username: str) -> None:
        with self._lock:
            self._fails.pop(("u", username.lower()), None)

    def reset(self) -> None:
        with self._lock:
            self._fails.clear()


login_throttle = LoginThrottle()


def client_address() -> str:
    return request.remote_addr or "unknown"


# ---------------------------------------------------------------------------
# Sessions and CSRF
# ---------------------------------------------------------------------------

def start_session(user_id: str, remember: bool) -> None:
    """A fresh session (new CSRF token) for a user who just signed in."""
    session.clear()
    session.permanent = bool(remember)
    session["uid"] = user_id
    session["seen"] = int(time.time())
    session["csrf"] = secrets.token_urlsafe(32)


def csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = session["csrf"] = secrets.token_urlsafe(32)
    return token


def _is_passive() -> bool:
    return request.path in _PASSIVE_PATHS or request.path.endswith(_PASSIVE_SUFFIXES)


def init_app(app) -> None:
    app.config.update(
        SESSION_COOKIE_NAME="cs_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=HTTPS,
        PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_DAYS),
    )

    @app.before_request
    def _guard():
        origin = (request.headers.get("Origin") or "").rstrip("/")
        if request.method == "OPTIONS" and origin in ALLOWED_ORIGINS:
            return app.make_default_options_response()

        if not request.path.startswith("/api/"):
            return None

        # Idle timeout
        if session.get("uid"):
            now = int(time.time())
            if now - int(session.get("seen") or 0) > IDLE_SECONDS:
                session.clear()
                return jsonify({"error": "You were signed out after a period of inactivity. Please sign in again.",
                                "auth_required": True, "expired": True}), 401
            if not _is_passive() and now - int(session.get("seen") or 0) > 60:
                session["seen"] = now

        if request.method in UNSAFE_METHODS:
            expected = session.get("csrf")
            sent = request.headers.get(CSRF_HEADER) or ""
            if not expected or not hmac.compare_digest(str(expected), sent):
                return jsonify({"error": "Your session's security token is missing or out of date. "
                                         "Reload the page and try again.", "csrf_failed": True}), 403
        return None

    @app.after_request
    def _headers(resp):
        token = csrf_token()
        if request.cookies.get(CSRF_COOKIE) != token:
            resp.set_cookie(CSRF_COOKIE, token, samesite="Lax", secure=HTTPS, httponly=False,
                            max_age=SESSION_DAYS * 86400)
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("Content-Security-Policy", _CSP)
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if HTTPS:
            h.setdefault("Strict-Transport-Security", "max-age=31536000")
        if request.path.startswith("/api/"):
            h.setdefault("Cache-Control", "no-store")
        origin = (request.headers.get("Origin") or "").rstrip("/")
        if origin and origin in ALLOWED_ORIGINS:
            h["Access-Control-Allow-Origin"] = origin
            h["Access-Control-Allow-Credentials"] = "true"
            h["Access-Control-Allow-Headers"] = f"Content-Type, {CSRF_HEADER}"
            h["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE"
            h.add("Vary", "Origin")
        return resp
