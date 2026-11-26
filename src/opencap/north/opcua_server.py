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
import socket
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


# Drivers whose address syntax has array dimensions (`[n]`, `[r][c]`), and their pure parser.
# Elsewhere brackets mean something else (Logix `Tag[3]` is one element), so no generic regex.
_ARRAY_PARSERS: dict[str, tuple[str, str]] = {
    "modbus_tcp": ("opencap.drivers.modbus_tcp", "parse_address"),
    "modbus_rtu": ("opencap.drivers.modbus_tcp", "parse_address"),
    "s7": ("opencap.drivers.s7", "parse_s7_address"),
    "melsec_mc": ("opencap.drivers.melsec_mc", "parse_address"),
}


def array_dims(device: Device, tag: Tag) -> tuple[int, ...]:
    """Array dimensions of a tag's value from its address, () for a scalar (or an address the
    driver refuses: that tag is bad_config and never gets a list value)."""
    where = _ARRAY_PARSERS.get(device.driver)
    if where is None or "[" not in tag.address:
        return ()
    import importlib

    try:
        parse = getattr(importlib.import_module(where[0]), where[1])
        return tuple(parse(tag.address, tag.dtype, device.params).dims)
    except (ValueError, ImportError, AttributeError):
        return ()


UNECE_NAMESPACE = "http://www.opcfoundation.org/UA/units/un/cefact"
# UNECE Recommendation 20 common codes for frequent units (OPC UA Part 8, EUInformation).
UNECE_CODES: dict[str, str] = {
    "%": "P1", "°C": "CEL", "degC": "CEL", "°F": "FAH", "degF": "FAH", "K": "KEL",
    "bar": "BAR", "mbar": "MBR", "Pa": "PAL", "kPa": "KPA", "MPa": "MPA", "psi": "PS",
    "mm": "MMT", "cm": "CMT", "m": "MTR", "km": "KMT", "m/s": "MTS", "m3": "MTQ", "m³": "MTQ", "m3/h": "MQH",
    "m³/h": "MQH", "l": "LTR", "L": "LTR", "kg": "KGM", "t": "TNE", "g": "GRM",
    "A": "AMP", "mA": "4K", "V": "VLT", "mV": "2Z", "kV": "KVT", "W": "WTT", "kW": "KWT", "MW": "MAW",
    "Wh": "WHR", "kWh": "KWH", "MWh": "MWH", "Hz": "HTZ", "rpm": "RPM", "s": "SEC", "min": "MIN", "h": "HUR",
}


def unece_unit_id(code: str) -> int:
    """OPC UA UnitId of a UNECE common code: the ASCII bytes packed big-endian (Part 8 5.6.3)."""
    value = 0
    for ch in code:
        value = (value << 8) | ord(ch)
    return value


def eu_information(units: str) -> tuple[str, int, str]:
    """(NamespaceUri, UnitId, DisplayName) for a tag's `scale.units`; UnitId -1 when unknown."""
    code = UNECE_CODES.get(units.strip())
    return UNECE_NAMESPACE, (unece_unit_id(code) if code else -1), units


def eu_range(tag: Tag) -> tuple[float, float] | None:
    """(low, high) engineering range of a scaled tag, None when it is not scaled."""
    if tag.scale is None:
        return None
    lo, hi = sorted((float(tag.scale.eng_lo), float(tag.scale.eng_hi)))
    return lo, hi


def device_fingerprint(device: Device) -> tuple:
    """What the address space of a device depends on; a change rebuilds its nodes on reload."""
    return (device.channel, device.name, device.driver, device.host, device.port, device.enabled,
            tuple((t.name, variant_type(t), array_dims(device, t), eu_range(t),
                   t.scale.units if t.scale else None, t.description) for t in device.tags if t.enabled))


