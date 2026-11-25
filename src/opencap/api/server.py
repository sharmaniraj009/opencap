"""Admin REST API (PLAN.md section 9), configuration API (`/api/v1`) and the web UI (`/ui/`).
Bind to localhost or the VPN only.

Auth (D-019, D-040): `Authorization: Bearer <token>`. Tokens are named and carry a role
(`viewer`: GET only; `admin`: also changes), see `opencap.api.auth`. The legacy single
token from the env var named in `poller.yaml` `api.token_env` is the admin "default".
`/health`, `/metrics`, `/api/v1/auth-info` and the UI's static files are open; everything
else needs a token, checked before the request body is read (`guard.py`, which also caps
body sizes).
With no token set the API refuses to start unless it is bound to a loopback address, and
then it is read-only and answers only requests addressed to localhost.

Routes: runtime status (`/channels`, `/devices/...`, `POST /reload`; unversioned, kept for
existing clients), the configuration API (`/api/v1/...`, see `config_api.py`), the live-values
WebSocket `/api/v1/ws` (`live.py`, authenticated in the handshake) and the UI, a static bundle
that uses only `/api/v1` (ADR-0010, `src/opencap/api/ui/`).
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from opencap import metrics
from opencap.api import schemas as S
from opencap.api.audit import AuditLog
from opencap.api.auth import Auth, AuthError, Principal
from opencap.api.config_api import Unavailable, build_router
from opencap.api.context import ApiContext
from opencap.api.guard import ApiGuard
from opencap.api.live import CONFLATE_S, PROTOCOL, LiveHub, live_endpoint
from opencap.config.models import ApiCfg
from opencap.config.store import ConfigError, ConfigStore

if TYPE_CHECKING:
    from opencap.core.runtime import Poller

API_VERSION = "1.1.0"     # the contract in api/openapi.json; additive changes bump the minor
LIVE_PATH = "/api/v1/ws"
UI_DIR = Path(__file__).parent / "ui"
# The UI has no inline script or style; Alpine.js runs as its CSP build (no eval).
UI_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                                "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
                                "base-uri 'none'; object-src 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}


class _UiFiles(StaticFiles):
    """The UI bundle, with the security headers on every file."""

    async def get_response(self, path: str, scope: Any) -> Response:
        response = await super().get_response(path, scope)
        response.headers.update(UI_HEADERS)
        if path in ("", ".", "index.html"):
            response.headers["Cache-Control"] = "no-cache"
        return response


def _is_loopback(host: str) -> bool:
    if host in {"localhost"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def create_app(poller: "Poller", cfg: ApiCfg, *, config_dir: Path | None = None,
               environ: dict[str, str] | None = None) -> FastAPI:
    """The FastAPI app. `config_dir` defaults to `poller.config_dir`; without one the
    configuration API answers 503 (runtime routes still work)."""
    auth = Auth(cfg, environ)
    if auth.open and not _is_loopback(cfg.bind):
        raise RuntimeError(
            f"API bound to {cfg.bind} without a token: set {cfg.token_env} or bind to 127.0.0.1"
        )
    config_dir = config_dir if config_dir is not None else getattr(poller, "config_dir", None)
    store = ConfigStore(Path(config_dir)) if config_dir is not None else None
    ctx = ApiContext(poller=poller, cfg=cfg, auth=auth, store=store,
                     audit=AuditLog(cfg.audit_log or None))

    def require_token(request: Request) -> Principal:
        principal = auth.principal(request.method, request.headers, request.headers.get("host"))
        request.state.principal = principal
        return principal

    app = FastAPI(title="opencap", version=API_VERSION, docs_url=None, redoc_url=None, openapi_url=None,
                  description="Admin and configuration API of opencap (contract: api/README.md).")
    app.state.ctx = ctx
    app.state.live = LiveHub(poller)
    # Authenticate before any body is read, and cap body sizes (guard.py). Added before CORS, so
    # the CORS middleware sits outside it and answers preflight requests.
    app.add_middleware(ApiGuard, auth=auth)
    if cfg.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=cfg.cors_origins, allow_credentials=False,
                           allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                           allow_headers=["Authorization", "Content-Type", "If-Match", "X-Opencap-Client"],
                           expose_headers=["ETag"])
    auth_dep = [Depends(require_token)]

    @app.exception_handler(AuthError)
    async def _auth_error(request: Request, exc: AuthError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse({"detail": exc.message}, status_code=exc.status, headers=headers)

    @app.exception_handler(ConfigError)
    async def _config_error(request: Request, exc: ConfigError) -> JSONResponse:
        body: dict[str, Any] = {"detail": exc.errors if exc.errors else exc.message}
        if exc.errors:
            body["message"] = exc.message
        return JSONResponse(body, status_code=exc.status)

    @app.exception_handler(Unavailable)
    async def _unavailable(request: Request, exc: Unavailable) -> JSONResponse:
        return JSONResponse({"detail": "the configuration API needs a config directory"}, status_code=503)

    def _lookup(fn, device_id: str) -> Any:
        try:
            return fn(device_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown device {device_id!r}") from None

    @app.get("/health", response_model=S.Health, response_model_exclude_unset=True, tags=["open"])
    def health() -> dict:
        return poller.health()

    @app.get("/api/v1/auth-info", response_model=S.AuthInfo, tags=["open"])
    def auth_info() -> dict:
        """Open: does this API need a token? (lets the UI skip its login page in open mode)"""
        return {"open": auth.open}

    @app.get("/metrics", tags=["open"], response_class=Response,
             responses={200: {"content": {"text/plain": {"schema": {"type": "string"}}},
                              "description": "Prometheus text exposition format"}})
    def prometheus() -> Response:
        return Response(metrics.render(), media_type="text/plain; version=0.0.4")

    @app.get("/channels", dependencies=auth_dep, deprecated=True, response_model=list[S.ChannelStatus],
             response_model_exclude_unset=True, responses=S.ERR_AUTH, tags=["legacy"])
    def channels() -> list:
        return poller.channels_summary()

    # Device ids contain "/" ("<channel>/<device>"), so the specific routes come first.
    @app.get("/devices/{device_id:path}/values", dependencies=auth_dep, deprecated=True, response_model=S.TagValues,
             responses=S.ERR_READ, tags=["legacy"])
    def device_values(device_id: str) -> dict:
        return _lookup(poller.device_values, device_id)

    @app.post("/devices/{device_id:path}/read-once", dependencies=auth_dep, deprecated=True, response_model=S.TagValues,
              responses=S.ERR_RUN, tags=["legacy"])
    async def read_once(device_id: str) -> dict:
        try:
            return await poller.read_once(device_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown device {device_id!r}") from None

    @app.get("/devices/{device_id:path}", dependencies=auth_dep, deprecated=True, response_model=S.DeviceDetail,
             response_model_exclude_unset=True, responses=S.ERR_READ, tags=["legacy"])
    def device(device_id: str) -> dict:
        return _lookup(poller.device_detail, device_id)

    @app.post("/reload", dependencies=auth_dep, deprecated=True, response_model=S.ReloadResult,
              responses=S.ERR_RUN, tags=["legacy"])
    async def reload(request: Request) -> dict:
        try:
            return await ctx.reload(request.state.principal)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"reload failed, old config kept: {exc}") from None

    app.include_router(build_router(), dependencies=auth_dep)
    app.add_api_websocket_route(LIVE_PATH, live_endpoint)
    _document_live(app)

    @app.get("/", include_in_schema=False)
    def root() -> Response:
        return RedirectResponse("/ui/", status_code=307)

    @app.get("/ui", include_in_schema=False)
    def ui_root() -> Response:
        return RedirectResponse("/ui/", status_code=307)

    app.mount("/ui", _UiFiles(directory=str(UI_DIR), html=True), name="ui")
    return app


def _document_live(app: FastAPI) -> None:
    """OpenAPI has no WebSocket operations: describe `GET /api/v1/ws` (the upgrade) by hand, with
    the message schemas under `components.schemas` and an `x-websocket` block pointing to them."""
    from pydantic.json_schema import models_json_schema

    base = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = base()
        models = [*S.WS_CLIENT_MESSAGES, *S.WS_SERVER_MESSAGES]
        _, top = models_json_schema([(m, "validation") for m in models], ref_template="#/components/schemas/{model}")
        schema.setdefault("components", {}).setdefault("schemas", {}).update(top["$defs"])
        ref = "#/components/schemas/{}".format
        error = {"content": {"application/json": {"schema": {"$ref": ref("Error")}}}}
        schema["paths"][LIVE_PATH] = {"get": {
            "tags": ["live"],
            "summary": "Live values (WebSocket)",
            "operationId": "live_values_ws",
            "description": (
                "WebSocket upgrade: the Quick Client feed (#31). Authenticated in the handshake with the REST "
                "API's tokens: `Authorization: Bearer <token>`, or for browsers the subprotocol "
                f"`opencap.bearer.<base64url(token)>` offered next to `{PROTOCOL}`; the server selects "
                f"`{PROTOCOL}`. A browser `Origin` must be the API's own origin or listed in `api.cors_origins`. "
                "The client sends `WsSubscribe`; the server answers `WsSubscribed`, then a `snapshot` of each "
                "device and conflated `changes` (at most one message per device per `conflate_ms`), "
                "`unavailable` for a device that is not polled, `error` for a message it cannot use. A client "
                "that falls behind gets fresh snapshots instead of a backlog."),
            "parameters": [{"name": "Sec-WebSocket-Protocol", "in": "header", "required": False,
                            "schema": {"type": "string"},
                            "description": f"`{PROTOCOL}`, optionally `opencap.bearer.<base64url(token)>`"}],
            "responses": {
                "101": {"description": f"Switching Protocols: a WebSocket speaking `{PROTOCOL}` (JSON text frames)"},
                "400": {**error, "description": f"subprotocols offered without `{PROTOCOL}`"},
                "401": {**error, "description": "missing or wrong token (`WWW-Authenticate: Bearer`)"},
                "403": {**error, "description": "origin not allowed, or open mode reached through a non-loopback "
                                                "host name"},
                "503": {**error, "description": "too many live connections"},
            },
            "x-websocket": {
                "subprotocol": PROTOCOL,
                "conflate_ms": round(CONFLATE_S * 1000),
                "client_messages": {"oneOf": [{"$ref": ref(m.__name__)} for m in S.WS_CLIENT_MESSAGES]},
                "server_messages": {"oneOf": [{"$ref": ref(m.__name__)} for m in S.WS_SERVER_MESSAGES]},
            },
        }}
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
