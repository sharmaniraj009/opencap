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
| Memory, tag store | ~10 MB | ~40 B per `TagValue` |
| Memory, definitions + plans | ~50 MB | ~200 B per tag |
| Memory, OPC UA address space | 150–500 MB | ~0.6–2 KB per node depending on the stack: **the dominant cost; benchmark gate before choosing the stack** |
| MQTT bandwidth | ~800 KB/s (~6.4 Mbit/s) | ~30 B per tag in SubDevice JSON |
| Python reference ceiling | to be measured (benchmark issue) | ~5–10 µs per tag in the hot path suggests ~25k tags/s per core; one process, GIL-bound |

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
