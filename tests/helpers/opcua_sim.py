"""In-process OPC UA server for tests: a real asyncua server, no mocked wire.

Quick start::

    from tests.helpers.opcua_sim import OpcUaSim

    sim = await OpcUaSim.start({"LTI002_PV": ("Double", 1.5), "RUN": ("Boolean", True)})
    sim.endpoint                  # "opc.tcp://127.0.0.1:<port>/sim"
    sim.node_id("LTI002_PV")      # "ns=7;s=LTI002_PV" (namespace 7, like the real KEPServer project)
    await sim.set("LTI002_PV", 2.5)                            # new value, Good, SourceTimestamp now
    await sim.set("LTI002_PV", 2.5, status="UncertainSensorNotAccurate", source_ts=dt)
    await sim.stop(); await sim.restart()                      # power cycle on the SAME port
    await sim.close()                                          # stop for good

Nodes are ``ns=<namespace>;s=<name>`` variables under ``Objects``, read-only for clients
(asyncua's default access level). A spec is ``(variant type name, value)``; ``"Double[]"``
makes an array. Values and status codes survive ``restart()``.

Counters (client sessions only, not the server's own internal reads):
``reads`` (Read service calls), ``read_nodes`` (nodes in them), ``tag_reads`` (nodes of the
sim's own namespace read: polling; a client's keep-alive probe of ServerState is not), ``write_attempts`` (Write
service calls that reached the server), ``monitored_items`` (monitored items created),
``sessions`` (sessions activated). ``reset_counters()`` zeroes them.

Security: ``policies`` takes asyncua ``SecurityPolicyType`` names (default ``["NoSecurity"]``).
A non-None policy needs a server certificate: one is generated once per process (or pass
``cert_dir``). ``users={"name": "password"}`` enables username login; ``anonymous=False``
refuses anonymous sessions. Client certificates are not validated (asyncua's default).

Standard address space: the first server of the process builds asyncua's address-space
shelf in a private temp directory (~3 s); every later server loads it in milliseconds.
"""

from __future__ import annotations

import asyncio
import contextlib
import atexit
import hmac
import logging
import shutil
import tempfile
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from asyncua import Server, ua
from asyncua.common.callback import CallbackType
from asyncua.crypto import cert_gen
from asyncua.crypto.permission_rules import User, UserRole
from cryptography.x509.oid import ExtendedKeyUsageOID

from tests.helpers.netcut import stable_port

__all__ = ["OpcUaSim", "make_cert", "shelf_file"]

SIM_HOST = "127.0.0.1"
SIM_PATH = "/sim"
SIM_APP_URI = "urn:opencap:test:sim"

logging.getLogger("asyncua").setLevel(logging.ERROR)

_shelf_locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()   # one lock per event loop
_shelf: Path | None = None
_shelf_failed = False
_tmp_root: Path | None = None


def _tmp() -> Path:
    global _tmp_root
    if _tmp_root is None:
        _tmp_root = Path(tempfile.mkdtemp(prefix="opencap-ua-"))
    return _tmp_root


async def shelf_file() -> Path | None:
    """Path of this process's standard-address-space shelf (built on first use), or None if
    the dbm backend writes a file asyncua cannot find again (then every init rebuilds)."""
    global _shelf, _shelf_failed
    lock = _shelf_locks.setdefault(asyncio.get_running_loop(), asyncio.Lock())
    async with lock:
        if _shelf is None and not _shelf_failed:
            path = _tmp() / "aspace"
            await Server().init(shelf_file=path)
            if path.is_file() or path.with_suffix(".db").is_file():
                _shelf = path
            else:
                _shelf_failed = True
        return _shelf


async def make_cert(directory: Path, app_uri: str, *, name: str = "cert",
                          server: bool = False) -> tuple[Path, Path]:
    """Self-signed (cert.der, key.pem) for `app_uri` with host name localhost."""
    directory.mkdir(parents=True, exist_ok=True)
    cert, key = directory / f"{name}.der", directory / f"{name}.pem"
    usage = [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH] if server else \
        [ExtendedKeyUsageOID.CLIENT_AUTH]
    await cert_gen.setup_self_signed_certificate(key, cert, app_uri, "localhost", usage, {})
    return cert, key


