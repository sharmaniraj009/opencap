"""Python driver host: every registered Python driver, served over `opencap.plugin.v1`.

ADR-0004 §3. The Rust engine starts one host per channel whose devices say `plugin: python`
and calls it through gRPC (`proto/opencap/plugin/v1/driver.proto`; the launch contract,
compatibility rules and block payload format are in `proto/README.md`):

    python -m opencap.plugin_host --driver ab_pccc --socket /tmp/opencap-plugin-x/plugin.sock
    python -m opencap.plugin_host --driver ab_pccc --listen 127.0.0.1:0      # loopback TCP

The driver classes are used unchanged: `plan()` answers Plan; each open device has its own
driver instance; blocking libraries run on one single-thread executor per process, which is
the channel executor of the Python engine (one host per channel, D-015). Retries, backoff,
demotion, scaling and staleness stay in the engine. There is no write RPC (ADR-0008).

Needs the `plugin` extra (`pip install opencap[plugin]`: grpcio, protobuf).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
import pickle
import secrets
import signal
import sys
import threading
import time
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
import structlog

from opencap import __version__
from opencap.config.models import Device
from opencap.core.cache import TagValue
from opencap.drivers import DRIVERS, get_driver_class
from opencap.drivers.base import Block, BlockConfigError, CommError, ConnectionLost, DecodeError, Driver
from opencap.logsetup import configure_logging
from opencap.plugin.v1 import driver_pb2 as pb
from opencap.plugin.v1 import driver_pb2_grpc as pb_grpc

PROTOCOL = "opencap.plugin.v1"
READY = "opencap-plugin-ready"          # the one line on stdout once the server listens
TOKEN_ENV = "OPENCAP_PLUGIN_TOKEN"
PAYLOAD_VERSION = b"\x01"
MAC_LEN = hashlib.sha256().digest_size
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
INT64_MIN, INT64_MAX, UINT64_MAX = -(2**63), 2**63 - 1, 2**64 - 1
MAX_MESSAGE = 64 * 1024 * 1024

QUALITY = {"good": pb.GOOD, "bad_comm": pb.BAD_COMM, "bad_config": pb.BAD_CONFIG,
           "bad_decode": pb.BAD_DECODE, "stale": pb.STALE}

log = structlog.get_logger("opencap.plugin_host")


# --- mapping --------------------------------------------------------------------------

def driver_error(exc: BaseException) -> pb.DriverError:
    """The driver contract's error classes, as the engine's DriverError kinds."""
    if isinstance(exc, ConnectionLost):
        kind = pb.DriverError.CONNECTION_LOST
    elif isinstance(exc, CommError):
        kind = pb.DriverError.COMM
    elif isinstance(exc, BlockConfigError):
        kind = pb.DriverError.BLOCK_CONFIG
    elif isinstance(exc, (NotImplementedError, LookupError)):
        kind = pb.DriverError.UNSUPPORTED
    else:
        return pb.DriverError(kind=pb.DriverError.OTHER, message=f"{type(exc).__name__}: {exc}")
    return pb.DriverError(kind=kind, message=str(exc))


def not_open(device_id: str) -> pb.DriverError:
    return pb.DriverError(kind=pb.DriverError.NOT_OPEN, message=f"device {device_id!r} is not open in this plugin host")


def variant(value: Any) -> pb.Variant | None:
    """A decoded Python value as a Variant; None when the engine's value model cannot hold it."""
    if isinstance(value, bool):
        return pb.Variant(bool_value=value)
    if isinstance(value, int):
        if INT64_MIN <= value <= INT64_MAX:
            return pb.Variant(int_value=value)
        if 0 <= value <= UINT64_MAX:
            return pb.Variant(uint_value=value)
        return None
    if isinstance(value, float):
        return pb.Variant(float_value=value)
    if isinstance(value, str):
        return pb.Variant(string_value=value)
    if isinstance(value, (list, tuple)):
        items = [variant(v) for v in value]
        if any(i is None for i in items):
            return None
        return pb.Variant(list_value=pb.VariantList(items=items))
    return None


def micros(ts: datetime) -> int:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (ts - EPOCH) // timedelta(microseconds=1)


def tag_value(tv: TagValue) -> pb.TagValue:
    """A driver result, as the engine's worker expects it. A DecodeError value (or one the value
    model cannot hold) is bad_decode; a non-good result carries no value."""
    out = pb.TagValue(quality=QUALITY.get(tv.quality, pb.BAD_DECODE))
    if tv.source_ts is not None:
        out.source_ts_us = micros(tv.source_ts)
    if tv.quality == "good" and tv.value is not None:
        v = None if isinstance(tv.value, DecodeError) else variant(tv.value)
        if v is None:
            out.quality = pb.BAD_DECODE
        else:
            out.value.CopyFrom(v)
    return out


