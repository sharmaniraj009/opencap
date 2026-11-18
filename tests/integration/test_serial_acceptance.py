"""Serial PLCs in simulation through the whole poller (ChannelWorker, cache, publisher, MQTT):
Allen-Bradley DF1 and Modbus RTU devices behind serial-over-TCP gateway simulators, configured
the way the KEPServer importer leaves them (`transport: unmapped`, `com_port: COMnn`) and mapped
in overrides.yaml `serial_ports`.

- 4 DF1 lines: CRC and BCC, two stations on one line (one channel), one line behind a real tty
  (pty bridge, mapped to the device path like a local serial port);
- 3 Modbus RTU lines (RTU frames on TCP): one mapped through `serial_ports`, one configured by
  hand (`transport: tcp`), one with two unit ids on one line;
- one DF1 device whose COM port is not mapped: skipped with a warning, never polled or published;
- every polled device publishes correct values, values follow the PLC, line noise (NAKs, a
  corrupted reply) costs nothing, powering off one gateway demotes only its devices and they
  resume after power-on; nothing but read commands goes on any line.

Real-hardware checks (a MicroLogix/SLC on a real serial line and on the site's router gateways,
a Modbus RTU slave on RS-485) are still to do: see docs/decisions.md D-038.
"""

from __future__ import annotations

import asyncio
import math

import pytest
import yaml

from opencap.config.models import Device, Tag
from opencap.core.runtime import Poller
from opencap.drivers.ab_pccc import coerce_value
from tests.helpers.broker import TestBroker
from tests.helpers.configdir import write_config
from tests.helpers.df1_sim import Df1Sim
from tests.helpers.modbus_sim import ModbusSim, make_sim_devices
from tests.helpers.netcut import stable_port
from tests.helpers.pccc_sim import tables_for
from tests.helpers.pty_bridge import PtyBridge, needs_pty, needs_pyserial
from tests.integration.test_ab_df1_sim import PATTERN_TAGS

# --engine=rust: skipped as "driver not ported" until the binary implements ab_df1, modbus_rtu
pytestmark = [pytest.mark.acceptance, pytest.mark.drivers("ab_df1", "modbus_rtu")]

INTERVAL_MS = 2000
SMALL_TAGS = [Tag(name="FLOW", address="F34:3", dtype="float32"), Tag(name="LEVEL", address="N54:17", dtype="uint16"),
              Tag(name="RUN", address="B3:17/14", dtype="bool"), Tag(name="DI_0", address="I:0/0", dtype="bool")]


async def _wait(pred, timeout: float, what: str, step: float = 0.1) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(step)


def _df1_device(site: str, name: str, com: str, station: int, tags, check: str) -> Device:
    # as `opencap import-kep` writes a KEPServer DF1 device (host = the COM port, port 0)
    return Device(id=f"{site}/{name}", channel=site, name=name, driver="ab_df1", host=com, port=0,
                  params={"station": station, "src_station": 0, "error_check": check, "transport": "unmapped",
                          "com_port": com, "baudrate": 19200, "bytesize": 8, "parity": "N", "stopbits": 1,
                          "ack_timeout_ms": 250},
                  scan_ms=1000, min_scan_ms=1000, timeout_ms=1000, retries=1, demote_after=3,
                  demote_for_ms=2000, tags=list(tags))


def _rtu_device(src: Device, site: str, name: str, **params) -> Device:
    return src.model_copy(update={
        "id": f"{site}/{name}", "channel": site, "name": name, "driver": "modbus_rtu",
        "scan_ms": 1000, "min_scan_ms": 1000, "timeout_ms": 1000, "retries": 1, "demote_after": 3,
        "demote_for_ms": 2000, "params": {**src.params, **params},
    })


def _matches(got, want) -> bool:
    if isinstance(want, float):
        return math.isclose(float(got), round(want, 4), rel_tol=1e-6, abs_tol=1e-4)
    return got == want


