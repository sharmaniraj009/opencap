"""KEPServer `project.json` importer (PLAN.md sections 1 and 8).

`import_kep()` reads a raw KEPServer JSON export and maps every channel/device/tag
onto the models in `opencap.config.models`. `write_outputs()` writes:

- `config/devices.json`      DevicesFile, overwritten on every import
- `config/devices_map.yaml`  never overwritten: stub entries are appended for new device ids
- `config/overrides.yaml`    template, written only when the file is missing
- `reports/same_address.csv` tags that read the same address, for manual review
- `reports/import_summary.json`

Nothing site-specific lives here: every value comes from the export. Credentials in
the export (OPC UA user/password, MQTT user/password) are never copied; OPC UA devices
get the *names* of environment variables instead (`username_env` / `password_env`).

Mapping notes (also in docs/decisions.md):
- Kepware "Attempts Before Timeout" (`DEVICE_RETRY_ATTEMPTS`) counts the first request,
  so `Device.retries = attempts - 1` (3 attempts -> 2 retries, same wire behaviour).
- Kepware data types without an exact `DType`: Char -> int16, Byte -> uint16 (widened,
  reported); BCD / LBCD / Date -> imported with `enabled: false` (no decoder yet).
- Same-address pairs are counted like the old kep2poller.py: per (device, address)
  shared by n tags, n - 1 pairs (each later tag paired with the first one).
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import unicodedata
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

from opencap.config.models import (
    Channel,
    Device,
    DeviceDefaults,
    DeviceMap,
    DeviceMapEntry,
    DevicesFile,
    ExcludedDevice,
    Scale,
    Tag,
)

# --- KEPServer keys ----------------------------------------------------------

K_NAME = "common.ALLTYPES_NAME"
K_DESC = "common.ALLTYPES_DESCRIPTION"
K_DRIVER = "servermain.MULTIPLE_TYPES_DEVICE_DRIVER"
K_MODEL = "servermain.DEVICE_MODEL"
K_ID = "servermain.DEVICE_ID_STRING"
K_COLLECT = "servermain.DEVICE_DATA_COLLECTION"
K_SCAN_MODE = "servermain.DEVICE_SCAN_MODE"
K_SCAN_RATE = "servermain.DEVICE_SCAN_MODE_RATE_MS"
K_TIMEOUT = "servermain.DEVICE_REQUEST_TIMEOUT_MILLISECONDS"
K_ATTEMPTS = "servermain.DEVICE_RETRY_ATTEMPTS"
K_DEMOTE_ON = "servermain.DEVICE_AUTO_DEMOTION_ENABLE_ON_COMMUNICATIONS_FAILURES"
K_DEMOTE_AFTER = "servermain.DEVICE_AUTO_DEMOTION_DEMOTE_AFTER_SUCCESSIVE_TIMEOUTS"
K_DEMOTE_MS = "servermain.DEVICE_AUTO_DEMOTION_PERIOD_MS"

K_TAG_ADDR = "servermain.TAG_ADDRESS"
K_TAG_TYPE = "servermain.TAG_DATA_TYPE"
K_TAG_SCAN = "servermain.TAG_SCAN_RATE_MILLISECONDS"
K_SCALE = "servermain.TAG_SCALING_"

KEP_MICRO800 = "Allen-Bradley Micro800 Ethernet"
KEP_CLX = "Allen-Bradley ControlLogix Ethernet"
KEP_MODBUS = "Modbus TCP/IP Ethernet"
KEP_S7 = "Siemens TCP/IP Ethernet"
KEP_MELSEC = "Mitsubishi Ethernet"
KEP_OPCUA = "OPC UA Client"
KEP_DF1 = "Allen-Bradley DF1"
KEP_SIMULATOR = "Simulator"
KEP_MQTT = "MQTT Client"
KEP_UCON = "U-CON (user-configurable)"

CLX_MODEL_LOGIX = 0      # ControlLogix 5500 (symbolic) -> ab_logix
CLX_MODEL_PCCC = 23      # data-table addresses (F34:3, N54:17, B3:17/14) -> ab_pccc

# --- exclusion reasons (stable strings: tests and import_summary.json use them) ----

REASON_DF1 = "DF1 serial: Phase 7"
REASON_SIMULATOR = "Simulator driver: no real device"
REASON_MQTT = "MQTT Client driver: not a PLC"
REASON_UCON = "U-CON user-configurable driver: not supported"
REASON_DISABLED = "data collection disabled in KEPServer"
REASON_NO_HOST = "no usable host in DEVICE_ID_STRING"

EXCLUDED_DRIVERS = {
    KEP_DF1: REASON_DF1,
    KEP_SIMULATOR: REASON_SIMULATOR,
    KEP_MQTT: REASON_MQTT,
    KEP_UCON: REASON_UCON,
}

# --- data types --------------------------------------------------------------

KEP_TYPE_NAMES = {
    -1: "Default", 0: "String", 1: "Boolean", 2: "Char", 3: "Byte", 4: "Short", 5: "Word",
    6: "Long", 7: "DWord", 8: "Float", 9: "Double", 10: "BCD", 11: "LBCD", 12: "Date",
    13: "LLong", 14: "QWord",
}
KEP_DTYPES = {
    0: "string", 1: "bool", 2: "int16", 3: "uint16", 4: "int16", 5: "uint16", 6: "int32",
    7: "uint32", 8: "float32", 9: "float64", 13: "int64", 14: "uint64",
    # no decoder yet: imported with enabled=false (see _map_dtype)
    10: "uint16", 11: "uint32", 12: "float64",
}
WIDENED_TYPES = {2, 3}            # Char (int8) / Byte (uint8): the model has no 8-bit dtype
DISABLED_TYPES = {10, 11, 12}     # BCD, LBCD, Date

OPCUA_SECURITY_POLICIES = {
    0: "None", 1: "Basic128Rsa15", 2: "Basic256", 3: "Basic256Sha256",
    4: "Aes128_Sha256_RsaOaep", 5: "Aes256_Sha256_RsaPss",
}
OPCUA_MESSAGE_MODES = {0: "None", 1: "None", 2: "Sign", 3: "SignAndEncrypt"}

# --- templates ---------------------------------------------------------------

OVERRIDES_TEMPLATE = """\
# overrides.yaml: hand fixes applied on top of devices.json when the config is loaded.
# `opencap import-kep` writes this template only when the file is missing and never
# touches an existing one, so fixes survive a re-import.
#
# Unknown channel names, device ids or tag names are reported as warnings by
# `opencap check`; they are never fatal.

