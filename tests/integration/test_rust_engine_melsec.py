"""The Rust `melsec_mc` driver (native MC 3E binary client, D-077) against the MC 3E simulator:
same config in, same text out as the Python driver, and it never sends a write.

- `read` prints every decoded value with Python's `repr()`: identical output for TCP, UDP, iQ-R,
  `first_word_low` off, and a 960-word reply split across TCP writes proves the addressing, the
  word order, the bit devices and the whole-frame reads match the Python driver's.
- the simulator's counters show only batch reads and the CPU model request, the end-code
  mapping (a block past the PLC's memory is `bad_config` and asked once per read), a mute PLC
  and a power cycle.

Skipped when the Rust binary has not been built (`cd engine && cargo build`).
"""

from __future__ import annotations

import asyncio
import json
import re
import sys

import pytest

pytest.importorskip("pymcprotocol")        # the Python side of the comparison

from opencap.config.models import Device, Tag  # noqa: E402
from tests.helpers.configdir import write_config  # noqa: E402
from tests.helpers.engine import rust_binary  # noqa: E402
from tests.helpers.mc_sim import McSim  # noqa: E402
from tests.helpers.netcut import stable_port  # noqa: E402
from tests.integration.test_melsec_sim import TAGS  # noqa: E402

BIN = rust_binary()
pytestmark = [
    pytest.mark.skipif(BIN is None or not BIN.exists(), reason="Rust engine not built (cd engine && cargo build)"),
    pytest.mark.drivers("melsec_mc"),
]
MISSING = "PAST_D_RANGE"
READS = {(0x0401, 0x0000), (0x0401, 0x0002), (0x0101, 0x0000)}


async def _run(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(proc.communicate(), 90)
    return proc.returncode, out.decode()


def _values(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if re.match(r"^\S+ +(good|bad_comm|bad_config|bad_decode|stale) ", ln)]


def _device(sim: McSim, tags: list[Tag], timeout_ms: int = 1000, **params) -> Device:
    return Device(id="MC_SITE/PLC", channel="MC_SITE", name="PLC", driver="melsec_mc", host=sim.host, port=sim.port,
                  params={"plc_type": "Q", "protocol": "tcp", "first_word_low": True, "frame": "3E", **params},
                  scan_ms=1000, min_scan_ms=1000, timeout_ms=timeout_ms, retries=1, tags=tags)


async def _sim(**kw) -> McSim:
    return await McSim.start({"SS": 64, "SC": 64, "SN": 64, "D": 12288}, **kw)


@pytest.mark.parametrize("params", [
    {}, {"protocol": "udp"}, {"first_word_low": False}, {"plc_type": "iQ-R"},
], ids=["tcp", "udp", "high-word-first", "iqr"])
async def test_read_matches_python_and_sends_only_reads(tmp_path, params):
    tags = TAGS + [Tag(name=MISSING, address="D12300", dtype="uint16")]       # inside Kepware's range, past the PLC's D
    async with await _sim() as sim:
        sim.fill(TAGS, seed=5, word_order="low_first" if params.get("first_word_low", True) else "high_first")
        config_dir = write_config(tmp_path, [_device(sim, tags, **params)])
        py = await _run(sys.executable, "-m", "opencap", "read", "MC_SITE/PLC", "--config", str(config_dir))
        sim.reset_counters()
        rs = await _run(str(BIN), "read", "MC_SITE/PLC", "--config", str(config_dir))
        assert rs[0] == py[0] == 3, (rs, py)           # one bad tag: the one past the PLC's memory
        assert _values(rs[1]) == _values(py[1])
        assert sum(1 for ln in _values(rs[1]) if " good " in ln) == len(TAGS)
        assert any(ln.startswith(MISSING) and "bad_config" in ln for ln in _values(rs[1]))
        assert sim.write_attempts == 0
        assert set(sim.command_counts) <= READS, dict(sim.command_counts)
        assert sim.error_replies == {0xC056: 1}, sim.error_replies       # the rejected block, asked once
        if params.get("protocol") == "udp":
            assert sim.frames["udp"] > 0 and sim.frames["tcp"] == 0
        if params.get("plc_type") == "iQ-R":
            assert sim.command_counts[(0x0401, 0x0002)] > 0 and sim.command_counts[(0x0401, 0x0000)] == 0


async def test_a_960_word_reply_split_across_tcp_writes_is_read_whole(tmp_path):
    tags = [Tag(name="big", address="D1000[960]", dtype="uint16"), Tag(name="f", address="D0000002", dtype="float32")]
    async with await _sim() as sim:
        sim.fill(tags, seed=2)
        sim.split_replies = True
        config_dir = write_config(tmp_path, [_device(sim, tags, timeout_ms=3000)])
        py = await _run(sys.executable, "-m", "opencap", "read", "MC_SITE/PLC", "--config", str(config_dir))
        rs = await _run(str(BIN), "read", "MC_SITE/PLC", "--config", str(config_dir))
        assert rs[0] == py[0] == 0, (rs, py)
        assert _values(rs[1]) == _values(py[1])
        assert all(" good " in ln for ln in _values(rs[1]))
        assert sim.write_attempts == 0


async def test_identity_reports_the_cpu_model(tmp_path):
    async with await McSim.start(cpu_type="Q06UDVCPU", cpu_code=0x0367) as sim:
        config_dir = write_config(tmp_path, [_device(sim, TAGS[:1])])
        code, out = await _run(str(BIN), "identity", "MC_SITE/PLC", "--config", str(config_dir))
        assert code == 0, out
        ident = json.loads(out[out.index("{"):])
        assert ident == {"plc_type": "Q", "protocol": "tcp", "frame": "3E", "commtype": "binary",
                         "cpu_type": "Q06UDVCPU", "cpu_code": "0367"}, ident
        sim.cpu_model_end_code = 0xC059
        code, out = await _run(str(BIN), "identity", "MC_SITE/PLC", "--config", str(config_dir))
        ident = json.loads(out[out.index("{"):])
        assert "0xC059" in ident["cpu_type_error"] and "cpu_type" not in ident
        assert sim.write_attempts == 0


async def test_mute_and_power_cycle_fail_the_read_then_recover(tmp_path):
    sim = await _sim(port=stable_port())
    try:
        sim.fill(TAGS, seed=3)
        config_dir = write_config(tmp_path, [_device(sim, TAGS, timeout_ms=300)])
        code, out = await _run(str(BIN), "read", "MC_SITE/PLC", "--config", str(config_dir))
        assert code == 0 and "good" in out, out
        sim.mute = True
        code, out = await _run(str(BIN), "read", "MC_SITE/PLC", "--config", str(config_dir))
        assert code != 0, out
        sim.mute = False
        await sim.stop()
        code, _ = await _run(str(BIN), "read", "MC_SITE/PLC", "--config", str(config_dir))
        assert code != 0
        await sim.restart()
        code, out = await _run(str(BIN), "read", "MC_SITE/PLC", "--config", str(config_dir))
        assert code == 0 and "good" in out, out
        assert sim.write_attempts == 0
    finally:
        await sim.stop()
