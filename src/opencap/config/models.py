"""Typed configuration models (PLAN.md section 4).

These models are the shared contract between the importer, the loader, the
drivers and the core. Site data never lives in code: everything here is filled
from config files.
"""

from __future__ import annotations

import functools
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DType = Literal[
    "bool", "int16", "uint16", "int32", "uint32", "int64", "uint64",
    "float32", "float64", "string",
]
DriverName = Literal["modbus_tcp", "ab_logix", "ab_pccc", "s7", "melsec_mc", "opcua_client",
                     "modbus_rtu", "ab_df1", "simulator"]
Quality = Literal["good", "bad_comm", "bad_config", "bad_decode", "stale"]

DEFAULT_TOPIC_PREFIX = "opencap"   # poller.yaml mqtt.topic_prefix default


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scale(_Model):
    """Kepware-style tag scaling. Defaults match Kepware's defaults."""

    type: Literal["linear", "sqrt"] = "linear"
    raw_lo: float = 0.0
    raw_hi: float = 1000.0
    eng_lo: float = 0.0
    eng_hi: float = 1000.0
    clamp_lo: bool = False
    clamp_hi: bool = False
    negate: bool = False
    units: str | None = None

    def apply(self, raw: float) -> float:
        span = self.raw_hi - self.raw_lo
        if span == 0:
            raise ValueError("scale raw_hi == raw_lo")
        frac = (float(raw) - self.raw_lo) / span
        if self.type == "sqrt":
            frac = math.sqrt(frac) if frac > 0 else 0.0
        value = frac * (self.eng_hi - self.eng_lo) + self.eng_lo
        lo, hi = sorted((self.eng_lo, self.eng_hi))
        if self.clamp_lo and value < lo:
            value = lo
        if self.clamp_hi and value > hi:
            value = hi
        return -value if self.negate else value


# Each device's diagnostics live under `<channel>.<device>._System` (OPC UA server, D-046), so no
# tag or tag group may use that name (compared without case, as KEPServerEX compares names).
RESERVED_GROUP = "_System"
# System-tag folders (docs/spec/system-tags.md section 3.2, PRJ-13, LOG-07). Compared without case.
RESERVED_CHANNEL_NAMES = ("_System",)
RESERVED_DEVICE_NAMES = ("_System", "_Statistics")
RESERVED_TAG_GROUPS = ("_System", "_Statistics")


def reserved_word(name: str, reserved: tuple[str, ...]) -> str | None:
    """The reserved word `name` equals (case-insensitively), or None."""
    return next((w for w in reserved if w.lower() == name.lower()), None)


def underscore_names(channels: Any) -> list[str]:
    """Names with a segment starting with `_` (the warning of spec 3.2; reserved words are errors
    and never get here). One text per name, in file order: the loader reports them, and API writes
    refuse the ones they would add (PRJ-13)."""
    out: list[str] = []
    for ch in channels:
        if ch.name.startswith("_"):
            out.append(f"channel name {ch.name!r} starts with '_'")
        for dev in ch.devices:
            if dev.name.startswith("_"):
                out.append(f"{dev.id}: device name {dev.name!r} starts with '_'")
            for tag in dev.tags:
                if any(seg.startswith("_") for seg in tag.name.split(".")):
                    out.append(f"{dev.id}: tag name {tag.name!r} has a segment starting with '_'")
    return out


# Device names the REST paths use as sub-resources (`/devices/<channel>/<device>/tags`): a device
# cannot have one of them as its name (case-sensitive, as the paths are).
API_DEVICE_NAMES = ("tags", "values", "plan", "identity", "status", "read-once")


def _name_faults(name: str, what: str, *, dotted: bool) -> list[str]:
    """Rule violations of one name (PRJ-13). `dotted`: a channel or device name, where `.` and `/`
    would make tag paths and device ids ambiguous. The `_` rule is `underscore_names`'."""
    out: list[str] = []
    if not name:
        return [f"{what} name is empty"]
    if name != name.strip():
        out.append(f"{what} name {name!r} has leading or trailing whitespace")
    if '"' in name:
        out.append(f"{what} name {name!r} contains '\"'")
    if dotted:
        for ch in (".", "/"):
            if ch in name:
                out.append(f"{what} name {name!r} contains {ch!r}")
    return out


