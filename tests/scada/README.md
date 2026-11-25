# L1 SCADA compatibility suite

Level L1 of [`docs/architecture/scada-compatibility.md`](../../docs/architecture/scada-compatibility.md):
the OPC UA server is tested in CI with the OPC UA client stacks SCADA products use.
Ignition's OPC UA client is Eclipse Milo, so the Milo client is the closest CI stand-in for
Ignition ([ADR-0009](../../docs/adr/0009-scada-connectivity-strategy.md)).

The suite starts the engine as a child process (`opencap run -c <config dir>`; nothing but
the config directory is shared) with simulated Modbus devices, and runs one scenario client
per OPC UA stack against it. Each client reports what it saw as JSON; `test_l1.py` judges the
three reports by the same rules.

| Client | Version | Licence | Built from |
|---|---|---|---|
| Eclipse Milo (`sdk-client`) | 0.6.16 (the 0.6 line is the client stack of Ignition 8.1) | EPL-2.0 | `milo-client/` (Maven, Java 17+) |
| open62541 | v1.4.22, commit `a9d7e2f2`, OpenSSL encryption | MPL-2.0 | `open62541-client/build.sh` (cmake, C99) |
| asyncua | 2.1 (the `opcua` extra) | LGPL-3.0-or-later | `asyncua_client.py` |

All three are test-only tools, never shipped ([oss-reuse.md](../../docs/architecture/oss-reuse.md)).

## Run it

```bash
mvn -B -q -f tests/scada/milo-client/pom.xml package          # -> milo-client/target/scada-l1-milo-client.jar
tests/scada/open62541-client/build.sh                          # -> open62541-client/build/scada_l1_open62541
pytest -m scada tests/scada                                    # clients that are not built are skipped
```

- The suite is opt-in: without `-m scada` (or `OPENCAP_SCADA_L1=1`) it is reported as skipped,
  so `pytest -m "not acceptance"` stays fast.
- `OPENCAP_SCADA_REQUIRE=milo,open62541,asyncua` turns a missing client into a failure (CI).
- `OPENCAP_SCADA_SUMMARY=<file.md>` writes the matrix below as markdown; `--junitxml=<file>`
  writes JUnit XML. The write and reconnect results are recorded as `<property name="result">`.
