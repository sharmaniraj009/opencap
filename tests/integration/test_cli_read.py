"""`opencap read` against a simulated Modbus device, as a real subprocess."""

from __future__ import annotations

import ast
import math
import sys
from pathlib import Path

from tests.helpers.configdir import write_config
from tests.helpers.engine import run_process
from tests.helpers.modbus_sim import ModbusSim, make_sim_devices

OOPOLL = [sys.executable, "-m", "opencap"]


async def _opencap(*args: str, cwd: Path) -> tuple[int, str]:
    return await run_process([*OOPOLL, *args], cwd=cwd, timeout=30)


def _close(a, b) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        return math.isclose(float(a), float(b), rel_tol=1e-6, abs_tol=1e-6)
    return a == b


async def test_opencap_read_matches_simulator(tmp_path):
    (device,) = make_sim_devices(1, 20, seed=7)
    sim = await ModbusSim.start(device)
    try:
        config_dir = write_config(tmp_path, [sim.device])
        code, out = await _opencap("read", sim.device.id, "--config", str(config_dir), cwd=tmp_path)
        assert code == 0, out
        expected = sim.expected()
        seen = 0
        for line in out.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[0] in expected:
                name, quality, raw = parts
                assert quality == "good", line
                value = ast.literal_eval(raw)
                assert _close(value, expected[name]), (name, value, expected[name])
                seen += 1
        assert seen == len(expected)

        code, out = await _opencap("read", sim.device.id, "--tag", next(iter(expected)),
                                  "--config", str(config_dir), cwd=tmp_path)
        assert code == 0 and len(out.strip().splitlines()) == 1, out
    finally:
        await sim.stop()


async def test_opencap_read_dead_device_exits_nonzero(tmp_path):
    (device,) = make_sim_devices(1, 5, seed=3)
    sim = await ModbusSim.start(device)
    dead = sim.device
    await sim.stop()                                   # nothing listens on that port now
    config_dir = write_config(tmp_path, [dead])
    code, out = await _opencap("read", dead.id, "--config", str(config_dir), cwd=tmp_path)
    assert code == 3, out
    assert "bad_comm" in out


async def test_opencap_read_unknown_device(tmp_path):
    (device,) = make_sim_devices(1, 5)
    config_dir = write_config(tmp_path, [device])
    code, out = await _opencap("read", "NOPE/X", "--config", str(config_dir), cwd=tmp_path)
    assert code == 1 and "unknown device" in out
