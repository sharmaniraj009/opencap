"""API and UI authentication: named tokens with roles, and UI login sessions.

Tokens (poller.yaml `api`):
- `token_env`: the legacy single token, named "default", role admin (D-019);
- `tokens`: any number of `{name, role, token_env}`. The secret is read from the env var;
  the yaml holds only its name. A listed token whose env var is unset stops the start.

Roles: `viewer` may use safe methods only (GET/HEAD): status, values, config reads,
`/check`, export, audit. `admin` may also change things (config writes, import, reload,
read-once). With no token at all the API starts only on a loopback bind, and every caller
is the admin "anonymous" (D-019); then unsafe requests from another web origin are refused.

UI sessions: `POST /ui/login` checks a token and sets an HttpOnly, SameSite=Strict cookie
(path /ui) holding a random session id. Sessions live in memory (a restart logs everyone
out) for `SESSION_TTL_S`. Every unsafe UI request must send the session's CSRF token in
`X-CSRF-Token`. The REST API never accepts the cookie, only `Authorization: Bearer`.
"""

from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from opencap.config.models import ApiCfg

Role = Literal["viewer", "admin"]
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SESSION_COOKIE = "opencap_session"
SESSION_TTL_S = 12 * 3600
MAX_SESSIONS = 1000
LEGACY_NAME = "default"


@dataclass(frozen=True)
class Principal:
    name: str
    role: Role
    via: Literal["api", "ui"]

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


@dataclass
class Session:
    sid: str
    name: str
    role: Role
    csrf: str
    expires: float

    def principal(self) -> Principal:
        return Principal(self.name, self.role, "ui")


class AuthError(Exception):
    """401 (not authenticated) or 403 (authenticated, not allowed)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def is_safe(method: str) -> bool:
    return method.upper() in SAFE_METHODS


def cross_site(headers: Mapping[str, str], host: str | None) -> bool:
    """True when a browser says the request comes from another web origin."""
    if headers.get("sec-fetch-site", "").lower() == "cross-site":
        return True
    origin = headers.get("origin")
    if origin and origin != "null" and host:
        return origin.split("://", 1)[-1].rstrip("/") != host
    return origin == "null"


class Auth:
    def __init__(self, cfg: ApiCfg, environ: Mapping[str, str] | None = None,
                 clock=time.monotonic) -> None:
        env = os.environ if environ is None else environ
        self._clock = clock
        self._tokens: list[tuple[bytes, str, Role]] = []
        legacy = env.get(cfg.token_env) or None
        if legacy:
            self._tokens.append((legacy.encode(), LEGACY_NAME, "admin"))
        for i, tok in enumerate(cfg.tokens):
            secret = env.get(tok.token_env) or None
            if secret is None:
                raise RuntimeError(f"api.tokens[{i}] {tok.name!r}: env var {tok.token_env} is not set")
            self._tokens.append((secret.encode(), tok.name, tok.role))
        secrets_seen = [t[0] for t in self._tokens]
        if len(set(secrets_seen)) != len(secrets_seen):
            raise RuntimeError("api tokens: two tokens have the same secret; give each its own")
        self.open = not self._tokens
        self.open_csrf = secrets.token_urlsafe(32)   # CSRF token of the open (no-token) UI
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    @property
    def token_names(self) -> list[tuple[str, Role]]:
        return [(name, role) for _, name, role in self._tokens]

    # tokens -------------------------------------------------------------------------
    def check_token(self, given: str) -> Principal | None:
        """The token's principal, or None. Compares against every token (constant time)."""
        given_b = given.encode()
        found: Principal | None = None
        for secret, name, role in self._tokens:
            if hmac.compare_digest(given_b, secret) and found is None:
                found = Principal(name, role, "api")
        return found

    def api_principal(self, method: str, headers: Mapping[str, str], host: str | None) -> Principal:
        """Authorize a REST request (Bearer token). Raises AuthError."""
        if self.open:
            if not is_safe(method) and cross_site(headers, host):
                raise AuthError(403, "cross-site request refused")
            return Principal("anonymous", "admin", "api")
        scheme, _, given = headers.get("authorization", "").partition(" ")
        principal = self.check_token(given.strip()) if scheme.lower() == "bearer" and given else None
        if principal is None:
            raise AuthError(401, "missing or bad bearer token")
        if not is_safe(method) and not principal.is_admin:
            raise AuthError(403, f"token {principal.name!r} has role viewer: read-only")
        return principal

    # sessions -----------------------------------------------------------------------
    def _prune(self, now: float) -> None:
        for sid in [s for s, sess in self._sessions.items() if sess.expires <= now]:
            del self._sessions[sid]

    def login(self, token: str) -> Session | None:
        principal = self.check_token(token)
        if principal is None:
            return None
        now = self._clock()
        session = Session(sid=secrets.token_urlsafe(32), name=principal.name, role=principal.role,
                          csrf=secrets.token_urlsafe(32), expires=now + SESSION_TTL_S)
        with self._lock:
            self._prune(now)
            if len(self._sessions) >= MAX_SESSIONS:   # drop the oldest
                oldest = min(self._sessions.values(), key=lambda s: s.expires)
                del self._sessions[oldest.sid]
            self._sessions[session.sid] = session
        return session

    def session(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        with self._lock:
            sess = self._sessions.get(sid)
            if sess is None:
                return None
            if sess.expires <= self._clock():
                del self._sessions[sid]
                return None
            return sess

    def logout(self, sid: str | None) -> None:
        if sid:
            with self._lock:
                self._sessions.pop(sid, None)

    def ui_session(self, sid: str | None) -> Session | None:
        """The UI caller's session; in open mode a fixed anonymous admin session."""
        if self.open:
            return Session(sid="", name="anonymous", role="admin", csrf=self.open_csrf, expires=float("inf"))
        return self.session(sid)

    @staticmethod
    def check_csrf(session: Session, given: str | None) -> bool:
        return bool(given) and hmac.compare_digest(given.encode(), session.csrf.encode())
