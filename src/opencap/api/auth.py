"""API authentication: named bearer tokens with roles (D-019, D-040).

Tokens (poller.yaml `api`):
- `token_env`: the legacy single token, named "default", role admin (D-019);
- `tokens`: any number of `{name, role, token_env}`. The secret is read from the env var;
  the yaml holds only its name. A listed token whose env var is unset stops the start.

Roles: `viewer` may use safe methods only (GET/HEAD): status, values, config reads,
validation, export, audit. `admin` may also change things (config writes, import, reload,
read-once). With no token at all the API starts only on a loopback bind (D-019) and is
**read-only**: every caller is the viewer "anonymous", and only requests addressed to a
loopback host name (`Host: 127.0.0.1`, `localhost` or `[::1]`) are answered, so a web page
cannot reach it through DNS rebinding either.

The web UI is a static page that sends the same `Authorization: Bearer` header; it keeps
the token in the browser tab's `sessionStorage`. There are no cookies, so no CSRF surface.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from opencap.config.models import ApiCfg

Role = Literal["viewer", "admin"]
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LEGACY_NAME = "default"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
OPEN_READ_ONLY = ("no API token is configured, so the API is read-only: "
                  "set api.tokens (or api.token_env) in poller.yaml to make changes")


@dataclass(frozen=True)
class Principal:
    name: str
    role: Role
    via: str = "api"            # "api", or "ui" when the request comes from the web UI

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class AuthError(Exception):
    """401 (not authenticated) or 403 (authenticated, not allowed)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def is_safe(method: str) -> bool:
    return method.upper() in SAFE_METHODS


def host_name(host: str | None) -> str:
    """The name part of a Host header: "[::1]:8080" -> "::1", "localhost:8080" -> "localhost"."""
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


class Auth:
    def __init__(self, cfg: ApiCfg, environ: Mapping[str, str] | None = None) -> None:
        env = os.environ if environ is None else environ
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

    def check_token(self, given: str) -> tuple[str, Role] | None:
        """(name, role) of the token, or None. Compares against every token (constant time)."""
        given_b = given.encode()
        found: tuple[str, Role] | None = None
        for secret, name, role in self._tokens:
            if hmac.compare_digest(given_b, secret) and found is None:
                found = (name, role)
        return found

    def principal(self, method: str, headers: Mapping[str, str], host: str | None) -> Principal:
        """Authorize a request. Raises AuthError."""
        via = "ui" if headers.get("x-opencap-client", "") == "ui" else "api"
        if self.open:
            # DNS rebinding: a foreign page reaching the loopback API sends its own host name
            if host_name(host) not in LOOPBACK_HOSTS:
                raise AuthError(403, "no API token is configured: only requests to localhost are answered")
            if not is_safe(method):
                raise AuthError(403, OPEN_READ_ONLY)
            return Principal("anonymous", "viewer", via)
        scheme, _, given = headers.get("authorization", "").partition(" ")
        found = self.check_token(given.strip()) if scheme.lower() == "bearer" and given.strip() else None
        if found is None:
            raise AuthError(401, "missing or bad bearer token")
        principal = Principal(found[0], found[1], via)
        if not is_safe(method) and not principal.is_admin:
            raise AuthError(403, f"token {principal.name!r} has role viewer: read-only")
        return principal
