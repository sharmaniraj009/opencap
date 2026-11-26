# Architecture decision records

One file per decision: `NNNN-short-title.md`, numbered in order, never renumbered. An ADR is
**Proposed** until merged, then **Accepted**. A later ADR can mark an earlier one
**Superseded by NNNN**; the old file stays. The lead owns final decisions; anyone may propose
an ADR in a PR.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-rust-engine-python-reference.md) | Rust production engine; Python as reference and conformance harness | Accepted |
| [0003](0003-shared-config-schema.md) | JSON Schema generated from the Pydantic models is the cross-language config contract | Accepted |
| [0004](0004-driver-contract-and-plugin-model.md) | Driver contract; compiled-in Rust drivers plus out-of-process gRPC plugins | Accepted |
| [0005](0005-north-plugins-tag-store-event-bus.md) | Tag store with single writer per device; bounded event bus with conflation | Accepted |
| [0006](0006-quality-and-timestamps.md) | Five qualities, two timestamps, one mapping table per north protocol | Accepted |
| [0007](0007-oss-licence-policy.md) | OSS reuse and licence policy | Accepted |
| [0008](0008-read-only-by-default.md) | Read-only toward PLCs until a write-safety RFC is approved | Accepted |
| [0009](0009-scada-connectivity-strategy.md) | SCADA connectivity: OPC UA first, KEPServerEX-compatible naming | Accepted |
| [0010](0010-one-ui-web-and-tauri.md) | One web UI, served by the engine and wrapped by Tauri | Accepted |
| [0011](0011-store-and-forward-outbox-per-agent.md) | Store-and-forward: one SQLite outbox per durable agent | Accepted |
| [0012](0012-redundancy-warm-standby.md) | Redundancy: active/warm-standby pair, standby never polls | Accepted (details in RFC 0003) |
| [0013](0013-project-file-v2.md) | Project file v2: one project document plus node settings | Proposed |
| [0014](0014-conformance-by-simulators-and-vectors.md) | Behaviour is proven by simulators and shared conformance vectors | Accepted |
| [0015](0015-dnp3-native-rust-subset.md) | DNP3: native Rust master and outstation for the read subset; opendnp3 and dnp3py as test peers | Proposed |
| [0016](0016-bacnet-native-rust-client.md) | BACnet/IP: native Rust read-only client; bacpypes3 simulator and a second independent peer | Proposed |

## Template

```markdown
# NNNN. Title

- Status: Proposed | Accepted | Superseded by NNNN
- Date: YYYY-MM-DD
- Deciders: technical lead (+ owner when the decision is reserved to the owner)

## Context
## Decision
## Consequences
## Alternatives considered
## Revisit when
```