# Per channel (= KEPServer channel = site):
#   "SITE NAME": {enabled: false}
channels: {}

# Per device, keyed by device id "<channel>/<device>". Every field is optional.
#   "SITE NAME/DEVICE":
#     enabled: false
#     host: 192.0.2.10
#     port: 502
#     scan_ms: 5000           # device default scan rate
#     min_scan_ms: 1000       # "request data no faster than"
#     timeout_ms: 2000
#     retries: 2
#     demote_after: 3
#     demote_for_ms: 30000
#     params: {max_holding: 32}          # merged into the device params (docs/driver_params.md)
#     publish: {mode: on_change, deadband: 0.5}   # merged into publish settings
#     tags:
#       PUMP_1_CURRENT_B: {enabled: false}
#       FLOW_1: {scan_ms: 10000, address: "400047", dtype: float32}
#       LEVEL_1: {scale: {raw_lo: 0, raw_hi: 4095, eng_lo: 0, eng_hi: 10}}
devices: {}
"""

DEVICES_MAP_HEADER = """\
# devices_map.yaml: SubDeviceId, MQTT topic and Env per device (PLAN.md 7.1).
# Keys are device ids "<channel>/<device>" from devices.json.
# Entries with `stub: true` were guessed by `opencap import-kep`: confirm the SubDeviceId
# with the backend, then delete the `stub` line. `opencap import-kep` never overwrites
# this file; it only appends stubs for device ids that are not in it yet.
# `topic` and `env` are optional; topic defaults to <mqtt.topic_prefix>/<channel>/<device>/data
# (slugged; poller.yaml mqtt.topic_prefix, default "opencap").
# `defaults` is merged into the publish settings of every device (e.g. env, interval_ms).
"""


# --- result types ------------------------------------------------------------

@dataclass
class SameAddressRow:
    """One (device, address) read by two or more tags."""

    channel: str
    device: str
    device_id: str
    address: str
    tags: list[str]
    dtypes: list[str]
    n_pairs: int                  # n_tags - 1, as kep2poller.py counted
    classification: str           # "alias" | "suspect"
    reason: str
    included: bool                # False = device excluded from opencap (e.g. DF1)

    CSV_FIELDS = (
        "channel", "device", "address", "tags", "dtypes", "n_tags", "n_pairs",
        "classification", "reason", "status",
    )

    def csv_row(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "device": self.device,
            "address": self.address,
            "tags": " | ".join(self.tags),
            "dtypes": " | ".join(self.dtypes),
            "n_tags": len(self.tags),
            "n_pairs": self.n_pairs,
            "classification": self.classification,
            "reason": self.reason,
            "status": "included" if self.included else "excluded",
        }


@dataclass
class ImportResult:
    devices_file: DevicesFile
    same_address: list[SameAddressRow]
    summary: dict[str, Any]
    problems: list[str] = field(default_factory=list)

    def summary_text(self) -> str:
        s = self.summary
        src, inc, exc, sa = s["source"], s["included"], s["excluded"], s["same_address"]
        lines = [
            f"KEPServer import of {src['file']}",
            f"  source    {src['channels']:,} channels, {src['devices']:,} devices, {src['tags']:,} tags",
            f"  included  {inc['channels']:,} channels, {inc['devices']:,} devices, {inc['tags']:,} tags",
        ]
        for drv, c in inc["by_driver"].items():
            extra = ""
            if drv == "ab_logix" and inc.get("micro800", {}).get("devices"):
                m = inc["micro800"]
                extra = f"  (Micro800: {m['devices']} devices, {m['tags']:,} tags)"
            lines.append(f"            {drv:<13} {c['devices']:>4} devices {c['tags']:>7,} tags{extra}")
        lines.append(f"  excluded  {exc['devices']:,} devices, {exc['tags']:,} tags")
        for reason, c in exc["by_reason"].items():
            lines.append(f"            {reason:<46} {c['devices']:>4} devices {c['tags']:>7,} tags")
        for name in exc.get("channels_without_devices", []):
            lines.append(f"            channel {name!r} has no devices")
        lines.append(
            f"  same address: {sa['groups']:,} addresses, {sa['pairs']:,} pairs "
            f"({sa['suspect_groups']:,} suspect, {sa['alias_groups']:,} alias addresses; "
            f"{sa['pairs_included']:,} pairs in included devices)"
        )
        if s.get("tags_below_min_scan"):
            lines.append(
                f"  scan rate: {s['tags_below_min_scan']:,} tags ask for less than min_scan_ms "
                f"({s['min_scan_ms']} ms) and will be polled at {s['min_scan_ms']} ms"
            )
        out = s.get("outputs", {}).get("devices_map")
        if out:
            lines.append(f"  devices_map.yaml: {out}")
        lines.append(f"  problems: {len(self.problems)}" + (" (all in import_summary.json)" if self.problems else ""))
        for p in self.problems[:10]:
            lines.append(f"    - {p}")
        if len(self.problems) > 10:
            lines.append(f"    ... {len(self.problems) - 10} more")
        return "\n".join(lines)


class _Exclude(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --- small helpers -----------------------------------------------------------

def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


def env_slug(s: str) -> str:
    """'Test_OPC1' -> 'TEST_OPC1'; uppercase, non-alphanumerics -> '_', repeats collapsed."""
    return re.sub(r"[^A-Z0-9]+", "_", _ascii(s).upper()).strip("_")


def stub_sub_device_id(channel: str, device: str, single_device: bool) -> str:
    """SUB_<SITE>_PLC for a one-device channel, else SUB_<SITE>_<DEVICE>."""
    site = env_slug(channel) or "SITE"
    tail = "PLC" if single_device else (env_slug(device) or "DEVICE")
    return f"SUB_{site}_{tail}"


def opcua_env_names(channel: str) -> tuple[str, str]:
    base = f"OPENCAP_OPCUA_{env_slug(channel) or 'CHANNEL'}"
    return f"{base}_USER", f"{base}_PASSWORD"


_BRACKETED = re.compile(r"^<(?P<host>[^>]*)>(?P<rest>.*)$")
_IP_WITH_UNIT = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})\.(\d+)$")
_HOST_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-:]*$")


def parse_device_id(raw: str) -> tuple[str, str]:
    """Split a KEPServer DEVICE_ID_STRING into (host, rest).

    '192.0.2.1'          -> ('192.0.2.1', '')
    '192.0.2.1 '         -> ('192.0.2.1', '')         surrounding whitespace dropped
    '<192.0.2.1>'        -> ('192.0.2.1', '')
    '<192.0.2.1>.0'      -> ('192.0.2.1', '.0')       Modbus unit id
    '<192.0.2.1>,1,0'    -> ('192.0.2.1', ',1,0')     CIP route
    '192.0.2.1,1,0'      -> ('192.0.2.1', ',1,0')
    '192.0.2.1.5'        -> ('192.0.2.1', '.5')
    """
    s = (raw or "").strip()
    m = _BRACKETED.match(s)
    if m:
        return m["host"].strip(), m["rest"].strip()
    host, sep, rest = s.partition(",")
    host = host.strip()
    rest = f",{rest.strip()}" if sep else ""
    m = _IP_WITH_UNIT.match(host)
    if m:
        return m[1], f".{m[2]}{rest}"
    return host, rest


def _cip_path(rest: str) -> tuple[str | None, int | None]:
    """',1,0' -> ('1,0', 0)."""
    rest = rest.strip()
    if not rest.startswith(","):
        return None, None
    parts = [p.strip() for p in rest[1:].split(",") if p.strip()]
    if not parts:
        return None, None
    slot = int(parts[-1]) if parts[-1].isdigit() else None
    return ",".join(parts), slot


def _iter_tags(container: dict[str, Any], prefix: str = "") -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (full_name, raw_tag) for device tags and nested tag_groups ('<group>.<tag>')."""
    for tag in container.get("tags") or []:
        yield f"{prefix}{tag.get(K_NAME, '')}", tag
    for group in container.get("tag_groups") or []:
        yield from _iter_tags(group, f"{prefix}{group.get(K_NAME, '')}.")