def _security_policy_type(name: str) -> str:
    """'Aes128_Sha256_RsaOaep_Sign' -> asyncua SecurityPolicyType member 'Aes128Sha256RsaOaep_Sign'."""
    if name == "None":
        return "NoSecurity"
    policy, _, mode = name.rpartition("_")
    return f"{policy.replace('_', '')}_{mode}"


# --- asyncua glue ------------------------------------------------------------------------------

def _default_value(vtype: str, dims: tuple[int, ...]) -> Any:
    one = DEFAULTS[vtype] if vtype != "DateTime" else NEVER
    if not dims:
        return one
    if len(dims) == 1:
        return [one] * dims[0]
    return [[one] * dims[1] for _ in range(dims[0])]


def _folder_item(nodeid: Any, qname: Any, parent: Any) -> Any:
    from asyncua import ua

    item = ua.AddNodesItem(RequestedNewNodeId=nodeid, BrowseName=qname, ParentNodeId=parent,
                           ReferenceTypeId=ua.NodeId(ua.ObjectIds.Organizes), NodeClass=ua.NodeClass.Object,
                           TypeDefinition=ua.NodeId(ua.ObjectIds.FolderType))
    attrs = ua.ObjectAttributes()
    attrs.DisplayName = ua.LocalizedText(qname.Name)
    attrs.Description = ua.LocalizedText(qname.Name)
    attrs.EventNotifier = 0
    item.NodeAttributes = attrs
    return item


def _variable_item(nodeid: Any, qname: Any, parent: Any, vtype: str, dims: tuple[int, ...],
                   description: str | None, *, analog: bool = False) -> Any:
    """A read-only variable: CurrentRead only (ADR-0008), ValueRank/ArrayDimensions from `dims`."""
    from asyncua import ua

    variant_type = getattr(ua.VariantType, vtype)
    item = ua.AddNodesItem(RequestedNewNodeId=nodeid, BrowseName=qname, ParentNodeId=parent,
                           ReferenceTypeId=ua.NodeId(ua.ObjectIds.HasComponent), NodeClass=ua.NodeClass.Variable,
                           TypeDefinition=ua.NodeId(ua.ObjectIds.AnalogItemType if analog
                                                    else ua.ObjectIds.BaseDataVariableType))
    attrs = ua.VariableAttributes()
    attrs.DisplayName = ua.LocalizedText(qname.Name)
    attrs.Description = ua.LocalizedText(description) if description else ua.LocalizedText()
    attrs.DataType = ua.NodeId(variant_type.value)
    attrs.Value = ua.Variant(_default_value(vtype, dims), variant_type, is_array=bool(dims))
    attrs.ValueRank = len(dims) if dims else ua.ValueRank.Scalar
    attrs.ArrayDimensions = list(dims) if dims else None
    attrs.AccessLevel = attrs.UserAccessLevel = ua.AccessLevel.CurrentRead.mask
    attrs.WriteMask = attrs.UserWriteMask = 0
    attrs.Historizing = False
    attrs.MinimumSamplingInterval = 0.0
    item.NodeAttributes = attrs
    return item


def _property_item(nodeid: Any, name: str, parent: Any, value: Any, datatype: Any) -> Any:
    from asyncua import ua

    item = ua.AddNodesItem(RequestedNewNodeId=nodeid, BrowseName=ua.QualifiedName(name, 0), ParentNodeId=parent,
                           ReferenceTypeId=ua.NodeId(ua.ObjectIds.HasProperty), NodeClass=ua.NodeClass.Variable,
                           TypeDefinition=ua.NodeId(ua.ObjectIds.PropertyType))
    attrs = ua.VariableAttributes()
    attrs.DisplayName = ua.LocalizedText(name)
    attrs.DataType = datatype
    attrs.Value = value
    attrs.ValueRank = ua.ValueRank.Scalar
    attrs.AccessLevel = attrs.UserAccessLevel = ua.AccessLevel.CurrentRead.mask
    item.NodeAttributes = attrs
    return item


