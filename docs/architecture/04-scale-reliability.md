# 4. Scale and reliability

## 4.1 Load model

Polling is **latency-bound per channel**, not CPU-bound. With one request on the wire per
channel (KEPServerEX semantics, D-015):

```
cycle_time(device) ≈ Σ over blocks (RTT + device processing) × attempts
feasible  ⇔  cycle_time ≤ scan rate          (else overruns, counted in poll_overrun_total)
channel capacity ≈ 1 / (RTT + processing)    requests/s   (≈ 16 req/s at 60 ms RTT)
```

`opencap check` computes this per device with `check.est_rtt_ms` (done).

### Reference project (real KEPServerEX export, kept private; numbers from `docs/decisions.md`)
| Driver | Devices | Planned blocks / requests | Note |
|---|---|---|---|
| Modbus TCP | 29 | 268 blocks | 32-register blocks from the KEPServerEX device setting (D-005) |
| AB Logix / Micro800 | 69 (66 Micro800) | 6,727 requests per pass unbatched, ~511 batched | Micro800 batching is the single biggest lever (D-008, D-025) |
| AB PCCC | 30 (10,320 tags) | 694 blocks → ~691 req/s at 1 s | ~23 blocks/device × 60 ms ≈ 1.4 s per cycle: these devices overrun a 1 s scan over the VPN, as they do under KEPServerEX |
| All | 144 publishing devices | ~15 MQTT msg/s at a 10 s interval (D-016) | ~30 KB/s north |

### Target node (NFR-1)
2,000 devices × 125 tags = 250,000 tags at 1 s; ~25 tags per request on average.