def _int(value: Any, default: int | None = None) -> int | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _get(d: dict[str, Any], key: str, default: Any) -> Any:
    value = d.get(key)
    return default if value is None else value


def _default_data_type(dev: dict[str, Any]) -> int | None:
    for key, value in dev.items():
        if key.endswith(".DEVICE_DEFAULT_DATA_TYPE") and _int(value, -1) not in (None, -1):
            return int(value)
    return None


def _dtype_label(code: Any) -> str:
    """dtype for reports (also for devices we do not import)."""
    c = _int(code)
    return KEP_DTYPES.get(c, f"kep:{code}") if c is not None else f"kep:{code}"


# --- same-address classification ---------------------------------------------

_TOKEN = re.compile(r"[A-Z]+|\d+")


def _tokens(name: str) -> list[str]:
    return _TOKEN.findall(_ascii(name).upper())


def _signal(tokens: list[str]) -> str:
    """Last alphabetic token: the signal kind in names like PUMP_1_CURRENT_Y (-> 'Y')."""
    for tok in reversed(tokens):
        if not tok.isdigit():
            return tok
    return ""


def _numbered_copy(a: str, b: str) -> bool:
    """'SEWAGE_LAST_DAY' vs 'SEWAGE_LAST_DAY_1' / 'RAW_MAX' vs 'RAW_MAX1'."""
    short, long_ = sorted((a, b), key=len)
    if not long_.startswith(short) or len(long_) == len(short):
        return False
    tail = long_[len(short):]
    if re.fullmatch(r"[_ ]\d+", tail):
        return True
    return bool(re.fullmatch(r"\d+", tail)) and not short[-1].isdigit()


def classify_pair(a: str, b: str, dtype_a: str, dtype_b: str) -> tuple[str, str]:
    """Heuristic: is (a, b) on one address an alias or a suspected addressing error?

    1. different dtypes                                   -> suspect
    2. one name is the other plus a copy number           -> alias   (X / X_1, X / X1)
       or the same words with other punctuation           -> alias   (PUMP_3_RFB / PUMP3_RFB)
    3. different signal kind (last alphabetic token)      -> suspect (..._CURRENT_Y / ..._CURRENT_B,
                                                                       ..._START / ..._STOP)
    4. same words, only an index number differs           -> suspect (LEVEL_1 / LEVEL_2)
    5. otherwise (same signal kind, other prefix)         -> alias   (EU_MAX / LEVEL_1_MAX,
                                                                       LT_CURRENT_R / PUMP_6_CURRENT_R)
    """
    if dtype_a != dtype_b:
        return "suspect", f"dtype differs ({dtype_a} vs {dtype_b})"
    ua, ub = _ascii(a).upper().strip(), _ascii(b).upper().strip()
    if ua == ub:
        return "alias", "same name"
    if _numbered_copy(ua, ub):
        return "alias", "numbered copy"
    ta, tb = _tokens(ua), _tokens(ub)
    if ta == tb:
        return "alias", "same name, other punctuation"     # PUMP_3_RFB / PUMP3_RFB
    sa, sb = _signal(ta), _signal(tb)
    if sa != sb:
        return "suspect", f"different signal ({sa} vs {sb})"
    shape_a = ["#" if t.isdigit() else t for t in ta]
    shape_b = ["#" if t.isdigit() else t for t in tb]
    if shape_a == shape_b:
        return "suspect", "same name, different index"
    return "alias", f"same signal ({sa})"


