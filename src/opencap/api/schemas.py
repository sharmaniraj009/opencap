"""Response models of the admin + config API: the shapes behind `api/openapi.json` (issue #32).

They describe what the handlers already return; they do not change it. Resources whose
content is user configuration (a device, a tag, a map entry) are typed as open objects
(`dict[str, Any]`): their field-level schema is `GET /api/v1/project/schema`
(= `schema/` JSON Schema of the config files). Additions within v1 are additive only
(`api/README.md`), so every model allows extra keys.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

Obj = dict[str, Any]


class _Open(BaseModel):
    """Extra keys are allowed: v1 only ever grows."""

    model_config = ConfigDict(extra="allow")


# --- errors ---------------------------------------------------------------------------------

class ErrorItem(_Open):
    loc: list[str | int] = Field(default_factory=list)
    msg: str
    type: str


class Error(_Open):
    """Every non-2xx answer. `detail` is a message, or the list of problems for a 400/422."""

    detail: str | list[ErrorItem]
    message: str | None = None


# --- runtime ----------------------------------------------------------------------------------

class DeviceStates(_Open):
    ok: int = 0
    offline: int = 0
    demoted: int = 0


class OpcUaServerHealth(_Open):
    running: bool
    error: str | None = None


class Health(_Open):
    status: str
    version: str
    instance: str
    mqtt_connected: bool
    saf_queue: int
    channels: int
    devices: DeviceStates
    opcua_server: OpcUaServerHealth | None = None   # only when poller.yaml enables the OPC UA server


class ChannelDevice(_Open):
    id: str
    driver: str
    state: str | None = None
    last_good: str | None = None
    errors: int | None = None
    cycle_ms: float | None = None


class ChannelStatus(_Open):
    name: str
    devices: list[ChannelDevice]


class TagValueJson(_Open):
    value: Any = None
    quality: str
    source_ts: str | None = None


# `GET /devices/{id}/values` and `POST /devices/{id}/read-once`: tag name -> value
TagValues = dict[str, TagValueJson]


class DeviceDetail(_Open):
    id: str
    channel: str
    driver: str
    host: str | None = None
    port: int | None = None
    enabled: bool
    tags: int
    blocks: Any = None
    bad_config: list[str]
    plan_error: str | None = None
    topic: str | None = None
    sub_device_id: str | None = None
    state: str | None = None


class DevicePlan(_Open):
    pass


class DeviceIdentity(_Open):
    connected: bool
    identity: Obj = Field(default_factory=dict)
    cached: bool | None = None
    age_s: int | None = None
    error: str | None = None


class ReloadResult(_Open):
    """Which channels a reload touched (`unchanged` is not part of the audit detail)."""

    started: list[str] = Field(default_factory=list)
    restarted: list[str] = Field(default_factory=list)
    stopped: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)


class AuthInfo(BaseModel):
    open: bool


class Whoami(_Open):
    name: str
    role: str
    open: bool
    config_writable: bool
    config_writable_reason: str | None = None


class ChannelDiff(_Open):
    added: list[str]
    removed: list[str]
    changed: list[str]


class Pending(_Open):
    available: bool
    pending_reload: bool
    revision: str | None = None
    disk_valid: bool | None = None
    error: str | None = None
    channels: ChannelDiff | None = None
    poller_changed: list[str] | None = None
    restart_needed: list[str] | None = None


class Status(_Open):
    health: Health
    channels: list[ChannelStatus]
    config: Pending


# --- configuration ------------------------------------------------------------------------------

class Revisioned(_Open):
    revision: str


class ChannelListItem(_Open):
    name: str
    enabled: bool
    devices: int
    override: Obj | None = None


class ChannelList(Revisioned):
    channels: list[ChannelListItem]


class ChannelResource(Revisioned):
    name: str
    enabled: bool
    override: Obj | None = None
    devices: list[Obj]


class DeviceList(Revisioned):
    devices: list[Obj]


class DeviceResource(Revisioned):
    device: Obj
    map_entry: Obj | None = None
    override: Obj | None = None


class TagList(Revisioned):
    tags: list[Obj]


class TagResource(Revisioned):
    tag: Obj
    override: Obj | None = None


class MapResource(Revisioned):
    defaults: Obj
    devices: Obj


class MapEntry(Revisioned):
    id: str
    entry: Obj


class Overrides(Revisioned):
    channels: Obj
    devices: Obj


class DeviceOverride(Revisioned):
    id: str
    override: Obj


class ChannelOverride(Revisioned):
    name: str
    override: Obj


class WriteResult(Revisioned):
    """The answer of every change (POST/PUT/PATCH/DELETE under /api/v1): what was written."""

    ok: bool
    action: str
    target: str
    files: list[str]
    changes: list[Obj]
    truncated: int
    warnings: list[str]
    comments_lost: list[str] | None = None
    data: Any = None
    pending_reload: bool
    reload: ReloadResult | None = None
    reload_error: str | None = None


class ProjectExport(Revisioned):
    format: str
    version: int
    opencap: str
    exported_at: str
    files: dict[str, str]


class ProjectStatus(Revisioned):
    config_dir: str
    files: dict[str, Obj]
    valid: bool
    error: str | None = None
    errors: list[Obj]
    warnings: list[str]
    counts: dict[str, int]
    drivers: list[str]
    dtypes: list[str]
    writable: bool
    writable_reason: str | None = None
    status: Pending


class ProjectRevision(_Open):
    revision: str | None = None
    ts: str | None = None
    who: str | None = None
    action: str | None = None
    target: str | None = None
    files: list[str] | None = None


class ProjectRevisions(_Open):
    current: str
    revisions: list[ProjectRevision]


class ImportReport(Revisioned):
    """A dry run or an applied import (KEPServerEX `project.json` or an opencap export)."""

    applied: bool
    pending_reload: bool
    changes: list[Obj] | None = None
    reload: ReloadResult | None = None
    reload_error: str | None = None


class AuditList(_Open):
    entries: list[Obj]
    limit: int
    offset: int


# `GET /project/schema`: model name -> JSON Schema; `GET /project/validate`: `opencap check` report
ProjectSchema = dict[str, Obj]
ValidateReport = Obj

# Error answers shared by routes, by status code
ERR_AUTH = {401: {"model": Error, "description": "missing or wrong token (`WWW-Authenticate: Bearer`)"},
            403: {"model": Error, "description": "the token's role may not do this (viewer: GET only)"}}
ERR_NOT_FOUND = {404: {"model": Error, "description": "unknown channel, device, tag or entry"}}
ERR_WRITE = {
    400: {"model": Error, "description": "a reload that failed ('reload failed, old config kept')"},
    404: ERR_NOT_FOUND[404],
    409: {"model": Error, "description": "conflicts with the config on disk (e.g. a name in use)"},
    412: {"model": Error, "description": "If-Match does not match: the config changed since your read"},
    422: {"model": Error, "description": "the change is invalid; `detail` lists each problem with its `loc`; "
                                         "nothing was written"},
    413: {"model": Error, "description": "body too large"},
    428: {"model": Error, "description": "If-Match missing"},
    503: {"model": Error, "description": "the API runs without a config directory"},
}

ERR_READ = {**ERR_AUTH, **ERR_NOT_FOUND, 503: ERR_WRITE[503]}
ERR_RUN = {**ERR_AUTH, **ERR_NOT_FOUND,
           400: {"model": Error, "description": "reload failed, old config kept"},
           409: {"model": Error, "description": "the device is not in a state to do this (e.g. not connected)"}}
ERR_WRITE = {**ERR_AUTH, **ERR_WRITE}


# --- live values: WebSocket /api/v1/ws (#31) -------------------------------------------------
# OpenAPI cannot describe WebSocket messages, so these models go into `components.schemas` by
# hand (`server._openapi`) and `GET /api/v1/ws` points to them with `x-websocket`.

class WsTagRef(_Open):
    device: str
    tag: str


class WsSubscription(_Open):
    """What a connection receives. Each `subscribe` replaces the previous subscription."""

    devices: list[str] = Field(default_factory=list, description="device ids: every tag of each")
    tags: list[WsTagRef] = Field(default_factory=list, description="single tags (of devices not in `devices`)")


class WsSubscribe(_Open):
    """Client to server: the only message a client sends."""

    subscribe: WsSubscription


class WsChange(_Open):
    tag: str
    value: Any = Field(None, description='non-finite floats are sent as "NaN", "Infinity", "-Infinity"')
    quality: str
    source_ts: str | None = None


class WsValues(_Open):
    """`snapshot`: every subscribed tag of the device, replacing what the client holds for it (sent after
    each `subscribe`, when a device comes back, and to a client that fell behind). `changes`: what
    changed since the previous message for the device. At most one per device per `conflate_ms`."""

    type: str = Field(description='"snapshot" or "changes"', pattern="^(snapshot|changes)$")
    device: str
    version: int = Field(description="change counter of the device's values; grows within a connection")
    changes: list[WsChange]
    missing: list[str] | None = Field(None, description="subscribed tags the device does not have (snapshot)")


class WsSubscribed(_Open):
    type: str = Field(pattern="^subscribed$")
    devices: list[str]
    tags: list[WsTagRef]
    conflate_ms: int


class WsUnavailable(_Open):
    """The device is not polled (unknown, its channel disabled, or not reloaded yet). Stays
    subscribed: a `snapshot` follows when it is polled again."""

    type: str = Field(pattern="^unavailable$")
    device: str
    detail: str


class WsError(_Open):
    """The server could not use a client message; the connection stays open."""

    type: str = Field(pattern="^error$")
    detail: str


WS_CLIENT_MESSAGES = (WsSubscribe,)
WS_SERVER_MESSAGES = (WsSubscribed, WsValues, WsUnavailable, WsError)
