"""KEPServerEX IoT Gateway JSON payload builder (#36, docs/spec/payload-iotgw-json.md).

Expected texts are written by hand, not taken from the builder; the shared vectors in
tests/conformance/payload/iotgw/ are checked by test_conformance_vectors.py.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from opencap.config.models import Device, PayloadCfg, PublishCfg, Tag
from opencap.core.cache import TagValue
from opencap.north.payload import encode
from opencap.north.payload_iotgw import build_iotgw_payload, epoch_ms, tag_id

NOW = datetime(2026, 11, 16, 10, 15, 1, 500_000, tzinfo=UTC)
NOW_MS = 1794824101500       # 2026-11-16T10:15:01.500Z


def _dev(tags, **publish) -> Device:
    return Device(id="Plant A/PLC1", channel="Plant A", name="PLC1", driver="modbus_tcp", host="192.0.2.1",
                  port=502, tags=tags, publish=PublishCfg(**publish))


def _tag(name, dtype="float32", **kw) -> Tag:
    return Tag(name=name, address="400001", dtype=dtype, **kw)


def good(v, ts=NOW) -> TagValue:
    return TagValue(v, "good", ts)


def test_epoch_ms_is_exact_and_floors():
    assert NOW_MS == 1794824101500
    assert epoch_ms(NOW) == NOW_MS
    assert epoch_ms(NOW.replace(microsecond=999_999)) == 1794824101999
    assert epoch_ms(NOW.replace(tzinfo=None)) == NOW_MS                    # naive means UTC
    assert epoch_ms(datetime(1970, 1, 1, tzinfo=UTC)) == 0
    assert epoch_ms(datetime(2026, 11, 16, 15, 45, 1, 500_000, tzinfo=timezone(timedelta(hours=5, minutes=30)))) == NOW_MS


def test_exact_json_for_known_input():
    dev = _dev([_tag("FLOW_1"), _tag("RUN", "bool"), _tag("PV.Raw", "int16"), _tag("SPARE", enabled=False)])
    values = {
        "FLOW_1": good(123.40000152587891, NOW - timedelta(seconds=3)),
        "RUN": good(True, NOW - timedelta(milliseconds=250)),
        "PV.Raw": TagValue(41, "bad_comm", NOW - timedelta(minutes=5)),
        "SPARE": good(1.0),
    }
    text = encode(build_iotgw_payload(dev, values, PayloadCfg(), now=NOW)).decode()
    assert text == (
        '{"timestamp":1794824101500,"values":['
        '{"id":"Plant A.PLC1.FLOW_1","v":123.4,"q":true,"t":1794824098500},'
        '{"id":"Plant A.PLC1.RUN","v":true,"q":true,"t":1794824101250},'
        '{"id":"Plant A.PLC1.PV.Raw","v":41,"q":false,"t":1794823801500}]}'
    )


def test_tag_id():
    assert tag_id(_dev([]), "G1.T") == "Plant A.PLC1.G1.T"


def test_never_read_and_missing_tags_use_the_message_timestamp():
    dev = _dev([_tag("A"), _tag("B")])
    out = build_iotgw_payload(dev, {"A": TagValue(None, "stale", None)}, PayloadCfg(), now=NOW)
    assert out["values"] == [
        {"id": "Plant A.PLC1.A", "v": None, "q": False, "t": NOW_MS},
        {"id": "Plant A.PLC1.B", "v": None, "q": False, "t": NOW_MS},
    ]


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, [1.0, math.nan]])
def test_unpublishable_values_are_null_and_not_good(bad):
    out = build_iotgw_payload(_dev([_tag("A")]), {"A": good(bad)}, PayloadCfg(), now=NOW)
    assert out["values"][0]["v"] is None and out["values"][0]["q"] is False
    json.loads(encode(out))                                                # still valid JSON (no NaN)


def test_good_with_no_value_is_not_good():
    out = build_iotgw_payload(_dev([_tag("A")]), {"A": good(None)}, PayloadCfg(), now=NOW)
    assert out["values"][0]["q"] is False


def test_float_rounding_and_negative_zero():
    out = build_iotgw_payload(_dev([_tag("A"), _tag("B")]), {"A": good(1.23456), "B": good(-0.00001)},
                              PayloadCfg(float_decimals=2), now=NOW)
    assert [e["v"] for e in out["values"]] == [1.23, 0.0]
    assert encode(out).decode().count("-0.0") == 0


def test_timezone_setting_does_not_change_epoch_times():
    dev = _dev([_tag("A")])
    a = build_iotgw_payload(dev, {"A": good(1)}, PayloadCfg(timezone="UTC"), now=NOW)
    b = build_iotgw_payload(dev, {"A": good(1)}, PayloadCfg(timezone="Asia/Kolkata"), now=NOW)
    assert a == b


def test_now_defaults_to_the_clock():
    before = epoch_ms(datetime.now(UTC))
    out = build_iotgw_payload(_dev([]), {}, PayloadCfg())
    assert before <= out["timestamp"] <= epoch_ms(datetime.now(UTC))
    assert out["values"] == []


def test_publish_format_config():
    assert PublishCfg().format == "subdevice_json"
    assert PublishCfg(format="iotgw_json").format == "iotgw_json"
    with pytest.raises(ValidationError):
        PublishCfg(format="sparkplug")