def classify_group(names: list[str], dtypes: list[str]) -> tuple[str, str]:
    reasons: list[str] = []
    for (na, da), (nb, db) in combinations(zip(names, dtypes), 2):
        cls, reason = classify_pair(na, nb, da, db)
        if cls == "suspect":
            return "suspect", f"{na} / {nb}: {reason}"
        if reason not in reasons:
            reasons.append(reason)
    return "alias", "; ".join(reasons)


# --- per-driver mapping ------------------------------------------------------

@dataclass
class _Mapped:
    driver: str
    host: str
    port: int
    params: dict[str, Any]
    source: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def _host_and_rest(dev: dict[str, Any], dev_id: str, problems: list[str]) -> tuple[str, str]:
    raw = dev.get(K_ID)
    if not isinstance(raw, str):
        raise _Exclude(REASON_NO_HOST)
    host, rest = parse_device_id(raw)
    if not host or not _HOST_OK.match(host):
        raise _Exclude(REASON_NO_HOST)
    if raw != raw.strip():
        problems.append(f"{dev_id}: DEVICE_ID_STRING {raw!r} has surrounding whitespace; using host {host!r}")
    return host, rest


def _map_modbus(ch: dict, dev: dict, dev_id: str) -> _Mapped:
    problems: list[str] = []
    host, rest = _host_and_rest(dev, dev_id, problems)
    unit_id = 0
    if rest:
        m = re.fullmatch(r"\.(\d+)", rest)
        if m:
            unit_id = int(m[1])
        else:
            problems.append(f"{dev_id}: cannot read a Modbus unit id from {rest!r}; using 0")
    p = "modbus_ethernet."
    if _int(dev.get(p + "DEVICE_ETHERNET_IP_PROTOCOL"), 1) == 0:
        problems.append(f"{dev_id}: KEPServer uses Modbus over UDP; modbus_tcp imports it as TCP")
    nonnormal_raw = ch.get("servermain.CHANNEL_NON_NORMALIZED_FLOATING_POINT_HANDLING")
    nonnormal = {0: "zero", 1: "unmodified"}.get(_int(nonnormal_raw, 0))
    if nonnormal is None:
        problems.append(f"{dev_id}: unknown non-normalized float handling {nonnormal_raw!r}; using 'zero'")
        nonnormal = "zero"
    params = {
        "unit_id": unit_id,
        "zero_based": bool(_get(dev, p + "DEVICE_ZERO_BASED_ADDRESSING", True)),
        "zero_based_bit": bool(_get(dev, p + "DEVICE_ZERO_BASED_BIT_ADDRESSING", True)),
        "modicon_bit_order": bool(_get(dev, p + "DEVICE_MODICON_BIT_ORDER", False)),
        "byte_order_big": bool(_get(dev, p + "DEVICE_MODBUS_BYTE_ORDER", True)),
        "first_word_low": bool(_get(dev, p + "DEVICE_FIRST_WORD_LOW", True)),
        "first_dword_low": bool(_get(dev, p + "DEVICE_FIRST_DWORD_LOW", True)),
        "max_coils": _int(dev.get(p + "DEVICE_OUTPUT_COILS"), 64),
        "max_discrete": _int(dev.get(p + "DEVICE_INPUT_COILS"), 64),
        "max_input": _int(dev.get(p + "DEVICE_INTERNAL_REGISTERS"), 64),
        "max_holding": _int(dev.get(p + "DEVICE_HOLDING_REGISTERS"), 64),
        "max_gap": 4,
        "max_gap_bits": None,           # driver default: max_gap * 16
        "nonnormal_float": nonnormal,
        "close_on_timeout": bool(_get(dev, p + "DEVICE_ETHERNET_CLOSE_TCP_SOCKET_ON_TIMEOUT", True)),
    }
    # The device-level port is the one KEPServer connects to (the channel-level
    # CHANNEL_ETHERNET_PORT_NUMBER is not the PLC port).
    port = _int(dev.get(p + "DEVICE_ETHERNET_PORT_NUMBER"), 502)
    return _Mapped("modbus_tcp", host, port, params, problems=problems)


def _map_micro800(ch: dict, dev: dict, dev_id: str) -> _Mapped:
    problems: list[str] = []
    host, rest = _host_and_rest(dev, dev_id, problems)
    path, slot = _cip_path(rest)
    params = {"micro800": True, "path": path, "slot": slot, "max_batch": 40, "connection_size": None}
    port = _int(dev.get("allenbradley_micro800_ethernet.DEVICE_PORT_NUMBER"), 44818)
    return _Mapped("ab_logix", host, port, params, problems=problems)


def _map_controllogix(ch: dict, dev: dict, dev_id: str) -> _Mapped:
    model = _int(dev.get(K_MODEL))
    if model not in (CLX_MODEL_LOGIX, CLX_MODEL_PCCC):
        raise _Exclude(f"unmapped ControlLogix model {dev.get(K_MODEL)}")
    problems: list[str] = []
    host, rest = _host_and_rest(dev, dev_id, problems)
    path, slot = _cip_path(rest)
    p = "controllogix_ethernet."
    port = _int(dev.get(p + "DEVICE_PORT_NUMBER"), _int(dev.get(p + "DEVICE_CL_ENET_PORT_NUMBER"), 44818))
    if model == CLX_MODEL_LOGIX:
        params = {
            "micro800": False,
            "path": path,
            "slot": slot,
            "max_batch": 40,
            "connection_size": _int(dev.get(p + "DEVICE_CONNECTION_SIZE_BYTES")),
        }
        return _Mapped("ab_logix", host, port, params, problems=problems)
    params = {
        "path": path,
        "max_elements": 100,
        "max_gap": 4,
        "request_size": _int(dev.get(p + "DEVICE_REQUEST_SIZE"), 232),
    }
    return _Mapped("ab_pccc", host, port, params, problems=problems)


