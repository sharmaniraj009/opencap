"""SubDevice JSON payload builder (PLAN.md section 7.1).

One JSON object per device per publish::

    {"SubDeviceId": "...", "FirmVersion": "opencap-0.1.0", "Env": "prod", "isSecure": false,
     "DataValid": true, "Data": {"FLOW_1": 123.4}, "DateTime": "2026-11-16 15:45:01",
     "BadTags": ["LEVEL_2"]}

Rules:

- Key order is fixed: ``SubDeviceId, FirmVersion, Env, isSecure, DataValid, Data, DateTime``,
  then ``BadTags`` (only when ``PayloadCfg.bad_tags_field`` is on and the list is not empty).
- ``Data`` holds only tags with quality ``good``, in the device's tag order. Bad, stale and
  never-read tags are left out and listed, sorted, in ``BadTags``.
- ``DataValid`` is true only if every *enabled* tag of the device is good.
- Floats are rounded to ``PayloadCfg.float_decimals`` **decimal places** (``round(v, n)``),
  not significant digits. ``-0.0`` becomes ``0.0``. A NaN or infinite float makes the tag bad.
  ``bool`` stays a JSON bool and ``int`` stays an int. Lists (array tags) are cleaned
  element by element.
- ``DateTime`` is the newest ``source_ts`` among the tags in ``Data``, written in
  ``PayloadCfg.timezone`` as ``%Y-%m-%d %H:%M:%S``. With no good tag it is the current time.
- ``SubDeviceId`` and ``Env`` come from ``device.publish``. A device without a SubDeviceId
  gets JSON ``null`` (the loader already warns); we never invent one.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from opencap import FIRM_VERSION
from opencap.config.loader import slug
from opencap.config.models import Device, PayloadCfg
from opencap.core.cache import TagValue

DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# Used only when the system has no tz database (e.g. a slim container without tzdata).
# IST has had no DST since 1945, so a fixed offset is exact.
_FIXED_ZONES: dict[str, tzinfo] = {
    "Asia/Kolkata": timezone(timedelta(hours=5, minutes=30), "IST"),
    "Asia/Calcutta": timezone(timedelta(hours=5, minutes=30), "IST"),
    "UTC": UTC,
}


class _Bad:
    """Marker: the value cannot go in the payload (non-finite float, None)."""


_BAD = _Bad()


@lru_cache(maxsize=16)
def zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        if name in _FIXED_ZONES:
            return _FIXED_ZONES[name]
        raise


def clean_value(value: Any, decimals: int) -> Any:
    """JSON-ready value, or ``_BAD`` when the value cannot be published."""
    if value is None:
        return _BAD
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return _BAD
        r = round(value, decimals)
        return 0.0 if r == 0 else r          # no "-0.0" in the payload
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        out = [clean_value(v, decimals) for v in value]
        return _BAD if any(v is _BAD for v in out) else out
    # Anything else (numpy scalars, Decimal...): try a float, else its text.
    try:
        return clean_value(float(value), decimals)
    except (TypeError, ValueError):
        return str(value)


def is_bad(value: Any) -> bool:
    return value is _BAD


def format_datetime(ts: datetime, tz_name: str) -> str:
    if ts.tzinfo is None:                     # internal timestamps are UTC; be lenient
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(zone(tz_name)).strftime(DATETIME_FORMAT)


def build_payload(
    device: Device,
    values: Mapping[str, TagValue],
    cfg: PayloadCfg,
    *,
    is_secure: bool,
    now: datetime | None = None,
) -> dict:
    """Build the SubDevice JSON message for one device from a cache snapshot.

    ``now`` is only used when no tag is good (``DateTime`` fallback); tests pass it to get
    a fixed output.
    """
    data: dict[str, Any] = {}
    bad: list[str] = []
    newest: datetime | None = None
    for tag in device.tags:
        if not tag.enabled:
            continue
        tv = values.get(tag.name)
        if tv is None or tv.quality != "good":
            bad.append(tag.name)
            continue
        v = clean_value(tv.value, cfg.float_decimals)
        if v is _BAD:
            bad.append(tag.name)
            continue
        data[tag.name] = v
        ts = tv.source_ts
        if ts is not None:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if newest is None or ts > newest:
                newest = ts

    when = newest or now or datetime.now(UTC)
    pub = device.publish
    payload: dict[str, Any] = {
        "SubDeviceId": pub.sub_device_id,
        "FirmVersion": FIRM_VERSION,
        "Env": pub.env,
        "isSecure": bool(is_secure),
        "DataValid": not bad,
        "Data": data,
        "DateTime": format_datetime(when, cfg.timezone),
    }
    if cfg.bad_tags_field and bad:
        payload["BadTags"] = sorted(bad)
    return payload


def encode(payload: Mapping[str, Any]) -> bytes:
    """Compact UTF-8 JSON. Key order is kept. NaN/Infinity are refused (never valid JSON)."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def status_topic(device: Device) -> str:
    return f"opencap/{slug(device.channel)}/{slug(device.name)}/status"


def build_status(status: Mapping[str, Any]) -> dict:
    """Status topic body ``{"state", "last_good", "errors"}`` from ``DeviceHealth.status()``.

    Extra keys (cycle_ms, last_error...) are dropped. A ``datetime`` ``last_good`` is written
    as ISO-8601 UTC, a string is passed through.
    """
    last_good = status.get("last_good")
    if isinstance(last_good, datetime):
        if last_good.tzinfo is None:
            last_good = last_good.replace(tzinfo=UTC)
        last_good = last_good.astimezone(UTC).isoformat()
    return {
        "state": status.get("state") or "offline",
        "last_good": last_good,
        "errors": int(status.get("errors") or 0),
    }