@dataclass
class _Var:
    nodeid: Any
    vtype: str
    array: bool = False             # ValueRank >= 1 (from the address, or at the first list value)
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
    """Anonymous on/off, user names/passwords (from env vars), X.509 user certificates trusted in
    `<pki_dir>/users/`. Never grants the Admin role."""

    def __init__(self, anonymous: bool, users: dict[str, str], pki: Any = None, x509_users: bool = False) -> None:
        self.anonymous = anonymous
        self.users = users
        self.pki = pki
        self.x509_users = x509_users
        self.state: Any = None          # opcua_services.ServerState: which token is being activated

    def get_user(self, iserver: Any, username: str | None = None, password: str | None = None,
                 certificate: Any = None) -> Any:
        from asyncua import ua
        from asyncua.crypto.permission_rules import User, UserRole

        # For an anonymous token `certificate` is the secure channel's peer certificate, for an
        # X.509 token the user certificate (signature already verified by asyncua): only the token
        # kind, recorded by our session, tells them apart.
        kind = self.state.token_kind if self.state is not None else None
        if kind is ua.X509IdentityToken:
            name = self.pki.user_name(bytes(certificate or b"")) if (self.x509_users and self.pki) else None
            if name is None:
                log.info("opcua_server.login_refused", user="x509")
                return None
            return User(role=UserRole.User, name=name)
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

    # Write is not refused here: the session answers it per node with Bad_NotWritable
    # (opcua_services._Session.write) and writes nothing.
    denied = {ua.NodeId(getattr(ua.ObjectIds, f"{name}Request_Encoding_DefaultBinary")) for name in (
        "HistoryUpdate", "Call", "AddNodes", "DeleteNodes", "AddReferences", "DeleteReferences",
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
        self._state: Any = None                    # opcua_services.ServerState while running

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
        from asyncua import ua

        from opencap.north.opcua_security import client_pki
        from opencap.north.opcua_services import Limits, UaServer, install

        cfg = self.cfg
        users = self._users()
        if not cfg.anonymous and not users and not cfg.x509_users:
            raise RuntimeError("opcua_server: anonymous is off and no user is configured "
                               "(set username_env/password_env or users, and those environment variables)")
        pki = client_pki(cfg)
        manager = _UserManager(cfg.anonymous, users, pki, cfg.x509_users)
        srv = UaServer(user_manager=manager)
        await srv.init(shelf_file=self._shelf)
        self._state = manager.state = install(srv, Limits.from_cfg(cfg))
        if cfg.advertised_endpoint:
            # bind where `endpoint` says, advertise one fixed URL (NAT, container port mappings)
            bind = urlsplit(cfg.endpoint)
            srv.set_endpoint(cfg.advertised_endpoint)
            srv.socket_address = (bind.hostname, bind.port)
            srv.set_match_discovery_endpoint_url(False)
            srv.set_match_discovery_client_ip(False)
        else:
            srv.set_endpoint(cfg.endpoint)    # GetEndpoints echoes the URL each client used
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
        if cfg.x509_users:
            tokens.append(ua.X509IdentityToken)
        srv.set_identity_tokens(tokens)
        if any(p != "None" for p in cfg.security_policies):
            await self._load_certificate(srv)
        if pki is not None:
            srv.set_certificate_validator(pki.validate)
        self._ns = await srv.register_namespace(cfg.namespace_uri)
        if self._ns != 2:     # pragma: no cover - asyncua layout change
            if cfg.kepserverex_namespace_index:
                with contextlib.suppress(Exception):
                    await _stop_server(srv)
                raise RuntimeError(f"opcua_server: the tag namespace got index {self._ns}, not 2 "
                                   "(kepserverex_namespace_index: true)")
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
        log.info("opcua_server.started", endpoint=cfg.endpoint, advertised=cfg.advertised_endpoint,
                 devices=len(self._nodes), tags=sum(len(n.tags) for n in self._nodes.values()), namespace=self._ns,
                 policies=cfg.security_policies, anonymous=cfg.anonymous, users=len(users),
                 x509_users=cfg.x509_users, pki=cfg.pki_dir or cfg.trusted_certs_dir)
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
        """{user name: password} from the env vars named by username_env/password_env and users."""
        cfg = self.cfg
        pairs = [(cfg.username_env, cfg.password_env)] if cfg.username_env else []
        pairs += [(u.username_env, u.password_env) for u in cfg.users]
        users: dict[str, str] = {}
        for user_env, password_env in pairs:
            user, password = os.environ.get(user_env), os.environ.get(password_env or "")
            if not user or not password:
                log.warning("opcua_server.user_not_set", username_env=user_env, password_env=password_env)
                continue
            users[user] = password
        return users

    async def _load_certificate(self, srv: Any) -> None:
        from asyncua.crypto import cert_gen
        from cryptography.x509.oid import ExtendedKeyUsageOID

        from opencap.north.opcua_security import server_certificate_paths

        cert, key = server_certificate_paths(self.cfg)
        if not cert.exists() and not key.exists():
            cert.parent.mkdir(parents=True, exist_ok=True)
            key.parent.mkdir(parents=True, exist_ok=True)
            host = urlsplit(self.cfg.advertised_endpoint or self.cfg.endpoint).hostname or ""
            if host in ("", "0.0.0.0", "::"):               # a wildcard bind: name this computer
                host = socket.gethostname()
            await cert_gen.setup_self_signed_certificate(
                key, cert, self.cfg.application_uri, host,
                [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH], {})
            with contextlib.suppress(OSError):
                key.chmod(0o600)
            log.info("opcua_server.certificate_created", certificate=str(cert), host=host,
                     hint="clients must trust this certificate")
        await srv.load_certificate(str(cert))
        await srv.load_private_key(str(key))

    def _add_nodes(self, items: list[Any]) -> list[Any]:
        """AddNodes straight into the address space (one batch; parents before children)."""
        return self._server.iserver.node_mgt_service.add_nodes(items)

    async def _channel_node(self, channel: str) -> Any:
        from asyncua import ua

        node = self._channels.get(channel)
        if node is None:
            nodeid = ua.NodeId(channel, self._ns)
            result = self._add_nodes([_folder_item(nodeid, ua.QualifiedName(channel, self._ns),
                                                   ua.NodeId(ua.ObjectIds.ObjectsFolder))])[0]
            result.StatusCode.check()
            node = self._channels[channel] = self._server.get_node(nodeid)
        return node

    async def _add_device(self, dev: Device) -> None:
        """Folders for the device and its tag groups, a variable per enabled tag (DataType,
        ValueRank/ArrayDimensions from the address, AccessLevel CurrentRead, Description; an
        AnalogItemType with EURange and EngineeringUnits when the tag is scaled), `_System`."""
        from asyncua import ua

        ns = self._ns
        try:
            channel = await self._channel_node(dev.channel)
        except ua.UaError as exc:
            log.warning("opcua_server.device_skipped", device=dev.id, error=str(exc))
            return
        base = f"{dev.channel}.{dev.name}"
        root_id = ua.NodeId(base, ns)
        result = self._add_nodes([_folder_item(root_id, ua.QualifiedName(dev.name, ns), channel.nodeid)])[0]
        if not result.StatusCode.is_good():
            log.warning("opcua_server.device_skipped", device=dev.id, error=result.StatusCode.name)
            return
        nodes = _DeviceNodes(device=dev, root=self._server.get_node(root_id), fingerprint=device_fingerprint(dev))
        folders: set[str] = {base}
        variables: set[str] = set()
        items: list[Any] = []
        planned: list[tuple[str, str, _Var, int]] = []      # (tag name, path, var, index of its item)
        for tag in sorted((t for t in dev.tags if t.enabled), key=lambda t: t.name.count(".")):
            parts = tag.name.split(".")
            groups = [f"{base}.{'.'.join(parts[:k + 1])}" for k in range(len(parts) - 1)]
            path = f"{base}.{tag.name}"
            clash = next((g for g in groups if g in variables), None) or (path if path in folders else None)
            if clash is not None:
                log.warning("opcua_server.tag_skipped", device=dev.id, tag=tag.name,
                            error=f"{clash} is both a tag and a tag group")
                continue
            parent = base
            for part, group in zip(parts[:-1], groups, strict=True):
                if group not in folders:
                    folders.add(group)
                    items.append(_folder_item(ua.NodeId(group, ns), ua.QualifiedName(part, ns), ua.NodeId(parent, ns)))
                parent = group
            variables.add(path)
            vtype, dims = variant_type(tag), array_dims(dev, tag)
            nodeid = ua.NodeId(path, ns)
            planned.append((tag.name, path, _Var(nodeid, vtype, array=bool(dims)), len(items)))
            items.append(_variable_item(nodeid, ua.QualifiedName(parts[-1], ns), ua.NodeId(parent, ns), vtype, dims,
                                        tag.description, analog=tag.scale is not None))
            rng = eu_range(tag)
            if rng is not None:
                items.append(_property_item(ua.NodeId(f"{path}.EURange", ns), "EURange", nodeid,
                                            ua.Variant(ua.Range(Low=rng[0], High=rng[1])),
                                            ua.NodeId(ua.ObjectIds.Range)))
                if tag.scale.units:
                    uri, unit_id, display = eu_information(tag.scale.units)
                    info = ua.EUInformation(NamespaceUri=uri, UnitId=unit_id, DisplayName=ua.LocalizedText(display),
                                            Description=ua.LocalizedText(display))
                    items.append(_property_item(ua.NodeId(f"{path}.EngineeringUnits", ns), "EngineeringUnits",
                                                nodeid, ua.Variant(info), ua.NodeId(ua.ObjectIds.EUInformation)))
        system = f"{base}.{SYSTEM}"
        items.append(_folder_item(ua.NodeId(system, ns), ua.QualifiedName(SYSTEM, ns), root_id))
        sys_index = len(items)
        for name, vtype, _get in SYSTEM_VARS:
            items.append(_variable_item(ua.NodeId(f"{system}.{name}", ns), ua.QualifiedName(name, ns),
                                        ua.NodeId(system, ns), vtype, (), None))
        results = self._add_nodes(items)
        for name, _path, var, index in planned:
            if results[index].StatusCode.is_good():
                nodes.tags[name] = var
            else:
                log.warning("opcua_server.tag_skipped", device=dev.id, tag=name, error=results[index].StatusCode.name)
        for k, (name, vtype, _get) in enumerate(SYSTEM_VARS):
            nodes.system[name] = _Var(ua.NodeId(f"{system}.{name}", ns), vtype)
            results[sys_index + k].StatusCode.check()
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
            if is_array and not var.array:      # a driver whose address syntax has no dimensions
                var.array = True
                await self._server.write_attribute_value(
                    var.nodeid, ua.DataValue(ua.Variant(ua.ValueRank.OneDimension.value, ua.VariantType.Int32)),
                    ua.AttributeIds.ValueRank)
                await self._server.write_attribute_value(          # 0 = length not known up front
                    var.nodeid, ua.DataValue(ua.Variant([0], ua.VariantType.UInt32, is_array=True)),
                    ua.AttributeIds.ArrayDimensions)
            variant = ua.Variant(value, getattr(ua.VariantType, var.vtype), is_array=is_array)
        now = datetime.now(UTC)
        dv = ua.DataValue(variant, StatusCode=ua.StatusCode(code), SourceTimestamp=source_ts or now,
                          ServerTimestamp=now)
        await self._server.write_attribute_value(var.nodeid, dv)