def name_rule_problems(channels: Any) -> list[tuple[tuple[Any, ...], str]]:
    """Names that break the naming rules other than the `_` one (PRJ-13): empty, surrounding
    whitespace, `"`, `.` or `/` in a channel or device name, an empty or padded dot segment of a tag
    name, a device name the REST paths use. One `(loc, text)` per problem, in file order; `loc` is
    the pydantic-style path of the name. Names are never changed: the loader reports these as
    warnings and API writes refuse the ones they would add."""
    out: list[tuple[tuple[Any, ...], str]] = []
    for ci, ch in enumerate(channels):
        loc: tuple[Any, ...] = ("channels", ci)
        out += [((*loc, "name"), t) for t in _name_faults(ch.name, "channel", dotted=True)]
        for di, dev in enumerate(ch.devices):
            dloc = (*loc, "devices", di)
            out += [((*dloc, "name"), f"{dev.id}: {t}") for t in _name_faults(dev.name, "device", dotted=True)]
            if dev.name in API_DEVICE_NAMES:
                out.append(((*dloc, "name"), f"{dev.id}: device name {dev.name!r} is used by the REST paths"))
            for ti, tag in enumerate(dev.tags):
                tloc = (*dloc, "tags", ti, "name")
                if tag.name == "":
                    out.append((tloc, f"{dev.id}: tag name is empty"))
                    continue
                for seg in tag.name.split("."):
                    out += [(tloc, f"{dev.id}: {t} (in tag {tag.name!r})")
                            for t in _name_faults(seg, "tag segment", dotted=False)]
    return out


class Tag(_Model):
    name: str                      # "PUMP_1_CURRENT_R" or "LTI002.Pv" (group path kept, dot-joined)
    address: str                   # native address: "400131", "F34:1", "FT_01", "MD232"
    dtype: DType
    scan_ms: int | None = None     # None = device default
    scale: Scale | None = None
    enabled: bool = True
    description: str | None = None


class PublishCfg(_Model):
    sub_device_id: str | None = None
    topic: str | None = None       # None = "<mqtt.topic_prefix>/<site>/<device>/data" (slugged)
    env: str = "prod"
    mode: Literal["interval", "on_change"] = "interval"
    interval_ms: int = 10_000
    heartbeat_ms: int = 300_000
    deadband: float = 0.0
    # "subdevice_json" = the SubDevice message; "iotgw_json" = KEPServerEX IoT Gateway standard
    # template (docs/spec/payload-iotgw-json.md). Applies to the data topic only.
    format: Literal["subdevice_json", "iotgw_json"] = "subdevice_json"


class Device(_Model):
    id: str                        # "<channel>/<device>", unique
    channel: str
    name: str
    driver: DriverName
    host: str
    port: int
    params: dict[str, Any] = Field(default_factory=dict)  # driver-specific
    scan_ms: int = 1000
    min_scan_ms: int = 1000        # "request data no faster than"
    timeout_ms: int = 3000
    retries: int = 2
    demote_after: int = 3          # consecutive failed cycles
    demote_for_ms: int = 30_000
    enabled: bool = True
    publish: PublishCfg = Field(default_factory=PublishCfg)
    tags: list[Tag]
    source: dict[str, Any] = Field(default_factory=dict)  # KEPServer provenance (driver, model, id string)

    @model_validator(mode="after")
    def _unique_tag_names(self) -> "Device":
        seen: set[str] = set()
        dupes = sorted({t.name for t in self.tags if t.name in seen or seen.add(t.name)})
        if dupes:
            raise ValueError(f"{self.id}: duplicate tag names {dupes}")
        word = reserved_word(self.name, RESERVED_DEVICE_NAMES)
        if word:
            raise ValueError(f"{self.id}: device name {self.name!r} is the reserved word {word!r} "
                             "(system-tag folder, docs/spec/system-tags.md 3.2)")
        for word in RESERVED_TAG_GROUPS:
            reserved = sorted(t.name for t in self.tags if t.name.split(".", 1)[0].lower() == word.lower())
            if reserved:
                raise ValueError(f"{self.id}: tag names {reserved} use the reserved group name {word!r} "
                                 + ("(the per-device diagnostics folder of the OPC UA server, D-046)"
                                    if word == RESERVED_GROUP else "(system-tag folder, docs/spec/system-tags.md 3.2)"))
        return self

    def effective_rate_ms(self, tag: Tag) -> int:
        return max(tag.scan_ms or self.scan_ms, self.min_scan_ms)

    def rate_groups(self) -> dict[int, list[Tag]]:
        """Enabled tags grouped by effective scan rate, fastest first."""
        groups: dict[int, list[Tag]] = {}
        for tag in self.tags:
            if tag.enabled:
                groups.setdefault(self.effective_rate_ms(tag), []).append(tag)
        return dict(sorted(groups.items()))


