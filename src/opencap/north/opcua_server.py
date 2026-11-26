"""Northbound OPC UA server (PLAN.md Phase 6): the ValueCache as a read-only address space.

It replaces KEPServer for the SCADA/HMI clients that read from it today (docs/decisions.md
D-046). Off by default (`poller.yaml` -> `opcua_server.enabled`).

Address space, KEPServer style, in the namespace `namespace_uri` (registered first: ns=2)::

    Objects/<channel>/<device>/<group>/.../<tag>     ns=2;s=<channel>.<device>.<group>...<tag>
    Objects/<channel>/<device>/_System/<name>        ns=2;s=<channel>.<device>._System.<name>

A tag name's dots are its tag-group path ("LTI002.Pv" -> folder LTI002, variable Pv), as in
KEPServer, so an existing client only changes the endpoint URL.

Values: copied from the cache when a device's cache version changes (woken by every device
cycle, at the latest every `update_ms`), only for tags whose value or quality changed.
SourceTimestamp = the cache's `source_ts`. DataType follows the tag dtype (Double for scaled
tags). Quality -> StatusCode comes from `opencap.core.quality` (ADR-0006); a value that does not
fit its variable's type is sent as bad_decode.

Read-only: variables have AccessLevel CurrentRead, the user manager never grants the Admin
role, and a permission ruleset refuses Write, HistoryUpdate, Call and node management for
every session (BadUserAccessDenied).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import math
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import structlog

from opencap import FIRM_VERSION, __version__
from opencap.config.models import Device, OpcUaServerCfg, Tag
from opencap.core import quality as q
from opencap.core.cache import TagValue, ValueCache

log = structlog.get_logger("opencap.north.opcua_server")

StatusFn = Callable[[str], dict[str, Any]]

# --- pure helpers (no asyncua) -----------------------------------------------------------------

VARIANT_TYPES: dict[str, str] = {
    "bool": "Boolean", "int16": "Int16", "uint16": "UInt16", "int32": "Int32", "uint32": "UInt32",
    "int64": "Int64", "uint64": "UInt64", "float32": "Float", "float64": "Double", "string": "String",
}
_INT_RANGES: dict[str, tuple[int, int]] = {
    "Int16": (-2**15, 2**15 - 1), "UInt16": (0, 2**16 - 1), "Int32": (-2**31, 2**31 - 1),
    "UInt32": (0, 2**32 - 1), "Int64": (-2**63, 2**63 - 1), "UInt64": (0, 2**64 - 1),
}
DEFAULTS: dict[str, Any] = {"Boolean": False, "String": "", "Float": 0.0, "Double": 0.0, "DateTime": None,
                            **dict.fromkeys(_INT_RANGES, 0)}

NEVER = datetime(1601, 1, 1, tzinfo=UTC)          # OPC UA's null DateTime (encodes as 0)


def variant_type(tag: Tag) -> str:
    """OPC UA built-in type name of a tag's variable. Scaled values are always Double."""
    return "Double" if tag.scale is not None else VARIANT_TYPES[tag.dtype]


def status_code(tv: TagValue) -> int:
    """OPC UA StatusCode of a cache entry (ADR-0006). The cache keeps the last value on every
    non-good quality, so a tag without a value was never read good."""
    return q.to_opcua(tv.quality, ever_good=tv.value is not None)


