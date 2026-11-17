# 0011. Store-and-forward: one SQLite outbox per durable agent

- Status: Accepted
- Date: 2026-11-17
- Deciders: technical lead

## Context
D-016 chose "outbox-always" for MQTT: every message is written to a SQLite WAL outbox,
drained in id order, deleted only after PUBACK. Phase 1 acceptance proved no loss across a
10-minute network cut (D-018, D-021). New durable agents (several MQTT agents, Sparkplug,
REST client, data logger) need the same guarantee without sharing a failure domain.

## Decision
- Every agent configured as durable owns **one outbox file** (`data/outbox/<agent>.sqlite`)
  with D-016 semantics: WAL, `synchronous=NORMAL`, ordered drain in windows, delete after
  the peer's acknowledgement, `max_bytes` cap with oldest-first drop and a counter.
- Agents never share an outbox: a slow database cannot hold back MQTT.
- Sparkplug: the outbox holds DATA messages while the primary host is offline; on reconnect
  the agent publishes a new BIRTH before draining (sequence numbers stay valid).
- Rust uses rusqlite with the same schema so a Python outbox file can be drained by the Rust
  engine at cut-over (conformance test).
- The UI and metrics show backlog count, oldest-message age, and "outage covered" (04 §4.1).

## Consequences
One small SQLite write per message (cheap in WAL/NORMAL); QoS 1 duplicates are possible at
reconnect and are documented for consumers.

## Alternatives considered
Write-to-outbox-only-on-failure (loses ordering and in-flight messages at crash); one shared
outbox (coupled failure domains); an embedded broker (extra component).

## Revisit when
Sustained > 2,000 msg/s per agent: segmented append-only log.
