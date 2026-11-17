"""SubDevice JSON payload builder (PLAN 7.1)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from opencap import FIRM_VERSION
from opencap.config.models import Device, PayloadCfg, PublishCfg, Tag
from opencap.core.cache import TagValue
from opencap.north.payload import build_payload, build_status, encode, format_datetime, status_topic

T0 = datetime(2026, 11, 16, 10, 15, 1, tzinfo=UTC)          # 15:45:01 IST


def _dev(tags, **publish):
    publish.setdefault("sub_device_id", "SUB_TEST_PLC")
    return Device(id="Site 001/8 PUMP", channel="Site 001", name="8 PUMP", driver="modbus_tcp",
                  host="192.0.2.1", port=502, tags=tags, publish=PublishCfg(**publish))


def _tag(name, dtype="float32", **kw):
    return Tag(name=name, address="400001", dtype=dtype, **kw)


def good(v, ts=T0):
    return TagValue(v, "good", ts)


TAGS = [
    _tag("FLOW_1"), _tag("LEVEL_1"), _tag("PUMP_1_RUN", "bool"), _tag("TOTAL", "uint32"),
    _tag("PUMP_1_CURRENT_R"), _tag("LEVEL_2"), _tag("SPARE", enabled=False),
]


def test_exact_json_for_known_input():
    dev = _dev(TAGS, env="prod")
    values = {
        "FLOW_1": good(123.40000152587891, T0 - timedelta(seconds=3)),
        "LEVEL_1": good(2.5600000381469727, T0),
        "PUMP_1_RUN": good(True, T0 - timedelta(seconds=1)),
        "TOTAL": good(4000000123, T0 - timedelta(seconds=1)),
        "PUMP_1_CURRENT_R": TagValue(41.2, "bad_comm", T0 - timedelta(minutes=5)),  # last value kept, quality bad
        "LEVEL_2": TagValue(None, "stale", None),                                   # never read
        "SPARE": good(1.0),                                                          # disabled: ignored
    }
    payload = build_payload(dev, values, PayloadCfg(), is_secure=False)
    expected = (
        '{"SubDeviceId":"SUB_TEST_PLC","FirmVersion":"' + FIRM_VERSION + '","Env":"prod",'
        '"isSecure":false,"DataValid":false,'
        '"Data":{"FLOW_1":123.4,"LEVEL_1":2.56,"PUMP_1_RUN":true,"TOTAL":4000000123},'
        '"DateTime":"2026-11-16 15:45:01","BadTags":["LEVEL_2","PUMP_1_CURRENT_R"]}'
    )
    assert encode(payload) == expected.encode()
    assert list(payload) == ["SubDeviceId", "FirmVersion", "Env", "isSecure", "DataValid", "Data",
                             "DateTime", "BadTags"]


def test_all_good_is_valid_and_has_no_badtags():
    dev = _dev([_tag("A"), _tag("B", "int16")], env="test")
    payload = build_payload(dev, {"A": good(1.5), "B": good(-3)}, PayloadCfg(), is_secure=True)
    assert payload == {
        "SubDeviceId": "SUB_TEST_PLC", "FirmVersion": FIRM_VERSION, "Env": "test", "isSecure": True,
        "DataValid": True, "Data": {"A": 1.5, "B": -3}, "DateTime": "2026-11-16 15:45:01",
    }


def test_datetime_is_ist_across_midnight_utc():
    dev = _dev([_tag("A"), _tag("B")])
    late = datetime(2026, 11, 16, 20, 0, 0, tzinfo=UTC)              # 01:30 IST next day
    values = {"A": good(1.0, late - timedelta(hours=1)), "B": good(2.0, late)}
    assert build_payload(dev, values, PayloadCfg(), is_secure=False)["DateTime"] == "2026-11-17 01:30:00"
    # 18:29:59 UTC is still the same IST day, 18:30:00 is the next one
    assert format_datetime(datetime(2026, 12, 31, 18, 29, 59, tzinfo=UTC), "Asia/Kolkata") == "2026-12-31 23:59:59"
    assert format_datetime(datetime(2026, 12, 31, 18, 30, 0, tzinfo=UTC), "Asia/Kolkata") == "2027-01-01 00:00:00"


def test_datetime_uses_newest_good_tag_only():
    dev = _dev([_tag("A"), _tag("B")])
    values = {"A": good(1.0, T0), "B": TagValue(2.0, "bad_comm", T0 + timedelta(hours=1))}
    assert build_payload(dev, values, PayloadCfg(), is_secure=False)["DateTime"] == "2026-11-16 15:45:01"


def test_datetime_falls_back_to_now_without_good_tags():
    dev = _dev([_tag("A")])
    values = {"A": TagValue(None, "stale", None)}
    fixed = datetime(2026, 11, 16, 23, 0, 0, tzinfo=UTC)
    p = build_payload(dev, values, PayloadCfg(), is_secure=False, now=fixed)
    assert p["DateTime"] == "2026-11-17 04:30:00"
    assert p["Data"] == {} and p["DataValid"] is False and p["BadTags"] == ["A"]
    # without `now`: the wall clock, in IST
    p = build_payload(dev, values, PayloadCfg(), is_secure=False)
    written = datetime.strptime(p["DateTime"], "%Y-%m-%d %H:%M:%S")
    ist_now = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=5, minutes=30)
    assert abs((written - ist_now).total_seconds()) < 5


@pytest.mark.parametrize(("raw", "decimals", "out"), [
    (2.5600000381469727, 4, 2.56),
    (123.456789, 4, 123.4568),
    (123.456789, 2, 123.46),
    (0.00004, 4, 0.0),
    (-0.00004, 4, 0.0),          # no "-0.0"
    (1e-5, 6, 1e-05),
    (12.0, 4, 12.0),
    (7, 4, 7),                   # int stays int
    (4294967295, 0, 4294967295),
    (True, 4, True),             # bool stays bool, not 1
    (False, 4, False),
    ("RUN", 4, "RUN"),
    ([1.23456, 2], 2, [1.23, 2]),
])
def test_value_cleaning(raw, decimals, out):
    dev = _dev([_tag("X")])
    p = build_payload(dev, {"X": good(raw)}, PayloadCfg(float_decimals=decimals), is_secure=False)
    assert p["Data"]["X"] == out
    assert type(p["Data"]["X"]) is type(out)
    assert p["DataValid"] is True


def test_rounding_is_decimal_places_not_significant_digits():
    dev = _dev([_tag("X")])
    p = build_payload(dev, {"X": good(123456.789012)}, PayloadCfg(float_decimals=4), is_secure=False)
    assert encode(p).count(b'"X":123456.789') == 1


@pytest.mark.parametrize("raw", [float("nan"), float("inf"), float("-inf"), [1.0, float("nan")], None])
def test_non_finite_or_missing_good_value_is_bad(raw):
    dev = _dev([_tag("X"), _tag("Y")])
    p = build_payload(dev, {"X": good(raw), "Y": good(1.0)}, PayloadCfg(), is_secure=False)
    assert p["Data"] == {"Y": 1.0}
    assert p["DataValid"] is False
    assert p["BadTags"] == ["X"]
    encode(p)                                     # still valid JSON


def test_badtags_field_off():
    dev = _dev([_tag("B"), _tag("A")])
    values = {"A": TagValue(1.0, "bad_decode", T0), "B": TagValue(1.0, "stale", T0)}
    p = build_payload(dev, values, PayloadCfg(bad_tags_field=False), is_secure=False)
    assert "BadTags" not in p
    assert p["DataValid"] is False
    p = build_payload(dev, values, PayloadCfg(bad_tags_field=True), is_secure=False)
    assert p["BadTags"] == ["A", "B"]             # sorted


@pytest.mark.parametrize(("qualities", "valid"), [
    ({"A": "good", "B": "good"}, True),
    ({"A": "good", "B": "stale"}, False),
    ({"A": "good", "B": "bad_comm"}, False),
    ({"A": "bad_config", "B": "good"}, False),
    ({"A": "good"}, False),                       # B missing from the snapshot = never read
])
def test_data_valid(qualities, valid):
    dev = _dev([_tag("A"), _tag("B"), _tag("OFF", enabled=False)])
    values = {n: TagValue(1.0, q, T0) for n, q in qualities.items()}
    values["OFF"] = TagValue(None, "bad_comm", None)  # a disabled tag never spoils DataValid
    assert build_payload(dev, values, PayloadCfg(), is_secure=False)["DataValid"] is valid


def test_missing_sub_device_id_is_null_not_invented():
    dev = _dev([_tag("A")], sub_device_id=None)
    assert build_payload(dev, {"A": good(1.0)}, PayloadCfg(), is_secure=False)["SubDeviceId"] is None


def test_other_timezone():
    dev = _dev([_tag("A")])
    p = build_payload(dev, {"A": good(1.0)}, PayloadCfg(timezone="UTC"), is_secure=False)
    assert p["DateTime"] == "2026-11-16 10:15:01"


def test_encode_is_compact_utf8():
    raw = encode({"a": "25 °C", "b": [1, 2]})
    assert raw == '{"a":"25 °C","b":[1,2]}'.encode()
    assert json.loads(raw) == {"a": "25 °C", "b": [1, 2]}
    with pytest.raises(ValueError):
        encode({"x": float("nan")})


def test_status_topic_is_slugged():
    assert status_topic(_dev([])) == "opencap/site_001/8_pump/status"


def test_build_status():
    assert build_status({"state": "ok", "last_good": "2026-11-16T10:15:01+00:00", "errors": 3,
                         "cycle_ms": 12}) == {"state": "ok", "last_good": "2026-11-16T10:15:01+00:00", "errors": 3}
    assert build_status({"state": "demoted", "last_good": T0, "errors": 1}) == {
        "state": "demoted", "last_good": "2026-11-16T10:15:01+00:00", "errors": 1}
    assert build_status({"state": "offline", "last_good": None}) == {"state": "offline", "last_good": None,
                                                                     "errors": 0}
