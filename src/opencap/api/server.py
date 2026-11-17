"""Admin REST API (PLAN.md section 9). Bind to localhost or the VPN only.

Auth: `Authorization: Bearer <token>`, the token is read from the env var named in
`poller.yaml` `api.token_env`. `/health` and `/metrics` are open so monitoring can scrape
them; everything else needs the token. With no token set the API refuses to start unless
it is bound to a loopback address.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response

from opencap import metrics
from opencap.config.models import ApiCfg

if TYPE_CHECKING:
    from opencap.core.runtime import Poller


def _is_loopback(host: str) -> bool:
    if host in {"localhost"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def create_app(poller: "Poller", cfg: ApiCfg) -> FastAPI:
    token = os.environ.get(cfg.token_env) or None
    if token is None and not _is_loopback(cfg.bind):
        raise RuntimeError(
            f"API bound to {cfg.bind} without a token: set {cfg.token_env} or bind to 127.0.0.1"
        )

    def require_token(request: Request) -> None:
        if token is None:
            return
        header = request.headers.get("authorization", "")
        scheme, _, given = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(status_code=401, detail="missing or bad bearer token")

    app = FastAPI(title="opencap", docs_url=None, redoc_url=None, openapi_url=None)
    auth = [Depends(require_token)]

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

    @app.get("/channels", dependencies=auth)
    def channels() -> list:
        return poller.channels_summary()

    # Device ids contain "/" ("<channel>/<device>"), so the specific routes come first.
    @app.get("/devices/{device_id:path}/values", dependencies=auth)
    def device_values(device_id: str) -> dict:
        return _lookup(poller.device_values, device_id)

    @app.post("/devices/{device_id:path}/read-once", dependencies=auth)
    async def read_once(device_id: str) -> dict:
        try:
            return await poller.read_once(device_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown device {device_id!r}") from None

    @app.get("/devices/{device_id:path}", dependencies=auth)
    def device(device_id: str) -> dict:
        return _lookup(poller.device_detail, device_id)

    @app.post("/reload", dependencies=auth)
    async def reload() -> dict:
        try:
            return await poller.reload()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"reload failed, old config kept: {exc}") from None

    return app
