"""The test broker stops even when a connection never finished its MQTT handshake (#223).

amqtt closes only the connections it holds a session for. A connection that sent no CONNECT, or
something else first, stayed open, and asyncio's `Server.wait_closed()` (Python 3.12.1+) waits
for every accepted connection. So `TestBroker.stop()` hung for good, even after the client went
away: the server side stays half-open. Under full-suite load an MQTT client caught mid-handshake
when the engine stopped did this to `test_fifty_simulator_devices_through_the_poller`, which then
failed at its 90 s timeout.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.helpers.broker import TestBroker

NOT_MQTT = b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"


@pytest.mark.parametrize("first_bytes", [NOT_MQTT, None], ids=["not-mqtt", "silent"])
@pytest.mark.parametrize("client_closes", [False, True], ids=["client-open", "client-closed"])
async def test_stop_returns_with_a_connection_that_never_sent_connect(first_bytes, client_closes):
    broker = TestBroker()
    await broker.start()
    _reader, writer = await asyncio.open_connection(broker.host, broker.port)
    try:
        if first_bytes is not None:
            writer.write(first_bytes)
            await writer.drain()
        await asyncio.sleep(0.2)                      # amqtt has read it (or is waiting for a CONNECT)
        if client_closes:
            writer.close()
        async with asyncio.timeout(5):
            await broker.stop()
    finally:
        writer.close()


async def test_a_restart_after_such_a_connection_still_serves_clients():
    async with TestBroker() as broker:
        _reader, writer = await asyncio.open_connection(broker.host, broker.port)
        writer.write(NOT_MQTT)
        await writer.drain()
        async with asyncio.timeout(10):
            await broker.restart()
        async with broker.collector("opencap/#") as col:
            await col.wait_ready()
            assert col.connects == 1
        writer.close()
