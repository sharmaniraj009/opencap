"""MqttLink against a real in-process broker (amqtt): ordering, outages, restarts, LWT, TLS."""

from __future__ import annotations

import asyncio
import json
import shutil
import ssl
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from opencap.metrics import REGISTRY
from opencap.north.mqtt import MqttLink, lwt_topic
from opencap.north.saf import StoreAndForward
from tests.helpers.broker import TestBroker, mqtt_broker  # noqa: F401

SRC = Path(__file__).resolve().parents[2] / "src"
TOPIC = "opencap/site_a/plc_1/data"


def published_total() -> float:
    return REGISTRY.get_sample_value("mqtt_published_total") or 0.0


def seq_payload(i: int) -> bytes:
    return json.dumps({"seq": i}).encode()


def seqs(msgs) -> list[int]:
    return [m.json()["seq"] for m in msgs]


async def wait_until(pred, timeout: float = 10.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} not met within {timeout}s")
        await asyncio.sleep(0.01)


class LinkRunner:
    """Runs a link in a task; stops it and closes the outbox at the end."""

    def __init__(self, link: MqttLink, saf: StoreAndForward):
        self.link, self.saf = link, saf
        self.task: asyncio.Task | None = None

    async def __aenter__(self):
        self.task = asyncio.create_task(self.link.run())
        return self.link

    async def __aexit__(self, *exc):
        await self.link.stop()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.saf.close()


def make_link(broker: TestBroker, tmp_path: Path, instance: str = "test-poller", *, path: Path | None = None,
              **kw) -> LinkRunner:
    saf = StoreAndForward(path or tmp_path / "saf.sqlite", max_bytes=100_000_000)
    return LinkRunner(MqttLink(broker.mqtt_cfg(), instance, saf, **kw), saf)


async def test_delivers_many_messages_in_order(mqtt_broker, tmp_path):
    n = 500                                                    # several in-flight windows
    before = published_total()
    async with mqtt_broker.collector("opencap/#") as col, make_link(mqtt_broker, tmp_path) as link:
        online = await col.wait_payload(lwt_topic("test-poller"), b"online")
        assert online.qos == 1
        t0 = time.perf_counter()
        for i in range(n):
            await link.publish(TOPIC, seq_payload(i))
        enqueue_s = time.perf_counter() - t0
        msgs = await col.wait_count(TOPIC, n, timeout=20)
        assert seqs(msgs) == list(range(n))                     # in order, no duplicates on a stable link
        assert all(m.qos == 1 and not m.retain for m in msgs)
        await wait_until(lambda: link._saf.count() == 0, what="outbox drained")
        assert published_total() - before == n
        assert link.connected and link.connects == 1
        assert enqueue_s < 5                                    # publish() only writes SQLite
    assert col.topic(lwt_topic("test-poller"))[-1].payload == b"offline"   # stop() says goodbye


async def test_retained_flag_is_kept(mqtt_broker, tmp_path):
    async with make_link(mqtt_broker, tmp_path) as link:
        await link.publish("opencap/site_a/plc_1/status", b'{"state":"ok"}', retain=True)
        await wait_until(lambda: link._saf.count() == 0, what="outbox drained")
        async with mqtt_broker.collector("opencap/site_a/#") as late:   # subscribes after the publish
            m = await late.wait_payload("opencap/site_a/plc_1/status", b'{"state":"ok"}')
            assert m.retain is True


async def test_publish_rejects_bad_topics(mqtt_broker, tmp_path):
    async with make_link(mqtt_broker, tmp_path) as link:
        for bad in ("", "opencap/+/x", "opencap/#", "$SYS/x", "a\x00b"):
            with pytest.raises(ValueError):
                await link.publish(bad, b"x")
        assert link._saf.count() == 0


async def test_queues_while_broker_down_then_drains_in_order(tmp_path):
    async with TestBroker() as broker, broker.collector("opencap/#") as col:
        await broker.stop()                                    # port known, broker down
        async with make_link(broker, tmp_path, backoff_min_s=0.1, backoff_max_s=0.5) as link:
            worst = 0.0
            for i in range(100):
                t0 = time.perf_counter()
                await link.publish(TOPIC, seq_payload(i))
                worst = max(worst, time.perf_counter() - t0)
            assert worst < 0.5                                  # never waits on the network
            await asyncio.sleep(0.3)
            assert not link.connected and link._saf.count() == 100
            await broker.start()                               # collector re-subscribes before the link gets in
            msgs = await col.wait_count(TOPIC, 100, timeout=10)
            assert seqs(msgs) == list(range(100))
            await wait_until(lambda: link._saf.count() == 0, what="outbox drained")


