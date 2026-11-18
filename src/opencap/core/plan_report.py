"""`GET /check`: the `opencap check` block plan and cycle estimates as JSON.

Same numbers as `core/check.py` (which prints them): per device the enabled tags, bad_config
tags, rate groups, blocks, wire requests per full pass, estimated cycle time and channel
load at `poller.yaml` `check.est_rtt_ms`; per site the totals and an overload flag.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from opencap.config.loader import LoadedConfig, load_config
from opencap.core.registry import DevicePlan, plan_device


def _blocks(dp: DevicePlan) -> list[dict[str, Any]]:
    return [{"rate_ms": g.rate_ms, "describe": b.describe(), "tags": list(b.tags),
             "wire_requests": b.wire_requests()} for g in dp.groups for b in g.plan.blocks]


def device_plan_json(dp: DevicePlan, rtt_ms: float, *, verbose: bool = True) -> dict[str, Any]:
    dev = dp.device
    out: dict[str, Any] = {
        "id": dev.id, "name": dev.name, "driver": dev.driver, "enabled": dev.enabled,
        "tags": len(dev.tags), "plan_error": dp.error,
    }
    if not dp.ok:
        return out
    out.update({
        "enabled_tags": dp.enabled_tags,
        "bad_config": len(dp.invalid),
        "rates_ms": [g.rate_ms for g in dp.groups],
        "groups": [{"rate_ms": g.rate_ms, "tags": g.tag_count, "blocks": len(g.plan.blocks),
                    "wire_requests": g.plan.wire_requests} for g in dp.groups],
        "blocks": dp.blocks,
        "wire_requests": dp.wire_requests,
        "est_cycle_ms": round(dp.est_cycle_ms(rtt_ms), 1),
        "load": round(dp.est_load(rtt_ms), 4),
    })
    if verbose:
        out["block_plan"] = _blocks(dp)
        out["invalid"] = dict(sorted(dp.invalid.items()))
    return out


def plan_report(loaded: LoadedConfig, *, site: str | None = None, verbose: bool = False) -> dict[str, Any]:
    rtt = loaded.poller.check.est_rtt_ms
    channels = loaded.channels if site is None else [c for c in loaded.channels if c.name == site]
    out_channels = []
    overloaded: list[str] = []
    plans: list[DevicePlan] = []
    errors: list[str] = []
    for ch in channels:
        devices = []
        site_req, site_load = 0, 0.0
        for dev in ch.devices:
            try:
                dp = plan_device(dev)
            except Exception as exc:  # noqa: BLE001 - a driver rejecting the device's params
                dp = DevicePlan(device=dev, error=f"driver rejects the device: {exc}")
                errors.append(f"{dev.id}: {exc}")
            plans.append(dp)
            devices.append(device_plan_json(dp, rtt, verbose=verbose))
            if dp.ok and dev.enabled:
                site_req += dp.wire_requests
                site_load += dp.est_load(rtt)
        if ch.enabled and site_load > 1.0:
            overloaded.append(ch.name)
        out_channels.append({
            "name": ch.name, "enabled": ch.enabled, "devices": devices,
            "wire_requests": site_req, "est_cycle_ms": round(site_req * rtt, 1),
            "load": round(site_load, 4), "overloaded": ch.enabled and site_load > 1.0,
        })
    by_driver = Counter(dp.device.driver for dp in plans)
    planned = Counter(dp.device.driver for dp in plans if dp.ok)
    return {
        "ok": not errors,
        "errors": errors,
        "rtt_ms": rtt,
        "site": site,
        "channels": out_channels,
        "totals": {
            "channels": len(channels),
            "devices": len(plans),
            "excluded_at_import": len(loaded.excluded),
            "blocks": sum(dp.blocks for dp in plans),
            "wire_requests": sum(dp.wire_requests for dp in plans),
            "bad_config": sum(len(dp.invalid) for dp in plans),
            "by_driver": {d: {"devices": by_driver[d], "planned": planned[d]} for d in sorted(by_driver)},
        },
        "overloaded": overloaded,
        "warnings": loaded.warnings,
    }


def check_report(config_dir: Path, *, site: str | None = None, verbose: bool = False) -> dict[str, Any]:
    """`plan_report` of the config on disk; `{"ok": false, "error": ...}` when it does not load."""
    try:
        loaded = load_config(config_dir)
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc), "errors": [str(exc)]}
    except ValidationError as exc:
        errs = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors(include_url=False)]
        return {"ok": False, "error": f"config invalid ({exc.title})", "errors": errs}
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "errors": [str(exc)]}
    report = plan_report(loaded, site=site, verbose=verbose)
    if site is not None and not report["channels"]:
        return {"ok": False, "error": f"no channel named {site!r}", "errors": [f"no channel named {site!r}"]}
    return report