@needs_pyserial                     # one DF1 line is a local serial port (behind a pty)
@needs_pty
async def test_serial_acceptance(tmp_path):
    df1_sims: list[Df1Sim] = []
    rtu_sims: list[ModbusSim] = []
    bridge: PtyBridge | None = None
    try:
        # --- DF1 lines ---------------------------------------------------------------------------
        specs = [("DF1_SITE_01", "crc", 1), ("DF1_SITE_02", "bcc", 4), ("DF1_SITE_03", "crc", 1),
                 ("DF1_SITE_04", "crc", 2)]
        for i, (_site, check, station) in enumerate(specs):
            tags = PATTERN_TAGS if i != 3 else SMALL_TAGS
            df1_sims.append(await Df1Sim.start(tables_for(tags, seed=40 + i, system_files=True),
                                               station=station, error_check=check, port=stable_port()))
        df1_sims[2].add_station(5, tables_for(SMALL_TAGS, seed=60, system_files=True))   # multi-drop line
        bridge = await PtyBridge.start("127.0.0.1", df1_sims[3].port)                   # line behind a tty
        df1 = [
            _df1_device("DF1_SITE_01", "PLC", "COM101", 1, PATTERN_TAGS, "crc"),
            _df1_device("DF1_SITE_02", "PLC", "COM102", 4, PATTERN_TAGS, "bcc"),
            _df1_device("DF1_SITE_03", "PUMP_A", "COM103", 1, PATTERN_TAGS, "crc"),
            _df1_device("DF1_SITE_03", "PUMP_B", "COM103", 5, SMALL_TAGS, "crc"),
            _df1_device("DF1_SITE_04", "PLC", "COM104", 2, SMALL_TAGS, "crc"),
        ]
        unmapped = _df1_device("DF1_SITE_09", "PLC", "COM109", 1, SMALL_TAGS, "crc")

        # --- Modbus RTU lines ---------------------------------------------------------------------
        base = make_sim_devices(4, 30, seed=70)
        for k in range(3):
            rtu_sims.append(await ModbusSim.start(base[k], framer="rtu", port=stable_port()))
        unit7 = base[3].model_copy(update={"params": {**base[3].params, "unit_id": 7}})
        image7 = rtu_sims[2].add_unit(unit7)
        rtu = [
            _rtu_device(base[0], "RTU_SITE_01", "METER", transport="unmapped", com_port="COM201",
                        baudrate=9600).model_copy(update={"host": "COM201", "port": 0}),
            _rtu_device(base[1], "RTU_SITE_02", "METER", transport="tcp").model_copy(
                update={"host": "127.0.0.1", "port": rtu_sims[1].port}),
            _rtu_device(base[2], "RTU_SITE_03", "UNIT0", transport="tcp").model_copy(
                update={"host": "127.0.0.1", "port": rtu_sims[2].port}),
            _rtu_device(unit7, "RTU_SITE_03", "UNIT7", transport="tcp").model_copy(
                update={"host": "127.0.0.1", "port": rtu_sims[2].port}),
        ]
        rtu_images = [rtu_sims[0].image, rtu_sims[1].image, rtu_sims[2].image, image7]
        devices = df1 + [unmapped] + rtu

        async with asyncio.timeout(240), TestBroker() as broker, broker.collector("opencap/#") as col:
            await col.wait_ready()
            config_dir = write_config(tmp_path, devices, mqtt_port=broker.port)
            overrides = {
                "serial_ports": {
                    "COM101": f"127.0.0.1:{df1_sims[0].port}",
                    "COM102": f"tcp://127.0.0.1:{df1_sims[1].port}",
                    "com103": f"127.0.0.1:{df1_sims[2].port}",            # one mapping, two stations
                    "COM104": bridge.path,                                 # a local serial device
                    "COM201": f"127.0.0.1:{rtu_sims[0].port}",
                },
                "devices": {d.id: {"publish": {"interval_ms": INTERVAL_MS}} for d in devices},
            }
            (config_dir / "overrides.yaml").write_text(yaml.safe_dump(overrides), encoding="utf-8")
            poller = Poller(config_dir, serve_api=False)
            warnings = [w for w in poller.loaded.warnings if "COM109" in w]
            assert len(warnings) == 1 and "not mapped" in warnings[0] and "serial_ports" in warnings[0]
            await poller.start()
            try:
                await _scenario(poller, col, df1, df1_sims, unmapped, rtu, rtu_sims, rtu_images)
            finally:
                await poller.stop()
    finally:
        if bridge is not None:
            await bridge.stop()
        await asyncio.gather(*(s.stop() for s in df1_sims), *(s.stop() for s in rtu_sims), return_exceptions=True)


def _df1_station_sim(dev: Device, df1_sims: list[Df1Sim]) -> tuple[Df1Sim, int]:
    index = {"DF1_SITE_01": 0, "DF1_SITE_02": 1, "DF1_SITE_03": 2, "DF1_SITE_04": 3}[dev.channel]
    return df1_sims[index], dev.params["station"]


