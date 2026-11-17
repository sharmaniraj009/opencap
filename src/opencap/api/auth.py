"""API authentication: named bearer tokens with roles (D-019, D-033).

Tokens (poller.yaml `api`):
- `token_env`: the legacy single token, named "default", role admin (D-019);
- `tokens`: any number of `{name, role, token_env}`. The secret is read from the env var;
  the yaml holds only its name. A listed token whose env var is unset stops the start.

Roles: `viewer` may use safe methods only (GET/HEAD): status, values, config reads,
validation, export, audit. `admin` may also change things (config writes, import, reload,
read-once). With no token at all the API starts only on a loopback bind, and every caller
is the admin "anonymous" (D-019); then unsafe requests a browser marks as cross-site are
refused, so a web page cannot drive the open local API.

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


def cross_site(headers: Mapping[str, str], host: str | None) -> bool:
    """True when a browser says the request comes from another web origin."""
    if headers.get("sec-fetch-site", "").lower() == "cross-site":
        return True
    origin = headers.get("origin")
    if origin and origin != "null" and host:
        return origin.split("://", 1)[-1].rstrip("/") != host
    return origin == "null"


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
        self._cors = set(cfg.cors_origins)

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
            origin = headers.get("origin")
            if not is_safe(method) and cross_site(headers, host) and origin not in self._cors:
                raise AuthError(403, "cross-site request refused")
            return Principal("anonymous", "admin", via)
        scheme, _, given = headers.get("authorization", "").partition(" ")
        found = self.check_token(given.strip()) if scheme.lower() == "bearer" and given.strip() else None
        if found is None:
            raise AuthError(401, "missing or bad bearer token")
        principal = Principal(found[0], found[1], via)
        if not is_safe(method) and not principal.is_admin:
            raise AuthError(403, f"token {principal.name!r} has role viewer: read-only")
        return principal