async def fast_stop(srv: Server) -> None:
    """`Server.stop()` without its up-to-1 s wait: asyncua awaits its once-a-second clock task
    through a `sleep(1)`; cancel that task first (private attribute, guarded)."""
    iserver = srv.iserver
    task = getattr(iserver, "time_task", None)
    if task is not None:
        iserver._time_task_stop = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        iserver.time_task = None
    await srv.stop()
    source = getattr(getattr(getattr(iserver, "aspace", None), "_nodes", None), "source", None)
    if source is not None:               # a standard-address-space shelf (tests): asyncua never closes it
        with contextlib.suppress(Exception):
            source.close()


@dataclass
class _Node:
    vtype: ua.VariantType
    is_array: bool
    value: Any
    status: int = 0
    source_ts: datetime | None = None


class _Users:
    """asyncua UserManager: anonymous on/off plus a fixed username/password table."""

    def __init__(self, users: dict[str, str], anonymous: bool) -> None:
        self.users = dict(users)
        self.anonymous = anonymous
        self.logins: list[str | None] = []

    def get_user(self, iserver: Any, username: str | None = None, password: str | None = None,
                 certificate: Any = None) -> User | None:
        if username is None:
            if not self.anonymous:
                return None
            self.logins.append(None)
            return User(role=UserRole.User)
        want = self.users.get(username)
        if want is None or not hmac.compare_digest(want.encode(), (password or "").encode()):
            return None
        self.logins.append(username)
        return User(role=UserRole.User, name=username)


