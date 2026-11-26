# RFC 0002: write safety

- Status: **Proposed.** [ADR-0008](../adr/0008-read-only-by-default.md) stays in force: opencap
  writes nothing to any PLC until the owner approves this RFC on the board issue (#1), and then
  only in the phases of §15, each one approved again.
- Date: 2026-11-26
- Author: `conformance` session (#19)
- Builds on: [ADR-0006](../adr/0006-quality-and-timestamps.md) (qualities),
  [ADR-0012](../adr/0012-redundancy-warm-standby.md) (active/standby),
  [ADR-0013](../adr/0013-project-file-v2.md) (`project.json` + `node.yaml`),
  [ADR-0014](../adr/0014-conformance-by-simulators-and-vectors.md) (simulators and vectors),
  RFC 0005 (users and roles, #21, not yet written).

## Summary

Many KEPServerEX users write setpoints from SCADA, a UI, or link tags. opencap does not: every
driver refuses write services, and every north server is read-only (ADR-0008). This RFC
describes how writes could be added **without making an accidental or malicious write easy**,
so the owner can decide whether to allow them at all.

The design rests on five ideas:
1. **Every layer says no by default.** A value reaches a PLC only when the node, the device, the
   tag, the user's role, the north interface and the build all allow it.
2. **Grants live in two places that travel separately.** The project file says *which* tags may
   be written. The node's own file says *whether this node writes at all*. Copying a project to
   another site, or importing one, cannot switch writes on there.
3. **One write pipeline.** Every write, whatever its source, passes the same core stages:
   validation, interlocks, rate limits, confirmation, audit, the driver, read-back. North
   interfaces only translate requests into it.
4. **Writes are proven to be absent.** Tests show that nothing goes on the wire unless every
   switch is on, and that a build without the write feature contains no write path at all.
5. **One action stops everything.** A kill switch on the node stops all writes at once and
   survives restarts. A remote client cannot undo it.

The RFC ends with the question for the owner (§17).

## 1. Scope

**A write** here is a request that changes a value in a PLC: a register, coil, tag, data-table
element, point or object property, through any driver.

Not writes, and allowed today through the authenticated, audited API (ADR-0008): changing
opencap's own configuration or state, such as config edits, reloads, demotion resets and,
later, alarm acknowledgement.

KEPServerEX features that would depend on this RFC (plan rows that point at #19):

| plan row | feature |
|---|---|
| TAG-06 | client access read/write per tag |
| UI-17 | write values from the test client |
| CH-15 | write optimisations (latest value only, write/read duty cycle) |
| SYS-09 | writable parameter-control and action tags |
| DEV-14 | device clock synchronisation |
| COM-04 | modem control through tag writes |
| IFIX-10 | the write side of iFIX conditioning and output blocks |
| LOG-20 | write failures and rejections in the event log |
| config API | value writes through the configuration API |

This RFC designs the mechanism behind them. It does not approve any of them. §14 lists what
stays forbidden even after approval.

## 2. Threat model

| # | Threat | Example | Mitigations (section) |
|---|---|---|---|
| T1 | Accidental write | an operator types 1000 instead of 100.0; a UI field keeps focus | value validation (§5), confirmation (§7), rate of change (§5) |
| T2 | Wrong tag | an alias or a copy-pasted address points at a different register; a scaled tag is written with an engineering value | per-tag grants (§4), raw-range check after inverse scaling (§5), read-back (§10) |
| T3 | Wrong site or wrong PLC | a project copied from Plant A runs at Plant B; a device's IP is reused after a hardware swap | node-local enable (§3), identity pin per device (§4) |
| T4 | Replay | a captured Sparkplug DCMD or REST request is sent again; a SCADA client re-sends after reconnecting | request ids and freshness windows (§9), idempotency keys (§9), no automatic retries (§10) |
| T5 | Compromised SCADA client | malware on an HMI writes to everything it can reach | per-interface switch and allow-list (§3, §9), per-tag grants and roles (§4), rate limits (§6), interlocks (§8) |
| T6 | Compromised UI or API user | a stolen token; an insider | roles (§4), two-person rule (§7), audit (§11), kill switch (§13) |
| T7 | Misconfiguration on import | KEPServerEX "Read/Write" tags imported as writable | import never grants writes (§4) |
| T8 | Redundancy split | both nodes of a pair write after a failover | only the active node writes; failover cancels pending writes (§10) |
| T9 | Stale or bad data in a decision | an interlock reads a value that is minutes old, or bad | interlocks need fresh good values (§8) |
| T10 | Read-modify-write races | writing one bit of a 16-bit word rewrites the other 15 | no bit writes inside words unless the protocol has a native masked write (§5) |
| T11 | Test traffic reaches a plant | a CI job or cloud session points at a real IP | simulation-only rule (§12) |

Out of scope: physical access to the node, and a PLC that is already compromised.

## 3. Defaults: off at every layer

A write goes ahead only if **all** of these are on. They are checked in this order, and the
first refusal is reported.

| Layer | Where it lives | Default | Who can change it |
|---|---|---|---|
| Build | Cargo feature `writes` / Python extra `opencap[writes]` | absent from every published build until phase 1 is approved | release process |
| Node | `node.yaml` → `writes.enabled` | `false` | someone with file access on the node; never the API |
| Kill switch | a `writes.disabled` file next to `node.yaml` (§13) | absent | file access, CLI on the node, or API (to stop, never to start) |
| North interface | `node.yaml` → `writes.interfaces: [rest, opcua_server, ...]` | empty | file access on the node |
| Device | `project.json` → device `writes.enabled` plus `writes.identity` | `false` | role `engineer`, through the config API, audited |
| Tag | `project.json` → tag `write: {...}` (§4) | absent (read-only) | role `engineer`, audited |
| Principal | RFC 0005 roles plus the tag's `roles` | none | role `admin` |
| Redundancy | this node is the active node (ADR-0012) | — | automatic |
| Maintenance window | when the tag requires one (§8) | closed | role `engineer`, audited, expires |

**Node switches are not in the project.** `project.json` is shared by a redundant pair, synced,
exported and imported (ADR-0013). If it carried the master switch, copying a project could turn
writes on somewhere else (T3). `node.yaml` is per node and is never synced or exported.

## 4. Per-tag permission model and config schema

A tag is writable only with an explicit `write` block. **No wildcard grants:** there are no
groups, patterns or "all tags of this device".

```jsonc
// project.json (v2), one tag
{
  "name": "PUMP_1_SPEED_SP",
  "address": "400120",
  "dtype": "float32",
  "scale": {"raw_lo": 0, "raw_hi": 32000, "eng_lo": 0, "eng_hi": 100, "units": "%"},
  "write": {
    "roles": ["operator"],                 // RFC 0005 roles allowed to write (also via SCADA users)
    "interfaces": ["opcua_server", "rest"],// subset of the node's writes.interfaces
    "min": 0, "max": 85,                   // engineering units, inclusive; required for numbers
    "max_step": 10,                        // largest change from the current good value per write
    "allowed": null,                       // or a list of allowed values (enums, bools, strings)
    "max_per_minute": 6,                   // per tag (§6)
    "confirm": "single",                   // none | single | two_person (UI and REST, §7)
    "interlocks": ["PUMP_1_REMOTE == true"],   // §8
    "maintenance_only": false,             // §8
    "read_back": true                      // §10; default true
  }
}
```

The device carries its own block:

```jsonc
{"id": "Plant A/PLC1", "writes": {"enabled": true,
  "identity": {"vendor": "...", "product_code": 123, "serial": "..."}}}
```

**Identity pin.** Before the first write of a session, the engine reads the device's identity
(Modbus FC43/14, CIP Identity, S7 SZL, MC CPU model, ...) and compares it with
`writes.identity`. A mismatch refuses every write to the device until the pin is updated
through the audited API (T3). A device whose protocol has no identity cannot be pinned. Writing
to it would need a separate, explicit `identity: "none"`, which the UI shows as a warning.

**Schema rules:**
- `write` is valid only on enabled tags of devices whose driver implements writes (§10).
- Numbers need `min` and `max`.
- Strings need `max_length` or `allowed`.
- Arrays and structures are not writable in this RFC.
- KEPServerEX import never creates `write` blocks (T7). The import report lists tags that were
  Read/Write in KEPServerEX, so that someone can decide per tag.
- Both engines validate the schema the same way (ADR-0003), with conformance fixtures in
  `tests/conformance/config/`.

## 5. Value validation

Validation runs in the core, before any driver code. Each request is either accepted or refused
with a reason.

1. **Type.** The value must be exactly the tag's dtype. There is no implicit string-to-number
   conversion. NaN and ±Inf are refused, and so are integers outside the dtype's range.
2. **Range.** `min ≤ value ≤ max` in engineering units. Values are refused, never clamped.
3. **Inverse scaling.** For scaled tags, the raw value is `raw_lo + (v − eng_lo) / (eng_hi −
   eng_lo) × (raw_hi − raw_lo)`, then rounded for integer dtypes. It is checked against
   `[raw_lo, raw_hi]` and against the dtype. Square-root and negated scales are invertible
   only where unique; the others are not writable. There are conformance vectors for the
   inversion (ADR-0014).
4. **Allowed set**, when given.
5. **Step.** `|value − current| ≤ max_step`, where `current` is the tag's last **good** value,
   no older than 2 × its scan rate. Without such a value the write is refused (T9).
6. **Bit writes.** Coils and native bit objects are writable. A bit inside a word (Modbus `.b`,
   PCCC `/b`, Logix `.n`) is writable only when the protocol has an atomic masked write (for
   example Modbus FC22, Mask Write Register). Otherwise it is refused, because a
   read-modify-write could overwrite a change the PLC made in between (T10).

## 6. Rate limits

There are token buckets, all checked before the confirmation step:
- per tag: `max_per_minute`, default 6;
- per device: default 60/min;
- per principal: default 30/min;
- per node: default 300/min.

Limits apply whatever the interface. A refusal is an event with its reason (LOG-20). Repeated
refusals from one client (default 20 in a minute) suspend that client's writes for 10 minutes,
and that is audited (T5).

## 7. Confirmation and the two-person rule (UI and REST)

- **`single`:** the client first asks for a preview. The engine answers with the device, its
  identity, the site, the tag, the current value with its age and quality, the requested value
  and its raw value, and a confirmation token valid for 60 s. The write only happens with that
  token. The UI shows all of this and asks the user to type the value again.
- **`two_person`:** the write is held as *pending* (`202 Accepted`). A second principal with a
  write role must approve it within 5 minutes. The approver must be a different identity, not
  the same user with another token, and sees the same preview with fresh values. The requester
  can cancel. An expired request stays in the audit log.
- **SCADA interfaces** (OPC UA, Modbus, DNP3, IEC 104, Sparkplug) cannot hold a write for
  confirmation. Tags with `confirm: two_person` are therefore not writable through them, and
  config validation says so. DNP3 and IEC 104 still use select-before-operate (§9).

## 8. Interlocks and maintenance mode

**Interlocks** are boolean conditions on other tags of the same device, in the derived-tag
expression language (#23). Example: `PUMP_1_REMOTE == true && PUMP_1_FAULT == false`. They are
evaluated at write time on values that are **good and fresh** (≤ 2 × their scan rate). Unknown,
stale or bad means refused (T9). Interlocks protect against mistakes, not against an attacker
who controls the PLC.

**Maintenance mode** is per device. An `engineer` opens a window with a reason and an end time,
at most 8 hours. Tags with `maintenance_only: true` are writable only inside the window. Opening
and closing are audited, the window ends by itself, and the device's `_System` tags show it (#22).

## 9. North interfaces

Each interface is off unless it is listed in the node's `writes.interfaces`. Every write it
receives goes through the pipeline. Refusals map to the protocol's own error codes.

| Interface | How writes would appear | Required before it may write | Refusal |
|---|---|---|---|
| REST (`POST /api/v1/devices/{id}/tags/{tag}/write`) | preview → write (single), or pending → approve (two-person) | TLS (or loopback), RFC 0005 user with a write role, `Idempotency-Key` header, request time within ±60 s | 403 / 409 / 422 / 429 with a reason code |
| OPC UA server | AccessLevel `CurrentRead \| CurrentWrite` only on granted tags; UserAccessLevel per user | security mode SignAndEncrypt, an authenticated user (anonymous never writes), certificate in the trust list | BadUserAccessDenied, BadOutOfRange, BadTypeMismatch, BadNotWritable, BadTooManyOperations (rate) |
| Modbus TCP server | FC05/06/15/16 on mapped writable registers only | client IP allow-list (Modbus has no authentication); loopback by default | exception 01/02/03 |
| DNP3 outstation | CROB and analog outputs on granted points, **select-before-operate only** (direct operate refused) | Secure Authentication v5 if the library supports it; otherwise allow-listed masters | status codes NOT_SUPPORTED / NOT_AUTHORIZED |
| IEC 60870-5-104 server | C_SC/C_DC/C_SE commands, select/execute only | allow-listed clients; IEC 62351 if supported | activation confirmation (COT 7) with P/N = 1 (negative) |
| Sparkplug B | DCMD on granted metrics only; NCMD stays limited to Rebirth | TLS, broker ACL limiting who publishes `spBv1.0/+/DCMD/#`, sequence and timestamp freshness (T4) | metric ignored, event with reason |
| SubDevice JSON MQTT, IoT Gateway | **no write path** | — | — |
| UI and Quick Client | through REST only | as REST | as REST |

**Replay (T4).** REST needs an idempotency key, remembered for 24 h, and a timestamp within
±60 s. Sparkplug commands outside the expected sequence or older than 60 s are dropped. OPC UA
relies on its secure channel. Modbus has no protection, which is why it is allow-list-only and
listed last in the rollout (§15).

## 10. Driver write path and core pipeline

**Separate capability.**
- A write is a separate trait or interface (`Writer`), not a method every driver inherits. In
  Rust it is compiled only with the `writes` feature. In Python it lives in a module that is
  imported only when `opencap[writes]` is installed and the node enables writes.
- Without the feature, `Driver.write()` keeps raising, and the existing write-service guards
  stay. Today these are: Modbus sends no write FCs; CIP `WRITE_SERVICES` are refused; PCCC
  `check_read_only`; the S7 and MC client subclasses refuse every PDU or command except their reads
  before anything is sent.
- With the feature, a guard turns into an allow-list. A write frame may carry only the address
  of a tag the pipeline approved. Anything else is refused before the socket.

**Pipeline**, the same for every source:

```
request → authenticate → layer checks (§3) → validate (§5) → interlocks (§8) → rate limit (§6)
        → confirm (§7) → audit "pending" (§11) → active-node check (ADR-0012) → driver write
        → read back (next read of the tag, or an immediate read) → audit "done" with the outcome
```

**Failure behaviour.**
- **Timeout:** the outcome is *unknown*, not failed. The read-back decides, and the audit log
  says "unknown" until it does.
- **No automatic retries,** ever (T4). A failed write is reported; the client decides.
- **Connection lost** before sending: refused. After sending: unknown, as above.
- **Read-back mismatch:** an event of severity warning (the PLC may clamp or reject silently),
  with both values.
- **Redundancy:** only the active node writes. The standby refuses with "standby". A failover
  cancels every pending and two-person request.
- **Ordering:** writes to one device go out in request order, one at a time, between poll
  blocks, using the channel's wire lock (D-015).

## 11. Audit log

There is one line per write request, written before the driver is called and again with the
outcome. It is append-only JSON lines next to the config audit log, and hash-chained: each line
carries the SHA-256 of the previous one, so a deleted or edited line is detectable.

Content of each line:
- the time;
- who and the role, plus the approver for two-person writes;
- the interface and client identity: OPC UA session, user and certificate thumbprint; MQTT
  client id; source IP;
- the node and whether it is active;
- the device, its pinned and observed identity, the tag and the address;
- the requested and raw value, the previous good value with its age, and the interlock results;
- the request id or idempotency key and the config revision;
- the outcome: written, refused with a reason code, unknown, or a read-back mismatch, with the
  wire status (exception code, CIP status, ...);
- the latency.

**Retention:**
- at least 1 year by default (`node.yaml` → `writes.audit_retention_days`);
- rotated into compressed files, never truncated;
- refusals included;
- optionally forwarded to syslog.

If the audit file cannot be written, **writes stop** (fail closed) and the node raises a
critical event.

## 12. Testing: simulation only

- Every write test runs against the simulators in `tests/helpers/` (ADR-0014). Each simulator
  counts write requests; `ModbusSim.write_attempts` does this today.
- **A test proves the absence of writes, not only their presence.** For each driver:
  - With any single layer of §3 off, a write request produces **zero write frames** on the
    simulator. This is a parametrised test over every layer.
  - A property test over random combinations of switches: a frame appears only when all are on.
  - A build without the feature: the existing read-only tests still pass, and a test asserts that
    the write module cannot be imported. On Rust, a test checks that the `Writer` symbol does
    not exist.
- **Vectors** in `tests/conformance/write/` cover the validation rules (§5), including inverse
  scaling, so both engines refuse the same requests with the same reasons.
- **No real plant, ever, from CI or a cloud session** (§14). With the feature enabled, test
  configurations accept only loopback and documentation addresses. A non-test build has no such
  limit, so commissioning on a real PLC is done by the owner on site, with the checklist the
  phase defines.

## 13. Incident response: the kill switch

- **Stop:** `opencap writes off` on the node, `POST /api/v1/writes/off` (any write role or
  admin), creating the file `writes.disabled`, or SIGUSR2 (POSIX only). Each one cancels pending and
  two-person requests, refuses new ones, raises a critical event and is audited.
- **Stays stopped:** the file survives restarts and config reloads. Upgrades never remove it.
- **Restart writes:** only on the node. The operator deletes the file, or runs `opencap writes
  on` with local file access, and states a reason, which is audited. **There is no API to turn
  writes back on** (T6).
- **Runbook** to ship with phase 1: stop writes, export the audit log, compare written values
  with the plant historian, rotate tokens and certificates, review grants, then re-enable.

## 14. What stays forbidden even after approval

1. **Writes from CI, cloud sessions or automated tests to anything but the simulators.** This
   includes the owner's own test PLCs reached from a cloud session.
2. Writes to a device or tag without its explicit grant. Wildcard grants. Grants created by an
   import.
3. Writes through SubDevice JSON MQTT or the KEPServerEX IoT Gateway formats.
4. Link tags and any automatic PLC-to-PLC copying. A copy loop has no human in it and would need
   its own RFC.
5. Anonymous writes, writes over unencrypted OPC UA, and writes from a standby node.
6. Automatic retries of a write, and write optimisations that drop a requested value silently
   (CH-15 "latest value only" would need its own decision).
7. Read-modify-write of bits inside a word.
8. Writes while any interlock is unknown, or the tag's current value is stale or bad.
9. PLC program downloads, mode changes (run/stop), firmware updates, clock synchronisation
   (DEV-14), modem control (COM-04) and writable system tags (SYS-09). These are not value
   writes and each would need its own RFC.
10. Turning writes back on remotely after the kill switch.

## 15. Rollout, if approved

Each phase is a separate owner approval, recorded on the board, with its own PRs and simulator
evidence.

| Phase | Scope | Exit evidence |
|---|---|---|
| 0 (today) | nothing; ADR-0008 | — |
| 1 | pipeline, audit, kill switch, REST + UI writes (`single`, `two_person`); Modbus TCP and Logix drivers; Python reference first, then Rust | §12 tests for both drivers, write vectors in both engines, kill-switch acceptance test |
| 2 | OPC UA server writes (SignAndEncrypt, user tokens) | SCADA L1 matrix (#29) clients write to granted tags and are refused elsewhere |
| 3 | other drivers (PCCC, S7, MC, OPC UA client, ...), one per PR, each with its simulator's write support | per-driver §12 tests |
| 4 | DNP3 / IEC 104 select-before-operate; Sparkplug DCMD; Modbus server (allow-list) | per-interface refusal tests, replay tests |

## 16. Open points for the lead

- The roles (`operator`, `engineer`) assume RFC 0005 (#21). Until it exists, only `admin` exists,
  so phase 1 would wait for it.
- Interlock expressions reuse the derived-tag language (#23). If that slips, phase 1 could ship
  with a fixed form, `TAG == value` joined with `&&`.
- The hash-chained audit could later be anchored outside the node, for example periodic digests
  to the MQTT broker. That is not needed for phase 1.

## 17. Question for the owner

> **Should opencap ever write values to PLCs?**
>
> - **A. No, permanently.** ADR-0008 becomes final and this RFC is closed as *Rejected*. The plan
>   rows in §1 stay *Out of scope*. Sites keep writing through their existing SCADA or HMI path.
> - **B. Yes, under this design, phase 1 only.** REST and UI writes with confirmation and the
>   two-person rule, on Modbus TCP and Logix, Python reference first. Each later phase (§15)
>   comes back to you. ADR-0008 is amended, not removed: everything in §14 stays forbidden.
> - **C. Yes, but narrower than phase 1.** The same design, with one interface (name it: REST or
>   OPC UA) and one driver (name it). Use this if one specific setpoint use case is what matters.
> - **D. Not now.** Keep this RFC as *Proposed* and revisit at milestone M5 or when a site asks.
>
> Whichever you choose, please also confirm or change the defaults most likely to matter on
> site: a 1-year audit retention (§11), the 8-hour maximum maintenance window (§8), and
> select-before-operate only for DNP3 and IEC 104 (§9).
