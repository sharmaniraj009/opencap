# Engine benchmark

`bench/run.py` measures an engine against the non-functional targets of
[`01-requirements.md`](../docs/architecture/01-requirements.md):

- NFR-1: tag updates/s;
- NFR-2: CPU and RSS;
- NFR-3: latency from reply to publish;
- NFR-9: the resource ceiling.

It also measures the Python reference ceiling of
[`04-scale-reliability.md`](../docs/architecture/04-scale-reliability.md) §4.1. It is not part of
any pytest run. The optional manual workflow `.github/workflows/bench.yml` (`workflow_dispatch`)
runs one profile on the standard runner and uploads the JSON results.

```bash
python bench/run.py --profile 10k                                   # Python engine, 80 devices x 125 tags at 1 s
python bench/run.py --engine python --devices 8 --tags-per-device 125 --scan-ms 1000 --duration 60
python bench/run.py --engine rust --binary engine/target/release/opencap-engine --profile 10k
pytest bench/test_bench.py                                          # the harness's own helpers
```

Options: `--profile 1k|10k|25k|50k` (125 tags per device, the NFR-1 node shape), `--devices`,
`--tags-per-device`, `--scan-ms`, `--duration` (measurement window, s), `--warmup`, `--change-ms`
(simulator change period), `--changes all|marker`, `--sim-procs`, `--out FILE` (appends one JSON line).
Linux only (CPU and RSS come from `/proc`).

## Method

**Engine-agnostic.** The engine runs as a child process (`<engine> run --config DIR`). It is driven
only through what both engines share:

- its config directory (`devices.json`, `devices_map.yaml`, `overrides.yaml`, `poller.yaml`);
- its admin API (`/health` and the Prometheus `/metrics`);
- the wire (Modbus TCP in, MQTT out).

`--engine rust --binary PATH` therefore runs the Rust engine with no other change.

**Load.**
- **Devices:** `make_sim_devices` (`tests/helpers/modbus_sim.py`) generates synthetic Modbus TCP
  devices, one channel each. They mix float32, 16/32-bit integers, register bits, coils and
  discrete inputs, with an alias, and plan to about 17 blocks per device at the KEPServerEX block
  size of 32.
- **Simulators:** each device has its own pymodbus-based simulator. They are spread over
  `--sim-procs` worker processes.
- **Broker:** the amqtt broker (`tests/helpers/broker.py`) runs in its own process.
- **Values:** every value changes every `--change-ms` (1 s), and every device publishes in
  `on_change` mode. That is the worst case for the north side, a full SubDevice JSON snapshot
  per device per second. `--changes marker` changes only the marker tag.

**Metrics.** The measurement window starts once every device has been polled once and
`--warmup` seconds have passed.

| result | how |
|---|---|
| tag updates/s | `poll_requests_total{result="ok"}` delta per device ÷ that device's blocks (planned by the Python planner) × its tags, summed, ÷ window |
| overruns, failed requests | `poll_overrun_total` delta; non-`ok` `poll_requests_total` delta |
| CPU % | user + system time of the engine process, all threads (`/proc/<pid>/stat`), ÷ window; 100 % = one core |
| RSS | `VmRSS` sampled every second (window maximum), and `VmHWM` (peak since start) |
| latency | see below |
| outbox backlog | `saf_queue_messages`, sampled every second (maximum and end) |
| north | messages and tag values received by the subscriber; `mqtt_published_total` delta (broker-acknowledged) |
| harness CPU | simulator, broker and subscriber processes, so a saturated harness is visible |

**Latency.** One integer holding-register tag per device carries a sequence number. The
simulator records two times per number: when it changed, and when a reply first carried it. The
subscriber records when a message first carried it. All three are `time.monotonic()` on one host.

- **reply → receive** is NFR-3 (reply arrival → north publish). It adds the broker hop and the
  reads of the device's remaining blocks in that cycle, because the cache is updated per cycle.
- **change → receive** adds the wait for the next scan, which is up to one scan period.

A number not received within two scans of its change counts as *missed*.

**Machine.** Every number below comes from a 4 vCPU Intel Xeon @ 2.10 GHz with 15.7 GB, on
Linux 6.18 and Python 3.13. That is the shape of the GitHub `ubuntu-latest` runner (4 vCPU,
16 GB). The harness shares those cores with the engine.

## Results: Python reference engine

Engine at `main` @ `591c00f`. The branch also carries #30, which touches only the OPC UA server, and that is not enabled here. 1 s scan, 125 tags per device (~17 blocks), every value changing
every second, a 60 s window after 10 s of warm-up. Where there are two runs, both are shown
(run 1 / run 2). CPU is the percentage of one core. "reply → receive" is p50 / p99.