def coerce(vtype: str, value: Any) -> Any:
    """A cache value as the Python value of an OPC UA `vtype` Variant (arrays element-wise).
    Raises ValueError, TypeError or OverflowError when it does not fit."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [coerce(vtype, v) for v in value]
    if vtype == "Boolean":
        if not isinstance(value, (bool, int, float)):
            raise TypeError(f"{type(value).__name__} as Boolean")
        return bool(value)
    if vtype in ("Float", "Double"):
        if isinstance(value, str):
            raise TypeError("str as a number")
        return float(value)
    if vtype == "String":
        return value if isinstance(value, str) else str(value)
    if vtype == "DateTime":
        if not isinstance(value, datetime):
            raise TypeError(f"{type(value).__name__} as DateTime")
        return value
    lo, hi = _INT_RANGES[vtype]
    if isinstance(value, str):
        raise TypeError("str as an integer")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{value} as {vtype}")
        value = round(value)
    number = int(value)
    if not lo <= number <= hi:
        raise OverflowError(f"{number} out of range for {vtype}")
    return number


def _iso(text: Any) -> datetime:
    if isinstance(text, datetime):
        return text
    if not text:
        return NEVER
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return NEVER


# `_System` diagnostics per device: (name, type, value from (device_status(), device)).
# _Error, _NoError, _AutoDemoted and _DeviceId keep KEPServer's names and meaning.
SYSTEM_VARS: tuple[tuple[str, str, Callable[[dict[str, Any], Device], Any]], ...] = (
    ("_State", "String", lambda st, d: str(st.get("state") or "offline")),
    ("_Error", "Boolean", lambda st, d: st.get("state") != "ok"),
    ("_NoError", "Boolean", lambda st, d: st.get("state") == "ok"),
    ("_AutoDemoted", "Boolean", lambda st, d: st.get("state") == "demoted"),
    ("_Connected", "Boolean", lambda st, d: bool(st.get("connected"))),
    ("_LastGood", "DateTime", lambda st, d: _iso(st.get("last_good"))),
    ("_ErrorCount", "UInt32", lambda st, d: min(int(st.get("errors") or 0), 2**32 - 1)),
    ("_LastError", "String", lambda st, d: str(st.get("last_error") or "")),
    ("_CycleMs", "Double", lambda st, d: float(st.get("cycle_ms") or 0.0)),
    ("_DeviceId", "String", lambda st, d: f"{d.host}:{d.port}"),
    ("_Driver", "String", lambda st, d: d.driver),
    ("_Enabled", "Boolean", lambda st, d: d.enabled),
)
SYSTEM = "_System"


def device_fingerprint(device: Device) -> tuple:
    """What the address space of a device depends on; a change rebuilds its nodes on reload."""
    return (device.channel, device.name, device.driver, device.host, device.port, device.enabled,
            tuple((t.name, variant_type(t)) for t in device.tags if t.enabled))


def _security_policy_type(name: str) -> str:
    """'Aes128_Sha256_RsaOaep_Sign' -> asyncua SecurityPolicyType member 'Aes128Sha256RsaOaep_Sign'."""
    if name == "None":
        return "NoSecurity"
    policy, _, mode = name.rpartition("_")
    return f"{policy.replace('_', '')}_{mode}"


# --- asyncua glue ------------------------------------------------------------------------------

@dataclass
class _Var:
    nodeid: Any
    vtype: str
    array: bool = False
    last: Any = ()                  # (value, quality) last written; () = never written


@dataclass
class _DeviceNodes:
    device: Device
    root: Any
    fingerprint: tuple
    tags: dict[str, _Var] = field(default_factory=dict)
    system: dict[str, _Var] = field(default_factory=dict)
    version: int = -1


class _UserManager:
    """Anonymous on/off plus at most one user name/password (from env vars). Never Admin."""

    def __init__(self, anonymous: bool, users: dict[str, str]) -> None:
        self.anonymous = anonymous
        self.users = users

    def get_user(self, iserver: Any, username: str | None = None, password: str | None = None,
                 certificate: Any = None) -> Any:
        from asyncua.crypto.permission_rules import User, UserRole

        # `certificate` is the secure channel's peer certificate, also for anonymous tokens; X509
        # user tokens are not offered, so asyncua rejects those before calling us.
        if username is None:
            return User(role=UserRole.User) if self.anonymous else None
        want = self.users.get(username)
        if want is None or not hmac.compare_digest(want.encode(), (password or "").encode()):
            log.info("opcua_server.login_refused", user=username)
            return None
        return User(role=UserRole.User, name=username)


def _read_only_ruleset() -> Any:
    from asyncua import ua
    from asyncua.crypto.permission_rules import PermissionRuleset

    denied = {ua.NodeId(getattr(ua.ObjectIds, f"{name}Request_Encoding_DefaultBinary")) for name in (
        "Write", "HistoryUpdate", "Call", "AddNodes", "DeleteNodes", "AddReferences", "DeleteReferences",
        "RegisterServer", "RegisterServer2",
    )}

    class ReadOnly(PermissionRuleset):
        def check_validity(self, user: Any, action_type_id: Any, body: Any) -> bool:
            return action_type_id not in denied

    return ReadOnly()


async def _stop_server(srv: Any) -> None:
    """`Server.stop()` without its up-to-1 s wait on asyncua's clock task (private, guarded)."""
    iserver = srv.iserver
    task = getattr(iserver, "time_task", None)
    if isinstance(task, asyncio.Task):
        iserver._time_task_stop = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        iserver.time_task = None
    await srv.stop()
    source = getattr(getattr(getattr(iserver, "aspace", None), "_nodes", None), "source", None)
    if source is not None:               # a standard-address-space shelf (tests): asyncua never closes it
        with contextlib.suppress(Exception):
            source.close()


