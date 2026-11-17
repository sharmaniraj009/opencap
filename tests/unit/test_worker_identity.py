"""ChannelWorker.identity / Poller.device_identity: over the existing connection only."""

from __future__ import annotations

from opencap.core.cache import ValueCache
from opencap.core.channel_worker import ChannelWorker
from tests.helpers.fake_driver import FakeFactory, channel, device, fake_plans, tags, uniq


async def test_identity_needs_a_connection_and_never_connects():
    name = uniq()
    ch = channel(name, [device(name, "PLC", tags("A", 2))])
    dev_id = f"{name}/PLC"
    factory = FakeFactory()
    worker = ChannelWorker(ch, fake_plans(ch), ValueCache(), driver_factory=factory, start_jitter=0.0)
    try:
        assert await worker.identity(dev_id) is None                # no driver yet
        assert factory.script(dev_id).connect_attempts == []        # and it did not connect
        await worker.read_once(dev_id)                              # connects
        drv = factory.drivers[dev_id]

        async def ident():
            return {"vendor": "Example"}

        drv.identity = ident
        assert await worker.identity(dev_id) == {"vendor": "Example"}
        await drv.close()
        assert await worker.identity(dev_id) is None
    finally:
        await worker.stop()