- `--engine rust` (or `OPENCAP_ENGINE=rust`, `OPENCAP_ENGINE_BIN`) runs the same suite against
  the Rust engine binary once it serves OPC UA (#50).
- `OPENCAP_OPEN62541_CLIENT=<path>` points at an open62541 client binary built elsewhere.

CI: [`.github/workflows/scada-l1.yml`](../../.github/workflows/scada-l1.yml) on pull requests
touching `src/opencap/north/`, `engine/crates/opencap-north/` or `tests/scada/`. The job summary
shows the matrix; the artefact `scada-l1-results` holds the JUnit XML, the matrix and the
engine log. A full run takes about 1.5 minutes after the clients are built.

## The plant and the server under test

Invented names only; every simulator listens on 127.0.0.1.

- Channel `Plant A`, device `PLC1` (scan 250 ms): static values of every data type
  (`Static.Int16` ... `Static.Text`, `Static.Int16Array`, `Static.FloatArray`, the nested group
  `Line1.Motor.Speed`), a scaled tag `Level` (raw 0..1000 -> 0..100 %, units `%`), a ramp
  `Ramp` (+1 every 250 ms), and a changing string and array.
- `PLC2` ... `PLC5` (scan 500 ms): 250 `uint16` tags each, `Values.V000` ... `Values.V249`, all
  changing every 250 ms: the 1,000 items of the subscription scenario.
- Server: every security policy (`None` and Sign / SignAndEncrypt for Basic256Sha256,
  Aes128_Sha256_RsaOaep, Aes256_Sha256_RsaPss), anonymous and one username/password user,
  the harness client certificate in the trusted folder, namespace `urn:opencap:scada-l1`.
- Data scenarios connect with Basic256Sha256 / SignAndEncrypt and the username token (the
  common SCADA set-up).

## Matrix

Every cell is one test. `pass` = green in CI; `pending` = a strict `xfail` for a known server
gap: the test fails (XPASS) as soon as the gap is closed, so the PR that closes it removes the
marker.

| Scenario | Milo | open62541 | asyncua |
|---|---|---|---|
| GetEndpoints: all policies, anonymous + username tokens, server certificate | pass | pass | pass |
| Connect with every policy x {anonymous, username}; wrong password refused | pass | pass | pass |
| Browse the full tree (folders per channel/device/group, `_System`), with BrowseNext | pass | pass | pass |
| TranslateBrowsePathsToNodeIds (and `BadNoMatch` for a missing path) | pass | pass | pass |
| Read Value, DataType, AccessLevel/UserAccessLevel = CurrentRead, SourceTimestamp | pass | pass | pass |
| Arrays (ValueRank 1) and strings | pass | pass | pass |
| ArrayDimensions of array variables | pending (#28) | pending (#28) | pending (#28) |
| EURange and EngineeringUnits from the tag's scaling | pending (#28) | pending (#28) | pending (#28) |
| Subscribe 1,000 items, every item sees a simulator-driven change | pass | pass | pass |
| Data-change filter, absolute deadband | pending (#28) | pending (#28) | pending (#28) |
| Write attempt rejected, value unchanged | pass | pass | pass |
| Reconnect after a server restart, data flows again | pass | pass | pass |

Notes from the runs:

- **Writes** are refused with a ServiceFault `BadUserAccessDenied` (0x801F0000), seen the same
  way by all three clients; #28 makes that a per-node `BadNotWritable`. The test accepts
  `BadNotWritable`, `BadUserAccessDenied` and `BadWriteNotSupported`.
- **Reconnect**: a restarted server has no sessions or subscriptions, so every client ends up
  re-creating its subscription. Milo reconnects and re-activates by itself and reports
  `onSubscriptionTransferFailed` (the client re-creates there, as Ignition does); open62541 1.4
  re-opens the channel and creates a new session by itself while `UA_Client_run_iterate` runs,
  but gives up while the port refuses connections, so the client reconnects then; asyncua has
  no session recovery, so the client runs the usual reconnect loop. Recovery took 3.5-7.5 s
  after a graceful restart of the engine.
- **Deadband**: the server (asyncua's server side) compares each sample with the previous
  sample instead of the last reported value (OPC UA Part 4, 7.22.2 DataChangeFilter), so a
  value that ramps by less than the deadband per sample is never reported. All three clients
  see only the initial value. Found by this suite; the fix belongs to the server hardening (#28).
- A deliberate bug shows what the suite catches: serving `int16` tags as `Int32` fails
  "Read Value, DataType" for every client (`assert 'i=6' == 'i=4'`).

## Client protocol

Each client is a program `<client> --scenario <name> [--option value]...`. It prints JSON lines
on stdout; lines `{"event": "<name>"}` are progress events, and the last line is
`{"result": {...}}` (or `{"result": {"error": "..."}}` with a non-zero exit). Anything else on
stdout is ignored; logs go to stderr. `asyncua_client.py` is the reference implementation.

Common options: `--endpoint`, `--ns` (namespace index of the tag namespace), `--app-uri`
(the client certificate's application URI), `--cert` (DER), `--key` (PEM) / `--key-der`
(PKCS#8 DER), `--user`, `--password`, `--policy` (`None`, `Basic256Sha256`,
`Aes128_Sha256_RsaOaep`, `Aes256_Sha256_RsaPss`; default `Basic256Sha256`), `--mode`
(`None`, `Sign`, `SignAndEncrypt`; default `SignAndEncrypt`).

| Scenario | Options | Result |
|---|---|---|
| `endpoints` | | `endpoints: [{url, policy (URI), mode (1/2/3), tokens: [UserTokenType...], server_cert}]` |
| `connect` | | `attempts: [{policy, mode, token: anonymous/username/bad_password, ok, state, error}]` for every endpoint |
| `browse` | `--max-refs` | `nodes: [{id, name ("ns:Name"), class (NodeClass), parent}]` of the tag namespace, `browse_calls` |
| `translate` | `--paths` (file: `Channel/Device/Group/Tag` per line) | `results: [{path, status, targets: [NodeId]}]` |
| `read` | `--nodes` (file: one NodeId per line) | `nodes: [{id, status, value, source_ts, datatype, valueRank, arrayDims, accessLevel, userAccessLevel, description, euRange: {low, high}, engineeringUnits: {display, unitId, namespace}}]` |
| `subscribe` | `--nodes`, `--timeout` | `{requested, created_ok, created_bad, initial, changed, notifications, elapsed_ms}`; `changed` = items that reported two different values |
| `deadband` | `--node`, `--deadband`, `--duration` | `{create_status, unfiltered: [values], filtered: [values]}` (two monitored items on one node) |
| `write` | `--node` | `{status, service_fault, before, after}` |
| `reconnect` | `--nodes`, `--timeout` | events `ready` (the harness restarts the engine then), `disconnected`; result `{disconnect_seen, recovered, recovery_ms, mechanism}` |

Status codes are `"0xXXXXXXXX"` strings; NodeIds are the stack's own text form
(`ns=0;i=85` and `i=85` are treated alike).

## Adding a scenario or a client

- A scenario: implement it in all three clients with the same result shape, add a test in
  `test_l1.py` with `@pytest.mark.l1("<matrix row>")`, and add the row above.
- A client: a program that speaks the protocol, an entry in `harness.CLIENTS`,
  `client_command()` and `build_hint()`, a build step in the workflow, and its licence in
  `docs/architecture/oss-reuse.md`.
