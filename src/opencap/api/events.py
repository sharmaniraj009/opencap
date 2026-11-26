"""Structured log events of the configuration API (plan 15: LOG-03, LOG-06, LOG-29; plan 10: API-14).

    config.write_failed     ERROR  a write that could not reach the files (503, 507, 500)
    config.change_rejected  INFO   a config write answered with a 4xx (nothing changed, nothing audited, D-043)
    api.request_refused     INFO / WARNING (401, 403)   a request refused before it reached a handler,
                            or by a handler, with 401, 403, 412, 413, 422 or 428
    api.started, api.bind_failed (core/runtime.py)

A refused config write is reported once, as `config.change_rejected`; `api.request_refused` covers
everything else (authentication, roles, body size, malformed queries and paths). Events carry the token
NAME (`who`), never its value, and never a request body, query string or field value (LOG-R12).
Every line is a plain structlog event, so it reaches stdout with the process log.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import structlog

log = structlog.get_logger("opencap.api")

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
REFUSED_STATUSES = frozenset({401, 403, 412, 413, 422, 428})
MSG_MAX = 300
UNKNOWN = "unknown"

# Route function name -> the audit action the change would have had (docs/decisions.md, audit.py).
ROUTE_ACTIONS = {
    "create_channel": "channel.create", "patch_channel": "channel.update", "delete_channel": "channel.delete",
    "create_device": "device.create", "put_device": "device.update", "patch_device": "device.update",
    "delete_device": "device.delete",
    "create_tags": "tag.create", "put_tag": "tag.update", "patch_tag": "tag.update", "delete_tag": "tag.delete",
    "put_map_defaults": "map.defaults", "put_map_entry": "map.put", "patch_map_entry": "map.update",
    "delete_map_entry": "map.delete",
    "put_device_override": "override.put", "patch_device_override": "override.update",
    "delete_device_override": "override.delete", "put_channel_override": "override.channel",
    "delete_channel_override": "override.channel_delete",
    "project_import": "config.import", "reload": "reload",
}


def client_address(request: Any) -> str:
    client = request.client
    return client.host if client is not None else UNKNOWN


def request_target(request: Any) -> str:
    """The object the URL names (`<channel>/<device>`, `<device>#<tag>`, a channel); object names only."""
    params = request.path_params
    if "tag" in params:
        return f"{params.get('device_id', '')}#{params['tag']}"
    return str(params.get("device_id") or params.get("channel") or "project")


def request_action(request: Any) -> str:
    route = request.scope.get("route")
    name = getattr(route, "name", None)
    return ROUTE_ACTIONS.get(name, f"{request.method} {getattr(route, 'path', request.url.path)}")


def principal_fields(request: Any) -> dict[str, str]:
    """`who` and `via` of the authenticated request (set by the auth dependency)."""
    principal = getattr(request.state, "principal", None)
    if principal is None:
        return {"who": UNKNOWN, "via": "ui" if request.headers.get("x-opencap-client") == "ui" else "api"}
    return {"who": principal.name, "via": principal.via}


def log_config_error(request: Any, exc: Any) -> None:
    """Log what the API is about to answer for a `ConfigError`: LOG-03 and LOG-06."""
    from opencap.config.store import StorageError

    who = principal_fields(request)
    if isinstance(exc, StorageError):
        fields: dict[str, Any] = {**who, "action": request_action(request), "files": list(exc.files),
                                  "reason": exc.reason, "status": exc.status}
        if exc.reason == "partial":
            fields["restore"] = [f"{name}.bak" for name in exc.files]    # copy these back over the files
        log.error("config.write_failed", error=exc.message[:MSG_MAX], **fields)
        return
    if request.method in WRITE_METHODS and 400 <= exc.status < 500:
        first = exc.errors[0] if exc.errors else {}
        log.info("config.change_rejected", **who, action=request_action(request), target=request_target(request),
                 status=exc.status, count=len(exc.errors) or 1,
                 loc=first.get("loc", []), msg=str(first.get("msg", exc.message))[:MSG_MAX])


class RefusalLog:
    """`api.request_refused`, at most once per second for the same client and status.

    Events dropped in between are counted and reported as `suppressed` on the next one that is
    written, so a flood of bad tokens costs one line a second, not one per request. (Replace with
    the shared flood control of #133 when it lands.)
    """

    PRUNE_AT = 512

    def __init__(self, interval_s: float = 1.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.interval_s = interval_s
        self._clock = clock
        self._state: dict[tuple[str, int], list[float | int]] = {}      # key -> [last written, suppressed]

    def refused(self, *, method: str, path: str, status: int, who: str | None, client: str, via: str) -> None:
        now = self._clock()
        key = (client, status)
        entry = self._state.get(key)
        if entry is not None and now - entry[0] < self.interval_s:
            entry[1] += 1
            return
        suppressed = int(entry[1]) if entry is not None else 0
        self._state[key] = [now, 0]
        if len(self._state) > self.PRUNE_AT:
            self._state = {k: v for k, v in self._state.items() if now - v[0] < 60 or v[1]}
        fields: dict[str, Any] = {"method": method, "path": path, "status": status, "who": who or UNKNOWN,
                                  "client": client, "via": via}
        if suppressed:
            fields["suppressed"] = suppressed
        (log.warning if status in (401, 403) else log.info)("api.request_refused", **fields)
