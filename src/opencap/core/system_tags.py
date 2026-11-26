"""System and diagnostic tags (`docs/spec/system-tags.md`, SYS-03..SYS-11, #120).

`evaluate(state)` is the pure function *engine state -> every system tag*: `{path: (dtype, value)}`.
The conformance vectors (`tests/conformance/system_tags/`) run against it; the Rust engine has the
same function (#56). `snapshot(poller)` builds the state from the running engine, and
`SystemTagTask` recomputes the tags (spec §2.1) and writes the changed ones to virtual devices of
the `ValueCache`, quality always `good`, `source_ts` the computation time. Nothing here causes a
wire request (spec §2.2) and nothing is writable (ADR-0008).

State shape: `tests/conformance/system_tags/README.md`. Values use JSON types; datetimes are
ISO-8601 UTC text with milliseconds and `Z`, or None for the placeholder.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import structlog

from opencap import __version__
from opencap.core.cache import TagValue
from opencap.core.health import iso_utc

if TYPE_CHECKING:
    from opencap.core.runtime import Poller

log = structlog.get_logger("opencap.system_tags")

ENGINE = "python"
U32 = 2**32
TICK_ALIGN_TOLERANCE_S = 0.05      # spec §2.1: ticks are aligned to the UTC second within 50 ms
COALESCE_S = 0.05                  # event updates arriving together are computed once

CLIENT_KINDS = {"mqtt", "sparkplug", "rest_client"}
SERVER_KINDS = {"opcua_server", "modbus_server", "opcda_server"}
OPC_SERVER_KINDS = {"opcua_server", "opcda_server"}
SERIAL_DRIVERS = {"modbus_rtu", "ab_df1"}
TRANSPORT_DEFAULT = {"modbus_rtu": "tcp", "ab_df1": "unmapped"}
SERIAL_DEFAULTS = {"baudrate": 19200, "bytesize": 8, "parity": "N", "stopbits": 1}
PARITY = {"n": "None", "none": "None", "e": "Even", "even": "Even", "o": "Odd", "odd": "Odd",
          "m": "Mark", "mark": "Mark", "s": "Space", "space": "Space"}
COUNTER_KEYS = ("ok", "timeout", "conn_error", "bad_config", "error", "failed_reads", "overruns",
                "rx_bytes", "tx_bytes")

# (level, tag name) -> dtype: the catalogue of the spec (tests/conformance/system_tags_catalogue.json
# is compared with this table by a test).
DTYPES: dict[tuple[str, str], str] = {
    # node
    ("node", "_ProductName"): "string", ("node", "_ProductVersion"): "string",
    ("node", "_Version"): "string", ("node", "_Engine"): "string", ("node", "_Instance"): "string",
    ("node", "_ProjectName"): "string", ("node", "_ProjectTitle"): "string",
    ("node", "_ProjectRevision"): "string", ("node", "_StartTime"): "datetime",
    ("node", "_UptimeS"): "uint64", ("node", "_DateTime"): "string",
    ("node", "_DateTimeLocal"): "string", ("node", "_Date"): "string", ("node", "_Time"): "string",
    ("node", "_Date_Day"): "uint32", ("node", "_Date_DayOfWeek"): "uint32",
    ("node", "_Date_Month"): "uint32", ("node", "_Date_Year2"): "uint32",
    ("node", "_Date_Year4"): "uint32", ("node", "_Time_Hour"): "uint32",
    ("node", "_Time_Hour24"): "uint32", ("node", "_Time_Minute"): "uint32",
    ("node", "_Time_Second"): "uint32", ("node", "_Time_PM"): "bool",
    ("node", "_TotalTagCount"): "uint32", ("node", "_ActiveTagCount"): "uint32",
    ("node", "_ClientCount"): "uint32", ("node", "_OpcClientNames"): "string_array",
    ("node", "_DevicesOk"): "uint32", ("node", "_DevicesOffline"): "uint32",
    ("node", "_DevicesDemoted"): "uint32", ("node", "_DevicesDisabled"): "uint32",
    ("node", "_MqttConnected"): "bool", ("node", "_OutboxMessages"): "uint64",
    ("node", "_OutboxAgeS"): "float64", ("node", "_OutboxDropped"): "uint64",
    # redundancy
    ("redundancy", "_Mode"): "string", ("redundancy", "_Role"): "string",
    # north
    ("north", "_Kind"): "string", ("north", "_Enabled"): "bool", ("north", "_Connected"): "bool",
    ("north", "_Published"): "uint64", ("north", "_Running"): "bool",
    ("north", "_Clients"): "uint32", ("north", "_LastError"): "string",
    ("north", "_OutboxMessages"): "uint64", ("north", "_OutboxBytes"): "uint64",
    ("north", "_OutboxAgeS"): "float64", ("north", "_OutboxCapacityBytes"): "uint64",
    ("north", "_OutboxFull"): "bool", ("north", "_OutboxDiskFreeBytes"): "uint64",
    ("north", "_OutboxDiskLow"): "bool", ("north", "_OutboxAvailable"): "bool",
    ("north", "_OutboxDropped"): "uint64", ("north", "_ForwardMode"): "string",
    # channel
    ("channel", "_Enabled"): "bool", ("channel", "_Description"): "string",
    ("channel", "_Driver"): "string", ("channel", "_DeviceCount"): "uint32",
    ("channel", "_ComId"): "uint32", ("channel", "_BaudRate"): "uint32",
    ("channel", "_DataBits"): "uint32", ("channel", "_Parity"): "string",
    ("channel", "_StopBits"): "float64", ("channel", "_EncapsulationPort"): "uint32",
    ("channel", "_EncapsulationProtocol"): "string",
    # channel_statistics
    ("channel_statistics", "_SuccessfulReads"): "uint32",
    ("channel_statistics", "_FailedReads"): "uint32", ("channel_statistics", "_RxBytes"): "uint32",
    ("channel_statistics", "_TxBytes"): "uint32", ("channel_statistics", "_PendingReads"): "uint32",
    ("channel_statistics", "_MaxPendingReads"): "uint32",
    ("channel_statistics", "_RequestsOk"): "uint64", ("channel_statistics", "_Timeouts"): "uint64",
    ("channel_statistics", "_ConnErrors"): "uint64", ("channel_statistics", "_BadConfig"): "uint64",
    ("channel_statistics", "_OtherErrors"): "uint64", ("channel_statistics", "_Overruns"): "uint64",
    # device
    ("device", "_State"): "string", ("device", "_Error"): "bool", ("device", "_NoError"): "bool",
    ("device", "_SecondsInError"): "uint32", ("device", "_AutoDemoted"): "bool",
    ("device", "_FailedConnection"): "bool", ("device", "_Connected"): "bool",
    ("device", "_LastGood"): "datetime", ("device", "_LastError"): "string",
    ("device", "_ErrorCount"): "uint64", ("device", "_CycleMs"): "float64",
    ("device", "_Overruns"): "uint64", ("device", "_Enabled"): "bool",
    ("device", "_Driver"): "string", ("device", "_DeviceId"): "string",
    ("device", "_Description"): "string", ("device", "_Simulated"): "bool",
    ("device", "_ScanMode"): "string", ("device", "_ScanRateMs"): "uint32",
    ("device", "_RequestTimeout"): "uint32", ("device", "_RequestAttempts"): "uint32",
    ("device", "_AutoDemotionEnabled"): "bool", ("device", "_AutoDemotedFailureCount"): "uint32",
    ("device", "_AutoDemotionIntervalMS"): "uint32", ("device", "_EncapsulationIp"): "string",
    ("device", "_EncapsulationPort"): "uint32", ("device", "_EncapsulationProtocol"): "string",
    # device_statistics
    ("device_statistics", "_SuccessfulReads"): "uint32",
    ("device_statistics", "_FailedReads"): "uint32", ("device_statistics", "_RxBytes"): "uint32",
    ("device_statistics", "_TxBytes"): "uint32", ("device_statistics", "_RequestsOk"): "uint64",
    ("device_statistics", "_Timeouts"): "uint64", ("device_statistics", "_ConnErrors"): "uint64",
    ("device_statistics", "_BadConfig"): "uint64", ("device_statistics", "_OtherErrors"): "uint64",
}


# Names of the `tick` class (spec 2.1): recomputed once per second, not on every device event.
# A name has the same class at every level of the catalogue (a test checks both facts).
TICK_TAGS: frozenset[str] = frozenset({
    "_ActiveTagCount", "_ClientCount", "_Clients", "_Date", "_DateTime", "_DateTimeLocal",
    "_Date_Day", "_Date_DayOfWeek", "_Date_Month", "_Date_Year2", "_Date_Year4", "_DevicesDemoted",
    "_DevicesOffline", "_DevicesOk", "_MaxPendingReads", "_MqttConnected", "_OpcClientNames",
    "_OutboxAgeS", "_OutboxAvailable", "_OutboxBytes", "_OutboxCapacityBytes",
    "_OutboxDiskFreeBytes", "_OutboxDiskLow", "_OutboxDropped", "_OutboxFull", "_OutboxMessages",
    "_PendingReads", "_Published", "_Time", "_Time_Hour", "_Time_Hour24", "_Time_Minute",
    "_Time_PM", "_Time_Second", "_UptimeS"
})


def parse_ts(text: str | None) -> datetime | None:
    if text is None:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def iso_ms(ts: datetime | None) -> str | None:
    return iso_utc(ts)


def _put(out: dict[str, tuple[str, Any]], level: str, path: str, name: str, value: Any) -> None:
    out[path + name] = (DTYPES[(level, name)], value)


# --- node ------------------------------------------------------------------------------------

def _clock(now: datetime, zone: str) -> dict[str, Any]:
    local = now.astimezone(ZoneInfo(zone))
    return {
        "_DateTime": iso_ms(now),
        "_DateTimeLocal": local.isoformat(timespec="milliseconds"),
        "_Date": local.strftime("%Y-%m-%d"),
        "_Time": local.strftime("%H:%M:%S"),
        "_Date_Day": local.day,
        "_Date_DayOfWeek": (local.weekday() + 1) % 7,       # Python: Monday = 0; spec: Sunday = 0
        "_Date_Month": local.month,
        "_Date_Year2": local.year % 100,
        "_Date_Year4": local.year,
        "_Time_Hour": local.hour % 12 or 12,
        "_Time_Hour24": local.hour,
        "_Time_Minute": local.minute,
        "_Time_Second": min(local.second, 59),
        "_Time_PM": local.hour >= 12,
    }


def _is_active(channel: dict[str, Any], dev: dict[str, Any]) -> bool:
    return bool(channel.get("enabled", True) and dev.get("enabled", True))


def _active_devices(state: dict[str, Any]):
    for ch in state.get("channels", []):
        for dev in ch.get("devices", []):
            if _is_active(ch, dev):
                yield ch, dev


def _outbox_age(now: datetime, outbox: dict[str, Any]) -> float:
    oldest = parse_ts(outbox.get("oldest"))
    if oldest is None or not outbox.get("messages"):
        return 0.0
    return round(max((now - oldest).total_seconds(), 0.0), 3)


def _node(state: dict[str, Any], now: datetime, out: dict[str, tuple[str, Any]]) -> None:
    node = state["node"]
    p = "_System."
    version = node["product_version"]
    start = parse_ts(node["start_time"])
    assert start is not None
    for name, value in {
        "_ProductName": "opencap", "_ProductVersion": version, "_Version": f"opencap-{version}",
        "_Engine": node["engine"], "_Instance": node["instance"],
        "_ProjectName": node["project_name"], "_ProjectTitle": node.get("project_title") or "",
        "_ProjectRevision": node["project_revision"], "_StartTime": iso_ms(start),
        "_UptimeS": max(math.floor((now - start).total_seconds()), 0),
    }.items():
        _put(out, "node", p, name, value)
    for name, value in _clock(now, node["timezone"]).items():
        _put(out, "node", p, name, value)

    total = active = 0
    states = {"ok": 0, "offline": 0, "demoted": 0}
    for _, dev in _active_devices(state):
        total += dev["tags_enabled"]
        st = dev["status"]["state"]
        states[st] += 1
        if st != "demoted":
            active += dev["tags_scanned"]
    disabled = sum(1 for ch in state.get("channels", []) for dev in ch.get("devices", [])
                   if not _is_active(ch, dev))
    sessions = node.get("sessions", [])
    north = state.get("north", [])
    mqtt = [a for a in north if a["kind"] == "mqtt" and a.get("enabled", True)]
    outboxes = [a["outbox"] for a in north if a.get("outbox") is not None]
    for name, value in {
        "_TotalTagCount": total, "_ActiveTagCount": active,
        "_ClientCount": len(sessions),
        "_OpcClientNames": sorted(s["name"] for s in sessions if s["kind"] in OPC_SERVER_KINDS),
        "_DevicesOk": states["ok"], "_DevicesOffline": states["offline"],
        "_DevicesDemoted": states["demoted"], "_DevicesDisabled": disabled,
        "_MqttConnected": bool(mqtt) and all(a.get("connected", False) for a in mqtt),
        "_OutboxMessages": sum(o["messages"] for o in outboxes),
        "_OutboxAgeS": max((_outbox_age(now, o) for o in outboxes), default=0.0),
        "_OutboxDropped": sum(o.get("dropped", 0) for o in outboxes),
    }.items():
        _put(out, "node", p, name, value)

    red = node.get("redundancy") or {"mode": "standalone", "role": "active"}
    _put(out, "redundancy", "_System._Redundancy.", "_Mode", red["mode"])
    _put(out, "redundancy", "_System._Redundancy.", "_Role", red["role"])


def _north(state: dict[str, Any], now: datetime, out: dict[str, tuple[str, Any]]) -> None:
    for agent in state.get("north", []):
        p = f"_System._North.{agent['name']}."
        kind = agent["kind"]

        def put(name: str, value: Any, p: str = p) -> None:
            _put(out, "north", p, name, value)

        put("_Kind", kind)
        put("_Enabled", agent.get("enabled", True))
        put("_LastError", agent.get("last_error") or "")
        if kind in CLIENT_KINDS:
            put("_Connected", agent.get("connected", False))
            put("_Published", agent.get("published", 0))
        if kind in SERVER_KINDS:
            put("_Running", agent.get("running", False))
            put("_Clients", agent.get("clients", 0))
        ob = agent.get("outbox")
        if ob is not None:
            put("_OutboxMessages", ob["messages"])
            put("_OutboxBytes", ob["bytes"])
            put("_OutboxAgeS", _outbox_age(now, ob))
            put("_OutboxCapacityBytes", max(ob["max_bytes"] - ob["bytes"], 0))
            put("_OutboxFull", ob.get("full", False))
            put("_OutboxDiskFreeBytes", ob.get("disk_free_bytes") or 0)
            put("_OutboxDiskLow", ob.get("disk_low", False))
            put("_OutboxAvailable", ob.get("available", True))
            put("_OutboxDropped", ob.get("dropped", 0))
            put("_ForwardMode", ob.get("forward_mode") or "ordered")


# --- channels and devices --------------------------------------------------------------------

def _transport(dev: dict[str, Any]) -> str | None:
    if dev["driver"] not in SERIAL_DRIVERS:
        return None
    return dev.get("params", {}).get("transport", TRANSPORT_DEFAULT[dev["driver"]])


def _serial(dev: dict[str, Any], key: str) -> Any:
    return dev.get("params", {}).get(key, SERIAL_DEFAULTS[key])


def _com_id(dev: dict[str, Any]) -> int:
    text = dev.get("params", {}).get("com_port") or dev.get("host") or ""
    m = re.fullmatch(r"COM(\d+)", str(text), flags=re.IGNORECASE)
    return int(m.group(1)) if m else 0


def _shared(values: list[Any], placeholder: Any) -> Any:
    return values[0] if values and all(v == values[0] for v in values) else placeholder


def _counter(dev: dict[str, Any], key: str) -> int:
    return int(dev.get("counters", {}).get(key) or 0)


def _channel(ch: dict[str, Any], out: dict[str, tuple[str, Any]]) -> None:
    name = ch["name"]
    devs = ch.get("devices", [])
    p = f"{name}._System."

    def put(n: str, v: Any) -> None:
        _put(out, "channel", p, n, v)

    put("_Enabled", ch.get("enabled", True))
    put("_Description", ch.get("description") or "")
    put("_Driver", _shared([d["driver"] for d in devs], ""))
    put("_DeviceCount", len(devs))
    transports = [_transport(d) for d in devs]
    if devs and all(t == "serial" for t in transports):
        put("_ComId", _shared([_com_id(d) for d in devs], 0))
        put("_BaudRate", _shared([int(_serial(d, "baudrate")) for d in devs], 0))
        put("_DataBits", _shared([int(_serial(d, "bytesize")) for d in devs], 0))
        put("_Parity", _shared([PARITY[str(_serial(d, "parity")).lower()] for d in devs], ""))
        put("_StopBits", _shared([float(_serial(d, "stopbits")) for d in devs], 0.0))
    if devs and all(t == "tcp" for t in transports):
        put("_EncapsulationPort", _shared([int(d["port"]) for d in devs], 0))
        put("_EncapsulationProtocol", "TCP/IP")

    s = f"{name}._Statistics."

    def sput(n: str, v: Any) -> None:
        _put(out, "channel_statistics", s, n, v)

    def total(key: str) -> int:
        return sum(_counter(d, key) for d in devs)

    sput("_SuccessfulReads", total("ok") % U32)
    sput("_FailedReads", total("failed_reads") % U32)
    if devs and all(d.get("counts_bytes", False) for d in devs):
        sput("_RxBytes", total("rx_bytes") % U32)
        sput("_TxBytes", total("tx_bytes") % U32)
    sput("_PendingReads", ch.get("in_flight", 0))
    sput("_MaxPendingReads", ch.get("max_in_flight", 0))
    sput("_RequestsOk", total("ok"))
    sput("_Timeouts", total("timeout"))
    sput("_ConnErrors", total("conn_error"))
    sput("_BadConfig", total("bad_config"))
    sput("_OtherErrors", total("error"))
    sput("_Overruns", total("overruns"))


def device_status_tags(dev: dict[str, Any], status: dict[str, Any] | None, now: datetime,
                       *, disabled: bool) -> dict[str, Any]:
    """The §8.1 status tags of one device from its status fields (§9)."""
    if disabled or status is None:
        return {"_State": "disabled", "_Error": False, "_NoError": True, "_SecondsInError": 0,
                "_AutoDemoted": False, "_FailedConnection": False, "_Connected": False,
                "_LastGood": None, "_LastError": "", "_ErrorCount": 0, "_CycleMs": 0.0,
                "_Overruns": _counter(dev, "overruns")}
    error = int(status.get("consecutive_failures") or 0) > 0
    since = parse_ts(status.get("error_since"))
    seconds = max(math.floor((now - since).total_seconds()), 0) if error and since else 0
    return {
        "_State": status["state"],
        "_Error": error,
        "_NoError": not error,
        "_SecondsInError": min(seconds, U32 - 1),
        "_AutoDemoted": status["state"] == "demoted",
        "_FailedConnection": bool(status.get("last_connect_failed", False)),
        "_Connected": bool(status.get("connected", False)),
        "_LastGood": iso_ms(parse_ts(status.get("last_good"))),
        "_LastError": status.get("last_error") or "",
        "_ErrorCount": int(status.get("errors") or 0),
        "_CycleMs": float(status.get("cycle_ms") or 0.0),
        "_Overruns": _counter(dev, "overruns"),
    }


def _device(ch: dict[str, Any], dev: dict[str, Any], now: datetime,
            out: dict[str, tuple[str, Any]]) -> None:
    base = f"{ch['name']}.{dev['name']}."
    p = base + "_System."

    def put(n: str, v: Any) -> None:
        _put(out, "device", p, n, v)

    for name, value in device_status_tags(dev, dev.get("status"), now, disabled=not _is_active(ch, dev)).items():
        put(name, value)
    transport = _transport(dev)
    demote_after = int(dev.get("demote_after", 3))
    put("_Enabled", dev.get("enabled", True))
    put("_Driver", dev["driver"])
    put("_DeviceId", dev["host"] if transport == "serial" else f"{dev['host']}:{dev['port']}")
    put("_Description", dev.get("description") or "")
    put("_Simulated", dev["driver"] == "simulator")
    put("_ScanMode", "tag_scan_rate")
    put("_ScanRateMs", int(dev.get("scan_ms", 1000)))
    put("_RequestTimeout", int(dev.get("timeout_ms", 3000)))
    put("_RequestAttempts", max(int(dev.get("retries", 2)), 0) + 1)
    put("_AutoDemotionEnabled", demote_after > 0)
    put("_AutoDemotedFailureCount", max(demote_after, 0))
    put("_AutoDemotionIntervalMS", int(dev.get("demote_for_ms", 30_000)))
    if transport == "tcp":
        put("_EncapsulationIp", dev["host"])
        put("_EncapsulationPort", int(dev["port"]))
        put("_EncapsulationProtocol", "TCP/IP")

    s = base + "_Statistics."

    def sput(n: str, v: Any) -> None:
        _put(out, "device_statistics", s, n, v)

    sput("_SuccessfulReads", _counter(dev, "ok") % U32)
    sput("_FailedReads", _counter(dev, "failed_reads") % U32)
    if dev.get("counts_bytes", False):
        sput("_RxBytes", _counter(dev, "rx_bytes") % U32)
        sput("_TxBytes", _counter(dev, "tx_bytes") % U32)
    sput("_RequestsOk", _counter(dev, "ok"))
    sput("_Timeouts", _counter(dev, "timeout"))
    sput("_ConnErrors", _counter(dev, "conn_error"))
    sput("_BadConfig", _counter(dev, "bad_config"))
    sput("_OtherErrors", _counter(dev, "error"))


def evaluate(state: dict[str, Any]) -> dict[str, tuple[str, Any]]:
    """Every system tag of a snapshot: {path: (dtype, value)}."""
    now = parse_ts(state["now"])
    assert now is not None
    out: dict[str, tuple[str, Any]] = {}
    _node(state, now, out)
    _north(state, now, out)
    for ch in state.get("channels", []):
        _channel(ch, out)
        for dev in ch.get("devices", []):
            _device(ch, dev, now, out)
    return out


# --- the state of the running engine ---------------------------------------------------------

def _metric(name: str, **labels: str) -> int:
    from opencap.metrics import REGISTRY

    return int(REGISTRY.get_sample_value(name, labels) or 0)


# status of an enabled device that has no worker (yet): spec §9, "no cycle counted yet"
_NOT_POLLED: dict[str, Any] = {
    "state": "offline", "consecutive_failures": 0, "error_since": None, "last_good": None, "errors": 0,
    "last_error": None, "cycle_ms": None, "connected": False, "last_connect_failed": False}


def device_counters(device_id: str) -> dict[str, int]:
    """The process-lifetime counters behind `/metrics` for one device (spec §8.3)."""
    out = {key: _metric("poll_requests_total", device=device_id, result=key)
           for key in ("ok", "timeout", "conn_error", "bad_config", "error")}
    out["failed_reads"] = _metric("poll_failed_reads_total", device=device_id)
    out["overruns"] = _metric("poll_overrun_total", device=device_id)
    out["rx_bytes"] = out["tx_bytes"] = 0          # drivers do not count bytes yet (#130)
    return out


def _outbox(poller: Poller) -> dict[str, Any] | None:
    saf = poller.saf
    if saf is None:
        return None
    available = not saf.closed
    oldest = None
    messages = nbytes = 0
    if available:
        messages, nbytes = saf.count(), saf.size_bytes()
        row = saf.db.execute("SELECT created FROM outbox ORDER BY id LIMIT 1").fetchone()
        if row is not None:
            oldest = iso_ms(datetime.fromtimestamp(float(row[0]), UTC))
    return {"messages": messages, "bytes": nbytes, "oldest": oldest, "max_bytes": saf.max_bytes,
            "full": available and nbytes >= saf.max_bytes, "disk_free_bytes": None, "disk_low": False,
            "available": available, "dropped": _metric("saf_dropped_total"), "forward_mode": None}


def opc_sessions(poller: Poller) -> list[dict[str, str]]:
    """Activated client sessions of the OPC UA server (the session name the client gave; the
    application name of spec §5.3 follows with the OPC UA catalogue work, #122)."""
    north = poller.opcua
    server = getattr(north, "_server", None)
    try:
        sessions = list(server.iserver._external_sessions.values())     # asyncua internals
    except AttributeError:
        return []
    return [{"server": "opcua_server", "kind": "opcua_server", "name": str(getattr(s, "name", "") or "")}
            for s in sessions if getattr(s, "is_activated", True)]


def snapshot(poller: Poller, now: datetime | None = None, *, revision: str | None = None,
             max_in_flight: dict[str, int] | None = None) -> dict[str, Any]:
    """The engine state at one instant, in the shape `evaluate` takes."""
    now = now or datetime.now(UTC)
    cfg = poller.loaded.poller
    sessions = opc_sessions(poller)
    link = poller.link
    north: list[dict[str, Any]] = [{
        "name": "mqtt", "kind": "mqtt", "enabled": True, "connected": bool(link and link.connected),
        "published": _metric("mqtt_published_total"), "last_error": None, "outbox": _outbox(poller)}]
    if cfg.opcua_server.enabled:
        north.append({"name": "opcua_server", "kind": "opcua_server", "enabled": True,
                      "running": poller.opcua is not None, "clients": len(sessions),
                      "last_error": poller._opcua_error})
    channels = []
    for ch in poller.loaded.channels:
        if poller._only is not None and ch.name not in poller._only:
            continue
        worker = poller.workers.get(ch.name)
        devices = []
        for dev in ch.devices:
            polled = worker is not None and ch.enabled and dev.enabled
            enabled_tags = sum(1 for t in dev.tags if t.enabled)
            devices.append({
                "name": dev.name, "enabled": dev.enabled, "driver": dev.driver, "host": dev.host,
                "port": dev.port, "params": dev.params, "scan_ms": dev.scan_ms, "timeout_ms": dev.timeout_ms,
                "retries": dev.retries, "demote_after": dev.demote_after, "demote_for_ms": dev.demote_for_ms,
                "description": None, "tags_enabled": enabled_tags,
                "tags_scanned": worker.scanned_tags(dev.id) if polled else 0, "counts_bytes": False,
                "status": worker.device_status(dev.id) if polled else dict(_NOT_POLLED),
                "counters": device_counters(dev.id)})
        channels.append({
            "name": ch.name, "enabled": ch.enabled, "description": None,
            "in_flight": worker.in_flight if worker else 0,
            "max_in_flight": max((max_in_flight or {}).get(ch.name, 0), worker.max_in_flight if worker else 0),
            "devices": devices})
    return {
        "now": iso_ms(now),
        "node": {"product_version": __version__, "engine": ENGINE, "instance": cfg.instance,
                 "project_name": Path(poller.config_dir).name, "project_title": "",
                 "project_revision": revision or "", "start_time": iso_ms(poller.started_at),
                 "timezone": cfg.payload.timezone, "sessions": sessions, "redundancy": None},
        "north": north, "channels": channels}


# --- the task ----------------------------------------------------------------------------------

@dataclass(slots=True)
class _VirtualTag:
    name: str
    enabled: bool = True


@dataclass(slots=True)
class VirtualDevice:
    """What `ValueCache.register()` needs of a device: an id and its tags. Its id is the system-tag
    path without the last segment (`_System`, `Plant A.PLC1._System`), which no configured device
    id can equal (device ids contain `/`, and `_System` is a reserved name)."""

    id: str
    tags: list[_VirtualTag] = field(default_factory=list)


def split_path(path: str) -> tuple[str, str]:
    head, _, name = path.rpartition(".")
    return head, name


class SystemTagTask:
    """Recomputes the system tags and writes the changed ones into the poller's `ValueCache`.

    Update classes (spec §2.1): `event` (a device finished a cycle, `notify()`), `tick` (every
    second, aligned to the UTC second) and `config` (`reconfigure()` after a load or reload). One
    computation serves all three, and only values that changed are written, so the `source_ts` of
    a tag is the time of its last change.
    """

    def __init__(self, poller: Poller, *, clock: Callable[[], datetime] | None = None,
                 revision: Callable[[], str] | None = None) -> None:
        self.poller = poller
        self._clock = clock or (lambda: datetime.now(UTC))
        self._revision_fn = revision
        self._revision = ""
        self._last: dict[str, Any] = {}                # path -> last written value
        self._registered: dict[str, set[str]] = {}     # virtual device id -> tag names
        self._max_in_flight: dict[str, int] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.writes = 0                                # values written, for tests
        self._tick_second = 0                          # UTC second of the last tick step

    # --- lifecycle
    async def start(self) -> None:
        self.reconfigure()
        self.step()
        self._wake.clear()
        self._tick_second = int(time.time())
        self._task = asyncio.create_task(self._run(), name="system-tags")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for device_id in list(self._registered):
            self.poller.cache.unregister(device_id)
        self._registered.clear()
        self._last.clear()

    # --- triggers
    def notify(self, device_id: str | None = None) -> None:     # event class
        self._wake.set()

    def reconfigure(self) -> None:                               # config class
        if self._revision_fn is not None:
            try:
                self._revision = self._revision_fn()
            except Exception as exc:       # noqa: BLE001 - a diagnostic must not break the engine
                log.warning("system_tags.revision_failed", error=str(exc))
        self._wake.set()

    # --- one computation
    def step(self, now: datetime | None = None, *, tick: bool = True) -> int:
        """Compute the tags at `now` and write the changed ones; returns how many were written.
        A `tick` step covers every class; an event or config step leaves the `tick` class tags alone
        (the clock would otherwise be rewritten at every device cycle)."""
        now = now or self._clock()
        for name, worker in self.poller.workers.items():
            self._max_in_flight[name] = max(self._max_in_flight.get(name, 0), worker.max_in_flight)
        state = snapshot(self.poller, now, revision=self._revision, max_in_flight=self._max_in_flight)
        tags = evaluate(state)
        cache = self.poller.cache
        by_device: dict[str, dict[str, Any]] = {}
        for path, (_, value) in tags.items():
            device_id, name = split_path(path)
            by_device.setdefault(device_id, {})[name] = value
        for device_id in set(self._registered) - set(by_device):
            cache.unregister(device_id)
            del self._registered[device_id]
            for path in [p for p in self._last if split_path(p)[0] == device_id]:
                del self._last[path]
        written = 0
        for device_id, values in by_device.items():
            names = set(values)
            if self._registered.get(device_id) != names:
                cache.register(VirtualDevice(device_id, [_VirtualTag(n) for n in sorted(names)]))   # type: ignore[arg-type]
                self._registered[device_id] = names
                for name in names:
                    self._last.pop(f"{device_id}.{name}", None)
            changed = {}
            for name, value in values.items():
                path = f"{device_id}.{name}"
                if not tick and name in TICK_TAGS and path in self._last:
                    continue
                if path not in self._last or self._last[path] != value or type(self._last[path]) is not type(value):
                    self._last[path] = value
                    changed[name] = TagValue(value, "good", now)
            if changed:
                cache.update(device_id, changed)
                written += len(changed)
        self.writes += written
        return written

    # --- the loop
    @staticmethod
    def seconds_to_next_tick(now_epoch: float) -> float:
        """Time until the next UTC second boundary (the tick alignment of spec §2.1)."""
        return 1.0 - (now_epoch % 1.0)

    async def _run(self) -> None:
        while True:
            tick = False
            try:
                await asyncio.wait_for(self._wake.wait(), self.seconds_to_next_tick(time.time()))
                await asyncio.sleep(COALESCE_S)          # let simultaneous events settle
            except TimeoutError:
                tick = True
            self._wake.clear()
            second = int(time.time())
            if second > self._tick_second:               # an event step that crossed the boundary is a tick too
                tick, self._tick_second = True, second
            try:
                self.step(tick=tick)
            except asyncio.CancelledError:
                raise
            except Exception:                            # a diagnostic must never stop the poller
                log.exception("system_tags.step_failed")