def driver_info(cls: type[Driver], driver: Driver) -> pb.DriverInfo:
    info = pb.DriverInfo(reports_connection=cls.connected is not Driver.connected,
                         stale_by_read_time=bool(cls.stale_by_read_time))
    timeout = driver.connect_timeout_s()
    if timeout is not None:
        info.connect_timeout_ms = max(1, round(timeout * 1000))
    return info


def is_connected(driver: Driver) -> bool:
    try:
        return bool(driver.connected)
    except Exception:          # noqa: BLE001 - a property must not break the reply
        return False


class Sealer:
    """Block payloads: pickle + HMAC-SHA256 under the engine run's token. `open()` verifies the
    tag before it unpickles anything, so only payloads this key produced are deserialised. A
    verified block is kept (by payload), so each block costs the check once per process."""

    CACHE_MAX = 65_536

    def __init__(self, key: bytes) -> None:
        self._key = key
        self._opened: dict[bytes, Block] = {}

    def _mac(self, body: bytes) -> bytes:
        return hmac.new(self._key, body, hashlib.sha256).digest()

    def seal(self, block: Block) -> bytes:
        body = pickle.dumps(block, protocol=pickle.HIGHEST_PROTOCOL)
        return PAYLOAD_VERSION + self._mac(body) + body

    def open(self, payload: bytes) -> Block:
        block = self._opened.get(payload)
        if block is None:
            block = self._verify(payload)
            if len(self._opened) >= self.CACHE_MAX:
                self._opened.clear()
            self._opened[payload] = block
        return block

    def _verify(self, payload: bytes) -> Block:
        if len(payload) <= 1 + MAC_LEN or payload[:1] != PAYLOAD_VERSION:
            raise ValueError("not a block payload of this plugin host")
        mac, body = payload[1:1 + MAC_LEN], payload[1 + MAC_LEN:]
        if not hmac.compare_digest(mac, self._mac(body)):
            raise ValueError("block payload signature does not verify (planned by another engine run?)")
        block = pickle.loads(body)   # noqa: S301 - authenticated above
        if not isinstance(block, Block):
            raise ValueError("block payload is not a Block")
        return block


# --- service --------------------------------------------------------------------------

@dataclass
class _Session:
    config: str
    driver: Driver
    info: pb.DriverInfo


