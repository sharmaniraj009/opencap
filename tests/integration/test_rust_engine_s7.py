"""The Rust `s7` driver (native S7comm read subset, D-085) against the S7 simulator (python-snap7's
own server): same config in, same text out as the Python driver, and it never sends a write.

- `read` prints every decoded value with Python's `repr()`: identical output for every address
  form, both byte orders, a PDU the CPU negotiates down (planned 960, negotiated 480: split
  reads) and a PG connection to another slot proves addressing, decoding, the TSAP and the
  split match the Python driver's.
- the simulator's counters show only setup communication, read var and SZL reads, the missing
  DB reported as `bad_config` and asked for once per read, a mute CPU and a power cycle, and
  the identity read.

Skipped when the Rust binary has not been built (`cd engine && cargo build`).
"""

from __future__ import annotations

import asyncio
import json
import re
import sys

import pytest

pytest.importorskip("snap7")                 # the simulator and the Python side of the comparison

from opencap.config.models import Device, Tag  # noqa: E402
from tests.helpers.configdir import write_config  # noqa: E402
from tests.helpers.engine import rust_binary  # noqa: E402
from tests.helpers.netcut import stable_port  # noqa: E402
from tests.helpers.s7_sim import S7Sim  # noqa: E402
from tests.integration.test_s7_sim import AREAS, form_tags, load_forms  # noqa: E402

BIN = rust_binary()
pytestmark = [
    pytest.mark.skipif(BIN is None or not BIN.exists(), reason="Rust engine not built (cd engine && cargo build)"),
    pytest.mark.drivers("s7"),
]
MISSING = "MISSING_DB"


async def _run(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(proc.communicate(), 90)
    return proc.returncode, out.decode()


def _values(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if re.match(r"^\S+ +(good|bad_comm|bad_config|bad_decode|stale) ", ln)]


def _device(sim: S7Sim, tags: list[Tag], timeout_ms: int = 1500, **params) -> Device:
    p = {"rack": 0, "slot": 1, "max_pdu": 480, "link_type": 3, "byte_order_big": True, **params}
    return Device(id="S7_SITE/PLC", channel="S7_SITE", name="PLC", driver="s7", host=sim.host, port=sim.port,
                  params=p, scan_ms=1000, min_scan_ms=1000, timeout_ms=timeout_ms, retries=1, tags=tags)


@pytest.mark.parametrize(("params", "sim_kw"), [
    ({}, {}),
    ({"byte_order_big": False}, {}),
    ({"max_pdu": 960}, {"max_pdu": 480}),
    ({"slot": 2, "link_type": "PG"}, {"slot": 2}),
], ids=["default", "little-endian", "pdu-negotiated-down", "pg-slot-2"])
async def test_read_matches_python_and_sends_only_reads(tmp_path, params, sim_kw):
    tags = form_tags() + [Tag(name=MISSING, address="DB77,W0", dtype="uint16")]
    big = params.get("byte_order_big", True)
    async with await S7Sim.start(AREAS, **sim_kw) as sim:
        load_forms(sim, big=big)
        config_dir = write_config(tmp_path, [_device(sim, tags, **params)])
        py = await _run(sys.executable, "-m", "opencap", "read", "S7_SITE/PLC", "--config", str(config_dir))
        sim.reset_counters()
        rs = await _run(str(BIN), "read", "S7_SITE/PLC", "--config", str(config_dir))
        assert rs[0] == py[0] == 3, (rs, py)           # one bad tag: the DB the CPU does not have
        assert _values(rs[1]) == _values(py[1])
        assert sum(1 for ln in _values(rs[1]) if " good " in ln) == len(form_tags())
        assert any(ln.startswith(MISSING) and "bad_config" in ln for ln in _values(rs[1]))
        assert sim.write_attempts == 0
        assert set(sim.function_counts) <= {"setup", "read"}, dict(sim.function_counts)
        assert sim.refused_reads == 1, sim.refused_reads                  # the missing DB, asked once
        assert sim.oversize_reads == 0
        assert all(t[1:] == (0, params.get("slot", 1)) for t in sim.tsaps), sim.tsaps
        if "max_pdu" in params:
            assert sim.pdu_requests[-1] == 960
        if params.get("link_type") == "PG":
            assert all(t[0] == 1 for t in sim.tsaps)


async def test_a_block_larger_than_the_negotiated_pdu_is_split(tmp_path):
    tags = [Tag(name="big", address="DB300,REAL0[200]", dtype="float32"), Tag(name="m", address="MW0", dtype="uint16")]
    async with await S7Sim.start({**AREAS, "DB300": 800}, max_pdu=480) as sim:
        for k in range(200):
            sim.set(f"DB300,REAL{4 * k}", "float32", k * 1.5 - 100.0)
        config_dir = write_config(tmp_path, [_device(sim, tags, max_pdu=960)])
        py = await _run(sys.executable, "-m", "opencap", "read", "S7_SITE/PLC", "--config", str(config_dir))
        sim.reset_counters()
        rs = await _run(str(BIN), "read", "S7_SITE/PLC", "--config", str(config_dir))
        assert rs[0] == py[0] == 0, (rs, py)
        assert _values(rs[1]) == _values(py[1])
        assert sorted(sim.read_sizes) == [2, 338, 462] and sim.oversize_reads == 0, sim.read_sizes
        assert sim.write_attempts == 0


async def test_identity_reports_the_cpu(tmp_path):
    async with await S7Sim.start(AREAS, max_pdu=480) as sim:
        config_dir = write_config(tmp_path, [_device(sim, form_tags()[:1], max_pdu=960)])
        py = await _run(sys.executable, "-m", "opencap", "identity", "S7_SITE/PLC", "--config", str(config_dir))
        sim.reset_counters()
        code, out = await _run(str(BIN), "identity", "S7_SITE/PLC", "--config", str(config_dir))
        assert code == 0, out
        ident = json.loads(out[out.index("{"):])
        want = json.loads(py[1][py[1].index("{"):])
        assert ident["module_type_name"] and ident["order_code"].startswith("6ES7"), ident
        assert (ident["pdu_requested"], ident["pdu_negotiated"], ident["cpu_state"]) == (960, 480, "run")
        for key in ("module_type_name", "order_code", "cpu_state", "pdu_requested", "pdu_negotiated", "rack", "slot",
                    "connection_type", "byte_order"):
            assert ident[key] == want[key], (key, ident[key], want[key])
        assert sim.write_attempts == 0 and set(sim.function_counts) <= {"setup", "szl"}, dict(sim.function_counts)


async def test_mute_and_power_cycle_fail_the_read_then_recover(tmp_path):
    sim = await S7Sim.start(AREAS, port=stable_port())
    try:
        tags = form_tags()[:3]
        load_forms(sim)
        config_dir = write_config(tmp_path, [_device(sim, tags, timeout_ms=400)])
        code, out = await _run(str(BIN), "read", "S7_SITE/PLC", "--config", str(config_dir))
        assert code == 0 and "good" in out, out
        sim.mute = True
        code, out = await _run(str(BIN), "read", "S7_SITE/PLC", "--config", str(config_dir))
        assert code != 0, out
        sim.mute = False
        await sim.stop()
        code, _ = await _run(str(BIN), "read", "S7_SITE/PLC", "--config", str(config_dir))
        assert code != 0
        await sim.restart()
        code, out = await _run(str(BIN), "read", "S7_SITE/PLC", "--config", str(config_dir))
        assert code == 0 and "good" in out, out
        assert sim.write_attempts == 0
    finally:
        await sim.stop()
