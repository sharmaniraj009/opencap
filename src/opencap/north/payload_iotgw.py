"""KEPServerEX IoT Gateway JSON payload ("standard template"), issue #36, docs/spec/payload-iotgw-json.md.

One JSON object per device per publish (``publish.format: iotgw_json``)::

    {"timestamp": 1763288101123,
     "values": [{"id": "Plant A.PLC1.FLOW_1", "v": 12.5, "q": true, "t": 1763288101123}, ...]}

Rules:

- ``timestamp`` is the publish time in milliseconds since the Unix epoch (UTC).
- ``values`` has one entry per **enabled** tag, in the device's tag order, good or not
  (KEPServerEX reports a bad tag with ``q: false`` instead of leaving it out).
- ``id`` is ``<channel>.<device>.<tag name>`` (the tag name already carries its group path).
- ``v`` is the value cleaned as for the SubDevice payload (floats rounded to
  ``PayloadCfg.float_decimals`` places, no ``-0.0``). A bad tag keeps its last value when it
  has one; a missing, ``None`` or non-finite value is JSON ``null``.
- ``q`` is true only for quality ``good`` (with a publishable value).
- ``t`` is the tag's source timestamp in milliseconds since the epoch; a tag that has never
  been read has no source timestamp and gets the message ``timestamp``.
- Key order is fixed ``id, v, q, t``. Time zone settings do not apply: epoch times are UTC.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from opencap.config.models import Device, PayloadCfg
from opencap.core.cache import TagValue
from opencap.north.payload import clean_value, is_bad

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MS = timedelta(milliseconds=1)


def epoch_ms(ts: datetime) -> int:
    """Milliseconds since the epoch, floored, in exact integer arithmetic (naive means UTC)."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (ts - _EPOCH) // _MS


def tag_id(device: Device, tag_name: str) -> str:
    return f"{device.channel}.{device.name}.{tag_name}"


def build_iotgw_payload(
    device: Device,
    values: Mapping[str, TagValue],
    cfg: PayloadCfg,
    *,
    now: datetime | None = None,
) -> dict:
    """Build the IoT Gateway standard-template message for one device from a cache snapshot."""
    when = epoch_ms(now or datetime.now(UTC))
    entries: list[dict[str, Any]] = []
    for tag in device.tags:
        if not tag.enabled:
            continue
        tv = values.get(tag.name)
        v: Any = None
        good = False
        t = when
        if tv is not None:
            cleaned = clean_value(tv.value, cfg.float_decimals)
            v = None if is_bad(cleaned) else cleaned
            good = tv.quality == "good" and v is not None
            if tv.source_ts is not None:
                t = epoch_ms(tv.source_ts)
        entries.append({"id": tag_id(device, tag.name), "v": v, "q": good, "t": t})
    return {"timestamp": when, "values": entries}