class DriverHost(pb_grpc.DriverServicer):
    """The `Driver` service over the Python driver classes."""

    def __init__(self, drivers: set[str], sealer: Sealer, executor: Executor) -> None:
        self.drivers = drivers
        self.sealer = sealer
        self.executor = executor
        self.sessions: dict[str, _Session] = {}
        self.reads = 0
        self.started = time.monotonic()

    def _driver_class(self, device: Device) -> type[Driver]:
        if device.driver not in self.drivers:
            raise LookupError(f"driver {device.driver!r} is not served by this plugin host "
                              f"(it serves {', '.join(sorted(self.drivers))})")
        return get_driver_class(device.driver)

    async def Hello(self, request: pb.HelloRequest, context: Any) -> pb.HelloReply:
        if request.protocol != PROTOCOL:
            log.warning("plugin_host.protocol_mismatch", engine=request.engine, protocol=request.protocol)
        return pb.HelloReply(protocol=PROTOCOL, host=f"opencap-python {__version__}", drivers=sorted(self.drivers))

    async def Plan(self, request: pb.PlanRequest, context: Any) -> pb.PlanReply:
        try:
            device = Device.model_validate_json(request.device.json)
            cls = self._driver_class(device)
            driver = cls(device, None)           # plan() is pure: no executor, no I/O
            by_name = {t.name: t for t in device.tags}
            unknown = [n for n in request.tags if n not in by_name]
            if unknown:
                raise ValueError(f"{device.id}: tags not in the device config: {unknown[:5]}")
            plan = driver.plan([by_name[n] for n in request.tags])
        except Exception as exc:                 # noqa: BLE001 - the device cannot be planned
            return pb.PlanReply(error=driver_error(exc))
        blocks = [pb.Block(payload=self.sealer.seal(b), tags=list(b.tags), wire_requests=b.wire_requests(),
                           describe=b.describe()) for b in plan.blocks]
        return pb.PlanReply(blocks=blocks, invalid=plan.invalid, info=driver_info(cls, driver))

    async def Open(self, request: pb.OpenRequest, context: Any) -> pb.OpenReply:
        existing = self.sessions.get(request.device_id)
        if existing is not None and existing.config == request.device.json:
            return pb.OpenReply(info=existing.info)
        try:
            device = Device.model_validate_json(request.device.json)
            if device.id != request.device_id:
                raise ValueError(f"device id {device.id!r} does not match {request.device_id!r}")
            cls = self._driver_class(device)
            driver = cls(device, self.executor)
        except Exception as exc:                 # noqa: BLE001
            return pb.OpenReply(error=driver_error(exc))
        if existing is not None:
            await _close(existing.driver)
        session = _Session(request.device.json, driver, driver_info(cls, driver))
        self.sessions[request.device_id] = session
        log.info("plugin_host.device_open", device=request.device_id, driver=device.driver)
        return pb.OpenReply(info=session.info)

    async def Connect(self, request: pb.DeviceRef, context: Any) -> pb.StatusReply:
        s = self.sessions.get(request.device_id)
        if s is None:
            return pb.StatusReply(error=not_open(request.device_id))
        try:
            await s.driver.connect()
        except Exception as exc:                 # noqa: BLE001 - mapped to the driver contract
            return self._status(s, driver_error(exc))
        return self._status(s)

    @staticmethod
    def _status(s: _Session, error: pb.DriverError | None = None) -> pb.StatusReply:
        return pb.StatusReply(error=error, connected=is_connected(s.driver), info=driver_info(type(s.driver), s.driver))

    async def Read(self, request: pb.ReadRequest, context: Any) -> pb.ReadReply:
        s = self.sessions.get(request.device_id)
        if s is None:
            return pb.ReadReply(error=not_open(request.device_id))
        try:
            block = self.sealer.open(request.block)
        except Exception as exc:                 # noqa: BLE001
            return pb.ReadReply(error=pb.DriverError(kind=pb.DriverError.BLOCK_CONFIG,
                                                     message=f"block payload refused: {exc}"))
        self.reads += 1
        t0 = time.perf_counter()
        try:
            values = await s.driver.read(block)
        except Exception as exc:                 # noqa: BLE001 - mapped to the driver contract
            return pb.ReadReply(error=driver_error(exc), connected=is_connected(s.driver), driver_us=_us_since(t0))
        reply = pb.ReadReply(connected=is_connected(s.driver), driver_us=_us_since(t0))
        for name, tv in (values or {}).items():
            reply.values[name].CopyFrom(tag_value(tv))
        return reply

    async def Close(self, request: pb.DeviceRef, context: Any) -> pb.StatusReply:
        s = self.sessions.get(request.device_id)
        if s is None:
            return pb.StatusReply()
        await _close(s.driver)
        return self._status(s)

    async def Identity(self, request: pb.DeviceRef, context: Any) -> pb.IdentityReply:
        s = self.sessions.get(request.device_id)
        if s is None:
            return pb.IdentityReply(error=not_open(request.device_id), json="{}")
        try:
            ident = await s.driver.identity()
            text = json.dumps(ident or {}, default=str)
        except Exception as exc:                 # noqa: BLE001 - identity never fails (contract)
            log.debug("plugin_host.identity_failed", device=request.device_id, error=str(exc))
            text = "{}"
        return pb.IdentityReply(json=text, connected=is_connected(s.driver))

    async def Browse(self, request: pb.DeviceRef, context: Any) -> pb.BrowseReply:
        s = self.sessions.get(request.device_id)
        if s is None:
            return pb.BrowseReply(error=not_open(request.device_id))
        browse = getattr(s.driver, "browse", None)
        if browse is None:
            return pb.BrowseReply(error=pb.DriverError(kind=pb.DriverError.UNSUPPORTED,
                                                       message=f"{s.driver.name}: browse() not supported"))
        try:
            tags = await browse()
        except Exception as exc:                 # noqa: BLE001
            return pb.BrowseReply(error=driver_error(exc))
        return pb.BrowseReply(tags=[pb.BrowsedTag(name=t.name, address=t.address, dtype=t.dtype,
                                                  description=t.description or "") for t in tags])

    async def Subscribe(self, request: pb.SubscribeRequest, context: Any):  # noqa: ANN201 - stream
        # Push drivers sample their notification buffer in read() (wire_requests() == 0), so the
        # engine serves them through Read; the streaming path is for Rust's subscribe() (#46).
        yield pb.Notification(error=pb.DriverError(kind=pb.DriverError.UNSUPPORTED,
                                                   message="the Python host serves push drivers through Read"))

    async def Health(self, request: pb.HealthRequest, context: Any) -> pb.HealthReply:
        return pb.HealthReply(protocol=PROTOCOL, pid=os.getpid(), uptime_s=time.monotonic() - self.started,
                              open_devices=len(self.sessions), reads=self.reads)

    async def close_all(self) -> None:
        for s in list(self.sessions.values()):
            await _close(s.driver)
        self.sessions.clear()


