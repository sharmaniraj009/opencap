"""Product and support information: `GET /api/v1/about`, `GET /api/v1/support`, `opencap info`.

The support summary is safe to paste into a public issue: no IP address, no host name or path
from the config, no secret and no environment variable value. Only facts about the build
(versions, platform) and the names of what is enabled (ADM-03, API-09).
"""

from __future__ import annotations

import platform
import sys
from importlib import metadata
from typing import Any

from opencap import __version__
from opencap.config.schema import SCHEMA_VERSION
from opencap.drivers import DRIVERS

ENGINE = "python"
API_VERSIONS = ["v1"]
# driver name -> distribution of the protocol library behind it
DRIVER_LIBRARIES: dict[str, str] = {
    "modbus_tcp": "pymodbus", "modbus_rtu": "pymodbus", "ab_logix": "pycomm3", "ab_pccc": "pycomm3",
    "s7": "python-snap7", "melsec_mc": "pymcprotocol", "opcua_client": "asyncua", "ab_df1": "pyserial",
}


def version_parts(version: str = __version__) -> tuple[int, int, int]:
    nums = []
    for part in version.split(".")[:3]:
        digits = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    nums += [0] * (3 - len(nums))
    return nums[0], nums[1], nums[2]


def about() -> dict[str, Any]:
    """`GET /api/v1/about`: open, so it carries nothing that identifies a site."""
    major, minor, patch = version_parts()
    return {"product": "opencap", "version": __version__, "version_major": major,
            "version_minor": minor, "version_patch": patch, "engine": ENGINE,
            "api_versions": list(API_VERSIONS), "config_schema_version": SCHEMA_VERSION}


def _library_version(dist: str) -> str | None:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None


def support_info(poller_cfg: Any = None, config_revision: str | None = None) -> dict[str, Any]:
    """`poller_cfg` is the loaded `PollerConfig`; without it only the build is described."""
    north = ["mqtt", "api"]
    if poller_cfg is not None and poller_cfg.opcua_server.enabled:
        north.append("opcua_server")
    drivers = [{"name": name, "library": DRIVER_LIBRARIES.get(name),
                "library_version": _library_version(DRIVER_LIBRARIES[name]) if name in DRIVER_LIBRARIES else None}
               for name in sorted(DRIVERS)]
    return {
        **about(),
        "os": platform.system() or "unknown",
        "arch": platform.machine() or "unknown",
        "toolchain": {"python": platform.python_version(), "implementation": sys.implementation.name},
        "drivers": drivers,
        "north_interfaces": north,
        "config_revision": config_revision,
    }


def render_text(info: dict[str, Any]) -> str:
    """The `opencap info` text."""
    lines = [
        f"opencap {info['version']} ({info['engine']} engine)",
        f"api versions: {', '.join(info['api_versions'])}; config schema {info['config_schema_version']}",
        f"os: {info['os']} {info['arch']}",
        f"toolchain: {info['toolchain']['implementation']} {info['toolchain']['python']}",
        "drivers:",
    ]
    for d in info["drivers"]:
        if d["library"] is None:
            lib = "built in"
        else:
            lib = f"{d['library']} {d['library_version']}" if d["library_version"] else f"{d['library']} (not installed)"
        lines.append(f"  {d['name']:<14} {lib}")
    lines.append(f"north interfaces: {', '.join(info['north_interfaces'])}")
    lines.append(f"config revision: {info['config_revision'] or 'unknown'}")
    return "\n".join(lines)
