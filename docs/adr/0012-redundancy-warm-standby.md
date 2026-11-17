# 0012. Redundancy: active/warm-standby pair, standby never polls

- Status: Accepted (protocol details in RFC 0003)
- Date: 2026-11-17
- Deciders: technical lead

## Context
KEPServerEX offers a Redundancy Master (primary/secondary). Many PLCs in the reference
project accept very few connections (Micro800); two nodes polling the same PLC can starve
it or the HMI.

## Decision
- Two nodes share one project (synchronised from the active node, revision-checked) and have
  their own node settings.
- Exactly one node is **active** and polls; the **standby** runs everything except south
  polling and north publishing, and serves its API and OPC UA endpoint with
  `ServiceLevel` low.
- Liveness: heartbeat between the nodes (UDP + TCP) and a **witness** (configurable: the MQTT
  broker's retained status topic, or a third host) so a network split cannot make both
  active. A node that loses both its peer and the witness stays (or becomes) standby.
- Takeover ≤ 15 s; north semantics: OPC UA non-transparent redundancy (`ServerUriArray`,
  `ServiceLevel`), MQTT/Sparkplug one publisher at a time; outboxes are per node and not
  shared.
- Media-level redundancy (alternate device paths) is independent and per device.

## Consequences
After takeover the standby's tag values are cold until its first cycle (seconds); SCADA sees
a short stale period, not two writers.

## Alternatives considered
Hot-hot (both poll): double PLC load; shared-storage cluster: needs infrastructure small
sites do not have; consensus cluster: unjustified for pairs.

## Revisit when
Sites need N+1 groups or sub-second failover.
