# BACnet/IP spike prototype (#15)

This is throwaway code that supports [`docs/spikes/bacnet.md`](../../docs/spikes/bacnet.md) and
[ADR-0016](../../docs/adr/0016-bacnet-native-rust-client.md). It is not product code: nothing
in `src/` or `engine/` imports it, and CI does not collect it (`testpaths = ["tests"]`).

- `proto_client.py`: a minimal BACnet/IP client.
  - It handles BVLC and NPDU, BACnet tags, Who-Is/I-Am, ReadProperty, ReadPropertyMultiple,
    segmented ACKs, COV, and foreign-device registration.
  - It also contains the KEPServerEX address parser, `browse()` and the status → quality
    mapping.
  - It can send only RP, RPM and SubscribeCOV.
- `sim_device.py`: a bacpypes3 device with every subset object type and a write counter. It can
  also run as a BBMD.
- `test_proto_client.py`: 11 tests covering the prototype against the device, and bacpypes3's own
  client against the same device.

```bash
python3.12 -m venv /tmp/bacnet-spike && /tmp/bacnet-spike/bin/pip install bacpypes3==0.1.0 pytest pytest-asyncio
/tmp/bacnet-spike/bin/pytest spikes/bacnet -q
```
