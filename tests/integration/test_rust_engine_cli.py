"""Conformance of the Rust engine's CLI with the Python CLI: same config in, same text out.

- `check -v` prints every block of every device: identical output proves the Rust block
  planner, rate groups, invalid-tag reasons, load estimate and config warnings match.
- `read` against a live simulator prints every decoded value with Python's `repr()`:
  identical output proves address parsing, word order and float decoding match.

Skipped when the Rust binary has not been built (`cd engine && cargo build`).
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import pytest
import yaml

from opencap.config.models import Device, Tag
from tests.helpers.configdir import write_config
from tests.helpers.engine import rust_binary
from tests.helpers.modbus_sim import ModbusSim, make_sim_devices

BIN = rust_binary()
pytestmark = pytest.mark.skipif(BIN is None or not BIN.exists(), reason="Rust engine not built (cd engine && cargo build)")


def _value_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if re.match(r"^\S+ +(good|bad_comm|bad_config|bad_decode|stale) ", line)]


async def _run(*args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), 60)
    return proc.returncode, out.decode(), err.decode()


async def _both(*args: str) -> tuple[tuple[int, str, str], tuple[int, str, str]]:
    py = await _run(sys.executable, "-m", "opencap", *args)
    rs = await _run(str(BIN), *args)
    return py, rs


def _mixed_devices() -> list[Device]:
    """Sim devices plus the awkward cases: rate groups, bad addresses, strings, arrays, scaling."""
    devices = make_sim_devices(6, 40, per_channel=3, scan_ms=1000)
    extra = devices[0].model_copy(update={
        "id": "SIM_SITE_01/ODD PLC", "name": "ODD PLC", "port": 15020, "min_scan_ms": 500,
        "tags": [
            Tag(name="FAST", address="400001", dtype="uint16", scan_ms=500),
            Tag(name="SLOW", address="400010", dtype="float32", scan_ms=10_000),
            Tag(name="TXT", address="400020.010H", dtype="string"),
            Tag(name="ARR", address="400040[3][2]", dtype="int16"),
            Tag(name="BITS", address="000001[16]", dtype="bool"),
            Tag(name="BAD_PREFIX", address="500001", dtype="uint16"),
            Tag(name="BAD_BOOL", address="400002", dtype="bool"),
            Tag(name="TOO_BIG", address="400100[40]", dtype="uint16"),
            Tag(name="OFF", address="400003", dtype="uint16", enabled=False),
            Tag(name="F64", address="400200", dtype="float64", scan_ms=2_500),
        ],
    })
    disabled = devices[1].model_copy(update={"enabled": False})
    return [devices[0], extra, disabled, *devices[2:]]


async def test_check_verbose_output_is_identical(tmp_path):
    devices = [d.model_copy(update={"port": 15000 + i}) for i, d in enumerate(_mixed_devices())]
    config_dir = write_config(tmp_path, devices, publish={"interval_ms": 2000})
    # warnings the loader must word identically: unknown ids, stubs, unmapped, duplicates
    dmap = yaml.safe_load((config_dir / "devices_map.yaml").read_text())
    dmap["devices"][devices[3].id]["stub"] = True
    dmap["devices"]["GONE/DEVICE"] = {"sub_device_id": "X"}
    dmap["devices"][devices[4].id]["sub_device_id"] = dmap["devices"][devices[5].id]["sub_device_id"]
    del dmap["devices"][devices[6].id]
    (config_dir / "devices_map.yaml").write_text(yaml.safe_dump(dmap, sort_keys=False))
    ov = yaml.safe_load((config_dir / "overrides.yaml").read_text())
    ov["channels"] = {"SIM_SITE_02": {"enabled": False}, "NOWHERE": {"enabled": True}}
    ov["devices"][devices[0].id]["tags"] = {"GHOST": {"enabled": False}}
    (config_dir / "overrides.yaml").write_text(yaml.safe_dump(ov, sort_keys=False))

    for args in (["check", "-c", str(config_dir), "-v"], ["check", "-c", str(config_dir)],
                 ["check", "-c", str(config_dir), "--site", "SIM_SITE_01", "-v"]):
        py, rs = await _both(*args)
        assert rs == py, f"{args}:\n--- python\n{py[1]}{py[2]}\n--- rust\n{rs[1]}{rs[2]}"
    assert "bad_config BAD_PREFIX" in py[1] or "SIM_SITE_01" in py[1]


async def test_check_errors_match(tmp_path):
    py, rs = await _both("check", "-c", str(tmp_path / "missing"))
    assert (rs[0], rs[1]) == (py[0], py[1]) == (1, "")
    assert rs[2].startswith("config error: ") and "devices.json not found" in rs[2]
    config_dir = write_config(tmp_path, make_sim_devices(2, 5))
    py, rs = await _both("check", "-c", str(config_dir), "--site", "NOPE")
    assert rs == py == (1, "", "no channel named 'NOPE'\n")


async def test_read_output_is_identical(tmp_path):
    devices = _mixed_devices()
    odd = next(d for d in devices if d.name == "ODD PLC")
    sim_dev = odd.model_copy(update={"tags": [t for t in odd.tags if not t.name.startswith(("BAD", "TOO"))]})
    sim = await ModbusSim.start(sim_dev)
    try:
        sim.set_value("TXT", "Pump °1")
        sim.set_value("SLOW", -1234.5678)
        sim.set_value("F64", 1e-7)
        normal = await ModbusSim.start(devices[0])
        try:
            config_dir = write_config(tmp_path, [sim.device, normal.device])
            for dev in (sim.device, normal.device):
                py, rs = await _both("read", dev.id, "-c", str(config_dir))
                assert rs == py, f"--- python\n{py}\n--- rust\n{rs}"
                assert py[0] == 0, py
            py, rs = await _both("read", sim.device.id, "--tag", "TXT", "-c", str(config_dir))
            assert rs == py and "'Pump °1'" in py[1]
            await normal.stop()                                   # PLC off: every tag bad_comm, exit 3
            py, rs = await _both("read", normal.device.id, "-c", str(config_dir))
            # pymodbus logs "Failed to connect ..." to stdout: compare the value lines only
            assert (rs[0], _value_lines(rs[1])) == (py[0], _value_lines(py[1]))
            assert py[0] == 3 and len(_value_lines(py[1])) == 40 and "bad_comm" in py[1]
        finally:
            await normal.stop()
    finally:
        await sim.stop()
    py, rs = await _both("read", "NO/SUCH", "-c", str(config_dir))
    assert rs[0] == py[0] == 1 and "unknown device 'NO/SUCH'" in rs[2] and "unknown device 'NO/SUCH'" in py[2]


def test_binary_reports_its_version():
    import subprocess

    out = subprocess.run([str(BIN), "--version"], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "opencap-engine 0.1.0", out
    assert Path(BIN).exists()