class OpcUaSim:
    def __init__(self, nodes: dict[str, tuple[str, Any]], *, namespace: int, port: int,
                 policies: list[str], users: dict[str, str], anonymous: bool,
                 cert_dir: Path | None) -> None:
        if namespace < 2:
            raise ValueError("namespace must be >= 2 (0 and 1 belong to the server)")
        self.namespace = namespace
        self.port = port
        self.policies = list(policies)
        self.user_manager = _Users(users, anonymous)
        self.cert_dir = cert_dir
        self.nodes: dict[str, _Node] = {}
        for name, (spec, value) in nodes.items():
            self.nodes[name] = self._make_node(spec, value)
        self.server: Server | None = None
        self.cert: Path | None = None
        self.key: Path | None = None
        self.generation = 0
        self.reset_counters()

    # --- lifecycle -----------------------------------------------------------------------

    @classmethod
    async def start(cls, nodes: dict[str, tuple[str, Any]], *, namespace: int = 7, port: int | None = None,
                    policies: list[str] | None = None, users: dict[str, str] | None = None,
                    anonymous: bool = True, cert_dir: Path | None = None) -> OpcUaSim:
        sim = cls(nodes, namespace=namespace, port=port or stable_port(SIM_HOST),
                  policies=policies or ["NoSecurity"], users=users or {}, anonymous=anonymous,
                  cert_dir=cert_dir)
        await sim._start()
        return sim

    @property
    def endpoint(self) -> str:
        return f"opc.tcp://{SIM_HOST}:{self.port}{SIM_PATH}"

    @property
    def running(self) -> bool:
        return self.server is not None

    def node_id(self, name: str) -> str:
        return f"ns={self.namespace};s={name}"

    async def _start(self) -> None:
        if self.server is not None:
            return
        srv = Server(user_manager=self.user_manager)
        await srv.init(shelf_file=await shelf_file())
        srv.set_endpoint(self.endpoint)
        srv.set_server_name("opencap test sim")
        await srv.set_application_uri(SIM_APP_URI)
        srv.set_security_policy([getattr(ua.SecurityPolicyType, p) for p in self.policies])
        tokens: list[type] = [ua.UserNameIdentityToken] if self.user_manager.users else []
        if self.user_manager.anonymous:
            tokens.insert(0, ua.AnonymousIdentityToken)
        srv.set_identity_tokens(tokens)
        if any(p != "NoSecurity" for p in self.policies):
            if self.cert is None:
                self.cert, self.key = await make_cert(self.cert_dir or _tmp() / "sim-pki", SIM_APP_URI,
                                                            name="sim", server=True)
            await srv.load_certificate(str(self.cert))
            await srv.load_private_key(str(self.key))
        idx = 0
        while idx < self.namespace:
            idx = await srv.register_namespace(f"urn:opencap:test:ns{len(await srv.get_namespace_array())}")
        objects = srv.nodes.objects
        for name, node in self.nodes.items():
            var = await objects.add_variable(ua.NodeId(name, self.namespace), name,
                                             ua.Variant(node.value, node.vtype, is_array=node.is_array))
            del var
        srv.subscribe_server_callback(CallbackType.PreRead, self._on_read)
        srv.subscribe_server_callback(CallbackType.PreWrite, self._on_write)
        srv.subscribe_server_callback(CallbackType.ItemSubscriptionCreated, self._on_items)
        await srv.start()
        self.server = srv
        self.generation += 1
        for name in self.nodes:
            await self._push(name)

    async def stop(self) -> None:
        """Power off: the listening socket and every session go away. Values are kept."""
        srv, self.server = self.server, None
        if srv is not None:
            await fast_stop(srv)

    async def restart(self, down_s: float = 0.0) -> None:
        await self.stop()
        if down_s:
            await asyncio.sleep(down_s)
        await self._start()

    async def close(self) -> None:
        await self.stop()

    async def __aenter__(self) -> OpcUaSim:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # --- values --------------------------------------------------------------------------

    async def set(self, name: str, value: Any = None, *, status: str | int = "Good",
                  source_ts: datetime | None = None) -> None:
        """Change a node. `value=None` keeps the current value (e.g. a status-only change)."""
        node = self.nodes[name]
        if value is not None:
            node.value = value
        node.status = status if isinstance(status, int) else getattr(ua.StatusCodes, status)
        node.source_ts = source_ts
        await self._push(name)

    def value(self, name: str) -> Any:
        return self.nodes[name].value

    async def add(self, name: str, spec: str, value: Any) -> None:
        """Add a node while running (and on every later restart)."""
        self.nodes[name] = node = self._make_node(spec, value)
        if self.server is not None:
            await self.server.nodes.objects.add_variable(
                ua.NodeId(name, self.namespace), name, ua.Variant(node.value, node.vtype, is_array=node.is_array))
            await self._push(name)

    async def _push(self, name: str) -> None:
        if self.server is None:
            return
        node = self.nodes[name]
        dv = ua.DataValue(
            ua.Variant(node.value, node.vtype, is_array=node.is_array),
            StatusCode=ua.StatusCode(node.status),
            SourceTimestamp=node.source_ts or datetime.now(UTC),
            ServerTimestamp=datetime.now(UTC),
        )
        await self.server.write_attribute_value(ua.NodeId(name, self.namespace), dv)

    @staticmethod
    def _make_node(spec: str, value: Any) -> _Node:
        is_array = spec.endswith("[]")
        vtype = getattr(ua.VariantType, spec.removesuffix("[]"))
        return _Node(vtype, is_array, value)

    # --- counters ------------------------------------------------------------------------

    def reset_counters(self) -> None:
        self.reads = 0
        self.read_nodes = 0
        self.tag_reads = 0
        self.write_attempts = 0
        self.monitored_items = 0

    @property
    def sessions(self) -> int:
        return len(self.user_manager.logins)

    def _on_read(self, event: Any, _dispatcher: Any) -> None:
        if event.is_external:
            self.reads += 1
            nodes = event.request_params.NodesToRead
            self.read_nodes += len(nodes)
            self.tag_reads += sum(1 for n in nodes if n.NodeId.NamespaceIndex == self.namespace)

    def _on_write(self, event: Any, _dispatcher: Any) -> None:
        if event.is_external:
            self.write_attempts += 1

    def _on_items(self, event: Any, _dispatcher: Any) -> None:
        if event.is_external:
            self.monitored_items += len(event.request_params.ItemsToCreate)


def _cleanup() -> None:  # pragma: no cover - interpreter exit
    if _tmp_root is not None:
        shutil.rmtree(_tmp_root, ignore_errors=True)


atexit.register(_cleanup)