async def test_broker_outage_mid_stream_loses_nothing(tmp_path):
    """Broker stopped for 3 s mid-stream and restarted on the same port.

    Every message arrives, in order. QoS 1 is at-least-once: a message whose PUBACK was lost
    in the outage is sent again after the reconnect, so the check dedupes by sequence number
    (keeping the first arrival) and the duplicates are only counted.
    """
    n = 400
    async with TestBroker() as broker, broker.collector("opencap/#") as col:
        port = broker.port
        async with make_link(broker, tmp_path) as link:          # production backoff: 1 s -> 30 s
            await broker.wait_client("test-poller")
            worst = 0.0

            async def produce():
                nonlocal worst
                for i in range(n):
                    t0 = time.perf_counter()
                    await link.publish(TOPIC, seq_payload(i))
                    worst = max(worst, time.perf_counter() - t0)
                    await asyncio.sleep(0.01)

            producer = asyncio.create_task(produce())
            await col.wait_count(TOPIC, 50, timeout=10)             # mid-stream
            await broker.stop()
            await wait_until(lambda: not link.connected, timeout=5, what="link notices the outage")
            await asyncio.sleep(3)
            backlog = link._saf.count()
            assert backlog > 0                                      # still producing during the outage
            await broker.start()
            assert broker.port == port
            await producer
            await col.wait_for(lambda: set(seqs(col.topic(TOPIC))) >= set(range(n)), timeout=30,
                               what="every sequence number")
            await wait_until(lambda: link._saf.count() == 0, what="outbox drained")

            got = seqs(col.topic(TOPIC))
            first_arrivals = list(dict.fromkeys(got))
            assert first_arrivals == list(range(n))                 # nothing lost, original order
            duplicates = len(got) - n
            assert duplicates <= link._window                       # only the in-flight window can repeat
            print(f"outage: backlog={backlog} duplicates={duplicates} max_publish_ms={worst * 1000:.1f}")
            assert worst < 0.5
            assert link.connects == 2
            assert col.payloads(lwt_topic("test-poller")).count(b"online") == 2


async def test_outbox_survives_process_restart(tmp_path):
    path = tmp_path / "saf.sqlite"
    async with TestBroker() as broker, broker.collector("opencap/#") as col:
        await broker.stop()
        async with make_link(broker, tmp_path, path=path, backoff_min_s=0.1) as link:   # "process 1"
            for i in range(30):
                await link.publish(TOPIC, seq_payload(i))
        # process 1 is gone (link stopped, outbox closed); process 2 opens the same file
        async with make_link(broker, tmp_path, path=path, backoff_min_s=0.1) as link2:
            assert link2._saf.count() == 30
            for i in range(30, 40):
                await link2.publish(TOPIC, seq_payload(i))
            await broker.start()
            msgs = await col.wait_count(TOPIC, 40, timeout=10)
            assert seqs(msgs) == list(range(40))


