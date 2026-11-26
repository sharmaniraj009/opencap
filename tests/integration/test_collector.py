"""The test Collector never loses a message it has acknowledged (#220, D-083).

A minimal MQTT server in the test streams QoS 1 PUBLISH packets to a Collector and records every
PUBACK the collector sends back. The test closes (disconnects) the collector while messages are
still arriving. Every packet the collector acknowledged must be among the messages it recorded:
a broker drops an acknowledged message from its session, so a subscriber that loses one after
the PUBACK loses it for good. aiomqtt's message iterator, which Collector used before, could:
it took a message off its queue in a separate task and discarded it on a cancel or disconnect.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tests.helpers.broker import Collector

N_MESSAGES = 400
TOPIC = "opencap/test/collector"


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    head = (await reader.readexactly(1))[0]
    length, shift = 0, 0
    while True:
        byte = (await reader.readexactly(1))[0]
        length |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
    return head, await reader.readexactly(length)


def _publish(packet_id: int, payload: bytes) -> bytes:
    topic = TOPIC.encode()
    body = len(topic).to_bytes(2, "big") + topic + packet_id.to_bytes(2, "big") + payload
    assert len(body) < 128
    return bytes([0x32, len(body)]) + body                     # PUBLISH, QoS 1


class StubBroker:
    """CONNACK, SUBACK, then a stream of QoS 1 PUBLISHes; records the PUBACKed packet ids."""

    def __init__(self) -> None:
        self.acked: set[int] = set()
        self.first_ack = asyncio.Event()
        self.server: asyncio.base_events.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head, _ = await _read_packet(reader)
            assert head >> 4 == 1                                   # CONNECT
            writer.write(b"\x20\x02\x00\x00")                       # CONNACK, accepted
            head, body = await _read_packet(reader)
            assert head >> 4 == 8                                   # SUBSCRIBE
            writer.write(b"\x90\x03" + body[:2] + b"\x01")          # SUBACK, granted QoS 1
            await writer.drain()
            sender = asyncio.create_task(self._send_all(writer))
            try:
                while True:
                    head, body = await _read_packet(reader)
                    if head >> 4 == 4:                              # PUBACK
                        self.acked.add(int.from_bytes(body[:2], "big"))
                        self.first_ack.set()
                    elif head >> 4 == 12:                           # PINGREQ
                        writer.write(b"\xd0\x00")
                    elif head >> 4 == 14:                           # DISCONNECT
                        break
            finally:
                sender.cancel()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def _send_all(self, writer: asyncio.StreamWriter) -> None:
        for i in range(1, N_MESSAGES + 1):
            writer.write(_publish(i, str(i).encode()))
            if i % 20 == 0:
                await writer.drain()
                await asyncio.sleep(0)


async def test_every_acknowledged_message_is_recorded_when_the_collector_disconnects_mid_stream():
    lost_runs = []
    for run in range(20):
        stub = StubBroker()
        await stub.start()
        fake = SimpleNamespace(host="127.0.0.1", port=stub.port, generation=1, running=True, _collectors=set())
        try:
            collector = Collector(fake, [TOPIC], client_id=f"collector-test-{run}")  # type: ignore[arg-type]
            await collector.__aenter__()
            await asyncio.wait_for(stub.first_ack.wait(), 10)
            await asyncio.sleep(0.001 * (run % 5))                  # close at different points of the stream
            await collector.close()
            await asyncio.sleep(0.05)                               # let the stub read the last PUBACKs
            recorded = {int(p) for p in collector.payloads(TOPIC)}
            lost = stub.acked - recorded
            assert stub.acked, "the collector acknowledged nothing"
            if lost:
                lost_runs.append((run, sorted(lost)[:10]))
        finally:
            await stub.stop()
    assert not lost_runs, f"acknowledged but not recorded: {lost_runs}"
