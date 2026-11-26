# DNP3 spike prototype (#14)

This is throwaway code that supports [`docs/spikes/dnp3.md`](../../docs/spikes/dnp3.md) and
[ADR-0015](../../docs/adr/0015-dnp3-native-rust-subset.md). It is not product code:

- nothing in `src/` or `engine/` imports it;
- CI does not collect it (`testpaths = ["tests"]`).

- `proto_master.py`: a minimal DNP3 master over TCP. It covers link frames with CRC-16/DNP,
  transport segments, application READ and CONFIRM, the read-subset objects, and the flag →
  quality mapping. It can frame only READ and CONFIRM.
- `test_proto_master.py`: runs the master against two independent outstations. Each peer's tests
  skip when that peer is not installed:
  - opendnp3, via yadnp3 3.2.1.5 (Apache-2.0);
  - dnp3py 0.6.0 (MIT).

```bash
python3.12 -m venv /tmp/dnp3-spike && /tmp/dnp3-spike/bin/pip install yadnp3==3.2.1.5 dnp3py==0.6.0 pytest pytest-asyncio
/tmp/dnp3-spike/bin/pytest spikes/dnp3 -q
```
