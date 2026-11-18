"""Put a TCP-served serial simulator behind a real tty, or behind an RFC 2217 server.

    bridge = await PtyBridge.start("127.0.0.1", sim.port)     # a pty wired to the simulator's socket
    driver opens bridge.path (/dev/pts/N) as a local serial port (transport: serial)
    await bridge.stop()

    server = await Rfc2217Server.start(tty_path)              # pyserial's own RFC 2217 port manager
    client opens rfc2217://127.0.0.1:<server.port>
    server.settings_seen()                                    # what the client negotiated

The simulators (`df1_sim.Df1Sim`, `modbus_sim.ModbusSim(framer="rtu")`) speak the serial
protocol on a TCP socket, as a serial-over-TCP gateway would. `PtyBridge` copies bytes between
the pty's master side and one TCP connection to the simulator, so the same simulator also
tests the local serial-port transport: pyserial opens the pty's slave side exactly as it would
open /dev/ttyUSB0 (baud rate and parity are accepted and have no effect on a pty).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import os
import pty
import socket
import threading
import tty
from typing import Any

import pytest

# Local serial ports and RFC 2217 go through pyserial (the optional extra `serial`).
needs_pyserial = pytest.mark.skipif(importlib.util.find_spec("serial") is None,
                                    reason="pyserial not installed (pip install -e '.[serial]')")


class PtyBridge:
    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        os.set_blocking(self.master, False)
        self.path = os.ttyname(self.slave)
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self.bytes_to_sim = 0
        self.bytes_from_sim = 0

    @classmethod
    async def start(cls, host: str, port: int) -> PtyBridge:
        bridge = cls(host, port)
        reader, writer = await asyncio.open_connection(host, port)
        bridge._writer = writer
        loop = asyncio.get_running_loop()
        loop.add_reader(bridge.master, bridge._from_tty)
        bridge._task = asyncio.create_task(bridge._to_tty(reader), name=f"pty-bridge {bridge.path}")
        return bridge

    def _from_tty(self) -> None:
        try:
            data = os.read(self.master, 4096)
        except (BlockingIOError, OSError):
            return                      # EIO while nobody has the slave open: nothing to forward
        if data and self._writer is not None:
            self.bytes_to_sim += len(data)
            self._writer.write(data)

    async def _to_tty(self, reader: asyncio.StreamReader) -> None:
        while data := await reader.read(4096):
            self.bytes_from_sim += len(data)
            view = memoryview(data)
            while view:
                try:
                    view = view[os.write(self.master, view):]
                except BlockingIOError:
                    await asyncio.sleep(0.001)

    async def stop(self) -> None:
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().remove_reader(self.master)
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._writer is not None:
            self._writer.close()
        for fd in (self.master, self.slave):
            with contextlib.suppress(OSError):
                os.close(fd)


class _NoModemLines:
    """A pyserial port whose modem lines are fixed: a pty has none (TIOCMGET/TIOCMBIS fail),
    and pyserial's RFC 2217 port manager reads and sets them."""

    _FAKE = ("cts", "dsr", "ri", "cd", "dtr", "rts", "break_condition")

    def __init__(self, port: Any) -> None:
        object.__setattr__(self, "_port", port)
        object.__setattr__(self, "_lines", dict.fromkeys(self._FAKE, False))

    def __getattr__(self, name: str) -> Any:
        if name in self._FAKE:
            return self._lines[name]
        return getattr(self._port, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._FAKE:
            self._lines[name] = value
        else:
            setattr(self._port, name, value)


class Rfc2217Server:
    """pyserial's `rfc2217.PortManager` serving one tty on 127.0.0.1 (threads, one client)."""

    def __init__(self, tty_path: str) -> None:
        import serial

        port = serial.serial_for_url(tty_path, do_not_open=True)
        port.timeout = 0.05
        port.open()
        self.serial = _NoModemLines(port)
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._conn: socket.socket | None = None

    @classmethod
    async def start(cls, tty_path: str) -> Rfc2217Server:
        server = cls(tty_path)
        t = threading.Thread(target=server._serve, daemon=True, name="rfc2217-server")
        t.start()
        server._threads.append(t)
        return server

    def settings_seen(self) -> dict[str, Any]:
        return {"baudrate": self.serial.baudrate, "bytesize": self.serial.bytesize,
                "parity": self.serial.parity, "stopbits": self.serial.stopbits}

    def _serve(self) -> None:
        from serial import rfc2217

        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            conn.settimeout(0.2)
            self._conn = conn

            class _Conn:
                @staticmethod
                def write(data: bytes) -> None:
                    with contextlib.suppress(OSError):
                        conn.sendall(data)

            manager = rfc2217.PortManager(self.serial, _Conn())
            reader = threading.Thread(target=self._tty_to_net, args=(conn, manager), daemon=True)
            reader.start()
            self._threads.append(reader)
            while not self._stop.is_set():
                try:
                    data = conn.recv(1024)
                except TimeoutError:
                    continue
                except OSError:
                    break
                if not data:
                    break
                payload = b"".join(manager.filter(data))
                if payload:
                    self.serial.write(payload)
            with contextlib.suppress(OSError):
                conn.close()

    def _tty_to_net(self, conn: socket.socket, manager: Any) -> None:
        while not self._stop.is_set():
            try:
                data = self.serial.read(self.serial.in_waiting or 1)
            except Exception:
                return
            if data:
                with contextlib.suppress(OSError):
                    conn.sendall(b"".join(manager.escape(data)))

    async def stop(self) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):
            self._listener.close()
        if self._conn is not None:
            with contextlib.suppress(OSError):
                self._conn.close()
        for t in self._threads:
            await asyncio.to_thread(t.join, 2)
        with contextlib.suppress(Exception):
            self.serial.close()