def _map_s7(ch: dict, dev: dict, dev_id: str) -> _Mapped:
    problems: list[str] = []
    host, rest = _host_and_rest(dev, dev_id, problems)
    if rest:
        problems.append(f"{dev_id}: ignored {rest!r} after the host in DEVICE_ID_STRING")
    p = "siemens_tcpip_ethernet."
    params = {
        "rack": _int(dev.get(p + "DEVICE_S7_COMMUNICATIONS_CPU_RACK"), 0),
        "slot": _int(dev.get(p + "DEVICE_S7_COMMUNICATIONS_CPU_SLOT"), 1),
        "max_pdu": _int(dev.get(p + "DEVICE_S7_COMMUNICATIONS_MAX_PDU"), 480),
        "link_type": _int(dev.get(p + "DEVICE_S7_COMMUNICATIONS_300_400_1200_1500_LINK_TYPE")),
        # Kepware: 0 = Big Endian (S7 default), 1 = Little Endian
        "byte_order_big": _int(dev.get(p + "DEVICE_ADDRESSING_BYTE_ORDER"), 0) == 0,
    }
    port = _int(dev.get(p + "DEVICE_COMMUNICATIONS_PORT_NUMBER"), 102)
    return _Mapped("s7", host, port, params, problems=problems)


def _map_melsec(ch: dict, dev: dict, dev_id: str) -> _Mapped:
    problems: list[str] = []
    host, rest = _host_and_rest(dev, dev_id, problems)
    if rest:
        problems.append(f"{dev_id}: ignored {rest!r} after the host in DEVICE_ID_STRING")
    p = "mitsubishi_ethernet."
    params = {
        "plc_type": "Q",
        # Kepware: 0 = UDP, 1 = TCP/IP
        "protocol": "udp" if _int(dev.get(p + "DEVICE_IP_PROTOCOL"), 1) == 0 else "tcp",
        "first_word_low": bool(_get(dev, p + "DEVICE_FIRST_WORD_LOW", True)),
        "frame": "3E",
    }
    problems.append(
        f"{dev_id}: Mitsubishi plc_type set to 'Q' (KEPServer model {dev.get(K_MODEL)}, "
        f"CPU {dev.get(p + 'DEVICE_CPU')}); confirm before go-live"
    )
    port = _int(dev.get(p + "DEVICE_PORT_NUMBER"), 5001)
    return _Mapped("melsec_mc", host, port, params, problems=problems)


def _enum_name(table: dict[int, str], value: Any, what: str, dev_id: str, problems: list[str], default: str) -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    code = _int(value)
    if code in table:
        return table[code]
    problems.append(f"{dev_id}: unknown OPC UA {what} code {value!r}; using {default!r}")
    return default


def _map_opcua(ch: dict, dev: dict, dev_id: str) -> _Mapped:
    problems: list[str] = []
    p = "opcuaclient."
    url = ch.get(p + "CHANNEL_UA_SERVER_ENDPOINT_URL") or ""
    parts = urlsplit(url.strip())
    host = parts.hostname or ""
    if not host:
        raise _Exclude("no OPC UA endpoint URL on the channel")
    port = parts.port or 4840
    netloc = f"{host}:{parts.port}" if parts.port else host   # drop any user:password@
    endpoint = urlunsplit((parts.scheme or "opc.tcp", netloc, parts.path, parts.query, parts.fragment))
    user_env, pass_env = opcua_env_names(ch.get(K_NAME, ""))
    has_user = bool(ch.get(p + "CHANNEL_AUTHENTICATION_USERNAME"))
    has_pass = bool(ch.get(p + "CHANNEL_AUTHENTICATION_PASSWORD"))
    params = {
        "endpoint": endpoint,
        "security_policy": _enum_name(
            OPCUA_SECURITY_POLICIES, ch.get(p + "CHANNEL_UA_SERVER_SECURITY_POLICY"),
            "security policy", dev_id, problems, "None"),
        "message_mode": _enum_name(
            OPCUA_MESSAGE_MODES, ch.get(p + "CHANNEL_UA_SERVER_MESSAGE_MODE"),
            "message mode", dev_id, problems, "None"),
        "username_env": user_env if has_user else None,
        "password_env": pass_env if has_pass else None,
        "publishing_interval_ms": _int(dev.get(p + "DEVICE_SUBSCRIPTION_PUBLISHING_INTERVAL_MILLISECONDS")),
        "sampling_interval_ms": _int(dev.get(p + "DEVICE_MONITORED_ITEMS_SAMPLE_INTERVAL_MILLISECONDS")),
    }
    if has_user or has_pass:
        names = " and ".join(n for n, on in ((user_env, has_user), (pass_env, has_pass)) if on)
        problems.append(f"{dev_id}: OPC UA credentials were not copied; set {names} in the environment")
    source = {
        "kep_security_policy": ch.get(p + "CHANNEL_UA_SERVER_SECURITY_POLICY"),
        "kep_message_mode": ch.get(p + "CHANNEL_UA_SERVER_MESSAGE_MODE"),
    }
    return _Mapped("opcua_client", host, port, params, source=source, problems=problems)


DRIVER_MAPPERS = {
    KEP_MODBUS: _map_modbus,
    KEP_MICRO800: _map_micro800,
    KEP_CLX: _map_controllogix,
    KEP_S7: _map_s7,
    KEP_MELSEC: _map_melsec,
    KEP_OPCUA: _map_opcua,
}


# --- tags --------------------------------------------------------------------

