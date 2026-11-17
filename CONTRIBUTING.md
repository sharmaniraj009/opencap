# Contributing to opencap

Thanks for helping build an open replacement for KEPServerEX. Bug reports, simulators,
drivers and documentation are all welcome.

Three rules come before everything else:

1. **opencap is read-only.** No code path writes to a PLC. `Driver.write()` raises and must
   never be implemented. Drivers refuse write services even if the library offers them.
   Write support needs an accepted design first (see [ROADMAP.md](ROADMAP.md)).
2. **No real site data, ever.** This is a public repository. Never commit or paste real
   PLC IP addresses, site or customer names, KEPServerEX project exports, or credentials.
   Use the documentation ranges `192.0.2.x`, `198.51.100.x` and `203.0.113.x`, and invented
   names (`Plant A/PLC1`). `tests/unit/test_no_private_data.py` enforces part of this.
   Real exports belong in the git-ignored `.private/` folder.
3. **Every deviation is written down.** When the code differs from what a reader would
   expect (a library quirk, a KEPServerEX semantic, a trade-off), add an entry to
   [docs/decisions.md](docs/decisions.md): what was expected, what we did, and why.

## Development setup

```bash
git clone https://github.com/sharmaniraj009/opencap && cd opencap
python3.12 -m venv .venv            # 3.12 and 3.13 are tested in CI
.venv/bin/pip install -e '.[dev,modbus,ab,north,serial]'
.venv/bin/pytest -m "not acceptance"          # must stay green (~30 s)
.venv/bin/pytest -m acceptance                # slower end-to-end runs
```

Pull requests run [CI](.github/workflows/ci.yml): the suite on Python 3.12 and 3.13, a
wheel build, and a container image build and smoke test. Keep a pull request to one topic,
and say in its description what you tested and how.

Test conventions:

- **Tests first** for every parser and planner (addresses, block merging, decoding):
  they are pure functions and cheap to test exhaustively.
- **Free ports only.** Bind port 0, or use `free_port()` / `stable_port()` from
  `tests/helpers`. Several test runs may share a machine.
- **Async tests** use plain `async def` (`asyncio_mode = "auto"`), and every task they
  start must be cancelled and awaited before the test ends.
- **No sleeps as synchronization.** Wait for a condition with a timeout (see
  `Collector.wait_for` in `tests/helpers/broker.py`).

## How to add a driver

A driver turns tag addresses into wire requests and replies into values. Everything else
(scheduling, retries, backoff, demotion, scaling, quality, publishing) belongs to the core
and stays out of the driver. The core never imports a protocol library.

### 1. The `Driver` interface (`src/opencap/drivers/base.py`)

```python
class MyBlock(Block):                 # one wire request; add the fields you need
    def describe(self) -> str: ...    # shown by `opencap check -v`, e.g. "DB10 0..63 (64 B)"
    def wire_requests(self) -> int: ...   # round trips this block costs (default 1)

class MyDriver(Driver):
    name = "my_proto"
    is_async = True                   # False for a blocking library: use self._run_sync(...)

    def plan(self, tags: list[Tag]) -> Plan: ...      # PURE: no I/O, no protocol import
    async def connect(self) -> None: ...              # one attempt; ConnectionLost on failure
    async def read(self, block: MyBlock) -> dict[str, TagValue]: ...
    async def close(self) -> None: ...                # idempotent, never raises
    async def identity(self) -> dict[str, Any]: ...   # vendor/model/firmware, or {}
    @property
    def connected(self) -> bool: ...
    def connect_timeout_s(self) -> float | None: ...  # only if connect needs several round trips
```

The contract:

- **`plan()` is pure.** It parses every address and merges tags into blocks within the
  protocol's limits. A tag it cannot read goes to `Plan.invalid` with a reason: it becomes
  `bad_config` and is never sent. Import the protocol library lazily, inside `connect()`, so
  `opencap check` works without it installed (decision D-002).