async def _scenario(poller, col, df1, df1_sims, unmapped, rtu, rtu_sims, rtu_images) -> None:
    polled = df1 + rtu
    topics = {d.id: poller.registry.plans[d.id].device.publish.topic for d in polled + [unmapped]}

    def check_df1(dev: Device) -> None:
        sim, station = _df1_station_sim(dev, df1_sims)
        msg = col.last(topics[dev.id]).json()
        assert msg.get("BadTags", []) == [] and msg["DataValid"] is True, (dev.id, msg.get("BadTags"))
        for tag in dev.tags:
            want = coerce_value(sim.value(tag.address, station=station), tag.dtype)
            assert _matches(msg["Data"][tag.name], want), (dev.id, tag.name, msg["Data"][tag.name], want)

    def check_rtu(dev: Device, image) -> None:
        msg = col.last(topics[dev.id]).json()
        assert msg.get("BadTags", []) == [] and msg["DataValid"] is True, (dev.id, msg.get("BadTags"))
        for name, want in image.expected().items():
            assert _matches(msg["Data"][name], want), (dev.id, name, msg["Data"][name], want)

    # 1. Every mapped device publishes correct values; the unmapped one is never polled.
    await _wait(lambda: all(len(col.topic(topics[d.id])) >= 2 for d in polled), 40, "2 messages per device")
    for dev in df1:
        check_df1(dev)
    for dev, image in zip(rtu, rtu_images):
        check_rtu(dev, image)
    assert col.topic(topics[unmapped.id]) == []
    detail = poller.device_detail(unmapped.id)
    assert detail["enabled"] is False and detail["state"] == "offline"
    assert df1_sims[2].connections == 1                       # two stations, one TCP connection
    assert poller.device_detail(df1[4].id)["host"].startswith("/dev/")

    # 2. Values follow the PLC.
    df1_sims[0].set("F34:3", 321.5)
    df1_sims[2].set("F34:3", -9.25, station=5)
    rtu_images[3].set_value(next(n for n, s in rtu_images[3].slots.items() if s.dtype == "float32"), 77.0)
    f32_7 = next(n for n, s in rtu_images[3].slots.items() if s.dtype == "float32")
    await _wait(lambda: col.last(topics[df1[0].id]).json()["Data"]["FLOW"] == 321.5
                and col.last(topics[df1[3].id]).json()["Data"]["FLOW"] == -9.25
                and col.last(topics[rtu[3].id]).json()["Data"][f32_7] == 77.0,
                3 * INTERVAL_MS / 1000 + 3, "changed values published")

    # 3. Line noise on a DF1 line: NAKed frames and a corrupted reply are retried by the link layer.
    df1_sims[1].reset_counters()
    df1_sims[1].nak_next = 3
    df1_sims[1].corrupt_replies = 2
    df1_sims[1].set("N54:17", 4321)
    await _wait(lambda: col.last(topics[df1[1].id]).json()["Data"]["LEVEL"] == 4321, 10, "value through noise")
    assert df1_sims[1].naks_sent == 3 and df1_sims[1].retransmits == 2
    assert poller.device_detail(df1[1].id)["state"] == "ok"
    check_df1(df1[1])

    # 4. Power off one DF1 gateway (two stations) and one RTU gateway: only their devices demote.
    victims = {df1[2].id, df1[3].id, rtu[1].id}
    await df1_sims[2].stop()
    await rtu_sims[1].stop()
    await _wait(lambda: all(poller.device_detail(v)["state"] == "demoted" for v in victims), 40, "victims demoted")
    others = {d.id: poller.device_detail(d.id)["state"] for d in polled if d.id not in victims}
    assert set(others.values()) == {"ok"}, others
    await df1_sims[2].restart()
    await rtu_sims[1].restart()
    await _wait(lambda: all(poller.device_detail(v)["state"] == "ok" for v in victims), 40, "victims resumed")
    n = len(col.topic(topics[df1[3].id]))
    await _wait(lambda: len(col.topic(topics[df1[3].id])) > n, 10, "station B publishes again")
    check_df1(df1[3])

    # 5. Read-only: every simulated PLC saw read commands only.
    assert sum(s.write_attempts for s in df1_sims) == 0
    assert sum(s.write_attempts for s in rtu_sims) == 0
    for sim in df1_sims:
        assert {(cmd, fnc) for _st, cmd, fnc in sim.commands} <= {(0x0F, 0xA2)}
    print(f"\n[serial] DF1 frames in: {[s.frames_in for s in df1_sims]}, "
          f"RTU requests: {[s.request_count for s in rtu_sims]}")
