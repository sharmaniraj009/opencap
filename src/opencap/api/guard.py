"""Request guard: authenticate before the body is read, and cap the body size (D-040).

A pure ASGI middleware in front of the whole app:

- every route except the open ones (`/health`, `/metrics`, `/api/v1/auth-info`, the UI's
  static files) is authenticated from the headers alone, so an unauthenticated caller gets
  401/403 before a single body byte is read or parsed;
- the body is capped at `MAX_BODY` (1 MiB), or `MAX_IMPORT_BODY` (64 MiB) for
  `POST /api/v1/project/import`. A `Content-Length` over the cap is refused at once with 413;
  a chunked body is counted while it is read and refused with 413 when it passes the cap.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException

from opencap.api.auth import Auth, AuthError
from opencap.config.store import MAX_IMPORT_BYTES

MAX_BODY = 1024 * 1024
MAX_IMPORT_BODY = MAX_IMPORT_BYTES
IMPORT_PATH = "/api/v1/project/import"
OPEN_PATHS = frozenset({"/health", "/metrics", "/api/v1/auth-info", "/", "/ui"})
OPEN_PREFIXES = ("/ui/",)


class BodyTooLarge(HTTPException):
    """Raised from `receive()`; FastAPI re-raises HTTPException from its body reader."""

    def __init__(self, limit: int) -> None:
        super().__init__(status_code=413, detail=f"request body larger than {limit} bytes")


def is_open(path: str) -> bool:
    return path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)


def body_limit(method: str, path: str) -> int:
    return MAX_IMPORT_BODY if method == "POST" and path == IMPORT_PATH else MAX_BODY


async def _send_json(send: Any, status: int, detail: str, headers: dict[str, str] | None = None) -> None:
    body = json.dumps({"detail": detail}).encode()
    raw = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    raw += [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    await send({"type": "http.response.start", "status": status, "headers": raw})
    await send({"type": "http.response.body", "body": body})


class ApiGuard:
    def __init__(self, app: Any, auth: Auth) -> None:
        self.app = app
        self.auth = auth

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path, method = scope["path"], scope["method"]
        headers = Headers(scope=scope)
        if not is_open(path) and method != "OPTIONS":      # CORS preflight carries no token
            try:
                self.auth.principal(method, headers, headers.get("host"))
            except AuthError as exc:
                extra = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
                await _send_json(send, exc.status, exc.message, extra)
                return
        limit = body_limit(method, path)
        length = headers.get("content-length")
        if length and length.isdigit() and int(length) > limit:
            await _send_json(send, 413, f"request body larger than {limit} bytes")
            return

        received = 0
        started = False

        async def limited_receive() -> Any:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BodyTooLarge(limit)
            return message

        async def tracking_send(message: Any) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except BodyTooLarge as exc:
            if started:
                raise
            await _send_json(send, 413, exc.detail)