class OpcUaNorth:
    """The northbound OPC UA server. `start()` -> listening; `stop()` -> closed."""

    def __init__(self, cfg: OpcUaServerCfg, devices: Iterable[Device], cache: ValueCache,
                 status_fn: StatusFn, *, shelf_file: Path | None = None) -> None:
        self.cfg = cfg
        self.cache = cache
        self.status_fn = status_fn
        self._shelf = shelf_file
        self._devices: dict[str, Device] = {d.id: d for d in devices}
        self._server: Any = None
        self._task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        self._dirty: set[str] = set()              # devices that finished a cycle since the last copy
        self._lock = asyncio.Lock()
        self._ns = 0
        self._nodes: dict[str, _DeviceNodes] = {}
        self._channels: dict[str, Any] = {}

    # --- lifecycle ---

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def namespace_index(self) -> int:
        return self._ns

    def node_id(self, device_id: str, name: str) -> str:
        """NodeId text of a tag (or `_System.<name>`) of a device, e.g. 'ns=2;s=SITE.PLC.LTI002.Pv'."""
        d = self._devices[device_id]
        return f"ns={self._ns or 2};s={d.channel}.{d.name}.{name}"

    async def start(self) -> None:
        from asyncua import Server, ua

        cfg = self.cfg
        users = self._users()
        if not cfg.anonymous and not users:
            raise RuntimeError("opcua_server: anonymous is off and no user is configured "
                               "(set username_env/password_env and those environment variables)")
        srv = Server(user_manager=_UserManager(cfg.anonymous, users))
        await srv.init(shelf_file=self._shelf)
        srv.set_endpoint(cfg.endpoint)
        srv.set_server_name(cfg.server_name)
        srv.product_uri = "urn:opencap"
        srv.manufacturer_name = "opencap"
        await srv.set_application_uri(cfg.application_uri)
        await srv.set_build_info("urn:opencap", "opencap contributors", "opencap", __version__, FIRM_VERSION,
                                 datetime.now(UTC))
        srv.set_security_policy([getattr(ua.SecurityPolicyType, _security_policy_type(p))
                                 for p in cfg.security_policies], permission_ruleset=_read_only_ruleset())
        tokens: list[type] = []
        if cfg.anonymous:
            tokens.append(ua.AnonymousIdentityToken)
        if users:
            tokens.append(ua.UserNameIdentityToken)
        srv.set_identity_tokens(tokens)
        if any(p != "None" for p in cfg.security_policies):
            await self._load_certificate(srv)
        if cfg.trusted_certs_dir:
            await self._trust(srv, Path(cfg.trusted_certs_dir))
        self._ns = await srv.register_namespace(cfg.namespace_uri)
        if self._ns != 2:     # pragma: no cover - asyncua layout change
            log.warning("opcua_server.namespace_index", index=self._ns, expected=2)
        self._server = srv
        try:
            for dev in self._devices.values():
                await self._add_device(dev)
            await self._sync()                       # every node has its value before clients connect
            await srv.start()
        except BaseException:
            self._server = None
            with contextlib.suppress(Exception):
                await _stop_server(srv)
            raise
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="opcua-server")
        host = urlsplit(cfg.endpoint).hostname or ""
        log.info("opcua_server.started", endpoint=cfg.endpoint, devices=len(self._nodes),
                 tags=sum(len(n.tags) for n in self._nodes.values()), namespace=self._ns,
                 policies=cfg.security_policies, anonymous=cfg.anonymous, users=len(users))
        if "None" in cfg.security_policies and host not in ("127.0.0.1", "localhost", "::1"):
            log.warning("opcua_server.unencrypted_endpoint", endpoint=cfg.endpoint,
                        hint="bind to localhost or the VPN only, or offer only Sign/SignAndEncrypt")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        srv, self._server = self._server, None
        self._nodes.clear()
        self._channels.clear()
        if srv is not None:
            with contextlib.suppress(Exception):
                await _stop_server(srv)
            log.info("opcua_server.stopped")

    def notify(self, device_id: str) -> None:
        """A device finished a cycle (hooked to ChannelWorker.on_cycle): copy its changes now."""
        if self._wake is not None:
            self._dirty.add(device_id)
            self._wake.set()

    async def set_devices(self, devices: Iterable[Device]) -> dict[str, list[str]]:
        """Config reload: add new devices, remove dropped ones, rebuild changed ones."""
        wanted = {d.id: d for d in devices}
        changed: dict[str, list[str]] = {"added": [], "removed": [], "rebuilt": [], "unchanged": []}
        async with self._lock:
            self._devices = wanted
            if self._server is None:
                return changed
            for dev_id in [i for i in self._nodes if i not in wanted]:
                await self._remove_device(dev_id)
                changed["removed"].append(dev_id)
            for dev_id, dev in wanted.items():
                nodes = self._nodes.get(dev_id)
                if nodes is not None and nodes.fingerprint == device_fingerprint(dev):
                    nodes.device = dev
                    changed["unchanged"].append(dev_id)
                    continue
                if nodes is not None:
                    await self._remove_device(dev_id)
                    changed["rebuilt"].append(dev_id)
                else:
                    changed["added"].append(dev_id)
                await self._add_device(dev)
            await self._sync()
        if changed["added"] or changed["removed"] or changed["rebuilt"]:
            log.info("opcua_server.devices_changed", **{k: len(v) for k, v in changed.items()})
        return changed

    # --- set-up ---

    def _users(self) -> dict[str, str]:
        cfg = self.cfg
        if not cfg.username_env:
            return {}
        user, password = os.environ.get(cfg.username_env), os.environ.get(cfg.password_env or "")
        if not user or not password:
            log.warning("opcua_server.user_not_set", username_env=cfg.username_env, password_env=cfg.password_env)
            return {}
        return {user: password}

    async def _load_certificate(self, srv: Any) -> None:
        from asyncua.crypto import cert_gen
        from cryptography.x509.oid import ExtendedKeyUsageOID

        cert, key = Path(self.cfg.certificate or ""), Path(self.cfg.private_key or "")
        if not cert.exists() and not key.exists():
            cert.parent.mkdir(parents=True, exist_ok=True)
            key.parent.mkdir(parents=True, exist_ok=True)
            host = urlsplit(self.cfg.endpoint).hostname or "localhost"
            await cert_gen.setup_self_signed_certificate(
                key, cert, self.cfg.application_uri, host,
                [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH], {})
            with contextlib.suppress(OSError):
                key.chmod(0o600)
            log.info("opcua_server.certificate_created", certificate=str(cert),
                     hint="clients must trust this certificate")
        await srv.load_certificate(str(cert))
        await srv.load_private_key(str(key))

    async def _trust(self, srv: Any, directory: Path) -> None:
        from asyncua.crypto.truststore import TrustStore
        from asyncua.crypto.validator import CertificateValidator, CertificateValidatorOptions

        store = TrustStore([directory], [])
        await store.load()
        opts = CertificateValidatorOptions.TRUSTED_VALIDATION | CertificateValidatorOptions.PEER_CLIENT
        srv.set_certificate_validator(CertificateValidator(opts, store))

    async def _channel_node(self, channel: str) -> Any:
        from asyncua import ua

        node = self._channels.get(channel)
        if node is None:
            node = await self._server.nodes.objects.add_folder(ua.NodeId(channel, self._ns),
                                                               ua.QualifiedName(channel, self._ns))
            self._channels[channel] = node
        return node

    async def _add_device(self, dev: Device) -> None:
        from asyncua import ua

        ns = self._ns
        try:
            channel = await self._channel_node(dev.channel)
            base = f"{dev.channel}.{dev.name}"
            root = await channel.add_folder(ua.NodeId(base, ns), ua.QualifiedName(dev.name, ns))
        except ua.UaError as exc:
            log.warning("opcua_server.device_skipped", device=dev.id, error=str(exc))
            return
        nodes = _DeviceNodes(device=dev, root=root, fingerprint=device_fingerprint(dev))
        containers: dict[str, Any] = {base: root}
        for tag in sorted((t for t in dev.tags if t.enabled), key=lambda t: t.name.count(".")):
            parts = tag.name.split(".")
            path, parent = base, root
            try:
                for part in parts[:-1]:
                    path = f"{path}.{part}"
                    node = containers.get(path)
                    if node is None:
                        node = await parent.add_folder(ua.NodeId(path, ns), ua.QualifiedName(part, ns))
                        containers[path] = node
                    parent = node
                vtype = variant_type(tag)
                path = f"{path}.{parts[-1]}"
                var = await parent.add_variable(ua.NodeId(path, ns), ua.QualifiedName(parts[-1], ns),
                                                ua.Variant(DEFAULTS[vtype], getattr(ua.VariantType, vtype)))
            except ua.UaError as exc:
                log.warning("opcua_server.tag_skipped", device=dev.id, tag=tag.name, error=str(exc))
                continue
            containers[path] = var
            nodes.tags[tag.name] = _Var(var.nodeid, vtype)
        system = await root.add_folder(ua.NodeId(f"{base}.{SYSTEM}", ns), ua.QualifiedName(SYSTEM, ns))
        for name, vtype, _get in SYSTEM_VARS:
            var = await system.add_variable(ua.NodeId(f"{base}.{SYSTEM}.{name}", ns), ua.QualifiedName(name, ns),
                                            ua.Variant(DEFAULTS[vtype] if vtype != "DateTime" else NEVER,
                                                       getattr(ua.VariantType, vtype)))
            nodes.system[name] = _Var(var.nodeid, vtype)
        self._nodes[dev.id] = nodes

    async def _remove_device(self, dev_id: str) -> None:
        nodes = self._nodes.pop(dev_id, None)
        if nodes is None:
            return
        await self._server.delete_nodes([nodes.root], recursive=True)
        channel = nodes.device.channel
        if not any(n.device.channel == channel for n in self._nodes.values()):
            folder = self._channels.pop(channel, None)
            if folder is not None:
                await self._server.delete_nodes([folder], recursive=True)

    # --- values ---

    async def _run(self) -> None:
        assert self._wake is not None
        period = self.cfg.update_ms / 1000
        loop = asyncio.get_running_loop()
        next_full = loop.time() + period
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), max(0.0, next_full - loop.time()))
            self._wake.clear()
            only: set[str] | None = None
            if loop.time() < next_full:
                only, self._dirty = self._dirty, set()    # a cycle ended: just those devices
            else:
                next_full = loop.time() + period          # every update_ms: everything (stale
                self._dirty.clear()                       # marking, health changes)
            try:
                async with self._lock:
                    await self._sync(only)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("opcua_server.update_error")
                await asyncio.sleep(1.0)

    async def _sync(self, only: set[str] | None = None) -> None:
        """Copy changed tag values and diagnostics into the address space (all devices, or `only`)."""
        for dev_id, nodes in list(self._nodes.items()):
            if only is not None and dev_id not in only:
                continue
            version = self.cache.version(dev_id)
            if version != nodes.version:
                nodes.version = version
                snap = self.cache.snapshot(dev_id)
                for name, var in nodes.tags.items():
                    tv = snap.get(name)
                    if tv is not None and var.last != (tv.value, tv.quality):
                        var.last = (tv.value, tv.quality)
                        await self._write_tag(var, tv)
            try:
                status = self.status_fn(dev_id)
            except Exception:
                status = {"state": "offline"}
            for name, vtype, get in SYSTEM_VARS:
                var = nodes.system[name]
                value = get(status, nodes.device)
                if var.last != value:
                    var.last = value
                    await self._write(var, value, q.to_opcua("good", ever_good=True), None)

    async def _write_tag(self, var: _Var, tv: TagValue) -> None:
        code = status_code(tv)
        try:
            value = coerce(var.vtype, tv.value)
        except (ValueError, TypeError, OverflowError):
            value, code = None, q.to_opcua("bad_decode", ever_good=tv.value is not None)
        await self._write(var, value, code, tv.source_ts)

    async def _write(self, var: _Var, value: Any, code: int, source_ts: datetime | None) -> None:
        from asyncua import ua

        if value is None:
            variant = ua.Variant()
        else:
            is_array = isinstance(value, list)
            if is_array and not var.array:
                var.array = True
                await self._server.write_attribute_value(
                    var.nodeid, ua.DataValue(ua.Variant(ua.ValueRank.OneDimension.value, ua.VariantType.Int32)),
                    ua.AttributeIds.ValueRank)
            variant = ua.Variant(value, getattr(ua.VariantType, var.vtype), is_array=is_array)
        now = datetime.now(UTC)
        dv = ua.DataValue(variant, StatusCode=ua.StatusCode(code), SourceTimestamp=source_ts or now,
                          ServerTimestamp=now)
        await self._server.write_attribute_value(var.nodeid, dv)
