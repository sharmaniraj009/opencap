"""Configuration REST API under `/api/v1` (contract: docs/architecture/03-deep-dive.md §3.6).

The open equivalent of KEPServerEX's Configuration API, and the only API the web UI uses
(ADR-0010). Resources:

    /api/v1/channels[/{channel}]           /api/v1/channels/{channel}/devices
    /api/v1/devices[/{id}]                 /api/v1/devices/{id}/tags[/{tag}]
    /api/v1/map, /map/defaults, /map/devices/{id}                     devices_map.yaml
    /api/v1/overrides, /overrides/devices/{id}, /overrides/channels/{c} overrides.yaml
    /api/v1/project (whole config), /project/status, /project/pending, /project/import,
    /project/validate (= opencap check), /project/revisions, /project/schema
    /api/v1/audit, /api/v1/whoami, /api/v1/support
    runtime: /api/v1/status, /api/v1/devices/{id}/status|values|plan|identity,
             POST /api/v1/devices/{id}/read-once, POST /api/v1/reload

Device ids are `<channel>/<device>`. Reads return the resource with the config `revision`
(also as `ETag`). **Writes need `If-Match: <revision>`**: 428 without it, 412 when the
config changed since. `PUT` replaces, `PATCH` is a JSON merge patch (RFC 7386, `null`
removes a key). A write changes the files only; `POST /api/v1/reload` applies, or add
`?apply=true` to a write to reload right after it (D-039).
"""

from __future__ import annotations

import time
from typing import Annotated, Any, Callable

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from opencap.api import schemas as S
from opencap.api import collection_query as CQ
from opencap.api.context import ApiContext
from opencap.config.models import Channel, Device, DeviceMapEntry, DeviceOverride, PublishCfg, Tag
from opencap.config.store import CURRENT_WRITER, MAX_IMPORT_BYTES, ConfigError, ConfigStore, Invalid, Result
from opencap.core.support import support_info
from opencap.drivers import catalogue

JSON_TYPES = ("application/json",)
IDENTITY_TTL_S = 300
IfMatch = Annotated[str | None, Header(alias="If-Match")]
JsonBody = Annotated[Any, Body()]
Apply = Annotated[bool, Query(description="reload right after the write")]


class Unavailable(Exception):
    """The API runs without a config directory (e.g. embedded in a test)."""


class PreconditionRequired(ConfigError):
    status = 428


def _ctx(request: Request) -> ApiContext:
    return request.app.state.ctx


def _store(request: Request) -> ConfigStore:
    store = _ctx(request).store
    if store is None:
        raise Unavailable()
    return store


def _need_if_match(if_match: str | None) -> str:
    if not if_match or not if_match.strip():
        raise PreconditionRequired("If-Match required: send the revision from your last read "
                                   "(GET /api/v1/project/status gives the current one)")
    return if_match


def with_rev(data: Any, status: int = 200) -> JSONResponse:
    headers = {"ETag": f'"{data["revision"]}"'} if isinstance(data, dict) and data.get("revision") else {}
    return JSONResponse(data, status_code=status, headers=headers)


async def _write(request: Request, fn: Callable[[], Result], *, if_match: str | None, apply: bool,
                 status: int = 200) -> JSONResponse:
    """Run one store change (If-Match required), audit it, optionally reload, answer."""
    _need_if_match(if_match)
    ctx = _ctx(request)
    result: Result = await run_in_threadpool(as_writer, request.state.principal, fn)
    body = result.body()
    if apply and result.files:
        try:
            body["reload"] = await ctx.reload(request.state.principal)
        except Exception as exc:  # noqa: BLE001 - the change is written; say the reload failed
            body["reload_error"] = f"reload failed, old config kept: {exc}"
    body["pending_reload"] = (await run_in_threadpool(ctx.tracker.status)).get("pending_reload", False)
    return with_rev(body, status)


def as_writer(principal: Any, fn: Callable[[], Any]) -> Any:
    """Run a store call (in a worker thread) on behalf of `principal`: the store audits the
    change with this name while it still holds its lock."""
    token = CURRENT_WRITER.set(principal)
    try:
        return fn()
    finally:
        CURRENT_WRITER.reset(token)