class Channel(_Model):
    name: str                      # KEPServer channel name = site
    enabled: bool = True
    devices: list[Device]

    @field_validator("name")
    @classmethod
    def _not_reserved(cls, value: str) -> str:
        word = reserved_word(value, RESERVED_CHANNEL_NAMES)
        if word:
            raise ValueError(f"channel name {value!r} is the reserved word {word!r} "
                             "(system-tag folder, docs/spec/system-tags.md 3.2)")
        return value


class ExcludedDevice(_Model):
    channel: str
    device: str
    kep_driver: str
    tags: int = 0
    reason: str


class DevicesFile(_Model):
    """Schema of config/devices.json (generated by `opencap import-kep`, hand-editable)."""

    version: int = 1
    generated_from: str | None = None
    generated_at: str | None = None
    channels: list[Channel]
    excluded: list[ExcludedDevice] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_device_ids(self) -> "DevicesFile":
        seen: set[str] = set()
        for ch in self.channels:
            for dev in ch.devices:
                if dev.id in seen:
                    raise ValueError(f"duplicate device id {dev.id!r}")
                seen.add(dev.id)
        return self


# --- devices_map.yaml -------------------------------------------------------

class DeviceMapEntry(_Model):
    sub_device_id: str
    topic: str | None = None
    env: str | None = None
    stub: bool = False             # True = generated guess, confirm before go-live


class DeviceMap(_Model):
    defaults: dict[str, Any] = Field(default_factory=dict)
    devices: dict[str, DeviceMapEntry] = Field(default_factory=dict)  # key = device id


# --- overrides.yaml ---------------------------------------------------------

class TagOverride(_Model):
    enabled: bool | None = None
    address: str | None = None
    dtype: DType | None = None
    scan_ms: int | None = None
    scale: Scale | None = None


class DeviceOverride(_Model):
    enabled: bool | None = None
    host: str | None = None
    port: int | None = None
    scan_ms: int | None = None
    min_scan_ms: int | None = None
    timeout_ms: int | None = None
    retries: int | None = None
    demote_after: int | None = None
    demote_for_ms: int | None = None
    params: dict[str, Any] = Field(default_factory=dict)   # merged into Device.params
    publish: dict[str, Any] = Field(default_factory=dict)  # merged into Device.publish
    tags: dict[str, TagOverride] = Field(default_factory=dict)


class Overrides(_Model):
    channels: dict[str, dict[str, bool]] = Field(default_factory=dict)  # {"SITE": {"enabled": false}}
    devices: dict[str, DeviceOverride] = Field(default_factory=dict)    # key = device id
    # KEPServer COM port -> where opencap reaches that line (serial drivers with transport: unmapped):
    # "COM112": "192.0.2.50:4001" (raw TCP gateway), "rfc2217://host:port", "/dev/ttyUSB0"
    serial_ports: dict[str, str] = Field(default_factory=dict)


