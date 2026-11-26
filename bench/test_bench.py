"""Self-test of the benchmark harness's pure helpers (#7). Not in the default pytest run
(`testpaths = ["tests"]`): `pytest bench/test_bench.py`."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("bench_run", HERE / "run.py")
assert spec and spec.loader
run = importlib.util.module_from_spec(spec)
sys.modules["bench_run"] = run              # @dataclass looks its module up there
spec.loader.exec_module(run)

PYTHON_METRICS = '''# HELP poll_requests_total Wire requests to PLCs by result
# TYPE poll_requests_total counter
poll_requests_total{device="SIM_SITE_01/SIM_PLC_01",result="ok"} 120.0
poll_requests_total{device="SIM_SITE_01/SIM_PLC_01",result="timeout"} 2.0
poll_requests_total{device="SIM_SITE_02/SIM_PLC_02",result="ok"} 80.0
poll_requests_created{device="SIM_SITE_01/SIM_PLC_01",result="ok"} 1.7e+09
poll_overrun_total{device="SIM_SITE_02/SIM_PLC_02"} 3.0
saf_queue_messages 7.0
mqtt_published_total 41.0
'''
RUST_METRICS = '''# TYPE poll_requests_total counter
poll_requests_total{device="A/B",result="ok"} 5
poll_requests_total{result="ok",device="A/C"} 6
saf_queue_messages 0
'''


def test_parse_and_sum_python_metrics():
    m = run.parse_metrics(PYTHON_METRICS)
    assert run.metric(m, "poll_requests_total") == 202
    assert run.metric(m, "poll_requests_total", result="ok") == 200
    assert run.metric(m, "poll_overrun_total") == 3
    assert run.metric(m, "saf_queue_messages") == 7
    assert run.metric(m, "absent") == 0
    assert run.by_device(m, "poll_requests_total", result="ok") == {
        "SIM_SITE_01/SIM_PLC_01": 120, "SIM_SITE_02/SIM_PLC_02": 80}


def test_parse_rust_metrics_label_order_and_integers():
    m = run.parse_metrics(RUST_METRICS)
    assert run.by_device(m, "poll_requests_total", result="ok") == {"A/B": 5, "A/C": 6}


def test_percentile_interpolates_like_numpy():
    assert run.percentile([], 50) is None
    assert run.percentile([3.0], 99) == 3.0
    assert run.percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert run.percentile(list(map(float, range(101))), 99) == 99.0
    assert run.percentile([10.0, 0.0], 100) == 10.0
    assert run.summary_ms([0.001, 0.002, 0.003]) == {"n": 3, "p50": 2.0, "p99": 3.0, "max": 3.0}
    assert run.summary_ms([]) == {"n": 0, "p50": None, "p99": None, "max": None}


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")
def test_proc_readers_on_this_process():
    before = run.proc_cpu_seconds(os.getpid())
    sum(i * i for i in range(300_000))
    assert run.proc_cpu_seconds(os.getpid()) >= before
    mem = run.proc_memory_kb(os.getpid())
    assert 0 < mem["VmRSS"] <= mem["VmHWM"]


def test_every_profile_device_gets_a_marker_in_its_last_block():
    sys.path.insert(0, str(HERE.parent))
    from opencap.config.models import Device, Tag
    from tests.helpers.modbus_sim import make_sim_devices

    for dev in make_sim_devices(40, run.TAGS_PER_DEVICE):
        t, last = run.marker_tag(dev)
        assert last, dev.id
        assert t.dtype in run.MARKER_DTYPES and t.address[0] in "34" and "." not in t.address
        assert sum(1 for x in dev.tags if x.address == t.address) == 1     # not shared with an alias

    def device(tags: list[Tag]) -> Device:
        return Device(id="S/D", channel="S", name="D", driver="modbus_tcp", host="192.0.2.1", port=502, tags=tags)

    shared = [Tag(name="U", address="400001", dtype="uint32"), Tag(name="U2", address="400001", dtype="uint32")]
    for tags in ([Tag(name="F", address="400001", dtype="float32"), Tag(name="C", address="000001", dtype="bool")],
                 shared):
        with pytest.raises(ValueError, match="marker"):
            run.marker_tag(device(tags))
    early = [Tag(name="U", address="400001", dtype="uint16"), Tag(name="F", address="300100", dtype="float32")]
    assert run.marker_tag(device(early)) == (early[0], False)       # an earlier block if the last has none
