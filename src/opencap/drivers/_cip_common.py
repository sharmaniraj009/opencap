"""Helpers shared by the two pycomm3 drivers (`ab_logix`, `ab_pccc`).

Nothing here imports pycomm3 at module level (D-002): the socket class is built lazily.

- `oo_socket_class()`: a pycomm3 `Socket` whose `receive()` reads exactly one encapsulation
  message and fails on a peer close (pycomm3's own reader loops forever when the PLC closes
  in the middle of a reply, and may read past the end of one message).
- Exception-chain helpers: pycomm3 wraps socket errors twice (`CommError` from `CommError`
  from `OSError`), and sometimes once more in `ResponseError`.
- `Attempt`: hand-over between a `connect()` coroutine and its executor job, so a cancelled
  connect (the worker's safety net) closes what the job opened instead of leaking it.
- `WRITE_SERVICES`: CIP services the drivers must never send.
"""

from __future__ import annotations

import functools
import struct
import threading
from typing import Any

__all__ = [
    "WRITE_SERVICES", "Attempt", "describe_exc", "exc_chain", "is_comm_error", "is_timeout",
    "oo_socket_class",
]

# Set_Attributes_All (0x02), Set_Attribute_List (0x04), Set_Attribute_Single (0x10),
# Write Tag (0x4D), Write Tag Fragmented (0x53). (0x4E is Forward Close on the Connection
# Manager, so it cannot be refused by code alone.)
WRITE_SERVICES = frozenset({0x02, 0x04, 0x10, 0x4D, 0x53})


def exc_chain(exc: BaseException) -> list[BaseException]:
    """`exc` and its causes/contexts, outermost first (at most 8, no cycles)."""
    out: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and cur not in out and len(out) < 8:
        out.append(cur)
        cur = cur.__cause__ or cur.__context__
    return out


def is_timeout(exc: BaseException) -> bool:
    """True when a pycomm3 CommError was caused by a socket timeout (no reply in time)."""
    return any(isinstance(e, TimeoutError) for e in exc_chain(exc))


def describe_exc(exc: BaseException) -> str:
    """'outer: inner: root' from the exception chain (pycomm3 wraps socket errors twice)."""
    parts: list[str] = []
    for e in exc_chain(exc):
        text = str(e) or type(e).__name__
        if not any(text in p for p in parts):
            parts.append(text)
    return ": ".join(parts)


def is_comm_error(exc: BaseException) -> bool:
    """True when a pycomm3 CommError (socket-level failure) is anywhere in the chain:
    pycomm3 wraps it in ResponseError during the tag-list upload."""
    from pycomm3.exceptions import CommError as PycommCommError
    return any(isinstance(e, PycommCommError) for e in exc_chain(exc))


@functools.cache
def oo_socket_class() -> type:
    """pycomm3 `Socket` subclass: one encapsulation message per `receive()`, fast failure on
    a peer close, every OSError (timeouts included) raised as pycomm3 `CommError`."""
    from pycomm3.const import HEADER_SIZE
    from pycomm3.exceptions import CommError as PycommCommError
    from pycomm3.socket_ import Socket

    class OoSocket(Socket):
        def receive(self, timeout: float = 0) -> bytes:
            try:
                if timeout != 0:
                    self.sock.settimeout(timeout)
                header = self._recv_exact(HEADER_SIZE)
                (length,) = struct.unpack_from("<H", header, 2)
                return header + self._recv_exact(length)
            except OSError as err:     # socket.error; includes timeouts and our peer-close error
                raise PycommCommError("socket connection broken") from err

        def _recv_exact(self, n: int) -> bytes:
            buf = bytearray()
            while len(buf) < n:
                chunk = self.sock.recv(n - len(buf))
                if not chunk:
                    raise ConnectionResetError("connection closed by the PLC")
                buf += chunk
            return bytes(buf)

    return OoSocket


class Attempt:
    """Hand-over between a connect() coroutine and its executor job. If the coroutine is
    cancelled (the worker's safety net), the job closes what it opened instead of leaking a
    CIP connection; work already cached by the driver (tag list, file directory) is kept."""

    __slots__ = ("lock", "abandoned", "result")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.abandoned = False
        self.result: Any = None

    def abandon(self) -> Any:
        with self.lock:
            self.abandoned = True
            result, self.result = self.result, None
            return result

    def finish(self, session: Any) -> bool:
        with self.lock:
            if self.abandoned:
                return False
            self.result = session
            return True
