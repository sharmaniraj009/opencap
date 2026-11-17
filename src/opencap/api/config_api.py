"""Configuration REST API (the KEPServerEX Configuration API equivalent).

Paths follow KEPServerEX's layout: `/config/channels/{channel}/devices/{device}/tags/{tag}`.
The publish map and the overrides are keyed by device id (`<channel>/<device>`):
`/config/map/devices/{id}`, `/config/overrides/devices/{id}`. Bodies are JSON; `PUT`
replaces, `PATCH` is a JSON merge patch (RFC 7386: `null` removes a key). Every response
to a change carries the new config `revision` (also as `ETag`); send it back as
`If-Match` to refuse a change when someone else changed the config in between.

Changes are written to the config files only. `POST /reload` applies them.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Header, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from opencap.api.context import ApiContext
from opencap.config.models import Channel, Device, DeviceMapEntry, DeviceOverride, PublishCfg, Tag
from opencap.config.store import MAX_IMPORT_BYTES, ConfigStore, Invalid, NotFound, Result

JSON_TYPES = ("application/json",)
IfMatch = Annotated[str | None, Header(alias="If-Match")]


class Unavailable(Exception):
    """The API runs without a config directory (e.g. embedded in a test)."""


def _ctx(request: Request) -> ApiContext:
    return request.app.state.ctx


def _store(request: Request) -> ConfigStore:
    store = _ctx(request).store
    if store is None:
        raise Unavailable()
    return store


def _with_rev(data: dict[str, Any], status: int = 200) -> JSONResponse:
    headers = {"ETag": f'"{data["revision"]}"'} if isinstance(data, dict) and data.get("revision") else {}
    return JSONResponse(data, status_code=status, headers=headers)


def _done(request: Request, result: Result, status: int = 200) -> JSONResponse:
    ctx = _ctx(request)
    ctx.record(request.state.principal, result)
    body = result.body()
    body["pending_reload"] = ctx.tracker.status().get("pending_reload", False)
    return _with_rev(body, status)


def _dev_id(channel: str, device: str) -> str:
    return f"{channel}/{device}"


def build_router() -> APIRouter:
    r = APIRouter()

    # --- overview ----------------------------------------------------------------
    @r.get("/config")
    def config_summary(request: Request) -> JSONResponse:
        store = _store(request)
        return _with_rev({**store.summary(), "status": _ctx(request).tracker.status()})

    @r.get("/config/schema")
    def config_schema() -> dict[str, Any]:
        models = {"channel": Channel, "device": Device, "tag": Tag, "map_entry": DeviceMapEntry,
                  "map_defaults": PublishCfg, "device_override": DeviceOverride}
        return {name: m.model_json_schema() for name, m in models.items()}

    # --- channels ----------------------------------------------------------------
    @r.get("/config/channels")
    def list_channels(request: Request) -> list[dict[str, Any]]:
        return _store(request).list_channels()

    @r.post("/config/channels", status_code=201)
    def create_channel(request: Request, body: Any = Body(...), if_match: IfMatch = None) -> JSONResponse:
        return _done(request, _store(request).create_channel(body, if_match=if_match), 201)

    @r.get("/config/channels/{channel}")
    def get_channel(request: Request, channel: str) -> JSONResponse:
        return _with_rev(_store(request).get_channel(channel))

    @r.patch("/config/channels/{channel}")
    def patch_channel(request: Request, channel: str, body: Any = Body(...),
                      if_match: IfMatch = None) -> JSONResponse:
        return _done(request, _store(request).update_channel(channel, body, if_match=if_match))

    @r.delete("/config/channels/{channel}")
    def delete_channel(request: Request, channel: str, if_match: IfMatch = None) -> JSONResponse:
        return _done(request, _store(request).delete_channel(channel, if_match=if_match))

    # --- devices -----------------------------------------------------------------
    @r.get("/config/devices")
    def list_all_devices(request: Request, channel: str | None = None) -> list[dict[str, Any]]:
        return _store(request).list_devices(channel)

    @r.get("/config/channels/{channel}/devices")
    def list_devices(request: Request, channel: str) -> list[dict[str, Any]]:
        store = _store(request)
        store.get_channel(channel)                     # 404 for an unknown channel
        return store.list_devices(channel)

    @r.post("/config/channels/{channel}/devices", status_code=201)
    def create_device(request: Request, channel: str, body: Any = Body(...),
                      if_match: IfMatch = None) -> JSONResponse:
        return _done(request, _store(request).create_device(channel, body, if_match=if_match), 201)

    @r.get("/config/channels/{channel}/devices/{device}")
    def get_device(request: Request, channel: str, device: str) -> JSONResponse:
        return _with_rev(_store(request).get_device(_dev_id(channel, device)))

    @r.put("/config/channels/{channel}/devices/{device}")
    def put_device(request: Request, channel: str, device: str, body: Any = Body(...),
                   if_match: IfMatch = None) -> JSONResponse:
        return _done(request, _store(request).update_device(_dev_id(channel, device), body, partial=False,
                                                            if_match=if_match))

    @r.patch("/config/channels/{channel}/devices/{device}")
    def patch_device(request: Request, channel: str, device: str, body: Any = Body(...),
                     if_match: IfMatch = None) -> JSONResponse:
        return _done(request, _store(request).update_device(_dev_id(channel, device), body, partial=True,
                                                            if_match=if_match))

    @r.delete("/config/channels/{channel}/devices/{device}")
    def delete_device(request: Request, channel: str, device: str, if_match: IfMatch = None) -> JSONResponse:
        return _done(request, _store(request).delete_device(_dev_id(channel, device), if_match=if_match))

    # --- tags --------------------------------------------------------------------
    @r.get("/config/channels/{channel}/devices/{device}/tags")
    def list_tags(request: Request, channel: str, device: str) -> list[dict[str, Any]]:
        return _store(request).get_device(_dev_id(channel, device))["device"]["tags"]

    @r.post("/config/channels/{channel}/devices/{device}/tags", status_code=201)
    def create_tags(request: Request, channel: str, device: str, body: Any = Body(...),
                    if_match: IfMatch = None) -> JSONResponse:
        return _done(request, _store(request).create_tags(_dev_id(channel, device), body, if_match=if_match), 201)

    @r.get("/config/channels/{channel}/devices/{device}/tags/{tag}")
    def get_tag(request: Request, channel: str, device: str, tag: str) -> JSONResponse:
        return _with_rev(_store(request).get_tag(_dev_id(channel, device), tag))

    @r.put("/config/channels/{channel}/devices/{device}/tags/{tag}")
    def put_tag(request: Request, channel: str, device: str, tag: str, body: Any = Body(...),
                if_match: IfMatch = None) -> JSONResponse:
        return _done(request, _store(request).update_tag(_dev_id(channel, device), tag, body, partial=False,
                                                         if_match=if_match))

    @r.patch("/config/channels/{channel}/devices/{device}/tags/{tag}")
    def patch_tag(request: Request, channel: str, device: str, tag: str, body: Any = Body(...),
                  if_match: IfMatch = None) -> JSONResponse:
        return _done(request, _store(request).update_tag(_dev_id(channel, device), tag, body, partial=True,
                                                         if_match=if_match))

    @r.delete("/config/channels/{channel}/devices/{device}/tags/{tag}")
    def delete_tag(request: Request, channel: str, device: str, tag: str,
                   if_match: IfMatch = None) -> JSONResponse:
        return _done(request, _store(request).delete_tag(_dev_id(channel, device), tag, if_match=if_match))

    # --- devices_map.yaml --------------------------------------------------------
    @r.get("/config/map")
    def get_map(request: Request) -> JSONResponse:
        return _with_rev(_store(request).get_map())

    @r.put("/config/map/defaults")
    def put_map_defaults(request: Request, body: Any = Body(...), if_match: IfMatch = None) -> JSONResponse:
        return _done(request, _store(request).put_map_defaults(body, if_match=if_match))

    @r.get("/config/map/devices/{device_id:path}")
    def get_map_entry(request: Request, device_id: str) -> JSONResponse:
        return _with_rev(_store(request).get_map_entry(device_id))

    @r.put("/config/map/devices/{device_id:path}")
    def put_map_entry(request: Request, device_id: str, body: Any = Body(...),
                      if_match: IfMatch = None) -> JSONResponse:
        return _done(request, _store(request).put_map_entry(device_id, body, partial=False, if_match=if_match))

    @r.patch("/config/map/devices/{device_id:path}")
    def patch_map_entry(request: Request, device_id: str, body: Any = Body(...),
                        if_match: IfMatch = None) -> JSONResponse:
        return _done(request, _store(request).put_map_entry(device_id, body, partial=True, if_match=if_match))

    @r.delete("/config/map/devices/{device_id:path}")
    def delete_map_entry(request: Request, device_id: str, if_match: IfMatch = None) -> JSONResponse:
        return _done(request, _store(request).delete_map_entry(device_id, if_match=if_match))

    # --- overrides.yaml ----------------------------------------------------------
    @r.get("/config/overrides")
    def get_overrides(request: Request) -> JSONResponse:
        return _with_rev(_store(request).get_overrides())

    @r.get("/config/overrides/channels/{channel}")
    def get_channel_override(request: Request, channel: str) -> JSONResponse:
        return _with_rev(_store(request).get_channel_override(channel))

    @r.put("/config/overrides/channels/{channel}")
    def put_channel_override(request: Request, channel: str, body: Any = Body(...),
                             if_match: IfMatch = None) -> JSONResponse:
        return _done(request, _store(request).put_channel_override(channel, body, if_match=if_match))

    @r.delete("/config/overrides/channels/{channel}")
    def delete_channel_override(request: Request, channel: str, if_match: IfMatch = None) -> JSONResponse:
        return _done(request, _store(request).delete_channel_override(channel, if_match=if_match))

    @r.get("/config/overrides/devices/{device_id:path}")
    def get_device_override(request: Request, device_id: str) -> JSONResponse:
        return _with_rev(_store(request).get_device_override(device_id))

    @r.put("/config/overrides/devices/{device_id:path}")
    def put_device_override(request: Request, device_id: str, body: Any = Body(...),
                            if_match: IfMatch = None) -> JSONResponse:
        return _done(request, _store(request).put_device_override(device_id, body, partial=False,
                                                                  if_match=if_match))

    @r.patch("/config/overrides/devices/{device_id:path}")
    def patch_device_override(request: Request, device_id: str, body: Any = Body(...),
                              if_match: IfMatch = None) -> JSONResponse:
        return _done(request, _store(request).put_device_override(device_id, body, partial=True,
                                                                  if_match=if_match))

    @r.delete("/config/overrides/devices/{device_id:path}")
    def delete_device_override(request: Request, device_id: str, if_match: IfMatch = None) -> JSONResponse:
        return _done(request, _store(request).delete_device_override(device_id, if_match=if_match))

    # --- export / import ---------------------------------------------------------
    @r.get("/config/export")
    def export(request: Request, download: bool = False) -> JSONResponse:
        data = _store(request).export()
        resp = _with_rev(data)
        if download:
            resp.headers["Content-Disposition"] = f'attachment; filename="opencap-config-{data["revision"]}.json"'
        return resp

    @r.post("/config/import")
    async def import_config(request: Request, dry_run: bool = True, filename: str | None = None,
                            if_match: IfMatch = None) -> JSONResponse:
        store = _store(request)
        raw = await read_json_body(request)
        report = await run_in_threadpool(store.import_config, raw, dry_run=dry_run, filename=filename,
                                         if_match=if_match)
        return await run_in_threadpool(import_response, _ctx(request), request.state.principal, report)

    # --- check / audit -----------------------------------------------------------
    @r.get("/check")
    def check(request: Request, site: str | None = None, verbose: bool = False) -> dict[str, Any]:
        from opencap.core.plan_report import check_report

        return check_report(_store(request).dir, site=site, verbose=verbose)

    @r.get("/audit")
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


def import_response(ctx: ApiContext, principal: Any, report: dict[str, Any]) -> JSONResponse:
    result: Result | None = report.pop("result", None)
    if result is not None:
        ctx.record(principal, result, detail={"kind": report["kind"], **(result.data or {})})
        report["applied"] = True
        report["changes"] = result.changes
    else:
        report["applied"] = False
    report["pending_reload"] = ctx.tracker.status().get("pending_reload", False)
    return _with_rev(report)


__all__ = ["build_router", "Unavailable", "NotFound", "read_json_body", "import_response"]
