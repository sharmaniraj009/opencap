# 0006. Five qualities, two timestamps, one mapping table per north protocol

- Status: Accepted (exact OPC UA codes pinned by the shared quality-mapping issue)
- Date: 2026-11-17
- Deciders: technical lead

## Context
The core has five qualities (`models.Quality`): `good`, `bad_comm`, `bad_config`,
`bad_decode`, `stale`. Each north protocol has its own model: OPC UA StatusCodes, OPC DA
quality bytes, Sparkplug (no standard quality; Ignition reads a `Quality` property),
SubDevice JSON (`Data` + `BadTags`, D-017). Without one table every north plugin invents
its own mapping.

## Decision
| opencap | Meaning | OPC UA StatusCode | OPC DA quality | Sparkplug metric | SubDevice JSON |
|---|---|---|---|---|---|
| `good` | last read good | `Good` (0x00000000) | 0xC0 Good | value, `Quality` property 192 | in `Data` |
| `bad_comm` | last attempt failed on the wire; last value kept | `Bad_CommunicationError` (0x80050000) | 0x18 Bad: Comm Failure | `is_null` false with last value, `Quality` 0x18 | in `BadTags` |
| `bad_config` | address unusable or rejected by the device | `Bad_ConfigurationError` (0x80890000) | 0x04 Bad: Config Error | `is_null` true, `Quality` 0x04 | in `BadTags` |
| `bad_decode` | reply could not be converted | `Bad_DecodingError` (0x80070000) (*pin*) | 0x00 Bad | `is_null` true, `Quality` 0x00 | in `BadTags` |
| `stale` | no good read within 3 × rate, or never read | `Uncertain_LastUsableValue` (0x40900000); never read: `Bad_WaitingForInitialData` (0x80320000) | 0x44 Uncertain: Last Usable; never read 0x00 | last value, `Quality` 0x44 | in `BadTags` |

- Timestamps: `source_ts` = reply arrival time, or the device's own timestamp when the
  protocol provides one (OPC UA client, DNP3 events); `server_ts` = when the store accepted
  the value. OPC UA `SourceTimestamp`/`ServerTimestamp` map to them; SubDevice JSON
  `DateTime` stays the newest `source_ts` among good tags (D-017).
- All internal timestamps are tz-aware UTC; local time zones exist only in payloads.
- The table lives in one module per engine (`opencap.core.quality`; `engine/crates/core`),
  with conformance vectors; north plugins must not hard-code codes.

## Consequences
- Finer distinctions (sensor failure, out of service) are not represented until a driver
  can actually detect them; adding a quality is an ADR change.

## Alternatives considered
Using OPC UA StatusCodes as the internal model (heavy, and most drivers cannot fill them
meaningfully).

## Revisit when
A driver can report device-level quality (DNP3 flags, OPC UA client pass-through) worth
preserving end to end.
