"""Modbus RTU driver: a local serial port, or RTU frames on a TCP socket (a serial-over-TCP
gateway in transparent mode / RTU over TCP).

Everything above the wire is `modbus_tcp`'s: the Kepware address parser, the block planner,
the register decoder and every param of `ModbusParams` (`unit_id`, word order, block sizes, ...),
so an RTU device plans and decodes exactly like a Modbus TCP device with the same tags. The
wire is pymodbus with its RTU framer: `AsyncModbusSerialClient` (pyserial, optional extra
`serial`) for `transport: serial`, `AsyncModbusTcpClient(framer=RTU)` for `transport: tcp`.
RFC 2217 is not offered: pymodbus' serial transport needs a file descriptor to watch, which a
pyserial `rfc2217://` port does not have.

Serial-line specifics
- Several devices (unit ids) on one line share one pymodbus client (`_serial.LinkPool`): a
  serial port can be opened once (pymodbus opens it exclusively) and many gateways accept one
  TCP client. An error that closes the line closes it for every unit on it.
- RTU has no transaction id: a reply that arrives after its request timed out could be taken
  for the answer to the next request with the same function code. `close_on_timeout` (default
  true, as for Modbus TCP) closes the line after a timeout, which throws such late bytes away.
- Inter-frame gap: before each request the driver waits until `inter_frame_ms` have passed
  since the last request/reply on the line (default 3.5 character times at `baudrate`, at least
  1.75 ms, the Modbus serial line spec). pymodbus does not enforce it.
- `local_echo: true` drops the echo of our own request that some 2-wire RS-485 adapters return.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

import structlog

from opencap.config.models import Device
from opencap.drivers import _schema as sc
from opencap.drivers._serial import (
    Endpoint,
    Lease,
    SerialSettings,
    TransportUnmapped,
    endpoint_for,
    link_pool,
    serial_properties,
)
from opencap.drivers.base import ConnectionLost
from opencap.drivers.modbus_tcp import (
    _LIBRARY_TIMEOUT_SLACK_S,
    _NO_AUTO_DISCONNECT,
    MODBUS_PROPERTIES,
    ModbusTcpDriver,
)

_log = structlog.get_logger("opencap.drivers.modbus_rtu")

RTU_TRANSPORTS = ("tcp", "serial")
MIN_INTER_FRAME_S = 0.00175          # Modbus over serial line V1.02, 2.5.1.1: fixed above 19200 baud


def inter_frame_s(settings: SerialSettings, inter_frame_ms: Any) -> float:
    """The gap the driver keeps between frames on the line."""
    if inter_frame_ms is None:
        return max(3.5 * settings.char_time_s(), MIN_INTER_FRAME_S)
    if isinstance(inter_frame_ms, bool) or not isinstance(inter_frame_ms, (int, float)):
        raise ValueError(f"modbus_rtu param inter_frame_ms={inter_frame_ms!r}: expected a number")
    if not 0 <= inter_frame_ms <= 10_000:
        raise ValueError(f"modbus_rtu param inter_frame_ms={inter_frame_ms}: must be 0..10000")
    return inter_frame_ms / 1000.0


@dataclass(eq=False)
class _RtuLine:
    """The pymodbus client of one line, shared by its units."""

    client: Any
    lost: asyncio.Future
    last_io: float = 0.0                                 # monotonic time of the last frame on the line
    io_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ModbusRtuDriver(ModbusTcpDriver):
    name: ClassVar[str] = "modbus_rtu"
    is_async: ClassVar[bool] = True
    transport: ClassVar[str] = "serial, tcp"
    params_schema: ClassVar[dict[str, Any]] = sc.params_schema("modbus_rtu params", {
        **MODBUS_PROPERTIES,
        **serial_properties(("tcp", "serial", "unmapped")),
        "inter_frame_ms": sc.number("silence kept between frames (null: 3.5 characters at the baud rate)",
                                    minimum=0, nullable=True),
        "local_echo": sc.boolean("drop the echo of our own request (2-wire RS-485 adapters)", False),
    })

    def __init__(self, device: Device, executor: Any = None) -> None:
        super().__init__(device, executor)                       # ModbusParams: ValueError on bad params
        self.settings = SerialSettings.from_params(device.params)
        self.gap_s = inter_frame_s(self.settings, device.params.get("inter_frame_ms"))
        echo = device.params.get("local_echo", False)
        if not isinstance(echo, bool):
            raise ValueError(f"modbus_rtu param local_echo={echo!r}: expected true or false")
        self.local_echo = echo
        self._lease: Lease | None = None
        self._endpoint: Endpoint | None = None

    @property
    def _where(self) -> str:
        ep = self._endpoint.describe() if self._endpoint is not None else f"{self.device.host}:{self.device.port}"
        return f"{self.device.id} (unit {self.params.unit_id}, {ep})"

    @property
    def connected(self) -> bool:
        return (self._lease is not None and self._lease.alive and self._client is not None
                and bool(self._client.connected))

    def _new_client(self, endpoint: Endpoint, on_connect: Callable[[bool], None]) -> Any:
        from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient
        from pymodbus.framer import FramerType

        common = dict(name=endpoint.describe(), timeout=self.timeout_s + _LIBRARY_TIMEOUT_SLACK_S,
                      retries=0, reconnect_delay=0, reconnect_delay_max=0, trace_connect=on_connect)
        if endpoint.kind == "serial":
            client = AsyncModbusSerialClient(endpoint.host, framer=FramerType.RTU,
                                             handle_local_echo=self.local_echo,
                                             **self.settings.pyserial_kwargs(), **common)
        else:
            client = AsyncModbusTcpClient(endpoint.host, port=endpoint.port, framer=FramerType.RTU, **common)
        client.set_max_no_responses(_NO_AUTO_DISCONNECT)
        return client

    async def connect(self) -> None:
        """Open (or join) the line: one attempt, bounded by `device.timeout_ms`. Raises ConnectionLost."""
        await self.close()
        try:
            endpoint = self._endpoint = endpoint_for(self.device, allowed=RTU_TRANSPORTS)
        except (TransportUnmapped, ValueError) as exc:
            raise ConnectionLost(str(exc)) from None

        async def opener() -> _RtuLine:
            lost: asyncio.Future[None] = asyncio.get_running_loop().create_future()

            def on_connect(up: bool) -> None:
                if not up and not lost.done():
                    lost.set_result(None)

            try:
                client = self._new_client(endpoint, on_connect)
            except (RuntimeError, ImportError) as exc:      # pymodbus: "Serial client requires pyserial"
                raise ConnectionLost(f"cannot open {endpoint.describe()}: {exc} "
                                     "(pip install 'opencap[serial]')") from None
            ok, reason = False, ""
            try:
                async with asyncio.timeout(self.timeout_s):
                    ok = bool(await client.connect())
            except TimeoutError:
                reason = f"no answer within {self.device.timeout_ms} ms"
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
            except BaseException:
                client.close()
                raise
            if not ok or not client.connected:
                client.close()
                raise ConnectionLost(reason or "refused, unreachable or cannot be opened")
            return _RtuLine(client, lost)

        async def closer(line: _RtuLine) -> None:
            if not line.lost.done():
                line.lost.set_result(None)            # wakes a request still waiting on this line
            line.client.close()

        started = time.monotonic()
        try:
            lease = await link_pool().acquire(("modbus_rtu", endpoint.key), id(self), opener, closer)
        except ConnectionLost as exc:
            _log.debug("modbus_rtu connect", device=self.device.id, result="failed", reason=str(exc))
            raise ConnectionLost(f"{self._where}: connect failed: {exc}") from None
        line: _RtuLine = lease.conn
        self._lease, self._client, self._lost = lease, line.client, line.lost
        _log.debug("modbus_rtu connect", device=self.device.id, result="ok", line=endpoint.describe(),
                   shared=len(lease.link.users) > 1, ms=round((time.monotonic() - started) * 1000, 1))

    async def close(self) -> None:
        """Leave the line (closed when no other unit uses it). Idempotent, never raises."""
        lease, self._lease = self._lease, None
        self._client = self._lost = None
        if lease is not None:
            try:
                await lease.release()
            except Exception as exc:   # pragma: no cover - pymodbus close() does not raise
                _log.debug("modbus_rtu close error", device=self.device.id, error=repr(exc))

    async def _drop(self, client: Any) -> None:
        """The line failed (timeout with close_on_timeout, peer close): close it for every unit."""
        lease = self._lease
        if lease is not None and lease.alive and lease.conn.client is client:
            await lease.drop()
        else:
            try:
                client.close()
            except Exception:   # pragma: no cover
                pass
        if self._client is client:
            self._client = self._lost = None

    async def _request(self, make: Callable[[], Awaitable[Any]], what: str, *,
                       close_on_timeout: bool) -> tuple[Any, datetime]:
        lease = self._lease
        if lease is None or not lease.alive:
            raise ConnectionLost(f"{self._where}: not connected")
        line: _RtuLine = lease.conn
        async with line.io_lock:
            wait = line.last_io + self.gap_s - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await super()._request(make, what, close_on_timeout=close_on_timeout)
            finally:
                line.last_io = time.monotonic()