# --- poller.yaml ------------------------------------------------------------

class MqttCfg(_Model):
    host: str = "localhost"
    port: int = 1883
    tls: bool = False
    ca_file: str | None = None
    username: str | None = None
    password_env: str | None = "OPENCAP_MQTT_PASSWORD"   # secret read from env, never stored in yaml
    client_id: str | None = None
    qos: int = 1
    keepalive_s: int = 30
    # First level(s) of every topic opencap derives: <prefix>/<channel>/<device>/data|status and
    # the LWT <prefix>/poller/<instance>/status. Used verbatim (not slugged); explicit
    # `publish.topic` values are not affected.
    topic_prefix: str = DEFAULT_TOPIC_PREFIX

    @field_validator("topic_prefix")
    @classmethod
    def _check_topic_prefix(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("topic_prefix must not be empty")
        if "+" in value or "#" in value or "\x00" in value:
            raise ValueError(f"topic_prefix {value!r} must not contain '+', '#' or NUL")
        if value.startswith("$"):
            raise ValueError(f"topic_prefix {value!r} must not start with '$' (reserved by brokers)")
        if any(level == "" for level in value.split("/")):
            raise ValueError(f"topic_prefix {value!r} must not start or end with '/' or contain '//'")
        return value


class SafCfg(_Model):
    path: str = "data/saf.sqlite"
    max_bytes: int = 2 * 1024**3


class ApiToken(_Model):
    """A named API token (also used to log in to the web UI). The secret lives in the env var
    `token_env`, never in yaml."""

    name: str = Field(min_length=1)   # shown in the audit log as "who"
    role: Literal["viewer", "admin"] = "viewer"   # viewer = read-only (GET), admin = also changes
    token_env: str = Field(min_length=1)


class ApiCfg(_Model):
    bind: str = "127.0.0.1"
    port: int = 8080
    token_env: str = "OPENCAP_API_TOKEN"   # legacy single token: name "default", role admin
    tokens: list[ApiToken] = Field(default_factory=list)
    audit_log: str = "data/audit.jsonl"    # config-change audit log (JSON lines)
    cors_origins: list[str] = Field(default_factory=list)   # other web origins allowed to call the API (desktop app)

    @model_validator(mode="after")
    def _unique_token_names(self) -> "ApiCfg":
        names = [t.name for t in self.tokens]
        if "default" in names:
            raise ValueError("api.tokens: the name 'default' is reserved for api.token_env")
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"api.tokens: duplicate names {dupes}")
        return self


def is_known_zone(name: str) -> bool:
    """An IANA zone name the payload can use (the fixed-offset zones cover images without tzdata,
    as `north.payload.zone` does)."""
    if name in _FALLBACK_ZONES:
        return True
    if not name or name != name.strip():
        return False
    zones = _available_zones()
    if zones:                       # exact, case-sensitive key match, as chrono-tz in the Rust engine
        return name in zones        # (Windows file lookups ignore case and trailing spaces)
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


_FALLBACK_ZONES = ("Asia/Kolkata", "Asia/Calcutta", "UTC")


@functools.lru_cache(maxsize=1)
def _available_zones() -> frozenset[str]:
    from zoneinfo import available_timezones

    return frozenset(available_timezones())


class PayloadCfg(_Model):
    float_decimals: int = 4
    bad_tags_field: bool = True
    timezone: str = "Asia/Kolkata"

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        if not is_known_zone(value):
            raise ValueError(f"payload.timezone {value!r} is not a known IANA time zone name")
        return value


class DeviceDefaults(_Model):
    scan_ms: int = 1000
    min_scan_ms: int = 1000
    timeout_ms: int = 3000
    retries: int = 2
    demote_after: int = 3
    demote_for_ms: int = 30_000


class CheckCfg(_Model):
    est_rtt_ms: float = 60.0       # assumed round trip per wire request over the IPsec link