def _lookup(fn, device_id: str) -> Any:
    try:
        return fn(device_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"device {device_id!r} is not polled") from None


def build_router() -> APIRouter:
    r = APIRouter(prefix="/api/v1")

    # --- driver catalogue (API-16) ---------------------------------------------------
    @r.get("/drivers", response_model=S.DriverList, response_model_exclude_unset=True, responses=S.ERR_AUTH)
    def list_drivers() -> dict[str, Any]:
        """Every implemented driver: name, transport, and whether it reads identity and browses."""
        return catalogue.listing()

    @r.get("/drivers/{name}", response_model=S.DriverEntry, response_model_exclude_unset=True,
           responses={**S.ERR_AUTH, **S.ERR_NOT_FOUND})
    def get_driver(name: str) -> dict[str, Any]:
        """One driver: the JSON Schema of its `params`, the dtypes it reads and address examples."""
        try:
            return catalogue.entry(name)
        except LookupError:
            raise HTTPException(status_code=404, detail=f"unknown driver {name!r}") from None

    # --- who / status ------------------------------------------------------------
    @r.get("/whoami", response_model=S.Whoami, response_model_exclude_unset=True, responses=S.ERR_READ)
    def whoami(request: Request) -> dict[str, Any]:
        p = request.state.principal
        ctx = _ctx(request)
        w = ctx.store.writable() if ctx.store is not None else {"writable": False, "reason": "no config directory"}
        return {"name": p.name, "role": p.role, "open": ctx.auth.open,
                "config_writable": w["writable"], "config_writable_reason": w["reason"]}

    @r.get("/support", response_model=S.Support, response_model_exclude_unset=True, responses=S.ERR_READ)
    def support_summary(request: Request) -> dict[str, Any]:
        """Support information (viewer): safe to paste into a public issue, as `opencap info`."""
        ctx = _ctx(request)
        cfg = getattr(getattr(ctx.poller, "loaded", None), "poller", None)
        revision = ctx.store.revision() if ctx.store is not None else None
        return support_info(cfg, revision)

    @r.get("/status", response_model=S.Status, response_model_exclude_unset=True, responses=S.ERR_READ)
    def status(request: Request) -> dict[str, Any]:
        """Dashboard in one call: process health, every polled device, pending reload."""
        ctx = _ctx(request)
        return {"health": ctx.poller.health(), "channels": ctx.poller.channels_summary(),
                "config": ctx.tracker.status()}

    # --- runtime per device (device ids contain "/": suffix routes come first) ----
    @r.get("/devices/{device_id:path}/tags", response_model=S.TagList, response_model_exclude_unset=True, responses=S.ERR_READ)
    def list_tags(request: Request, device_id: str, query: CQ.CollectionParams) -> JSONResponse:
        """The device's tags. `q`, `sort`, `order`, `limit` and `offset` as on the other lists (API-17, API-18);
        sortable: name, address, dtype, scan_ms, enabled, description."""
        dev = _store(request).get_device(device_id)
        page = CQ.apply_query(dev["device"]["tags"], query, CQ.TAG_SORT)
        return with_rev({"tags": page["items"], "total": page["total"], "limit": page["limit"],
                         "offset": page["offset"], "revision": dev["revision"]})

    @r.post("/devices/{device_id:path}/tags", status_code=201, response_model=S.WriteResult, response_model_exclude_unset=True, responses=S.ERR_WRITE)
    async def create_tags(request: Request, device_id: str, body: JsonBody, if_match: IfMatch = None,
                          apply: Apply = False) -> JSONResponse:
        return await _write(request, lambda: _store(request).create_tags(device_id, body, if_match=if_match),
                            if_match=if_match, apply=apply, status=201)

    @r.get("/devices/{device_id:path}/tags/{tag}", response_model=S.TagResource, response_model_exclude_unset=True, responses=S.ERR_READ)
    def get_tag(request: Request, device_id: str, tag: str) -> JSONResponse:
        return with_rev(_store(request).get_tag(device_id, tag))

    @r.put("/devices/{device_id:path}/tags/{tag}", response_model=S.WriteResult, response_model_exclude_unset=True, responses=S.ERR_WRITE)
    async def put_tag(request: Request, device_id: str, tag: str, body: JsonBody, if_match: IfMatch = None,
                      apply: Apply = False) -> JSONResponse:
        return await _write(request, lambda: _store(request).update_tag(device_id, tag, body, partial=False,
                                                                        if_match=if_match),
                            if_match=if_match, apply=apply)

    @r.patch("/devices/{device_id:path}/tags/{tag}", response_model=S.WriteResult, response_model_exclude_unset=True, responses=S.ERR_WRITE)
    async def patch_tag(request: Request, device_id: str, tag: str, body: JsonBody, if_match: IfMatch = None,
                        apply: Apply = False) -> JSONResponse:
        return await _write(request, lambda: _store(request).update_tag(device_id, tag, body, partial=True,
                                                                        if_match=if_match),
                            if_match=if_match, apply=apply)

    @r.delete("/devices/{device_id:path}/tags/{tag}", response_model=S.WriteResult, response_model_exclude_unset=True, responses=S.ERR_WRITE)
    async def delete_tag(request: Request, device_id: str, tag: str, if_match: IfMatch = None,
                         apply: Apply = False) -> JSONResponse:
        return await _write(request, lambda: _store(request).delete_tag(device_id, tag, if_match=if_match),
                            if_match=if_match, apply=apply)

    @r.get("/devices/{device_id:path}/status", response_model=S.DeviceDetail, response_model_exclude_unset=True, responses=S.ERR_READ)
    def device_status(request: Request, device_id: str) -> dict[str, Any]:
        return _lookup(_ctx(request).poller.device_detail, device_id)

    @r.get("/devices/{device_id:path}/values", response_model=S.TagValues, response_model_exclude_unset=True, responses=S.ERR_READ)
    def device_values(request: Request, device_id: str) -> dict[str, Any]:
        return _lookup(_ctx(request).poller.device_values, device_id)

    @r.get("/devices/{device_id:path}/plan", response_model=S.DevicePlan, response_model_exclude_unset=True, responses=S.ERR_READ)
    def device_plan(request: Request, device_id: str) -> dict[str, Any]:
        return _lookup(_ctx(request).poller.device_plan, device_id)

    @r.get("/devices/{device_id:path}/identity", response_model=S.DeviceIdentity, response_model_exclude_unset=True, responses=S.ERR_READ)
    async def device_identity(request: Request, device_id: str, refresh: bool = False) -> dict[str, Any]:
        """Identity over the existing connection. Cached for IDENTITY_TTL_S because asking can
        cost a request on the wire (Modbus FC43); `?refresh=true` (admin) asks again now."""
        ctx = _ctx(request)
        if refresh and not request.state.principal.is_admin:
            raise HTTPException(status_code=403, detail="refreshing the identity needs the admin role")
        now = time.monotonic()
        cached = ctx.identity_cache.get(device_id)
        if cached is not None and not refresh and now - cached[0] < IDENTITY_TTL_S:
            return {**cached[1], "cached": True, "age_s": round(now - cached[0])}
        try:
            result = await ctx.poller.device_identity(device_id)
            if result.get("connected"):
                ctx.identity_cache[device_id] = (now, result)
            return {**result, "cached": False}
        except KeyError:
            raise HTTPException(status_code=404, detail=f"device {device_id!r} is not polled") from None
        except Exception as exc:  # noqa: BLE001 - e.g. the safety timeout: report, do not 500
            return {"connected": True, "identity": {}, "error": f"{type(exc).__name__}: {exc}"}

    @r.post("/devices/{device_id:path}/read-once", response_model=S.TagValues, response_model_exclude_unset=True, responses=S.ERR_RUN)
    async def read_once(request: Request, device_id: str) -> dict[str, Any]:
        try:
            return await _ctx(request).poller.read_once(device_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"device {device_id!r} is not polled") from None
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @r.post("/reload", response_model=S.ReloadResult, response_model_exclude_unset=True, responses=S.ERR_RUN)
    async def reload(request: Request) -> dict[str, Any]:
        try:
            return await _ctx(request).reload(request.state.principal)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"reload failed, old config kept: {exc}") from None

    # --- channels ----------------------------------------------------------------
    @r.get("/channels", response_model=S.ChannelList, response_model_exclude_unset=True, responses=S.ERR_READ)
    def list_channels(request: Request, query: CQ.CollectionParams) -> JSONResponse:
        """All channels. `q`, `sort`, `order`, `limit` and `offset` (API-17, API-18);
        sortable: name, enabled, devices."""
        data = _store(request).list_channels()
        page = CQ.apply_query(data["channels"], query, CQ.CHANNEL_SORT)
        return with_rev({"channels": page["items"], "total": page["total"], "limit": page["limit"],
                         "offset": page["offset"], "revision": data["revision"]})

    @r.post("/channels", status_code=201, response_model=S.WriteResult, response_model_exclude_unset=True, responses=S.ERR_WRITE)
    async def create_channel(request: Request, body: JsonBody, if_match: IfMatch = None,
                             apply: Apply = False) -> JSONResponse:
        return await _write(request, lambda: _store(request).create_channel(body, if_match=if_match),
                            if_match=if_match, apply=apply, status=201)

    @r.get("/channels/{channel}/devices", response_model=S.DeviceList, response_model_exclude_unset=True, responses=S.ERR_READ)
    def list_channel_devices(request: Request, channel: str) -> JSONResponse:
        store = _store(request)
        store.get_channel(channel)                      # 404 for an unknown channel
        return with_rev(store.list_devices(channel))

    @r.post("/channels/{channel}/devices", status_code=201, response_model=S.WriteResult, response_model_exclude_unset=True, responses=S.ERR_WRITE)
    async def create_device(request: Request, channel: str, body: JsonBody, if_match: IfMatch = None,
                            apply: Apply = False) -> JSONResponse:
        return await _write(request, lambda: _store(request).create_device(channel, body, if_match=if_match),
                            if_match=if_match, apply=apply, status=201)

    @r.get("/channels/{channel}", response_model=S.ChannelResource, response_model_exclude_unset=True, responses=S.ERR_READ)
    def get_channel(request: Request, channel: str) -> JSONResponse:
        return with_rev(_store(request).get_channel(channel))

    @r.patch("/channels/{channel}", response_model=S.WriteResult, response_model_exclude_unset=True, responses=S.ERR_WRITE)
    async def patch_channel(request: Request, channel: str, body: JsonBody, if_match: IfMatch = None,
                            apply: Apply = False) -> JSONResponse:
        return await _write(request, lambda: _store(request).update_channel(channel, body, if_match=if_match),
                            if_match=if_match, apply=apply)

    @r.delete("/channels/{channel}", response_model=S.WriteResult, response_model_exclude_unset=True, responses=S.ERR_WRITE)
    async def delete_channel(request: Request, channel: str, if_match: IfMatch = None,
                             apply: Apply = False) -> JSONResponse:
        return await _write(request, lambda: _store(request).delete_channel(channel, if_match=if_match),
                            if_match=if_match, apply=apply)

    # --- devices -----------------------------------------------------------------
    @r.get("/devices", response_model=S.DeviceList, response_model_exclude_unset=True, responses=S.ERR_READ)
    def list_devices(request: Request, query: CQ.CollectionParams, channel: str | None = None) -> JSONResponse:
        """All devices, or one channel's. `q`, `sort`, `order`, `limit` and `offset` (API-17, API-18);
        sortable: id, channel, name, driver, host, port, enabled, tags."""
        data = _store(request).list_devices(channel)
        page = CQ.apply_query(data["devices"], query, CQ.DEVICE_SORT)
        return with_rev({"devices": page["items"], "total": page["total"], "limit": page["limit"],
                         "offset": page["offset"], "revision": data["revision"]})

    @r.get("/devices/{device_id:path}", response_model=S.DeviceResource, response_model_exclude_unset=True, responses=S.ERR_READ)
    def get_device(request: Request, device_id: str) -> JSONResponse:
        return with_rev(_store(request).get_device(device_id))

    @r.put("/devices/{device_id:path}", response_model=S.WriteResult, response_model_exclude_unset=True, responses=S.ERR_WRITE)
    async def put_device(request: Request, device_id: str, body: JsonBody, if_match: IfMatch = None,
                         apply: Apply = False) -> JSONResponse:
        return await _write(request, lambda: _store(request).update_device(device_id, body, partial=False,
                                                                           if_match=if_match),
                            if_match=if_match, apply=apply)

    @r.patch("/devices/{device_id:path}", response_model=S.WriteResult, response_model_exclude_unset=True, responses=S.ERR_WRITE)
    async def patch_device(request: Request, device_id: str, body: JsonBody, if_match: IfMatch = None,
                           apply: Apply = False) -> JSONResponse:
        return await _write(request, lambda: _store(request).update_device(device_id, body, partial=True,
                                                                           if_match=if_match),
                            if_match=if_match, apply=apply)

    @r.delete("/devices/{device_id:path}", response_model=S.WriteResult, response_model_exclude_unset=True, responses=S.ERR_WRITE)
    async def delete_device(request: Request, device_id: str, if_match: IfMatch = None,
                            apply: Apply = False) -> JSONResponse:
        return await _write(request, lambda: _store(request).delete_device(device_id, if_match=if_match),
                            if_match=if_match, apply=apply)

    # --- devices_map.yaml --------------------------------------------------------
    @r.get("/map", response_model=S.MapResource, response_model_exclude_unset=True, responses=S.ERR_READ)
    def get_map(request: Request) -> JSONResponse:
        return with_rev(_store(request).get_map())

    @r.put("/map/defaults", response_model=S.WriteResult, response_model_exclude_unset=True, responses=S.ERR_WRITE)
    async def put_map_defaults(request: Request, body: JsonBody, if_match: IfMatch = None,
                               apply: Apply = False) -> JSONResponse:
        return await _write(request, lambda: _store(request).put_map_defaults(body, if_match=if_match),
                            if_match=if_match, apply=apply)

    @r.get("/map/devices/{device_id:path}", response_model=S.MapEntry, response_model_exclude_unset=True, responses=S.ERR_READ)
    def get_map_entry(request: Request, device_id: str) -> JSONResponse:
        return with_rev(_store(request).get_map_entry(device_id))

    @r.put("/map/devices/{device_id:path}", response_model=S.WriteResult, response_model_exclude_unset=True, responses=S.ERR_WRITE)
    async def put_map_entry(request: Request, device_id: str, body: JsonBody, if_match: IfMatch = None,
                            apply: Apply = False) -> JSONResponse:
        return await _write(request, lambda: _store(request).put_map_entry(device_id, body, partial=False,
                                                                           if_match=if_match),
                            if_match=if_match, apply=apply)

    @r.patch("/map/devices/{device_id:path}", response_model=S.WriteResult, response_model_exclude_unset=True, responses=S.ERR_WRITE)
    async def patch_map_entry(request: Request, device_id: str, body: JsonBody, if_match: IfMatch = None,
                              apply: Apply = False) -> JSONResponse:
        return await _write(request, lambda: _store(request).put_map_entry(device_id, body, partial=True,
                                                                           if_match=if_match),
                            if_match=if_match, apply=apply)

    @r.delete("/map/devices/{device_id:path}", response_model=S.WriteResult, response_model_exclude_unset=True, responses=S.ERR_WRITE)
    async def delete_map_entry(request: Request, device_id: str, if_match: IfMatch = None,
                               apply: Apply = False) -> JSONResponse:
        return await _write(request, lambda: _store(request).delete_map_entry(device_id, if_match=if_match),
                            if_match=if_match, apply=apply)

    # --- overrides.yaml ----------------------------------------------------------
    @r.get("/overrides", response_model=S.Overrides, response_model_exclude_unset=True, responses=S.ERR_READ)
    def get_overrides(request: Request) -> JSONResponse:
        return with_rev(_store(request).get_overrides())

    @r.get("/overrides/channels/{channel}", response_model=S.ChannelOverride, response_model_exclude_unset=True, responses=S.ERR_READ)
    def get_channel_override(request: Request, channel: str) -> JSONResponse:
        return with_rev(_store(request).get_channel_override(channel))

    @r.put("/overrides/channels/{channel}", response_model=S.WriteResult, response_model_exclude_unset=True, responses=S.ERR_WRITE)
    async def put_channel_override(request: Request, channel: str, body: JsonBody, if_match: IfMatch = None,
                                   apply: Apply = False) -> JSONResponse:
        return await _write(request, lambda: _store(request).put_channel_override(channel, body, if_match=if_match),
                            if_match=if_match, apply=apply)

    @r.delete("/overrides/channels/{channel}", response_model=S.WriteResult, response_model_exclude_unset=True, responses=S.ERR_WRITE)
    async def delete_channel_override(request: Request, channel: str, if_match: IfMatch = None,
                                      apply: Apply = False) -> JSONResponse:
        return await _write(request, lambda: _store(request).delete_channel_override(channel, if_match=if_match),
                            if_match=if_match, apply=apply)

    @r.get("/overrides/devices/{device_id:path}", response_model=S.DeviceOverride, response_model_exclude_unset=True, responses=S.ERR_READ)
    def get_device_override(request: Request, device_id: str) -> JSONResponse:
        return with_rev(_store(request).get_device_override(device_id))

    @r.put("/overrides/devices/{device_id:path}", response_model=S.WriteResult, response_model_exclude_unset=True, responses=S.ERR_WRITE)
    async def put_device_override(request: Request, device_id: str, body: JsonBody, if_match: IfMatch = None,
                                  apply: Apply = False) -> JSONResponse:
        return await _write(request, lambda: _store(request).put_device_override(device_id, body, partial=False,
                                                                                 if_match=if_match),
                            if_match=if_match, apply=apply)

    @r.patch("/overrides/devices/{device_id:path}", response_model=S.WriteResult, response_model_exclude_unset=True, responses=S.ERR_WRITE)
    async def patch_device_override(request: Request, device_id: str, body: JsonBody, if_match: IfMatch = None,
                                    apply: Apply = False) -> JSONResponse:
        return await _write(request, lambda: _store(request).put_device_override(device_id, body, partial=True,
                                                                                 if_match=if_match),
                            if_match=if_match, apply=apply)

    @r.delete("/overrides/devices/{device_id:path}", response_model=S.WriteResult, response_model_exclude_unset=True, responses=S.ERR_WRITE)
    async def delete_device_override(request: Request, device_id: str, if_match: IfMatch = None,
                                     apply: Apply = False) -> JSONResponse:
        return await _write(request, lambda: _store(request).delete_device_override(device_id, if_match=if_match),
                            if_match=if_match, apply=apply)

    # --- project -----------------------------------------------------------------
    @r.get("/project", response_model=S.ProjectExport, response_model_exclude_unset=True, responses=S.ERR_READ)
    def project(request: Request, download: bool = False) -> JSONResponse:
        """The whole config (the four files' texts): a backup, or a body for /project/import."""
        data = _store(request).export()
        resp = with_rev(data)
        if download:
            resp.headers["Content-Disposition"] = f'attachment; filename="opencap-config-{data["revision"]}.json"'
        return resp

    @r.get("/project/status", response_model=S.ProjectStatus, response_model_exclude_unset=True, responses=S.ERR_READ)
    def project_status(request: Request) -> JSONResponse:
        store = _store(request)
        w = store.writable()
        return with_rev({**store.summary(), "writable": w["writable"], "writable_reason": w["reason"],
                         "status": _ctx(request).tracker.status()})

    @r.get("/project/pending", response_model=S.Pending, response_model_exclude_unset=True, responses=S.ERR_READ)
    def project_pending(request: Request) -> dict[str, Any]:
        """Does the config on disk differ from the running config? (cheap; the UI polls it)"""
        return _ctx(request).tracker.status()

    @r.get("/project/schema", response_model=S.ProjectSchema, response_model_exclude_unset=True, responses=S.ERR_READ)
    def project_schema() -> dict[str, Any]:
        models = {"channel": Channel, "device": Device, "tag": Tag, "map_entry": DeviceMapEntry,
                  "map_defaults": PublishCfg, "device_override": DeviceOverride}
        return {name: m.model_json_schema() for name, m in models.items()}

    @r.get("/project/validate", response_model=S.ValidateReport, response_model_exclude_unset=True, responses=S.ERR_READ)
    def project_validate(request: Request, site: str | None = None, verbose: bool = False) -> dict[str, Any]:
        """`opencap check` as JSON: validation, block plan and cycle estimates of the files."""
        from opencap.core.plan_report import check_report

        return check_report(_store(request).dir, site=site, verbose=verbose)

    @r.get("/project/revisions", response_model=S.ProjectRevisions, response_model_exclude_unset=True, responses=S.ERR_READ)
    def project_revisions(request: Request, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        """Revisions written through the API, newest first (from the audit log)."""
        entries = [e for e in _ctx(request).audit.tail(limit * 4) if e.get("revision") and e.get("files")]
        return {"current": _store(request).revision(),
                "revisions": [{k: e.get(k) for k in ("revision", "ts", "who", "action", "target", "files")}
                              for e in entries[:limit]]}

    @r.post("/project/import", response_model=S.ImportReport, response_model_exclude_unset=True, responses=S.ERR_WRITE)
    async def project_import(request: Request, dry_run: bool = True, filename: str | None = None,
                             if_match: IfMatch = None, apply: Apply = False) -> JSONResponse:
        """A KEPServerEX project.json or an opencap export (GET /api/v1/project). Dry run by default."""
        store = _store(request)
        if not dry_run:
            _need_if_match(if_match)
        raw = await read_json_body(request)
        report = await run_in_threadpool(as_writer, request.state.principal,
                                         lambda: store.import_config(raw, dry_run=dry_run, filename=filename,
                                                                     if_match=if_match))
        ctx = _ctx(request)
        result: Result | None = report.pop("result", None)
        if result is not None:
            report["applied"], report["changes"] = True, result.changes
            if apply and result.files:
                try:
                    report["reload"] = await ctx.reload(request.state.principal)
                except Exception as exc:  # noqa: BLE001
                    report["reload_error"] = f"reload failed, old config kept: {exc}"
        else:
            report["applied"] = False
        report["pending_reload"] = (await run_in_threadpool(ctx.tracker.status)).get("pending_reload", False)
        return with_rev(report)

    # --- audit -------------------------------------------------------------------
    @r.get("/audit", response_model=S.AuditList, response_model_exclude_unset=True, responses=S.ERR_READ)
    def audit(request: Request, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
              action: str | None = None, who: str | None = None, target: str | None = None) -> dict[str, Any]:
        entries = _ctx(request).audit.tail(limit, offset, action=action, who=who, target=target)
        return {"entries": entries, "limit": limit, "offset": offset}

    return r


async def read_json_body(request: Request) -> bytes:
    """The raw request body, which must be JSON (a text/plain or form post is refused, so a
    foreign web page cannot submit it) and at most MAX_IMPORT_BYTES."""
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype not in JSON_TYPES and not ctype.endswith("+json"):
        raise Invalid("send the file as JSON", [{"loc": [], "msg": "Content-Type must be application/json",
                                                 "type": "content_type"}])
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_IMPORT_BYTES:
        raise Invalid("upload too large", [{"loc": [], "msg": f"larger than {MAX_IMPORT_BYTES} bytes",
                                            "type": "too_large"}])
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_IMPORT_BYTES:
            raise Invalid("upload too large", [{"loc": [], "msg": f"larger than {MAX_IMPORT_BYTES} bytes",
                                                "type": "too_large"}])
        chunks.append(chunk)
    return b"".join(chunks)
