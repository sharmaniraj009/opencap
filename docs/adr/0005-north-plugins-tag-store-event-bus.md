# 0005. Tag store with single writer per device; bounded event bus with conflation

- Status: Accepted
- Date: 2026-11-17
- Deciders: technical lead

## Context
Many consumers read the same values: MQTT agents, Sparkplug, the OPC UA server, Modbus and
DNP3 servers, the data logger, alarms, derived tags, the UI. KEPServerEX's core rule holds
for us: field polling must never slow down because a consumer is slow. The Python
`ValueCache` + `on_cycle` callback works at today's scale on one event loop with no locks.

## Decision
- **Tag store**: one slot per device (values array, version, last good); exactly one writer
  per device (its channel worker or the processing task owning a virtual device); readers
  take snapshots under a short per-device lock, never across an await.
- **Event bus**: after each write batch, an `Arc<ChangeBatch>` is broadcast on a bounded
  channel. A lagging subscriber gets `Lagged(n)` and resynchronises from snapshots
  (conflation to latest). The writer never waits.
- **North plugins** implement the `NorthPlugin` trait and receive a `NorthContext`
  ([03 §3.5](../architecture/03-deep-dive.md#35-north-plugin-contract)); durable agents
  write to their outbox as events arrive ([ADR-0011](0011-store-and-forward-outbox-per-agent.md)).
- **Processing** (derived tags, alarms, system tags) are subscribers that write to their
  own virtual devices, so cycles are impossible by construction (a virtual device's writer is
  its owner; derived-on-derived chains are ordered by a DAG check at config load).
- All north reads come from the store; device reads are demand polls only where the
  device allows it and rate-limited.

## Consequences
- Bounded memory under any consumer behaviour; skipped intermediate values are counted
  (`north_lagged_total`).
- Same semantics in Python (cache + callback) and Rust (store + broadcast).

## Alternatives considered
- Unbounded queues per consumer: memory blow-up when a consumer stalls.
- Backpressure into the scan loop: a slow SCADA would slow polling, as some OPC servers do.
- An external broker inside the node (e.g. NATS): an extra moving part on small boxes.

## Revisit when
> 1M tags per node or lock contention shows up in profiles (shard per channel/core,
`arc-swap` snapshots).
