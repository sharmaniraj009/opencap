"""Golden payload cases shared by the Python and the Rust engine.

The Rust engine (engine/crates/opencap-north) must encode the SubDevice JSON payload
byte-for-byte like `opencap.north.payload` for the same input. This module builds a fixed,
seeded set of inputs (devices, cache snapshots, payload settings), runs the Python
`build_payload()` + `encode()` on each, and stores inputs and expected bytes in
`engine/crates/opencap-north/tests/golden/payload_cases.json`.

- `tests/unit/test_payload_golden.py` checks the checked-in file is what Python produces now.
- `engine/crates/opencap-north/tests/payload_golden.rs` checks Rust produces the same bytes.

Regenerate after a deliberate payload change:

    .venv/bin/python -m tests.helpers.payload_golden
"""

from __future__ import annotations

import json
import random
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from opencap import FIRM_VERSION
from opencap.config.models import Device, PayloadCfg, PublishCfg, Tag
from opencap.core.cache import TagValue
from opencap.north.payload import build_payload, build_status, encode

ROOT = Path(__file__).resolve().parents[2]
GOLDEN_PATH = ROOT / "engine" / "crates" / "opencap-north" / "tests" / "golden" / "payload_cases.json"

T0 = datetime(2026, 11, 16, 10, 15, 1, 123456, tzinfo=UTC)
ZONES = ["Asia/Kolkata", "UTC", "America/New_York", "Europe/London", "Asia/Kathmandu", "Australia/Lord_Howe"]
QUALITIES = ["good", "good", "good", "bad_comm", "bad_config", "bad_decode", "stale"]
NAMES = ["FLOW_1", "LEVEL_1", "PUMP_1_RUN", "TOTAL", "Temp °C", 'quo"te', "back\\slash", "tab\tname",
         "LTI002.Pv", "ünïcødé", "😀_EMOJI", "ctl\x01", "Z_LAST", "a", "B"]
STRINGS = ["RUN", "", "25 °C", 'say "hi"', "back\\slash", "line\nbreak\r\t", "\b\f\x00\x01\x1f\x7f",
           "ÿ latin-1 \xa0", "😀", "plain text 123"]
TIE_CASES = [(0.125, 2), (0.375, 2), (2.675, 2), (1.005, 2), (0.5, 0), (1.5, 0), (2.5, 0), (-0.125, 2),
             (123456.789012, 4), (0.00004, 4), (-0.00004, 4), (1e-5, 6), (12.0, 4), (1e16, 4), (1.5e17, 2),
             (123.45, 1), (-2.5, 0), (5e-324, 4), (1.7976931348623157e308, 4), (99.99995, 4), (0.1 + 0.2, 4),
             (1234567890123456.7, 4), (2.5600000381469727, 4), (123.40000152587891, 4), (15.0, -1), (25.0, -1),
             (1234.5, -2), (1.23456789e-7, 10), (9.999999999e-5, 8)]


def enc_value(v: Any) -> Any:
    """Tagged encoding that keeps Python types (bool vs int vs float) and exact floats."""
    if v is None:
        return {"t": "none"}
    if isinstance(v, bool):
        return {"t": "bool", "v": v}
    if isinstance(v, int):
        return {"t": "int", "v": str(v)}
    if isinstance(v, float):
        return {"t": "float", "v": repr(v)}
    if isinstance(v, str):
        return {"t": "str", "v": v}
    if isinstance(v, list):
        return {"t": "list", "v": [enc_value(x) for x in v]}
    raise TypeError(type(v))


def _f32(x: float) -> float:
    try:
        return struct.unpack(">f", struct.pack(">f", x))[0]
    except OverflowError:
        return x


def _random_float(rng: random.Random) -> float:
    kind = rng.random()
    if kind < 0.1:
        return rng.choice([float("nan"), float("inf"), float("-inf"), -0.0, 0.0])
    x = rng.uniform(-1, 1) * 10 ** rng.randint(-9, 21)
    if kind < 0.6:
        x = _f32(x)                                  # most PLC floats are float32 widened
    if rng.random() < 0.1:
        x = round(x * 8) / 8                         # exact binary fractions: rounding ties
    return x


def _random_scalar(rng: random.Random, dtype: str) -> Any:
    if dtype == "bool":
        return rng.random() < 0.5
    if dtype == "string":
        return rng.choice(STRINGS)
    if dtype.startswith("float"):
        return _random_float(rng)
    bits = {"16": 16, "32": 32, "64": 64}[dtype[-2:]]
    if dtype.startswith("u"):
        return rng.choice([0, 1, (1 << bits) - 1, rng.randrange(1 << bits)])
    return rng.choice([0, -1, -(1 << (bits - 1)), (1 << (bits - 1)) - 1, rng.randrange(-(1 << (bits - 1)), 1 << (bits - 1))])