OpcUaServerPolicy = Literal[
    "None", "Basic256Sha256_Sign", "Basic256Sha256_SignAndEncrypt",
    "Aes128_Sha256_RsaOaep_Sign", "Aes128_Sha256_RsaOaep_SignAndEncrypt",
    "Aes256_Sha256_RsaPss_Sign", "Aes256_Sha256_RsaPss_SignAndEncrypt",
]


class OpcUaServerCfg(_Model):
    """Northbound OPC UA server (PLAN Phase 6, docs/decisions.md D-046): the cache as a
    read-only, KEPServer-style address space. Off by default."""

    enabled: bool = False
    endpoint: str = "opc.tcp://127.0.0.1:4840/opencap"   # bind address and advertised URL
    server_name: str = "opencap"
    application_uri: str = "urn:opencap:server"
    namespace_uri: str = "urn:opencap"                    # registered first, so it is ns=2
    security_policies: list[OpcUaServerPolicy] = Field(default_factory=lambda: ["None"])
    certificate: str | None = None         # server certificate (DER/PEM); generated if missing with the key
    private_key: str | None = None
    trusted_certs_dir: str | None = None   # when set, only client certificates found here are accepted
    anonymous: bool = False                # anonymous clients only when set (D-046)
    username_env: str | None = None        # env var NAMES; the values are never in a file
    password_env: str | None = None
    update_ms: int = 250                   # how often cache changes are copied into the address space
    allow_insecure_remote: bool = False    # offer policy None on a non-loopback endpoint (plaintext)

    @model_validator(mode="after")
    def _check(self) -> "OpcUaServerCfg":
        if not self.endpoint.startswith("opc.tcp://"):
            raise ValueError(f"opcua_server.endpoint {self.endpoint!r} must start with opc.tcp://")
        if not self.security_policies:
            raise ValueError("opcua_server.security_policies must not be empty")
        if any(p != "None" for p in self.security_policies) and not (self.certificate and self.private_key):
            raise ValueError("opcua_server: secure security_policies need certificate and private_key paths")
        if bool(self.username_env) != bool(self.password_env):
            raise ValueError("opcua_server: set both username_env and password_env, or neither")
        if self.update_ms < 10:
            raise ValueError("opcua_server.update_ms must be >= 10")
        if "None" in self.security_policies and not self.allow_insecure_remote \
                and not _is_loopback_host(self.endpoint_host()):
            raise ValueError(
                f"opcua_server: security policy None on {self.endpoint!r} would serve plaintext beyond this "
                "computer: offer only Sign/SignAndEncrypt policies, bind to 127.0.0.1, or set "
                "allow_insecure_remote: true")
        if self.enabled and not self.anonymous and not self.username_env:
            raise ValueError("opcua_server: anonymous is off and no user is configured: set "
                             "username_env/password_env, or anonymous: true")
        return self

    def endpoint_host(self) -> str:
        """Host part of `endpoint` (`opc.tcp://host:port/path`), without brackets."""
        rest = self.endpoint[len("opc.tcp://"):].split("/", 1)[0]
        if rest.startswith("["):
            return rest[1:].split("]", 1)[0]
        return rest.rsplit(":", 1)[0] if rest.count(":") == 1 else rest


def _is_loopback_host(host: str) -> bool:
    """`localhost` or a loopback IP (all of 127/8, ::1), as `server._is_loopback` decides for the API."""
    import ipaddress

    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class PollerConfig(_Model):
    instance: str = "opencap-1"
    log_level: str = "INFO"        # DEBUG logs every request
    stats_interval_s: float = 300.0   # INFO `device.stats` line (cycle times) per device; 0 = off
    mqtt: MqttCfg = Field(default_factory=MqttCfg)
    saf: SafCfg = Field(default_factory=SafCfg)
    api: ApiCfg = Field(default_factory=ApiCfg)
    payload: PayloadCfg = Field(default_factory=PayloadCfg)
    defaults: DeviceDefaults = Field(default_factory=DeviceDefaults)
    check: CheckCfg = Field(default_factory=CheckCfg)
    opcua_server: OpcUaServerCfg = Field(default_factory=OpcUaServerCfg)