| Quantity | Estimate | Basis |
|---|---|---|
| Wire requests | ~10,000 req/s total, ~5 req/s per channel | 250k tags / 25 per request; one device per channel |
| Channel busy time | ~0.3 s per second at 60 ms RTT | feasible, 3× headroom |
| CPU, south (Rust) | ~0.3 core | ~20 µs per request (syscalls, framing), ~0.5 µs per tag (decode, scale, store) |
| CPU, north MQTT | < 0.1 core | 2,000 devices / 10 s = 200 msg/s, ~4 KB each |
| CPU, OPC UA server | ~1 core | 250k monitored items sampled from the store at 1 s; notifications only on change |
| Memory, tag store | ~17 MB (measured, #41) | 64 B per `TagValue` (two timestamps), 68 B per tag with the slot |
| Memory, definitions + plans | ~50 MB | ~200 B per tag; of which the store's names and name index measure 102 B per tag (#41) |
| Memory, OPC UA address space | 150–500 MB | ~0.6–2 KB per node depending on the stack: **the dominant cost; benchmark gate before choosing the stack** |
| MQTT bandwidth | ~800 KB/s (~6.4 Mbit/s) | ~30 B per tag in SubDevice JSON |
| Python reference ceiling | **~26k tag updates/s at one core**, measured (#7, [bench/README.md](../../bench/README.md)); the system-tag task (#120) capped it at ~1.2k (10k tags, 99 % CPU) until #238: now 10k tags keep up at 33–34 % of one core and 25k at 89 %, and the system tags cost ~9 % of throughput at saturation (D-095) | 50k-tag profile saturates one core (GIL) at 25.8–26.0k; 25k keeps up at 76 % CPU; ~38 µs engine CPU per tag update end to end (Modbus, decode, cache, on-change SubDevice JSON every second, outbox, MQTT); ~2.5 KB RSS per tag |

### Measured: tag store and change-event bus (#41)

`cargo bench -p opencap-core --bench store_load` (paced scenario) and `--bench store`
(Criterion), on a 4 vCPU x86-64 cloud VM (Xeon @ 2.1 GHz), tokio runtime with 4 worker
threads, 10 s per scenario. The ARM64 figures for NFR-2 come with the benchmark harness (#7).

**Load:** 250,000 tags on 2,000 devices (125 tags each); 250,000 tag updates/s from 8 writer
tasks, each owning 250 devices; one write = one 25-tag block (10,000 writes/s); every value
changes on every write. Subscribers count the changes they receive; the slow one sleeps 1 ms
after every batch (so it can take ~1,000 of the 10,000 batches/s) and resynchronises from
snapshots of all 2,000 devices when it lags.

| Scenario | Updates/s achieved | Write p50 | Write p99 | Write p99.9 | Process CPU | Subscribers |
|---|---|---|---|---|---|---|
| No subscribers | 250,000 | 1.4 µs | 3.4 µs | 20–25 µs | 0.07 core | — |
| 4 fast | 250,000 | 2.3 µs | 11.8–14.0 µs | 35–40 µs | 0.11 core | none lagged |
| 3 fast + 1 slow | 250,000 | 2.7 µs | 14.2–14.6 µs | 36–38 µs | 0.13 core | fast: none lagged; slow: 23 resyncs, 94.5k of 100k batches skipped |

Ranges are two runs. **The slow subscriber does not move the writer's p99** beyond
run-to-run noise (11.8–14.0 µs with 4 fast subscribers, 14.2–14.6 µs with 3 fast + 1 slow);
the fast subscribers never lag. The step from no subscribers to some (3.4 → ~13 µs p99) is the
cost of waking parked subscriber tasks inside `send`, which does not depend on subscriber
speed. Maxima (1.5–6 ms) are OS scheduling preemptions on a shared VM and occur in every
scenario. Before the receiver jumped to the newest batch on `Lagged` (tokio resumes at the
oldest slot, which the next send overwrites), the slow subscriber resynchronised 150 times
a second and pushed p99 to 32 µs (D-066).

| Criterion micro-benchmark (mean) | 25 tags | 125 tags |
|---|---|---|
| `update_indexed`, no subscriber | 0.34 µs | 1.56 µs |
| `update` by name, no subscriber | 1.05 µs | 4.2 µs |
| `update_indexed`, 4 subscribers that never read (every send overwrites a batch) | 1.08 µs | 4.3 µs |
| `update_indexed`, 4 live subscribers, one slow (back-to-back writes) | 2.9 µs | 7.5 µs |
| `snapshot` of a device | — | 2.6 µs |
| `get` of one tag | 71 ns | — |

**Memory per tag** (RSS deltas, float values): values 68 B (`TagValue` is 64 B: value 32,
two timestamps 12 each, quality), names and name index 102 B (each name allocated once,
shared by the layout and the index). The bus keeps only batches some subscriber has not read
yet: at most `capacity` × batch size, 4,096 × ~1.9 KB ≈ 7.6 MB for 25-tag batches, reached
only by a subscriber a full ring behind.

### Store-and-forward sizing
```
outage covered = max_bytes / (msg/s × avg message bytes)
```
- Reference project: 2 GiB / (15 msg/s × 2 KB) ≈ 19 h.
- Target node: 2 GiB / (200 msg/s × 4 KB) ≈ 45 min. Size `max_bytes` per site for the
  longest WAN outage expected; the UI shows "outage covered" from the live rates.
- Drain after recovery is broker-bound: ~4,500 msg/s against amqtt (D-018); a 45-minute
  backlog drains in ~2 min while new data keeps flowing in order.
- SQLite in WAL with `synchronous=NORMAL` does not fsync per commit, so one row per commit is
  cheap; the Rust outbox still groups inserts per event batch.

## 4.2 Scaling

1. **Vertical first.** One async process; channels are tasks; no shared lock on the hot path
   (single writer per device, ADR-0005). Rust engine target 250k tags on 4 cores.
2. **Partition by channel across nodes.** A site too big for one node runs several nodes,
   each owning disjoint channels (`opencap run --site …` exists today; project split and
   multi-node management come with the desktop app). No cluster consensus: nodes are
   independent, which keeps failure domains small.
3. **Fan-out at the broker, not in opencap.** Many consumers subscribe to the MQTT broker;
   opencap publishes once.
4. **Bound north servers.** OPC UA: `max_sessions`, `max_monitored_items`, minimum
   sampling/publishing intervals; device reads served from cache (03 §3.5).
5. **Per-channel concurrency** (opt-in) for protocols with transaction ids (Modbus TCP,
   OPC UA, S7 with multiple PDUs) when a site has many devices behind one gateway.

## 4.3 Failover and redundancy

Single node: the supervisor (systemd `Restart=on-failure` with no start limit (`deploy/opencap.service`), WinSW or Windows service recovery actions)
restarts a crashed engine; the outbox and project survive; polling resumes from
`offline` state; north status topics show `offline` (LWT) during the gap.

Redundant pair ([ADR-0012](../adr/0012-redundancy-warm-standby.md), RFC on the board):
active/warm-standby, heartbeat between nodes plus a witness to avoid split brain, standby
never polls (Micro800s and many PLCs allow very few connections), takeover ≤ 15 s; OPC UA
non-transparent redundancy (`ServerUriArray`, `ServiceLevel`), MQTT/Sparkplug: one
publisher at a time, outbox not shared (each node publishes what it polled).

Media-level redundancy: a device may list alternate hosts/routes; the worker switches after
`demote_after` failed cycles on the current path.

## 4.4 Failure modes

| Failure | Detection | Behaviour | Visible as |
|---|---|---|---|
| PLC off / unreachable | connect fails | backoff 1 → 60 s; demotion | `device_state`, status topic `offline`, `_Error` |
| PLC slow or silent after TCP accept | timeouts | retries, then demotion; slows its channel (known limit) | `poll_requests_total{result="timeout"}` |
| PLC rejects addresses | exception / status codes | block disabled until reload | `bad_config` tags, `opencap check` |
| VPN / WAN down (north side) | publish fails | outbox fills; ordered drain on recovery (done) | `saf_queue_messages`, outbox age |
| Broker restart | link drops | reconnect, re-drain; QoS 1 duplicates possible | `mqtt_connected` |
| Disk full / outbox cap | write error / size cap | drop oldest (done), alarm | `saf_dropped_total` |
| Engine crash | supervisor | restart ≤ 5 s | process restarts metric, LWT |
| Bad config pushed | validation | rejected, old config kept (done) | API `400`, event log |
| Plugin process crash | plugin host | restart with backoff | `plugin_restarts_total` |
| North consumer too slow | bus lag | resync from snapshot | `north_lagged_total` |
| OPC UA client flood | limits | refuse sessions/items beyond limits | server diagnostics, event log |
| Clock jump | monotonic scheduling | timestamps follow wall clock; schedules unaffected | — |
| Memory growth | RSS metric | alert; restart policy | `process_resident_memory_bytes` |
| Active node of a pair fails | heartbeat + witness | standby takes over ≤ 15 s | `_System._Redundancy`, `ServiceLevel` |

## 4.5 Monitoring

- **Metrics** (Prometheus, done): `poll_requests_total`, `poll_cycle_seconds`,
  `poll_overrun_total`, `device_state`, `mqtt_published_total`, `saf_queue_messages`,
  `saf_dropped_total`. Planned: per-agent `north_published_total{agent}`,
  `north_backlog_messages{agent}`, `north_backlog_age_seconds{agent}`,
  `north_lagged_total{agent}`, `tag_store_tags`, `plugin_restarts_total{plugin}`,
  `opcua_sessions`, `opcua_monitored_items`, `config_revision`.
- **Health:** `/health` (done) splits into `/health/live` and `/health/ready` (ready = config
  loaded and north agents started) for container orchestration.
- **System tags** (FR-10): the same numbers as tags, so SCADA can alarm on them without
  Prometheus.
- **Logs:** structured (structlog/tracing), state changes at INFO only, one `device.stats`
  line per device every 5 min (D-023), DEBUG logs every request.
- **Event log:** bounded ring buffer + JSONL file, shown in the UI (KEPServerEX event log).
- **Alert rules:** a reference `deploy/prometheus/alerts.yml` (device demoted > 10 min,
  outbox age > 5 min, outbox dropping, overruns rising, engine restarting).
