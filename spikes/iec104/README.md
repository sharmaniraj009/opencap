# IEC 60870-5-104 spike prototype (#16)

This is throwaway code supporting [`docs/spikes/iec104.md`](../../docs/spikes/iec104.md) and
[ADR-0017](../../docs/adr/0017-iec104-native-rust-subset.md). It is not product code: nothing
in `src/` or `engine/` imports it, and CI does not collect it (`testpaths = ["tests"]`).

- `proto_client.py`: a minimal IEC 104 client. It covers APCI with k/w accounting,
  STARTDT/STOPDT/TESTFR, general and counter interrogation, the monitor types, CP56Time2a and the
  quality mapping. It can frame only C_IC_NA_1 and C_CI_NA_1 (read, no freeze).
- `test_proto_client.py`: 9 tests. They run against lib60870-C through c104 and, when the binary
  is available, against `igs104-server` (igs-iec104, Apache-2.0).

c104 is GPL-3.0. Install it only into a throwaway venv, as here; it is never vendored or added
to the project's extras (ADR-0007, test-only tools).

```bash
python3.12 -m venv /tmp/iec104-spike && /tmp/iec104-spike/bin/pip install c104==2.2.1 pytest pytest-asyncio
cargo install --root /tmp/igs igs-iec104 --version 0.1.0 --no-default-features --features rustls   # optional
IGS104_SERVER=/tmp/igs/bin/igs104-server /tmp/iec104-spike/bin/pytest spikes/iec104 -q
```
