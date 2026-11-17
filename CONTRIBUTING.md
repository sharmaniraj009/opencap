# Contributing to opencap

Thanks for helping build an open replacement for KEPServerEX. Bug reports, simulators,
drivers and documentation are all welcome.

opencap is built by several engineer sessions working in parallel and coordinated only
through GitHub (issues, labels, PR reviews, files on `main`), with a technical lead who
reviews and merges. Before your first PR, read this file, [`docs/architecture/`](docs/architecture/)
and [`docs/adr/`](docs/adr/).

These rules come before everything else:

1. **opencap is read-only.** No code path writes to a PLC. `Driver.write()` raises and must
   never be implemented. Drivers refuse write services even if the library offers them.
   Write support needs an accepted design first: RFC 0002 plus the owner's approval
   ([ADR-0008](docs/adr/0008-read-only-by-default.md)).
2. **No real site data, ever.** This is a public repository. Never commit or paste real
   PLC IP addresses, site or customer names, KEPServerEX project exports, or credentials,
   and never put secrets in issues, PR descriptions or comments.
   Use the documentation ranges `192.0.2.x`, `198.51.100.x` and `203.0.113.x`, and invented
   names (`Plant A/PLC1`). `tests/unit/test_no_private_data.py` enforces part of this.
   Real exports belong in the git-ignored `.private/` folder.
3. **Nothing connects to real plant equipment** from CI or a cloud session. Protocols are
   proven against simulators and conformance vectors
   ([ADR-0014](docs/adr/0014-conformance-by-simulators-and-vectors.md)).
4. **Licences follow [ADR-0007](docs/adr/0007-oss-licence-policy.md).** A PR that adds a
   dependency names its licence and links its `LICENSE` file at the pinned version.
5. **Every deviation is written down.** When the code differs from what a reader would
   expect (a library quirk, a KEPServerEX semantic, a trade-off), add an entry to
   [docs/decisions.md](docs/decisions.md): what was expected, what we did, and why.
   Architecture decisions go into an ADR instead ([ADR-0001](docs/adr/0001-record-architecture-decisions.md)).
6. **No force-push to `main`. Engineers never merge their own PRs.**

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

Once `engine/` (the Rust engine) exists, also run in `engine/`:

```bash
cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test
```


## Engineering protocol

### The board

