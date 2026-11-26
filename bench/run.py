"""Engine benchmark harness (#7): tag updates/s, overruns, CPU, RSS, latency, outbox backlog.

    python bench/run.py --engine python --devices 80 --tags-per-device 125 --scan-ms 1000 --duration 60
    python bench/run.py --profile 10k                       # 80 devices x 125 tags at 1 s
    python bench/run.py --engine rust --binary engine/target/release/opencap-engine --profile 10k

The engine under test is a subprocess driven only through its config directory, its admin API
(`/health`, `/metrics`) and the wire (Modbus in, MQTT out), so the Rust engine runs unchanged.
Simulators (tests/helpers/modbus_sim.py) run in `--sim-procs` worker processes and the MQTT
broker (tests/helpers/broker.py) in its own process, so their load is not counted as the
engine's; their CPU is reported next to it. Method and results: bench/README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import multiprocessing as mp
import os
import platform
import re
import signal
import socket
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TAGS_PER_DEVICE = 125                       # NFR-1's target node shape: 2,000 devices x 125 tags
PROFILES = {"1k": 1_000, "10k": 10_000, "25k": 25_000, "50k": 50_000}
MARKER_DTYPES = ("uint32", "int32", "uint16", "int16")    # an integer register tag carries the sequence number
# Diagnostic only (--no-system-tags): the unchanged Python engine with its system-tag task
# (opencap.core.system_tags, #120) stubbed out at start, to show what that task costs.
NO_SYSTEM_TAGS = ("import runpy, opencap.core.system_tags as st; "
                  "st.SystemTagTask.step = lambda self, *a, **k: 0; "
                  "runpy.run_module('opencap', run_name='__main__')")


# --- /proc, /metrics and statistics (pure; bench/test_bench.py) ---------------------------------

def proc_cpu_seconds(pid: int) -> float:
    """User + system CPU seconds of a process, all threads (Linux /proc/<pid>/stat)."""
    text = Path(f"/proc/{pid}/stat").read_text()
    fields = text[text.rindex(")") + 2:].split()       # the command name may contain spaces
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def proc_memory_kb(pid: int) -> dict[str, int]:
    """VmRSS (now) and VmHWM (peak) of a process in kB (Linux /proc/<pid>/status)."""
    out = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in ("VmRSS", "VmHWM"):
            out[key] = int(value.split()[0])
    return out


_SAMPLE_RE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)')
_LABEL_RE = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def parse_metrics(text: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    """Prometheus text exposition -> {(name, sorted labels): value}."""
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE_RE.match(line)
        if m:
            labels = tuple(sorted(_LABEL_RE.findall(m[2] or "")))
            out[m[1], labels] = float(m[3])
    return out


def metric(samples: dict, name: str, **labels: str) -> float:
    """Sum of every sample of `name` whose labels include `labels`."""
    want = set(labels.items())
    return sum(v for (n, lab), v in samples.items() if n == name and want <= set(lab))


def by_device(samples: dict, name: str, **labels: str) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    want = set(labels.items())
    for (n, lab), v in samples.items():
        d = dict(lab)
        if n == name and want <= set(lab) and "device" in d:
            out[d["device"]] += v
    return dict(out)


def percentile(values: list[float], p: float) -> float | None:
    """Linear interpolation between closest ranks (numpy's default); None for no values."""
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def summary_ms(seconds: list[float]) -> dict[str, Any]:
    ms = [x * 1000 for x in seconds]
    r = lambda x: None if x is None else round(x, 1)  # noqa: E731
    return {"n": len(ms), "p50": r(percentile(ms, 50)), "p99": r(percentile(ms, 99)), "max": r(max(ms, default=None))}


def marker_tag(device: Any) -> tuple[Any, bool]:
    """The tag that carries the sequence number, and whether it sits in the device's last block.

    The worker writes the cache once per device cycle, after the last block, so a marker in the
    last block makes "reply -> receive" the engine's own latency (NFR-3) rather than the time to
    read the rest of the cycle. The tag is an integer whose address no other tag shares (an
    alias would be published with the marker's value)."""
    from opencap.drivers.modbus_tcp import ModbusTcpDriver

    uses: dict[str, int] = defaultdict(int)
    for t in device.tags:
        uses[t.address.strip()] += 1
    by_name = {t.name: t for t in device.tags}
    blocks = ModbusTcpDriver(device).plan(list(device.tags)).blocks
    for block in reversed(blocks):
        if block.fc not in (3, 4):
            continue
        for dtype in MARKER_DTYPES:
            for name in block.tags:
                t = by_name[name]
                if t.dtype == dtype and "." not in t.address and uses[t.address.strip()] == 1:
                    return t, block is blocks[-1]
    raise ValueError(f"{device.id}: no integer register tag to carry the latency marker (use >= 20 tags per device)")


# --- simulator worker processes ------------------------------------------------------------------

def _quiet() -> None:
    """Harness processes log only errors; pymodbus logs an error for every reply to a client that
    already hung up (the engine closes the socket on a timeout), which is not a harness fault."""
    import logging

    logging.basicConfig(level=logging.ERROR)
    for name, level in (("pymodbus", logging.CRITICAL), ("amqtt", logging.ERROR), ("transitions", logging.ERROR)):
        logging.getLogger(name).setLevel(level)


def _sim_process(conn: Any, devices_json: list[str], change_ms: int, changes: str) -> None:
    _quiet()
    asyncio.run(_sim_main(conn, devices_json, change_ms, changes))


async def _sim_main(conn: Any, devices_json: list[str], change_ms: int, changes: str) -> None:
    from opencap.config.models import Device
    from tests.helpers.modbus_sim import ModbusSim, sim_slot

    class MarkedSim(ModbusSim):
        """Records, per sequence number, when the value changed and when a reply first carried it."""

        def __init__(self, device: Device, *a: Any, **kw: Any) -> None:
            super().__init__(device, *a, **kw)
            self.marker, _ = marker_tag(device)
            slot = sim_slot(self.marker.address, self.marker.dtype, device.params)
            assert slot is not None and slot.area in ("holding", "input")
            self.marker_fc = 3 if slot.area == "holding" else 4
            self.marker_reg = slot.start
            self.modulus = 1 << {"uint32": 32, "int32": 31, "uint16": 16, "int16": 15}[self.marker.dtype]
            self.seq = 0
            self.log: dict[int, list[float | None]] = {}     # seq -> [changed, first reply]

        def change(self, seq: int, now: float) -> None:
            if changes == "all":
                self.tick()
            self.set_value(self.marker.name, seq % self.modulus)
            self.seq = seq
            self.log[seq] = [now, None]

        def _serve(self, device_id: int, func_code: int, address: int, count: int):  # type: ignore[override]
            out = super()._serve(device_id, func_code, address, count)
            if func_code == self.marker_fc and address <= self.marker_reg < address + count and self.seq:
                entry = self.log[self.seq]
                if entry[1] is None:
                    entry[1] = time.monotonic()
            return out

    devices = [Device.model_validate_json(d) for d in devices_json]
    sims = [await MarkedSim.start(d) for d in devices]
    conn.send({d.id: s.port for d, s in zip(devices, sims, strict=True)})
    loop = asyncio.get_running_loop()
    seq = 0
    next_t = time.monotonic()
    while not conn.poll():
        seq += 1
        for s in sims:
            s.change(seq, time.monotonic())
        next_t += change_ms / 1000
        await asyncio.sleep(max(0.0, next_t - time.monotonic()))
    conn.recv()
    for s in sims:
        await s.stop()
    conn.send({s.device.id: s.log for s in sims})
    await loop.shutdown_asyncgens()


def _broker_process(conn: Any) -> None:
    _quiet()

    async def main() -> None:
        from tests.helpers.broker import TestBroker

        async with TestBroker() as broker:
            conn.send(broker.port)
            await asyncio.get_running_loop().run_in_executor(None, conn.recv)
    asyncio.run(main())


# --- the run -------------------------------------------------------------------------------------

@dataclass
class Receipts:
    """Subscriber side: first arrival per (device, sequence number), north message counts."""

    first: dict[tuple[str, int], float] = field(default_factory=dict)
    messages: int = 0
    tag_values: int = 0
    counting: bool = False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def machine() -> dict[str, Any]:
    model = ""
    with contextlib.suppress(OSError):
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                model = line.split(":", 1)[1].strip()
                break
    mem_kb = 0
    with contextlib.suppress(OSError, ValueError):
        mem_kb = int(Path("/proc/meminfo").read_text().split()[1])
    return {"cpus": os.cpu_count(), "cpu_model": model, "memory_gb": round(mem_kb / 2**20, 1),
            "os": platform.platform(), "python": platform.python_version()}


async def _subscribe(port: int, prefix: str, sub_ids: dict[str, str], markers: dict[str, str],
                     rec: Receipts, ready: asyncio.Event) -> None:
    import aiomqtt

    async with aiomqtt.Client("127.0.0.1", port, identifier="opencap-bench-subscriber") as client:
        await client.subscribe(f"{prefix}/+/+/data", qos=0)
        ready.set()
        async for msg in client.messages:
            now = time.monotonic()
            try:
                body = json.loads(msg.payload)
            except ValueError:
                continue
            dev = sub_ids.get(body.get("SubDeviceId"))
            if dev is None:
                continue
            data = body.get("Data") or {}
            if rec.counting:
                rec.messages += 1
                rec.tag_values += len(data)
            seq = data.get(markers[dev])
            if isinstance(seq, int):
                rec.first.setdefault((dev, seq), now)


async def _get(url: str, timeout: float = 5.0) -> Any:
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as http:
        return await http.get(url)


async def _scrape(base: str, timeout: float) -> dict | None:
    """Parsed `/metrics`, or None when the engine does not answer within `timeout` (overloaded)."""
    import httpx

    try:
        return parse_metrics((await _get(f"{base}/metrics", timeout)).text)
    except httpx.HTTPError:
        return None


async def _snapshot(base: str, give_up_s: float = 180.0) -> dict:
    """`/metrics` at a window edge: retried with a generous timeout, an overloaded engine still answers."""
    deadline = time.monotonic() + give_up_s
    while (m := await _scrape(base, 30.0)) is None:
        if time.monotonic() > deadline:
            raise RuntimeError(f"/metrics did not answer for {give_up_s:.0f}s")
    return m


async def run(args: argparse.Namespace) -> dict[str, Any]:
    from opencap.config.models import Device
    from opencap.drivers.modbus_tcp import ModbusTcpDriver
    from tests.helpers.configdir import write_config
    from tests.helpers.modbus_sim import make_sim_devices

    work = Path(args.workdir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    for stale in work.glob("saf.sqlite*"):            # a previous run's outbox would count as backlog
        stale.unlink()
    devices = make_sim_devices(args.devices, args.tags_per_device, scan_ms=args.scan_ms,
                               timeout_ms=max(1000, args.scan_ms), seed=args.seed)
    chosen = {d.id: marker_tag(d) for d in devices}
    markers = {dev_id: tag.name for dev_id, (tag, _) in chosen.items()}
    markers_last = sum(1 for _, last in chosen.values() if last)
    tags = sum(sum(1 for t in d.tags if t.enabled) for d in devices)
    blocks = {d.id: len(ModbusTcpDriver(d).plan(list(d.tags)).blocks) for d in devices}

    ctx = mp.get_context("spawn")
    procs: list[Any] = []
    broker_conn, child = ctx.Pipe()
    broker = ctx.Process(target=_broker_process, args=(child,), daemon=True)
    broker.start()
    procs.append(broker)
    mqtt_port = broker_conn.recv()

    groups = [devices[i::args.sim_procs] for i in range(args.sim_procs) if devices[i::args.sim_procs]]
    sim_conns = []
    for group in groups:
        parent, child = ctx.Pipe()
        p = ctx.Process(target=_sim_process, args=(child, [d.model_dump_json() for d in group],
                                                   args.change_ms, args.changes), daemon=True)
        p.start()
        procs.append(p)
        sim_conns.append(parent)
    ports: dict[str, int] = {}
    for c in sim_conns:
        ports.update(c.recv())
    devices = [Device.model_validate({**d.model_dump(), "port": ports[d.id], "host": "127.0.0.1"}) for d in devices]

    api_port = _free_port()
    config_dir = write_config(work, devices, mqtt_port=mqtt_port, api_port=api_port,
                              poller={"log_level": "WARNING", "instance": "opencap-bench",
                                      "mqtt": {"client_id": "opencap-bench-engine"}},
                              publish={"mode": "on_change"})
    sub_ids = {f"OL_TEST_{i:03d}": d.id for i, d in enumerate(devices)}
    prefix = "opencap"

    rec = Receipts()
    ready = asyncio.Event()
    sub_task = asyncio.create_task(_subscribe(mqtt_port, prefix, sub_ids, markers, rec, ready))
    await asyncio.wait_for(ready.wait(), 10)

    if args.engine == "rust":
        argv = [str(Path(args.binary).resolve()), "run", "--config", str(config_dir)]
    elif args.no_system_tags:
        argv = [sys.executable, "-c", NO_SYSTEM_TAGS, "run", "--config", str(config_dir)]
    else:
        argv = [sys.executable, "-m", "opencap", "run", "--config", str(config_dir)]
    log = (work / "engine.log").open("wb")
    engine = subprocess.Popen(argv, cwd=work, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{api_port}"
    result: dict[str, Any] = {}
    try:
        deadline = time.monotonic() + args.startup_timeout
        while True:
            if engine.poll() is not None:
                raise RuntimeError(f"engine exited with {engine.returncode}; see {work / 'engine.log'}")
            with contextlib.suppress(Exception):
                if (await _get(f"{base}/health", 2)).status_code == 200:
                    break
            if time.monotonic() > deadline:
                raise RuntimeError(f"engine API not up after {args.startup_timeout}s")
            await asyncio.sleep(0.2)
        started_at = time.monotonic()
        while True:                                        # every device polled once
            ok = by_device(await _snapshot(base), "poll_requests_total", result="ok")
            if all(ok.get(d.id, 0) > 0 for d in devices):
                break
            if time.monotonic() > deadline:
                missing = [d.id for d in devices if not ok.get(d.id)]
                raise RuntimeError(f"{len(missing)} devices never polled, e.g. {missing[:3]}")
            await asyncio.sleep(0.5)
        polling_after = time.monotonic() - started_at
        await asyncio.sleep(args.warmup)

        m0 = await _snapshot(base)
        cpu0, t0 = proc_cpu_seconds(engine.pid), time.monotonic()
        harness0 = {p.pid: proc_cpu_seconds(p.pid) for p in procs}
        main0 = time.process_time()
        rec.counting = True
        rss: list[int] = []
        backlog: list[float] = []
        slow_samples = 0                                   # 1 s samples where /metrics took > 2 s
        while time.monotonic() - t0 < args.duration:
            await asyncio.sleep(1.0)
            rss.append(proc_memory_kb(engine.pid)["VmRSS"])
            sample = await _scrape(base, 2.0)
            if sample is None:
                slow_samples += 1
            else:
                backlog.append(metric(sample, "saf_queue_messages"))
        rec.counting = False
        m1 = await _snapshot(base)
        cpu1, t1 = proc_cpu_seconds(engine.pid), time.monotonic()
        mem = proc_memory_kb(engine.pid)
        harness = {p.pid: proc_cpu_seconds(p.pid) - harness0[p.pid] for p in procs}
        main_cpu = time.process_time() - main0
        elapsed = t1 - t0

        for c in sim_conns:
            c.send("stop")
        logs: dict[str, dict[int, list[float | None]]] = {}
        for c in sim_conns:
            logs.update(c.recv())

        ok0 = by_device(m0, "poll_requests_total", result="ok")
        ok1 = by_device(m1, "poll_requests_total", result="ok")
        cycles = {d.id: (ok1.get(d.id, 0) - ok0.get(d.id, 0)) / blocks[d.id] for d in devices}
        updates = sum(cycles[d.id] * sum(1 for t in d.tags if t.enabled) for d in devices) / elapsed
        delta = lambda name, **lab: metric(m1, name, **lab) - metric(m0, name, **lab)  # noqa: E731

        reply_lat, change_lat, missed = [], [], 0
        for dev_id, log_ in logs.items():
            for seq, (changed, replied) in log_.items():
                if changed is None or not t0 <= changed <= t1 - args.scan_ms / 1000 * 2:
                    continue                          # outside the window, or too late to be read
                got = rec.first.get((dev_id, seq))            # runs are far shorter than 65,536 changes
                if got is None:
                    missed += 1
                    continue
                change_lat.append(got - changed)
                if replied is not None:
                    reply_lat.append(got - replied)

        target = tags * 1000 / args.scan_ms
        result = {
            "engine": args.engine + (" (no system tags)" if args.no_system_tags and args.engine == "python" else ""),
            "profile": {"devices": len(devices), "tags_per_device": args.tags_per_device, "tags": tags,
                        "blocks": sum(blocks.values()), "scan_ms": args.scan_ms, "change_ms": args.change_ms,
                        "changes": args.changes, "duration_s": round(elapsed, 1), "warmup_s": args.warmup},
            "machine": machine(),
            "polling_after_s": round(polling_after, 1),
            "tag_updates_per_s": {"target": round(target), "achieved": round(updates),
                                  "ratio": round(updates / target, 3) if target else None},
            "requests_per_s": round(delta("poll_requests_total", result="ok") / elapsed, 1),
            "failed_requests": int(delta("poll_requests_total") - delta("poll_requests_total", result="ok")),
            "overruns": int(delta("poll_overrun_total")),
            "cpu_percent": round(100 * (cpu1 - cpu0) / elapsed, 1),
            "rss_mb": {"end": round(mem["VmRSS"] / 1024, 1), "peak": round(mem["VmHWM"] / 1024, 1),
                       "window_max": round(max(rss, default=0) / 1024, 1)},
            "latency_ms": {"reply_to_receive": summary_ms(reply_lat), "change_to_receive": summary_ms(change_lat),
                           "missed": missed, "markers_in_last_block": markers_last},
            "north": {"messages_per_s": round(rec.messages / elapsed, 1),
                      "tag_values_per_s": round(rec.tag_values / elapsed),
                      "acked_per_s": round(delta("mqtt_published_total") / elapsed, 1)},
            "outbox": {"max": max(backlog, default=0), "end": backlog[-1] if backlog else 0},
            "api_slow_samples": slow_samples,
            "harness_cpu_percent": {
                "simulators": round(100 * sum(harness[p.pid] for p in procs[1:]) / elapsed, 1),
                "busiest_simulator_process": round(100 * max(harness[p.pid] for p in procs[1:]) / elapsed, 1),
                "broker": round(100 * harness[broker.pid] / elapsed, 1),
                "subscriber": round(100 * main_cpu / elapsed, 1)},
        }
    finally:
        if engine.poll() is None:
            engine.send_signal(signal.SIGTERM)
            try:
                engine.wait(15)
            except subprocess.TimeoutExpired:
                engine.kill()
        log.close()
        sub_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await sub_task
        for c in sim_conns:
            with contextlib.suppress(Exception):
                c.send("stop")
        with contextlib.suppress(Exception):
            broker_conn.send("stop")
        for p in procs:
            p.join(5)
            if p.is_alive():
                p.kill()
    return result


def one_line(r: dict[str, Any]) -> str:
    p, lat = r["profile"], r["latency_ms"]
    return (f"{r['engine']} {p['tags']} tags/{p['devices']} dev @ {p['scan_ms']} ms: "
            f"{r['tag_updates_per_s']['achieved']}/{r['tag_updates_per_s']['target']} tag updates/s, "
            f"{r['overruns']} overruns, CPU {r['cpu_percent']} %, RSS {r['rss_mb']['window_max']} MB, "
            f"reply->receive p50 {lat['reply_to_receive']['p50']} / p99 {lat['reply_to_receive']['p99']} ms, "
            f"outbox max {r['outbox']['max']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--engine", choices=("python", "rust"), default="python")
    ap.add_argument("--binary", help="the engine binary for --engine rust (opencap-engine)")
    ap.add_argument("--profile", choices=sorted(PROFILES, key=PROFILES.get),
                    help=f"total tags at {TAGS_PER_DEVICE} tags per device (overrides --devices)")
    ap.add_argument("--devices", type=int, default=8)
    ap.add_argument("--tags-per-device", type=int, default=TAGS_PER_DEVICE)
    ap.add_argument("--scan-ms", type=int, default=1000)
    ap.add_argument("--duration", type=float, default=60.0, help="measurement window, seconds")
    ap.add_argument("--warmup", type=float, default=10.0, help="seconds after every device polled once")
    ap.add_argument("--change-ms", type=int, default=1000, help="simulator value change period")
    ap.add_argument("--changes", choices=("all", "marker"), default="all",
                    help="all: every tag changes every period (on-change publishes every device); "
                         "marker: only the marker")
    ap.add_argument("--sim-procs", type=int, default=2, help="simulator worker processes")
    ap.add_argument("--no-system-tags", action="store_true",
                    help="diagnostic, Python only: stub out the system-tag task (#120) to measure its cost")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--startup-timeout", type=float, default=120.0)
    ap.add_argument("--workdir", default=str(ROOT / "bench" / "work"))
    ap.add_argument("--out", help="append the result as one JSON line to this file")
    args = ap.parse_args(argv)
    if args.profile:
        args.devices = math.ceil(PROFILES[args.profile] / args.tags_per_device)
    if args.engine == "rust" and not args.binary:
        ap.error("--engine rust needs --binary PATH (cargo build --release -p opencap-engine)")
    if sys.platform != "linux":
        ap.error("the harness reads CPU and RSS from /proc: Linux only")
    result = asyncio.run(run(args))
    if args.out:                                   # first: a closed stdout must not lose the result
        with open(args.out, "a", encoding="utf-8") as f:
            f.write(json.dumps(result) + "\n")
    print(one_line(result), flush=True)
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
