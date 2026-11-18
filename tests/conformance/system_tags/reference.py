"""Test oracle for the system-tag spec (`docs/spec/system-tags.md`), used to check the vectors.

This is not product code. It evaluates the two vector kinds of `tests/conformance/system_tags/`
exactly as the spec words them, so that a hand-written vector that disagrees with the spec is
caught in CI. When the Python implementation of the spec lands (follow-up issue 1 in §14), the
conformance test runs against `opencap.core.system_tags` instead and this file is deleted.

`evaluate(state)`        snapshot vector input -> {path: (dtype, value)}
`run_sequence(vector)`   device_sequence vector -> [(step index, {name: (dtype, value)})]

Values use JSON types; `datetime` values are ISO-8601 UTC strings with milliseconds and `Z`
(or None for the placeholder), as in the vectors.
"""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from opencap.core.health import DeviceHealth

HERE = Path(__file__).resolve().parent
CATALOGUE = json.loads((HERE / "catalogue.json").read_text(encoding="utf-8"))
U32 = 2**32

CLIENT_KINDS = {"mqtt", "sparkplug", "rest_client"}
SERVER_KINDS = {"opcua_server", "modbus_server", "opcda_server"}
OPC_SERVER_KINDS = {"opcua_server", "opcda_server"}
SERIAL_DRIVERS = {"modbus_rtu", "ab_df1"}
TRANSPORT_DEFAULT = {"modbus_rtu": "tcp", "ab_df1": "unmapped"}
SERIAL_DEFAULTS = {"baudrate": 19200, "bytesize": 8, "parity": "N", "stopbits": 1}
PARITY = {"n": "None", "none": "None", "e": "Even", "even": "Even", "o": "Odd", "odd": "Odd",
          "m": "Mark", "mark": "Mark", "s": "Space", "space": "Space"}
PLACEHOLDER = {"bool": False, "uint32": 0, "uint64": 0, "float64": 0.0, "string": "",
               "datetime": None, "string_array": []}

_DTYPE: dict[tuple[str, str], str] = {(t["level"], t["name"]): t["dtype"] for t in CATALOGUE["tags"]}


def parse_ts(text: str | None) -> datetime | None:
    if text is None:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def iso_ms(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    return ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _put(out: dict[str, tuple[str, Any]], level: str, path: str, name: str, value: Any) -> None:
    out[path + name] = (_DTYPE[(level, name)], value)


# --- node ------------------------------------------------------------------------------------

def _clock(now: datetime, zone: str) -> dict[str, Any]:
    local = now.astimezone(ZoneInfo(zone))
    hour12 = local.hour % 12 or 12
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
        "_Time_Hour": hour12,
        "_Time_Hour24": local.hour,
        "_Time_Minute": local.minute,
        "_Time_Second": min(local.second, 59),
        "_Time_PM": local.hour >= 12,
    }


def _active_devices(state: dict[str, Any]):
    for ch in state.get("channels", []):
        for dev in ch.get("devices", []):
            if ch.get("enabled", True) and dev.get("enabled", True):
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
                   if not (ch.get("enabled", True) and dev.get("enabled", True)))
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
        put = lambda name, value: _put(out, "north", p, name, value)  # noqa: E731
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
    put = lambda n, v: _put(out, "channel", p, n, v)  # noqa: E731
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
    sput = lambda n, v: _put(out, "channel_statistics", s, n, v)  # noqa: E731
    total = lambda key: sum(_counter(d, key) for d in devs)  # noqa: E731
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
    disabled = not (ch.get("enabled", True) and dev.get("enabled", True))
    p = base + "_System."
    put = lambda n, v: _put(out, "device", p, n, v)  # noqa: E731
    for name, value in device_status_tags(dev, dev.get("status"), now, disabled=disabled).items():
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
    sput = lambda n, v: _put(out, "device_statistics", s, n, v)  # noqa: E731
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


# --- device sequences --------------------------------------------------------------------------

def run_sequence(vector: dict[str, Any]) -> list[tuple[int, dict[str, tuple[str, Any]]]]:
    """Feed the events of a device_sequence vector; return the tags after each step with `expect`."""
    dev = vector["device"]
    start = parse_ts(vector["start"])
    assert start is not None
    health = DeviceHealth(int(dev.get("demote_after", 3)), int(dev.get("demote_for_ms", 30_000)))
    status: dict[str, Any] = {"error_since": None, "last_connect_failed": False, "connected": False,
                              "last_error": None, "cycle_ms": 0.0}
    results = []
    for index, step in enumerate(vector["steps"]):
        at = parse_ts(step["at"])
        assert at is not None
        mono = (at - start).total_seconds()
        event = step["event"]
        if event == "connect_ok":
            status.update(connected=True, last_connect_failed=False)
        elif event == "connect_failed":
            status.update(connected=False, last_connect_failed=True)
        elif event == "cycle_good":
            health.record_good(mono, at)
            status.update(error_since=None, last_error=step.get("error"), cycle_ms=step.get("cycle_ms", 0.0))
        elif event == "cycle_failed":
            if health.consecutive_failures == 0:
                status["error_since"] = iso_ms(at)
            ev = health.record_failure(mono)
            status.update(last_error=step["error"], cycle_ms=step.get("cycle_ms", 0.0))
            if ev in ("demoted", "redemoted"):
                status["connected"] = False          # the worker drops the connection on demotion
        elif event == "cycle_neutral":
            status.update(cycle_ms=step.get("cycle_ms", 0.0))
            if step.get("error") is not None:
                status["last_error"] = step["error"]
        elif event != "tick":
            raise ValueError(f"step {index}: unknown event {event!r}")
        if "expect" in step:
            full = dict(status, state=health.state, consecutive_failures=health.consecutive_failures,
                        last_good=iso_ms(health.last_good), errors=health.errors)
            tags = device_status_tags(dev, full, at, disabled=False)
            results.append((index, {k: (_DTYPE[("device", k)], v) for k, v in tags.items()}))
    return results
