"""The Rust `ab_logix` driver (native CIP client, D-076) against the CIP simulator: same config
in, same text out as the Python driver, and it never sends a write service.

- `read` prints every decoded value with Python's `repr()`: identical output for Logix and for
  Micro800 with batching accepted and refused proves addressing, UDT members, array elements,
  `.n` bits and the tag-existence check match pycomm3's.
- the simulator's counters show zero write services, the Micro800 probe, the request counts, and
  a power-off / power-on of the PLC.

Skipped when the Rust binary has not been built (`cd engine && cargo build`).
"""

from __future__ import annotations

import asyncio
import json
import re
import sys

import pytest

from opencap.config.models import Device, Tag
from tests.helpers.cip_sim import WRITE_SERVICES, CipSim, make_logix_tags
from tests.helpers.configdir import write_config
from tests.helpers.engine import rust_binary
from tests.integration.test_ab_logix_sim import oo_tags

BIN = rust_binary()
pytestmark = [
    pytest.mark.skipif(BIN is None or not BIN.exists(), reason="Rust engine not built (cd engine && cargo build)"),
    pytest.mark.drivers("ab_logix"),
]
MISSING = "NOT_IN_PLC"


async def _run(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(proc.communicate(), 90)
    return proc.returncode, out.decode()


def _values(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if re.match(r"^\S+ +(good|bad_comm|bad_config|bad_decode|stale) ", ln)]


def _device(sim: CipSim, tags: list[Tag], **params) -> Device:
    p = {"micro800": True} if sim.kind == "micro800" else {"path": f"1,{sim.slot}", "slot": sim.slot}
    return Device(id="AB_SITE/PLC", channel="AB_SITE", name="PLC", driver="ab_logix", host=sim.host, port=sim.port,
                  params={**p, **params}, scan_ms=1000, min_scan_ms=1000, timeout_ms=1000, retries=1, tags=tags)


@pytest.mark.parametrize("kind,batching", [("logix", True), ("micro800", True), ("micro800", False)])
async def test_read_matches_python_and_sends_no_write(tmp_path, kind, batching):
    sim_tags = make_logix_tags(60, seed=7)
    tags = oo_tags(sim_tags) + [Tag(name=MISSING, address=MISSING, dtype="float32")]
    async with await CipSim.start(sim_tags, kind=kind, batching=batching) as sim:
        config_dir = write_config(tmp_path, [_device(sim, tags)])
        py = await _run(sys.executable, "-m", "opencap", "read", "AB_SITE/PLC", "--config", str(config_dir))
        sim.reset_counters()
        rs = await _run(str(BIN), "read", "AB_SITE/PLC", "--config", str(config_dir))
        assert rs[0] == py[0] == 3, (rs, py)           # one bad tag: the missing one
        assert _values(rs[1]) == _values(py[1])
        assert any(ln.startswith(MISSING) and "bad_config" in ln for ln in _values(rs[1]))
        assert sim.write_attempts == 0
        assert not any(sim.service_counts[s] for s in WRITE_SERVICES), dict(sim.service_counts)
        if kind == "micro800" and batching:
            assert sim.service_counts[0x0A] >= 1                 # Multiple Service Packets on a Micro800
        if kind == "micro800" and not batching:
            assert sim.service_counts[0x4C] >= len(tags) - 20    # one Read Tag per name


async def test_identity_reports_the_batching_probe(tmp_path):
    for batching in (True, False):
        sim_tags = make_logix_tags(40)
        dint = next(n for n, (spec, _v) in sim_tags.items() if spec == "DINT")
        async with await CipSim.start(sim_tags, kind="micro800", batching=batching) as sim:
            config_dir = write_config(tmp_path / str(batching), [_device(sim, [Tag(name="A", address=dint, dtype="int32")])])
            code, out = await _run(str(BIN), "identity", "AB_SITE/PLC", "--config", str(config_dir))
            assert code == 0, out
            ident = json.loads(out[out.index("{"):])
            assert ident["micro800"] is True and ident["batching"] is batching, ident
            assert ident["product_name"].startswith("2080") and ident["tag_count"] == 40
            assert ("probe ok" if batching else "probe rejected") in ident["batching_reason"]


async def test_mute_and_power_cycle_fail_the_read_then_recover(tmp_path):
    sim_tags = make_logix_tags(40, seed=3)
    async with await CipSim.start(sim_tags, kind="logix") as sim:
        dint = next(n for n, (spec, _v) in sim_tags.items() if spec == "DINT")
        tags = [Tag(name="A", address=dint, dtype="int32")]
        config_dir = write_config(tmp_path, [_device(sim, tags, batching="auto")])
        code, out = await _run(str(BIN), "read", "AB_SITE/PLC", "--config", str(config_dir))
        assert code == 0 and "good" in out, out
        sim.mute = True
        code, out = await _run(str(BIN), "read", "AB_SITE/PLC", "--config", str(config_dir))
        assert code != 0, out
        sim.mute = False
        await sim.stop()
        code, _ = await _run(str(BIN), "read", "AB_SITE/PLC", "--config", str(config_dir))
        assert code != 0
        await sim.restart()
        code, out = await _run(str(BIN), "read", "AB_SITE/PLC", "--config", str(config_dir))
        assert code == 0 and "good" in out, out
