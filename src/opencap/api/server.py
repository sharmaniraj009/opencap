"""Admin REST API (PLAN.md section 9), configuration API and web UI. Bind to localhost or
the VPN only.

Auth (D-019, D-029): `Authorization: Bearer <token>`. Tokens are named and carry a role
(`viewer`: GET only; `admin`: also changes), see `opencap.api.auth`. The legacy single
token from the env var named in `poller.yaml` `api.token_env` is the admin "default".
`/health` and `/metrics` are open so monitoring can scrape them; everything else needs a
token. With no token set the API refuses to start unless it is bound to a loopback address.

Routes: runtime status (`/channels`, `/devices/...`), `POST /reload`, the configuration API
(`/config/...`, `/check`, `/audit`, see `config_api.py`) and the UI under `/ui` (`ui.py`).
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from opencap import metrics
from opencap.api.audit import AuditLog
from opencap.api.auth import Auth, AuthError, Principal
from opencap.api.config_api import Unavailable, build_router
from opencap.api.context import ApiContext
from opencap.config.models import ApiCfg
from opencap.config.store import ConfigError, ConfigStore

if TYPE_CHECKING:
    from opencap.core.runtime import Poller

log = structlog.get_logger("opencap.api")


def _is_loopback(host: str) -> bool:
    if host in {"localhost"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def create_app(poller: "Poller", cfg: ApiCfg, *, config_dir: Path | None = None,
               environ: dict[str, str] | None = None, ui: bool = True) -> FastAPI:
    """The FastAPI app. `config_dir` defaults to `poller.config_dir`; without one the
    configuration API answers 503 (runtime routes still work)."""
    auth = Auth(cfg, environ)
    if auth.open and not _is_loopback(cfg.bind):
        raise RuntimeError(
            f"API bound to {cfg.bind} without a token: set {cfg.token_env} or bind to 127.0.0.1"
        )
    config_dir = config_dir if config_dir is not None else getattr(poller, "config_dir", None)
    store = ConfigStore(Path(config_dir)) if config_dir is not None else None
    ctx = ApiContext(poller=poller, cfg=cfg, auth=auth, store=store, audit=AuditLog(cfg.audit_log))

    def require_token(request: Request) -> Principal:
        principal = auth.api_principal(request.method, request.headers, request.headers.get("host"))
        request.state.principal = principal
        return principal

    app = FastAPI(title="opencap", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
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

    @app.get("/health")
    def health() -> dict:
        return poller.health()

    @app.get("/metrics")
    def prometheus() -> Response:
        return Response(metrics.render(), media_type="text/plain; version=0.0.4")

    @app.get("/channels", dependencies=auth_dep)
    def channels() -> list:
        return poller.channels_summary()

    # Device ids contain "/" ("<channel>/<device>"), so the specific routes come first.
    @app.get("/devices/{device_id:path}/values", dependencies=auth_dep)
    def device_values(device_id: str) -> dict:
        return _lookup(poller.device_values, device_id)

    @app.get("/devices/{device_id:path}/plan", dependencies=auth_dep)
    def device_plan(device_id: str) -> dict:
        return _lookup(poller.device_plan, device_id)

    @app.get("/devices/{device_id:path}/identity", dependencies=auth_dep)
    async def device_identity(device_id: str) -> dict:
        try:
            return await poller.device_identity(device_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown device {device_id!r}") from None
        except Exception as exc:  # noqa: BLE001 - e.g. the safety timeout: report, do not 500
            return {"connected": True, "identity": {}, "error": f"{type(exc).__name__}: {exc}"}

    @app.post("/devices/{device_id:path}/read-once", dependencies=auth_dep)
    async def read_once(device_id: str) -> dict:
        try:
            return await poller.read_once(device_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown device {device_id!r}") from None

    @app.get("/devices/{device_id:path}", dependencies=auth_dep)
    def device(device_id: str) -> dict:
        return _lookup(poller.device_detail, device_id)

    @app.post("/reload", dependencies=auth_dep)
    async def reload(request: Request) -> dict:
        try:
            return await ctx.reload(request.state.principal)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"reload failed, old config kept: {exc}") from None

    app.include_router(build_router(), dependencies=auth_dep)

    if ui:
        try:
            from opencap.api.ui import install_ui
        except ModuleNotFoundError as exc:   # the API must not depend on the UI's template engine
            log.warning("ui_disabled", reason=f"{exc.name} is not installed (pip install 'opencap[north]')")
        else:
            install_ui(app, ctx)
    return app