def _us_since(t0: float) -> int:
    return max(0, round((time.perf_counter() - t0) * 1e6))


async def _close(driver: Driver) -> None:
    with contextlib.suppress(Exception):
        await driver.close()


class TokenAuth(grpc.aio.ServerInterceptor):
    """Every call must carry `authorization: Bearer <token>` (proto/README.md)."""

    def __init__(self, token: str) -> None:
        self._expected = f"Bearer {token}"

    async def intercept_service(self, continuation, details):  # noqa: ANN001, ANN201 - grpc API
        handler = await continuation(details)
        got = dict(details.invocation_metadata or ()).get("authorization", "")
        if handler is None or hmac.compare_digest(str(got), self._expected):
            return handler

        async def deny(request: Any, context: grpc.aio.ServicerContext) -> None:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing or wrong plugin token")

        if handler.unary_stream is not None:
            async def deny_stream(request: Any, context: grpc.aio.ServicerContext):  # noqa: ANN202
                await context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing or wrong plugin token")
                yield  # pragma: no cover - makes this an async generator

            return grpc.unary_stream_rpc_method_handler(
                deny_stream, request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer)
        return grpc.unary_unary_rpc_method_handler(
            deny, request_deserializer=handler.request_deserializer, response_serializer=handler.response_serializer)


# --- process --------------------------------------------------------------------------

def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return host == "localhost"


def _parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m opencap.plugin_host",
                                description="Serve Python drivers to the Rust engine over opencap.plugin.v1.")
    p.add_argument("--driver", action="append", default=[], metavar="NAME",
                   help="driver to serve (repeatable; default: every registered driver)")
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument("--socket", metavar="PATH", help="listen on a Unix socket")
    where.add_argument("--listen", metavar="HOST:PORT", help="listen on loopback TCP (port 0 = any free port)")
    p.add_argument("--exit-on-stdin-eof", action="store_true", help="exit when stdin closes (the engine is gone)")
    p.add_argument("--log-level", default=None)
    args = p.parse_args(argv)
    unknown = sorted(set(args.driver) - set(DRIVERS))
    if unknown:
        p.error(f"unknown driver(s): {', '.join(unknown)} (known: {', '.join(DRIVERS)})")
    token = os.environ.get(TOKEN_ENV)
    if token is not None:
        try:
            if len(bytes.fromhex(token)) < 16:
                raise ValueError
        except ValueError:
            p.error(f"{TOKEN_ENV} must be at least 32 hex characters")
    if args.listen:
        host = args.listen.rsplit(":", 1)[0]
        if not _loopback(host):
            p.error("--listen only accepts a loopback address")
        if token is None:
            p.error(f"--listen needs {TOKEN_ENV}: every local user can reach a loopback port")
    args.token = token
    return args


async def serve(args: argparse.Namespace) -> int:
    key = bytes.fromhex(args.token) if args.token else secrets.token_bytes(32)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="opencap-plugin")
    host = DriverHost(set(args.driver) or set(DRIVERS), Sealer(key), executor)
    server = grpc.aio.server(
        interceptors=[TokenAuth(args.token)] if args.token else [],
        options=[("grpc.max_receive_message_length", MAX_MESSAGE), ("grpc.max_send_message_length", MAX_MESSAGE)],
    )
    pb_grpc.add_DriverServicer_to_server(host, server)
    if args.socket:
        address = f"unix:{args.socket}"
        server.add_insecure_port(address)
    else:
        port = server.add_insecure_port(args.listen)
        address = f"{args.listen.rsplit(':', 1)[0]}:{port}"
    await server.start()

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, ValueError, AttributeError):
            loop.add_signal_handler(sig, stop.set)
    if args.exit_on_stdin_eof:
        def watch_stdin() -> None:
            with contextlib.suppress(Exception):
                while sys.stdin.buffer.read(4096):
                    pass
            loop.call_soon_threadsafe(stop.set)

        threading.Thread(target=watch_stdin, name="opencap-plugin-stdin", daemon=True).start()

    print(f"{READY} {json.dumps({'protocol': PROTOCOL, 'address': address})}", flush=True)
    log.info("plugin_host.ready", address=address, drivers=sorted(host.drivers), pid=os.getpid())
    await stop.wait()
    log.info("plugin_host.stopping", reads=host.reads)
    await server.stop(grace=1.0)
    await host.close_all()
    executor.shutdown(wait=False, cancel_futures=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    configure_logging(args.log_level, stream=sys.stderr)
    return asyncio.run(serve(args))


if __name__ == "__main__":
    sys.exit(main())