def _map_scale(raw: dict[str, Any], dev_id: str, name: str, problems: list[str]) -> Scale | None:
    kind = _int(raw.get(K_SCALE + "TYPE"), 0)
    if kind == 0:
        return None
    if kind not in (1, 2):
        problems.append(f"{dev_id}: tag {name!r}: unknown scaling type {kind}; scaling ignored")
        return None
    units = raw.get(K_SCALE + "UNITS") or None
    scale = Scale(
        type="linear" if kind == 1 else "sqrt",
        raw_lo=float(_get(raw, K_SCALE + "RAW_LOW", 0.0)),
        raw_hi=float(_get(raw, K_SCALE + "RAW_HIGH", 1000.0)),
        eng_lo=float(_get(raw, K_SCALE + "SCALED_LOW", 0.0)),
        eng_hi=float(_get(raw, K_SCALE + "SCALED_HIGH", 1000.0)),
        clamp_lo=bool(_get(raw, K_SCALE + "CLAMP_LOW", False)),
        clamp_hi=bool(_get(raw, K_SCALE + "CLAMP_HIGH", False)),
        negate=bool(_get(raw, K_SCALE + "NEGATE_VALUE", False)),
        units=str(units) if units else None,
    )
    if scale.raw_hi == scale.raw_lo:
        problems.append(f"{dev_id}: tag {name!r}: scaling raw_hi == raw_lo; every read will be bad_decode")
    return scale


def _map_dtype(code_raw: Any, dev: dict, dev_id: str, name: str, problems: list[str]) -> tuple[str | None, bool]:
    """Return (dtype, enabled). dtype None = cannot import the tag."""
    code = _int(code_raw, -1)
    if code == -1:
        default = _default_data_type(dev)
        if default is None:
            problems.append(f"{dev_id}: tag {name!r}: KEPServer 'Default' data type and no device default; tag skipped")
            return None, False
        code = default
    dtype = KEP_DTYPES.get(code)
    if dtype is None:
        problems.append(f"{dev_id}: tag {name!r}: unsupported KEPServer data type {code_raw!r}; tag skipped")
        return None, False
    kep_name = KEP_TYPE_NAMES.get(code, str(code))
    if code in WIDENED_TYPES:
        problems.append(f"{dev_id}: tag {name!r}: KEPServer {kep_name} imported as {dtype} (no 8-bit dtype); verify")
    if code in DISABLED_TYPES:
        problems.append(f"{dev_id}: tag {name!r}: KEPServer {kep_name} has no decoder yet; imported disabled")
        return dtype, False
    return dtype, True


def _map_tags(dev: dict, dev_id: str, per_tag_scan: bool, problems: list[str]) -> list[Tag]:
    tags: list[Tag] = []
    seen: set[str] = set()
    for name, raw in _iter_tags(dev):
        if name in seen:
            problems.append(f"{dev_id}: duplicate tag name {name!r} (address {raw.get(K_TAG_ADDR)!r}); kept the first")
            continue
        address_raw = raw.get(K_TAG_ADDR)
        address = str(address_raw if address_raw is not None else "").strip()
        if not address:
            problems.append(f"{dev_id}: tag {name!r} has no address; tag skipped")
            continue
        dtype, enabled = _map_dtype(raw.get(K_TAG_TYPE), dev, dev_id, name, problems)
        if dtype is None:
            continue
        seen.add(name)
        scan = _int(raw.get(K_TAG_SCAN)) if per_tag_scan else None
        desc = raw.get(K_DESC)
        tags.append(Tag(
            name=name,
            address=address,
            dtype=dtype,
            scan_ms=scan if scan and scan > 0 else None,
            scale=_map_scale(raw, dev_id, name, problems),
            enabled=enabled,
            description=str(desc) if desc else None,
        ))
    return tags


# --- import ------------------------------------------------------------------

def _load_project(project_path: Path) -> dict[str, Any]:
    data = json.loads(Path(project_path).read_text(encoding="utf-8-sig"))   # KEPServer writes a BOM
    project = data.get("project", data) if isinstance(data, dict) else None
    if not isinstance(project, dict) or not isinstance(project.get("channels"), list):
        raise ValueError(f"{project_path}: not a KEPServer project export (no project.channels list)")
    return project


def _build_device(
    ch: dict, dev: dict, channel_name: str, device_name: str, defaults: DeviceDefaults, problems: list[str]
) -> Device:
    dev_id = f"{channel_name}/{device_name}"
    kep_driver = dev.get(K_DRIVER) or ch.get(K_DRIVER) or ""
    mapper = DRIVER_MAPPERS.get(kep_driver)
    if mapper is None:
        raise _Exclude(EXCLUDED_DRIVERS.get(kep_driver, f"unsupported KEPServer driver {kep_driver!r}"))
    mapped = mapper(ch, dev, dev_id)
    if not dev.get(K_COLLECT, True):
        raise _Exclude(REASON_DISABLED)
    problems.extend(mapped.problems)

    scan_mode = _int(dev.get(K_SCAN_MODE), 0)
    rate = _int(dev.get(K_SCAN_RATE))
    scan_ms, min_scan_ms, per_tag_scan = defaults.scan_ms, defaults.min_scan_ms, True
    if scan_mode == 1 and rate:            # "request data no faster than scan rate"
        min_scan_ms = max(defaults.min_scan_ms, rate)
    elif scan_mode == 2 and rate:          # "request all data at scan rate"
        scan_ms, per_tag_scan = rate, False
        problems.append(f"{dev_id}: KEPServer scans every tag at {rate} ms; device scan_ms={rate}, tag rates dropped")
    elif scan_mode == 3:
        problems.append(f"{dev_id}: KEPServer scan mode 3 (demand poll only); imported with tag scan rates")

    attempts = _int(dev.get(K_ATTEMPTS))
    demote_on = bool(dev.get(K_DEMOTE_ON, False))
    source = {
        "kep_driver": kep_driver,
        "kep_model": dev.get(K_MODEL),
        "device_id_string": dev.get(K_ID),
        "kep_retry_attempts": attempts,
        "kep_scan_mode": scan_mode,
        **mapped.source,
    }
    tags = _map_tags(dev, dev_id, per_tag_scan, problems)
    return Device(
        id=dev_id,
        channel=channel_name,
        name=device_name,
        driver=mapped.driver,
        host=mapped.host,
        port=mapped.port,
        params=mapped.params,
        scan_ms=scan_ms,
        min_scan_ms=min_scan_ms,
        timeout_ms=_int(dev.get(K_TIMEOUT), defaults.timeout_ms),
        retries=max(attempts - 1, 0) if attempts is not None else defaults.retries,
        demote_after=_int(dev.get(K_DEMOTE_AFTER), defaults.demote_after) if demote_on else defaults.demote_after,
        demote_for_ms=_int(dev.get(K_DEMOTE_MS), defaults.demote_for_ms) if demote_on else defaults.demote_for_ms,
        tags=tags,
        source=source,
    )