- The issue **"opencap program board"** ([#1](https://github.com/sharmaniraj009/opencap/issues/1))
  and its mirror [`docs/BOARD.md`](docs/BOARD.md) show status, who works on what, the next
  priorities, and decisions waiting for the owner.
- Labels:
  - status: `ready` (can start now), `in-progress` (claimed), `blocked` (waiting on a listed
    dependency), `needs-review` (PR open, waiting for the lead)
  - area: `driver`, `north`, `core`, `engine-rust`, `ui`, `desktop`, `scada`, `docs`, `infra`
  - priority: `P1` (next), `P2` (soon), `P3` (later)
- Every issue states its goal, acceptance criteria, test plan, the files it may touch,
  what not to touch, and its dependencies. If one of these is missing, ask on the issue
  before you start.

### Picking work

1. When your current PR is open and reviewed (or merged), pick the **highest-priority
   `ready` issue in your area**: `P1` before `P2` before `P3`, then the lowest issue number.
   If your area has none, take any `ready` issue.
2. **Claim it:** comment `claimed by <session name>` on the issue, remove `ready`, add
   `in-progress`. Hold one claim at a time, plus at most one PR in review.
3. A `blocked` issue becomes `ready` once every dependency it lists has merged. If that is
   clearly the case, you may flip it yourself; say so in your claim comment.
4. If the issue turns out to be bigger than one PR, say so on the issue and propose a split.
   The lead files the follow-ups.
5. If you stop working on a claimed issue, comment why, remove `in-progress` and re-add `ready`.

### Branches

- Work on a branch, never on `main`. If your session is bound to a single working branch,
  use it and keep **one open PR per branch**. After that PR merges, restart the branch from
  the latest `main`. You may prepare your next issue locally while your PR is in review;
  push it only after the previous PR has merged.
- Rebase on `main` before you open the PR, and again whenever the lead asks. Resolve
  conflicts yourself. In the shared files below, keep both sides.
- Keep PRs small: one issue, ideally under ~800 changed lines excluding tests, fixtures and
  generated files. Split otherwise.

### Opening a PR

- Title: `<area>: <what> (#<issue>)`. The body starts with `Closes #<issue>` and follows the
  PR template: what changed, how it was tested, the licence of any new dependency, and
  anything the reviewer must look at.
- Add the `needs-review` label to the PR and to the issue.
- CI must be green. Run the fast suite locally; also run `pytest -m acceptance` if you
  touched the core, a driver, north or the config loader (CI runs acceptance nightly).
- Tests must exercise behaviour, not just lines: a driver against its simulator, a planner
  against vectors, a fault path by switching on the simulator's fault.
- Contract changes go into `docs/architecture/` or an ADR in the same PR.

### Review and merge

- The lead reviews every open `needs-review` PR on each pass over the board. The review
  checks correctness against the architecture and the issue, that the tests really exercise
  behaviour, licence compliance and the privacy rules.
- Review comments are concrete. Answer each one: fix it, or explain why not. Then push and
  comment `ready for re-review`.
- The lead squash-merges when CI is green and the review passes, in the order that causes
  the fewest conflicts, with the message `<area>: <what> (#PR)`. Engineers never merge.
- After a merge, the lead updates the board and flips the issues that waited on it to `ready`.

### Decisions only the owner makes

Licence, naming, write support for real plants, paid services, and shipping copyleft
plugins. Raise them on the board issue under "Decisions for the owner", then carry on with
everything else.

## How to add a driver

**Which language.** Drivers already in flight in Python finish in Python. Once the Rust
driver trait is on `main` (the `rust-engine` PR), new drivers are written **Rust-first** in
`engine/drivers/<name>`, and existing Python drivers are ported with their simulator suites
as the acceptance test ([ADR-0002](docs/adr/0002-rust-engine-python-reference.md),
[ADR-0004](docs/adr/0004-driver-contract-and-plugin-model.md)). The simulator stays in the
Python test harness either way. The contract below is the same in both languages
([`docs/architecture/03-deep-dive.md`](docs/architecture/03-deep-dive.md) §3.3).

A new driver is accepted when it has:
- a simulator that an independent client library talks to unmodified;
- conformance vectors for `plan()` and decoding in `tests/conformance/<driver>/`;
- read-only guards, with a test that write services are refused or can never be built;
- a `docs/driver_params.md` section;
- the KEPServerEX import mapping, where KEPServerEX has the driver.


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

## Shared files (hotspots)

Several PRs run in parallel. Keep edits to these files minimal and additive (append, don't
reorder), expect trivial conflicts, and keep every entry when you resolve them:

| File | Typical change |
|---|---|
| `src/opencap/drivers/__init__.py` | one line in `DRIVERS` |
| `src/opencap/config/models.py` | the `DriverName` literal, new optional fields |
| `src/opencap/config/kep_import.py` | one driver mapping |
| `src/opencap/__main__.py` | one new subcommand function |
| `src/opencap/core/runtime.py` | wiring for one new component |
| `pyproject.toml` | one extra or dependency |
| `docs/decisions.md` | new `D-NNN` entries at the end; if two PRs take the same number, the one merged second renumbers |
| `docs/driver_params.md` | one section per driver |
| `engine/Cargo.toml` | one workspace member |

## Reporting bugs

Open an issue with the opencap version, Python version, OS, the driver, and a log excerpt
(`log_level: DEBUG` if relevant). **Redact first:** replace real IP addresses with
`192.0.2.x`, and real channel, device and tag names with invented ones. Never attach a
KEPServerEX project export. Security problems go through [SECURITY.md](SECURITY.md), not
public issues.

By contributing you agree that your contribution is licensed under the
[Apache License 2.0](LICENSE).