async def test_lwt_and_retained_status_after_kill(tmp_path):
    """The poller process is SIGKILLed: the broker publishes the retained LWT `offline`, and the
    retained device status stays readable for late subscribers."""
    status_topic = "opencap/site_a/plc_1/status"
    status = b'{"state":"ok","last_good":"2026-11-16T10:15:01+00:00","errors":0}'
    async with TestBroker() as broker, broker.collector("opencap/#") as early:
        script = textwrap.dedent(f"""
            import asyncio, sys
            sys.path.insert(0, {str(SRC)!r})
            from opencap.config.models import MqttCfg
            from opencap.north.mqtt import MqttLink
            from opencap.north.saf import StoreAndForward

            async def main():
                saf = StoreAndForward({str(tmp_path / "killed.sqlite")!r}, 10_000_000)
                link = MqttLink(MqttCfg(host={broker.host!r}, port={broker.port}, password_env=None),
                                "killme", saf)
                task = asyncio.create_task(link.run())
                await link.publish({status_topic!r}, {status!r}, retain=True)
                while not (link.connected and saf.count() == 0):
                    await asyncio.sleep(0.02)
                print("READY", flush=True)
                await task

            asyncio.run(main())
        """)
        proc = await asyncio.create_subprocess_exec(sys.executable, "-c", script,
                                                    stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        try:
            out: list[bytes] = []

            async def ready() -> None:                          # skip the child's log lines
                while (line := await proc.stdout.readline()) and line.strip() != b"READY":
                    out.append(line)
                assert line, b"".join(out)                        # child exited early

            await asyncio.wait_for(ready(), timeout=30)
            await early.wait_payload(lwt_topic("killme"), b"online")
        finally:
            proc.kill()                                         # SIGKILL: no DISCONNECT, no "offline" from us
            await proc.wait()
        await broker.wait_client("killme", connected=False, timeout=10)
        await early.wait_payload(lwt_topic("killme"), b"offline", timeout=10)   # the broker sent the LWT

        async with broker.collector("opencap/#") as late:
            lwt = await late.wait_payload(lwt_topic("killme"), b"offline")
            st = await late.wait_payload(status_topic, status)
            assert lwt.retain and st.retain
            # amqtt 0.12 sends each retained message twice on subscribe (a QoS 0 and a QoS 1 copy)
            assert set(late.payloads(lwt_topic("killme"))) == {b"offline"}


async def test_graceful_stop_publishes_offline(mqtt_broker, tmp_path):
    runner = make_link(mqtt_broker, tmp_path, "graceful")
    async with mqtt_broker.collector("opencap/poller/#") as col:
        async with runner as link:
            await col.wait_payload(lwt_topic("graceful"), b"online")
        assert runner.task.done() and not link.connected
        await mqtt_broker.wait_client("graceful", connected=False)
        assert col.payloads(lwt_topic("graceful")) == [b"online", b"offline"]


async def test_reconnect_after_connection_drop(mqtt_broker, tmp_path):
    async with mqtt_broker.collector("opencap/#") as col, \
            make_link(mqtt_broker, tmp_path, "dropper", backoff_min_s=0.1) as link:
        await mqtt_broker.wait_client("dropper")
        assert await mqtt_broker.kick("dropper")               # broker-side abort, no DISCONNECT
        await col.wait_payload(lwt_topic("dropper"), b"offline")
        await wait_until(lambda: link.connects == 2, what="reconnect")
        await link.publish(TOPIC, seq_payload(1))
        await col.wait_count(TOPIC, 1)


def test_client_kwargs(monkeypatch, tmp_path):
    from opencap.config.models import MqttCfg

    saf = StoreAndForward(tmp_path / "s.sqlite", 1000)
    monkeypatch.setenv("TEST_MQTT_PW", "s3cret")
    cfg = MqttCfg(host="broker.example", port=8883, username="poller", password_env="TEST_MQTT_PW",
                  keepalive_s=20)
    link = MqttLink(cfg, "opencap-7", saf)
    kw = link.client_kwargs()
    assert kw["identifier"] == "opencap-7"
    assert (kw["username"], kw["password"]) == ("poller", "s3cret")
    assert kw["keepalive"] == 20 and kw["clean_session"] is True
    assert (kw["will"].topic, kw["will"].payload, kw["will"].qos, kw["will"].retain) == (
        "opencap/poller/opencap-7/status", b"offline", 1, True)
    assert "tls_context" not in kw and link.is_secure is False

    link = MqttLink(cfg.model_copy(update={"client_id": "custom", "username": None}), "x", saf)
    kw = link.client_kwargs()
    assert kw["identifier"] == "custom" and "username" not in kw and "password" not in kw
    saf.close()


# ---------------------------------------------------------------------- TLS

def _make_certs(d: Path) -> tuple[Path, Path, Path]:
    def run(*args: str) -> None:
        subprocess.run(["openssl", *args], cwd=d, check=True, capture_output=True)

    ec = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes"]
    # keyUsage on the CA: Python >= 3.13 verifies with VERIFY_X509_STRICT by default
    run("req", "-x509", *ec, "-keyout", "ca.key", "-out", "ca.pem", "-days", "2", "-subj", "/CN=oo-test-ca",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    run("req", *ec, "-keyout", "server.key", "-out", "server.csr", "-subj", "/CN=localhost")
    (d / "ext.cnf").write_text(
        "subjectAltName=IP:127.0.0.1,DNS:localhost\nbasicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n"
        "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n"
    )
    run("x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
        "-out", "server.pem", "-days", "2", "-extfile", "ext.cnf")
    return d / "ca.pem", d / "server.pem", d / "server.key"


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI not available")
async def test_tls_link(tmp_path):
    ca, cert, key = _make_certs(tmp_path)
    ssl.create_default_context(cafile=str(ca))                 # the CA is usable by Python's default context
    async with TestBroker(listener={"ssl": True, "certfile": str(cert), "keyfile": str(key)}) as broker:
        saf = StoreAndForward(tmp_path / "tls.sqlite", 1_000_000)
        link = MqttLink(broker.mqtt_cfg(tls=True, ca_file=str(ca)), "tls-poller", saf)
        async with LinkRunner(link, saf):
            assert link.is_secure
            await broker.wait_client("tls-poller")
            await link.publish(TOPIC, b"{}")
            await wait_until(lambda: saf.count() == 0, what="acked over TLS")

        # wrong CA: the handshake fails, nothing is sent, the link keeps retrying
        other = tmp_path / "other"
        other.mkdir()
        wrong_ca, _, _ = _make_certs(other)
        saf2 = StoreAndForward(tmp_path / "tls2.sqlite", 1_000_000)
        bad = MqttLink(broker.mqtt_cfg(tls=True, ca_file=str(wrong_ca)), "tls-bad", saf2, backoff_min_s=0.1)
        async with LinkRunner(bad, saf2):
            await bad.publish(TOPIC, b"{}")
            await wait_until(lambda: bad.last_error is not None, what="TLS failure recorded")
            assert not bad.connected and saf2.count() == 1
            assert "certificate" in bad.last_error.lower() or "ssl" in bad.last_error.lower()