def _same_address_rows(
    channel_name: str, device_name: str, dev: dict, included: bool
) -> list[SameAddressRow]:
    by_addr: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for name, raw in _iter_tags(dev):
        by_addr[str(raw.get(K_TAG_ADDR, ""))].append((name, _dtype_label(raw.get(K_TAG_TYPE))))
    rows = []
    for address, entries in by_addr.items():
        if len(entries) < 2:
            continue
        names = [n for n, _ in entries]
        dtypes = [d for _, d in entries]
        cls, reason = classify_group(names, dtypes)
        rows.append(SameAddressRow(
            channel=channel_name, device=device_name, device_id=f"{channel_name}/{device_name}",
            address=address, tags=names, dtypes=dtypes, n_pairs=len(entries) - 1,
            classification=cls, reason=reason, included=included,
        ))
    return rows


def import_kep(project_path: Path, defaults: DeviceDefaults | None = None) -> ImportResult:
    """Map a raw KEPServer project export onto `DevicesFile` (no files are written)."""
    project_path = Path(project_path)
    defaults = defaults or DeviceDefaults()
    project = _load_project(project_path)
    problems: list[str] = []
    channels: list[Channel] = []
    excluded: list[ExcludedDevice] = []
    same_address: list[SameAddressRow] = []
    src_by_driver: dict[str, Counter] = defaultdict(Counter)
    empty_channels: list[str] = []
    n_src_devices = n_src_tags = 0
    seen_ids: set[str] = set()

    for ch in project["channels"]:
        channel_name = str(ch.get(K_NAME, ""))
        kep_driver = ch.get(K_DRIVER) or ""
        devices_raw = ch.get("devices") or []
        src_by_driver[kep_driver].update({"channels": 1, "devices": 0, "tags": 0})
        if not devices_raw:
            empty_channels.append(channel_name)
            problems.append(f"channel {channel_name!r} ({kep_driver}) has no devices; skipped")
            continue
        included_devices: list[Device] = []
        for dev in devices_raw:
            device_name = str(dev.get(K_NAME, ""))
            dev_id = f"{channel_name}/{device_name}"
            n_tags = sum(1 for _ in _iter_tags(dev))
            n_src_devices += 1
            n_src_tags += n_tags
            src_by_driver[kep_driver]["devices"] += 1
            src_by_driver[kep_driver]["tags"] += n_tags
            dev_problems: list[str] = []
            try:
                if "/" in channel_name or "/" in device_name:
                    dev_problems.append(f"{dev_id}: '/' in a channel or device name makes the device id ambiguous")
                if dev_id in seen_ids:
                    raise _Exclude("duplicate device id")
                device = _build_device(ch, dev, channel_name, device_name, defaults, dev_problems)
            except _Exclude as exc:
                excluded.append(ExcludedDevice(
                    channel=channel_name, device=device_name, kep_driver=dev.get(K_DRIVER) or kep_driver,
                    tags=n_tags, reason=exc.reason,
                ))
                if exc.reason != REASON_DISABLED and not dev.get(K_COLLECT, True):
                    dev_problems.append(f"{dev_id}: excluded ({exc.reason}); data collection is also disabled in KEPServer")
                problems.extend(dev_problems)
                same_address.extend(_same_address_rows(channel_name, device_name, dev, included=False))
                continue
            seen_ids.add(dev_id)
            problems.extend(dev_problems)
            if kep_driver == KEP_OPCUA:
                problems.append(
                    f"{dev_id}: kept although the channel looks like a test channel "
                    "(needed to develop the opcua_client driver)"
                )
            included_devices.append(device)
            same_address.extend(_same_address_rows(channel_name, device_name, dev, included=True))
        if included_devices:
            channels.append(Channel(name=channel_name, devices=included_devices))

    devices_file = DevicesFile(
        generated_from=project_path.name,
        generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        channels=channels,
        excluded=excluded,
    )

    # stub SubDeviceId collisions are worth knowing about before go-live
    stub_ids = Counter(
        stub_sub_device_id(c.name, d.name, len(c.devices) == 1) for c in channels for d in c.devices
    )
    for sid, n in sorted(stub_ids.items()):
        if n > 1:
            problems.append(f"stub SubDeviceId {sid!r} would be used by {n} devices; fix devices_map.yaml")

    all_devices = [d for c in channels for d in c.devices]
    below_min = sum(
        1 for d in all_devices for t in d.tags if t.enabled and (t.scan_ms or d.scan_ms) < d.min_scan_ms
    )
    by_driver: dict[str, dict[str, int]] = {}
    for d in all_devices:
        c = by_driver.setdefault(d.driver, {"devices": 0, "tags": 0})
        c["devices"] += 1
        c["tags"] += len(d.tags)
    micro = [d for d in all_devices if d.params.get("micro800")]
    by_reason: dict[str, dict[str, int]] = {}
    for e in excluded:
        c = by_reason.setdefault(e.reason, {"devices": 0, "tags": 0})
        c["devices"] += 1
        c["tags"] += e.tags

    summary = {
        "source": {
            "file": project_path.name,
            "channels": len(project["channels"]),
            "devices": n_src_devices,
            "tags": n_src_tags,
            "by_kep_driver": {k: dict(v) for k, v in sorted(src_by_driver.items())},
        },
        "included": {
            "channels": len(channels),
            "devices": len(all_devices),
            "tags": sum(len(d.tags) for d in all_devices),
            "by_driver": dict(sorted(by_driver.items())),
            "micro800": {"devices": len(micro), "tags": sum(len(d.tags) for d in micro)},
        },
        "excluded": {
            "devices": len(excluded),
            "tags": sum(e.tags for e in excluded),
            "by_reason": dict(sorted(by_reason.items())),
            "channels_without_devices": empty_channels,
        },
        "same_address": {
            "groups": len(same_address),
            "pairs": sum(r.n_pairs for r in same_address),
            "pairs_included": sum(r.n_pairs for r in same_address if r.included),
            "alias_groups": sum(1 for r in same_address if r.classification == "alias"),
            "suspect_groups": sum(1 for r in same_address if r.classification == "suspect"),
            "pair_counting": "n_tags - 1 per (device, address), as kep2poller.py",
        },
        "min_scan_ms": defaults.min_scan_ms,
        "tags_below_min_scan": below_min,
    }
    return ImportResult(devices_file=devices_file, same_address=same_address, summary=summary, problems=problems)