| profile | devices | tag updates/s (target) | overruns | failed requests | CPU | RSS | reply → receive | outbox max |
|---|---|---|---|---|---|---|---|---|
| 1k | 8 | 995 / 993 (1,000) | 0 / 0 | 0 / 0 | 5.9 / 7.8 % | 74 MB | 2.3 / 17 ms · 3.1 / 27 ms | 1 |
| 10k | 80 | **1,213 / 1,182** (10,000) | 613 / 602 | 1,486 / 1,935 | 99 % | 98 MB | 1.3 / 3.1 s · 1.3 / 3.0 s | 108 / 121 |
| 25k | 200 | **40** (25,000) | 242 | 1,410 | 99 % | 139 MB | 13 / 33 s | 78 |
| 50k | 400 | **0** (50,000) | 3 | 484 | 99 % | 218 MB | (nothing published) | 0 |

**As built, the Python engine saturated one core from about 80 devices** (fixed by #238, see
[After #238](#after-238-the-system-tag-task-fixed)). The system-tag task (#120,
`opencap.core.system_tags`) was the cause, measured with py-spy at 10k tags:

- It takes 77 % of the process CPU.
- Every step reads each device's counters with `REGISTRY.get_sample_value`.
- Each of those calls collects the whole Prometheus registry, so one step is O(devices²).
- A step runs every second and after every device cycle.

At 25k and 50k tags polling nearly stops, and `/metrics` stops answering within 2 s for a third
of the window. This was reported on #120. Engine code is out of scope here. It still reproduces on `a0fd405`, the tip of `main` when this PR was opened: 1,602 of 10,000 tag updates/s at 98.7 % CPU in a 30 s window.

### After #238: the system-tag task fixed

The system tags now read plain per-device totals kept beside the counters (no registry
collection). An event step computes only the devices that finished a cycle and their channels.
The once-per-second tick computes every tag but writes only the changed ones (D-090).

Measured on one 4-vCPU container (Intel Xeon @ 2.1 GHz, Python 3.12.3), not the standard
runner, so compare the columns with each other rather than with the tables above. Before is
`main` @ `21a564a` and after is the #238 head on top of it. 30 s window after 10 s of warm-up.
Where there are two runs, both are shown. The diagnostic column is the same container with
`--no-system-tags` (`d8571ac`).

| profile | devices | before: tag updates/s, CPU | after: tag updates/s (target) | overruns | failed requests | CPU | RSS | reply → receive | no system tags |
|---|---|---|---|---|---|---|---|---|---|
| 1k | 8 | 984, 8.4 % | 983 (1,000) | 0 | 0 | 4.3 % | 71 MB | 2.0 / 26 ms | — |
| 10k | 80 | **1,115**, 99 % | **9,976 / 9,965** (10,000) | 0 / 0 | 0 | **34 / 33 %** | 95 MB | 26 / 66 ms · 27 / 90 ms | — |
| 25k | 200 | **132**, 99 % | **24,942 / 24,784** (25,000) | 103 / 467 | 0 | 89 % | 135 MB | 84 / 263 ms · 23 / 359 ms | 24,847, 83 % |
| 50k | 400 | **160**, 99 % | **19,688** (50,000) | 4,778 | 0 | 97 % | 205 MB | 15 / 21 s | 21,624, 97 % |

- 10k keeps up at about a third of one core: 9,965–9,976 of 10,000 at 33–34 % CPU,
  against 99 % before.
- 25k keeps up: 99.1–99.8 % of the target at 89 % CPU. A few hundred late cycles remain in
  30 s, and none fail.
- 50k is past the Python ceiling on this machine: 19.7k tag updates/s, against 21.6k without
  system tags. The task now costs about 6 % more CPU than no system tags at 25k, and about 9 % of
  throughput at saturation, against 77 % of the CPU before.
- The guard: `tests/unit/test_system_tag_task.py` fails if a step collects the metrics registry,
  if an event step computes more than the notified devices, if the totals drift from `/metrics`,
  or if code bypasses them with `.inc()`.

Reproduce: `python bench/run.py --profile 10k --duration 30 --out after.jsonl` (and `25k`,
`50k`, `1k`). For the baseline, check out `21a564a` in a second worktree and run the same
command with `PYTHONPATH=<worktree>/src`. Without that, the editable install keeps importing
this checkout.

### Without the system-tag task (diagnostic)

`--no-system-tags` starts the same engine with `SystemTagTask.step` stubbed out. This shows the
ceiling of the polling and publishing path itself, and what fixing #120 is worth. It is not a
supported configuration.

| profile | tag updates/s (target) | overruns | failed requests | CPU | RSS | reply → receive | outbox max |
|---|---|---|---|---|---|---|---|
| 1k | 995 / 995 (1,000) | 0 / 0 | 0 | 4.5 / 4.1 % | 73 MB | 2.1 / 6.1 ms · 2.3 / 14 ms | 0 |
| 10k | 9,905 / 9,880 (10,000) | 0 / 0 | 0 | 29 / 28 % | 96 MB | 24 / 56 ms · 23 / 53 ms | 15 / 19 |
| 25k | 24,959 / 24,987 (25,000) | 0 / 39 | 0 | 75 / 76 % | 132 MB | 42 / 128 ms · 66 / 164 ms | 183 / 70 |
| 50k | **25,995 / 25,798** (50,000) | 12,857 / 12,630 | 0 | 97 % | 195 MB | 8.1 / 13.9 s · 7.1 / 11.7 s | 4,529 / 3,637 |

**Python reference ceiling: about 26,000 tag updates/s at one core** (GIL-bound, one process).

- The 50k profile saturates the core at 25.8–26.0k. The 25k profile keeps up at 76 % CPU.
- That is ~38 µs of engine CPU per tag update, end to end: Modbus requests (~7 tags each here),
  decoding, the cache, on-change SubDevice JSON for every device every second, the SQLite
  outbox and MQTT.
- Memory grows by ~2.5 KB per tag.

## Results: Rust engine (informational)

These are the same harness and profiles, using the release build of the same commit
(`cargo build --release -p opencap-engine`).

| profile | tag updates/s (target) | overruns | failed requests | CPU | RSS | reply → receive | outbox max |
|---|---|---|---|---|---|---|---|
| 1k | 996 (1,000) | 0 | 0 | 1.3 % | 18 MB | 1.3 / 8.4 ms | 1 |
| 10k | 9,914 / 10,075 (10,000) | 0 / 0 | 0 | 8.5 / 8.3 % | 53 / 52 MB | 2.4 / 15 ms · 2.5 / 16 ms | 10 / 5 |
| 25k | 24,886 (25,000) | 0 | 0 | 19 % | 110 MB | 6.5 / 35 ms | 14 |
| 50k | 50,185 / 50,242 (50,000) | 0 / 0 | 0 | 40 % | 206 MB | 11 / 61 ms · 13 / 68 ms | 75 / 46 |

## Against the targets

- **NFR-2** (10k tags at 1 s: ≤ 10 % of one core, ≤ 64 MB RSS). Measured on x86-64; the
  ARM64 box is still to be measured.
  - Rust: 8.3–8.5 % and 52–53 MB, within the target.
  - Python: 33–34 % and 95 MB with the system-tag task fixed (#238, measured on a 4-vCPU
    container); 28–29 % and 96 MB without it on the standard runner.
- **NFR-3** (reply → publish ≤ 20 ms p99). Here the latency includes the broker hop, with the
  harness on the same 4 cores.
  - Rust meets it at 10k (p99 15–16 ms), but not at 25k (35 ms) or 50k (61–68 ms).
  - Python: 53–56 ms at 10k.
- **NFR-1 / NFR-9** (250k tags, ≤ 1 GB, ≤ 4 cores). Rust at 50k uses 0.4 core.
  - Its RSS grows by ~3.8 KB per tag (~480 KB per device), which extrapolates to ~1 GB at the
    2,000-device target node.
  - Worth a look before NFR-9 is claimed. The tag store itself measures 68 B per tag (#41), so
    the cost is elsewhere (per-device connections or buffers).
- **Harness load.** In every run the busiest simulator process stayed ≤ 33 % of a core, the
  broker ≤ 9.4 % and the subscriber ≤ 11 %, so the harness was never the bottleneck.

## Reproducibility

Per the issue's test plan, the 1k and 10k profiles ran twice each, for both engines and with the
diagnostic. Across the pairs:

- Within ±15 %: tag updates/s (≤ 2.6 % apart), RSS (≤ 2 %), CPU at 10k (≤ 4 %) and p50 latency
  at 10k (≤ 6 %).
- Noisier: CPU at 1k, a few percent of a core (5.9 vs 7.8 % as built), and p99 at 1k. With
  ~470 samples per run, p99 is roughly the fifth-largest sample.
- Not compared: change → receive. It depends on the phase between the simulators' change tick
  and the engine's scan, so it varies from run to run (0.2–1 s at a 1 s scan) for both engines
  and is reported in the JSON only.

Raw results are one JSON object per run (`--out`), with the machine description, the plan size,
markers in the last block (≥ 97 % of devices), missed markers and API timeouts.