- **`read()` makes exactly one attempt** within `device.timeout_ms`. It returns a `TagValue`
  for every name in `block.tags`: `good` with the raw value (the core applies scaling), or
  `bad_decode` for a per-tag `DecodeError`. No retries in the driver.
- **Errors tell the worker what to do:**

  | Raise | When | The worker |
  |---|---|---|
  | `CommError` | timeout, transient device error | retries up to `device.retries`, then `bad_comm` |
  | `ConnectionLost` | refused, reset, peer closed, not connected | closes, reconnects with backoff |
  | `BlockConfigError` | the device permanently rejects this block (bad address) | marks its tags `bad_config`, skips it until reload |

- **Blocking libraries** run on the channel's single-thread executor via `self._run_sync()`.
  Never block the event loop.

### 2. Register it

- Add the name to `DriverName` in `src/opencap/config/models.py` and a lazy entry to
  `DRIVERS` in `src/opencap/drivers/__init__.py`.
- Add an optional dependency (extra) for its library in `pyproject.toml`.
- Document its `params` keys, defaults and KEPServerEX sources in
  [docs/driver_params.md](docs/driver_params.md).
- If KEPServerEX has an equivalent driver, map it in `src/opencap/config/kep_import.py`,
  and add a device of that kind to `tests/fixtures/project_sample.json` (invented names,
  `192.0.2.x` addresses).

### 3. A simulator that the real library works against

Protocol tests run against a simulator in `tests/helpers/<proto>_sim.py`. The rule that
makes these tests worth having: **the real client library talks to the simulator
unmodified.** No monkeypatching the library, no fake transport. If the library cannot talk
to your simulator, the simulator is wrong.

- Implement the requests the library actually sends: capture them from the library, and
  check them against the protocol specification.
- Encode values independently of the driver's decoder (byte, word and bit order), so a
  decoding bug cannot cancel itself out. Mutation-check it: break the driver's byte order
  and see a test fail.
- Serve on an ephemeral port, and provide fault switches: no answer (`mute`), protocol error
  codes, short replies, peer close.
- **Refuse and count write requests.** Tests assert the count stays zero.
- Record anything the simulator does not cover in `docs/decisions.md`.

The existing simulators are examples: `modbus_sim.py` (pymodbus), `cip_sim.py` (pycomm3
Logix) and `pccc_sim.py` (pycomm3 SLC).

### 4. Tests

- `tests/unit/test_<proto>_address.py`, `test_<proto>_plan.py`: written **first**, against
  the parser and planner. Include the protocol limits (maximum request size, gaps,
  alignment) and every rejected form.
- `tests/unit/test_<proto>_io.py`: decoding and error mapping, with crafted replies where
  the simulator cannot produce a case.
- `tests/integration/test_<proto>_sim.py`: real driver against the simulator through the
  `ChannelWorker`: round trip for every data type, timeout → `bad_comm`, peer close →
  reconnect, rejected block → `bad_config`, no writes sent.

### 5. Document

Add the driver to the support matrix in the [README](README.md), update its row in
[ROADMAP.md](ROADMAP.md), and add `docs/decisions.md` entries for library quirks and
anything you could not verify without real hardware. Mark the latter clearly ("check on a
real PLC").

## Shared files

Several pull requests often run in parallel. Keep edits to shared files
(`models.py`, `kep_import.py`, `runtime.py`, `pyproject.toml`, `docs/decisions.md`)
minimal and additive. Append new decisions at the end of `docs/decisions.md`; if two pull
requests take the same `D-0xx` number, the one merged second renumbers.

## Reporting bugs

Open an issue with the opencap version, Python version, OS, the driver, and a log excerpt
(`log_level: DEBUG` if relevant). **Redact first:** replace real IP addresses with
`192.0.2.x`, and real channel, device and tag names with invented ones. Never attach a
KEPServerEX project export. Security problems go through [SECURITY.md](SECURITY.md), not
public issues.

By contributing you agree that your contribution is licensed under the
[Apache License 2.0](LICENSE).