# --- outputs -----------------------------------------------------------------

def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def devices_json_text(devices_file: DevicesFile) -> str:
    """Pretty JSON with one tag per line (hand-editable, diff-friendly)."""
    doc = devices_file.model_dump(mode="json", exclude_none=True)
    nonce = uuid.uuid4().hex                     # placeholders cannot collide with real strings
    tag_lines: dict[str, str] = {}
    for ch in doc["channels"]:
        for dev in ch["devices"]:
            placeholders = []
            for tag in dev["tags"]:
                key = f"@@{nonce}:{len(tag_lines)}@@"
                tag_lines[key] = json.dumps(tag, ensure_ascii=False)
                placeholders.append(key)
            dev["tags"] = placeholders
    text = json.dumps(doc, indent=2, ensure_ascii=False)
    text = re.sub(rf'"(@@{nonce}:\d+@@)"', lambda m: tag_lines[m[1]], text)
    return text + "\n"


def _stub_entries(devices_file: DevicesFile) -> dict[str, DeviceMapEntry]:
    out: dict[str, DeviceMapEntry] = {}
    for ch in devices_file.channels:
        single = len(ch.devices) == 1
        for dev in ch.devices:
            out[dev.id] = DeviceMapEntry(sub_device_id=stub_sub_device_id(ch.name, dev.name, single), stub=True)
    return out


def _yaml_entries(entries: dict[str, DeviceMapEntry]) -> str:
    data = {k: v.model_dump(exclude_none=True) for k, v in entries.items()}
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False, width=1000)


def update_devices_map(path: Path, devices_file: DevicesFile) -> tuple[str, Path | None]:
    """Create devices_map.yaml, or append stubs for new device ids. Never rewrites it.

    Returns (human status, path written or None).
    """
    stubs = _stub_entries(devices_file)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not path.exists():
        body = "defaults: {}\ndevices:\n" + _indent(_yaml_entries(stubs)) if stubs else "defaults: {}\ndevices: {}\n"
        _atomic_write(path, DEVICES_MAP_HEADER + body)
        return f"created with {len(stubs)} stub entries", path

    text = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text) or {}
        existing = DeviceMap.model_validate(data)
    except Exception as exc:  # noqa: BLE001 - any parse/validation error: leave the file alone
        return f"not updated: existing file is invalid ({type(exc).__name__}); fix it and re-import", None

    new = {k: v for k, v in stubs.items() if k not in existing.devices}
    if not new:
        return f"unchanged ({len(existing.devices)} entries, no new devices)", None
    expected = DeviceMap(defaults=existing.defaults, devices={**existing.devices, **new})
    block = f"  # stubs added by opencap import-kep on {stamp}\n" + _indent(_yaml_entries(new))
    base = text if text.endswith("\n") or not text else text + "\n"
    candidates = [base + block]
    if "devices" not in data:
        candidates.append(base + "devices:\n" + block)
    for candidate in candidates:
        try:
            if DeviceMap.model_validate(yaml.safe_load(candidate) or {}) == expected:
                _atomic_write(path, candidate)
                return f"appended {len(new)} stub entries ({len(existing.devices)} existing kept)", path
        except Exception:  # noqa: BLE001
            continue
    side = path.with_name(path.stem + ".stubs.yaml")
    _atomic_write(side, "# New device ids not in devices_map.yaml: merge these by hand.\ndevices:\n"
                  + _indent(_yaml_entries(new)))
    return (f"not updated: cannot append safely to its layout; {len(new)} new stub entries "
            f"written to {side.name} for a manual merge"), side


def _indent(text: str, prefix: str = "  ") -> str:
    return "".join(prefix + line if line.strip() else line for line in text.splitlines(keepends=True))


def same_address_csv_text(rows: list[SameAddressRow]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=SameAddressRow.CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    order = {"suspect": 0, "alias": 1}
    for row in sorted(rows, key=lambda r: (not r.included, order.get(r.classification, 2), r.channel, r.device)):
        writer.writerow(row.csv_row())
    return buf.getvalue()


def write_outputs(result: ImportResult, config_dir: Path, reports_dir: Path) -> list[Path]:
    """Write config files and review reports; return the paths written."""
    config_dir, reports_dir = Path(config_dir), Path(reports_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    outputs: dict[str, str] = {}

    devices_json = config_dir / "devices.json"
    _atomic_write(devices_json, devices_json_text(result.devices_file))
    written.append(devices_json)
    outputs["devices_json"] = "written"

    status, map_path = update_devices_map(config_dir / "devices_map.yaml", result.devices_file)
    outputs["devices_map"] = status
    if map_path is not None:
        written.append(map_path)
    if status.startswith("not updated"):
        result.problems.append(f"devices_map.yaml {status}")

    overrides = config_dir / "overrides.yaml"
    if overrides.exists():
        outputs["overrides"] = "existing file kept"
    else:
        _atomic_write(overrides, OVERRIDES_TEMPLATE)
        written.append(overrides)
        outputs["overrides"] = "template written"

    csv_path = reports_dir / "same_address.csv"
    _atomic_write(csv_path, same_address_csv_text(result.same_address))
    written.append(csv_path)

    result.summary["outputs"] = outputs
    summary_path = reports_dir / "import_summary.json"
    doc = {**result.summary, "problems": result.problems}
    _atomic_write(summary_path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    written.append(summary_path)
    return written