def _random_value(rng: random.Random, dtype: str) -> Any:
    r = rng.random()
    if r < 0.08:
        return [_random_scalar(rng, dtype) for _ in range(rng.randint(0, 4))]
    if r < 0.12:
        return [[_random_scalar(rng, dtype) for _ in range(2)] for _ in range(2)]
    return _random_scalar(rng, dtype)


def _device(rng: random.Random, n_tags: int, k: int) -> Device:
    dtypes = ["bool", "int16", "uint16", "int32", "uint32", "int64", "uint64", "float32", "float64", "string"]
    names = rng.sample(NAMES, min(n_tags, len(NAMES)))
    while len(names) < n_tags:
        names.append(f"TAG_{len(names):03d}")
    tags = [Tag(name=n, address="400001", dtype=rng.choice(dtypes), enabled=rng.random() > 0.1) for n in names]
    sub = rng.choice([f"SUB_{k:03d}", None, "ünï \"sub\""])
    return Device(id=f"Site {k:03d}/PLC {k}", channel=f"Site {k:03d}", name=f"PLC {k}", driver="modbus_tcp",
                  host="192.0.2.1", port=502, tags=tags,
                  publish=PublishCfg(sub_device_id=sub, env=rng.choice(["prod", "test", "stäging"])))


def _case(device: Device, values: dict[str, TagValue], cfg: PayloadCfg, is_secure: bool, now: datetime) -> dict:
    payload = build_payload(device, values, cfg, is_secure=is_secure, now=now)
    return {
        "device": device.model_dump(mode="json", exclude_defaults=True),
        "values": {n: {"value": enc_value(tv.value), "quality": tv.quality,
                       "source_ts": tv.source_ts.isoformat() if tv.source_ts else None}
                   for n, tv in values.items()},
        "cfg": cfg.model_dump(mode="json", exclude_defaults=True),
        "is_secure": is_secure,
        "now": now.isoformat(),
        "expected": encode(payload).decode("utf-8"),
    }


def build_cases() -> dict:
    rng = random.Random(20261117)
    cases: list[dict] = []
    # 1. rounding ties and repr edge cases, one tag each
    for x, decimals in TIE_CASES:
        dev = Device(id="S/D", channel="S", name="D", driver="modbus_tcp", host="192.0.2.1", port=502,
                     tags=[Tag(name="X", address="400001", dtype="float64")],
                     publish=PublishCfg(sub_device_id="SUB"))
        cases.append(_case(dev, {"X": TagValue(x, "good", T0)}, PayloadCfg(float_decimals=decimals), False, T0))
    # 2. random devices and snapshots
    for k in range(200):
        dev = _device(rng, rng.randint(0, 14), k)
        values: dict[str, TagValue] = {}
        for tag in dev.tags:
            if rng.random() < 0.07:
                continue                                   # missing from the snapshot
            quality = rng.choice(QUALITIES)
            ts = T0 + timedelta(seconds=rng.randint(-200_000, 200_000), microseconds=rng.randint(0, 999_999))
            value = _random_value(rng, tag.dtype)
            if quality != "good" and rng.random() < 0.5:
                value, ts = None, None
            if quality == "good" and rng.random() < 0.03:
                value = None                               # good but no value: bad in the payload
            values[tag.name] = TagValue(value, quality, ts)
        cfg = PayloadCfg(float_decimals=rng.choice([0, 1, 2, 3, 4, 4, 4, 5, 6, 8, 10]),
                         bad_tags_field=rng.random() < 0.8, timezone=rng.choice(ZONES))
        now = T0 + timedelta(days=rng.randint(-400, 400), seconds=rng.randint(0, 86_399))
        cases.append(_case(dev, values, cfg, rng.random() < 0.3, now))
    status = []
    for body in ({"state": "ok", "last_good": "2026-11-16T10:15:01.123Z", "errors": 0},
                 {"state": "demoted", "last_good": "2026-11-16T10:15:01.000Z", "errors": 12},
                 {"state": "offline", "last_good": None, "errors": 3}):
        status.append({"status": body, "expected": encode(build_status(body)).decode("utf-8")})
    return {"firm_version": FIRM_VERSION, "payload": cases, "status": status}


def render() -> str:
    """One case per line: small diffs, small file."""
    data = build_cases()
    one = lambda obj: json.dumps(obj, ensure_ascii=False, separators=(",", ":"))  # noqa: E731
    cases = data["payload"]
    lines = ["{", f' "firm_version": {one(data["firm_version"])},', f' "status": {one(data["status"])},', ' "payload": [']
    lines += [f"  {one(c)}{',' if i < len(cases) - 1 else ''}" for i, c in enumerate(cases)]
    lines += [" ]", "}"]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(render(), encoding="utf-8")
    print(f"wrote {GOLDEN_PATH.relative_to(ROOT)}")
