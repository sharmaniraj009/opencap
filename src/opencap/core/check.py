"""`opencap check`: validate config, print the block plan and estimated cycle time per site."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import typer
from pydantic import ValidationError

from opencap.core.registry import DevicePlan, Registry

MAX_WARNINGS_SHOWN = 15


def _fmt_rates(dp: DevicePlan) -> str:
    return ",".join(f"{g.rate_ms // 1000}s" if g.rate_ms % 1000 == 0 else f"{g.rate_ms}ms" for g in dp.groups) or "-"


def run_check(config_dir: Path, site: str | None = None, verbose: bool = False) -> int:
    """Return the process exit code: 0 = config valid, 1 = config invalid."""
    from opencap.config.loader import load_config

    echo = typer.echo
    try:
        loaded = load_config(config_dir)
    except FileNotFoundError as exc:
        echo(f"config error: {exc}", err=True)
        return 1
    except (ValidationError, ValueError) as exc:
        echo(f"config invalid:\n{exc}", err=True)
        return 1

    channels = loaded.channels
    if site is not None:
        channels = [c for c in channels if c.name == site]
        if not channels:
            echo(f"no channel named {site!r}", err=True)
            return 1

    rtt = loaded.poller.check.est_rtt_ms
    registry = Registry.build(channels)

    header = f"{'SITE / DEVICE':<44} {'DRIVER':<13} {'TAGS':>5} {'BAD':>4} {'RATES':<9} {'BLK':>4} {'REQ':>4} {'CYCLE':>8} {'LOAD':>6}"
    echo(header)
    echo("-" * len(header))

    overloaded: list[str] = []
    for channel in channels:
        site_req = 0
        site_load = 0.0
        if not channel.enabled:
            echo(f"{channel.name}  (channel disabled)")
            continue
        echo(channel.name)
        for device in channel.devices:
            dp = registry.plans[device.id]
            label = f"  {device.name}" + ("" if device.enabled else " (disabled)")
            if not dp.ok:
                echo(f"{label:<44} {device.driver:<13} {len(device.tags):>5}  -- no plan: {dp.error}")
                continue
            load = dp.est_load(rtt)
            if device.enabled:
                site_req += dp.wire_requests
                site_load += load
            echo(
                f"{label[:44]:<44} {device.driver:<13} {dp.enabled_tags:>5} {len(dp.invalid):>4} "
                f"{_fmt_rates(dp):<9} {dp.blocks:>4} {dp.wire_requests:>4} "
                f"{dp.est_cycle_ms(rtt):>6.0f}ms {load:>6.0%}"
            )
            if verbose:
                for g in dp.groups:
                    for block in g.plan.blocks:
                        echo(f"      [{g.rate_ms}ms] {block.describe()}  <- {', '.join(block.tags)}")
                for name, reason in sorted(dp.invalid.items()):
                    echo(f"      bad_config {name}: {reason}")
        if len(channel.devices) > 1:
            echo(f"{'  = site total':<44} {'':<13} {'':>5} {'':>4} {'':<9} {'':>4} {site_req:>4} "
                 f"{site_req * rtt:>6.0f}ms {site_load:>6.0%}")
        if site_load > 1.0:
            overloaded.append(channel.name)

    plans = list(registry.plans.values())
    by_driver = Counter(dp.device.driver for dp in plans)
    planned = Counter(dp.device.driver for dp in plans if dp.ok)
    echo("")
    echo(f"channels: {len(channels)}   devices: {len(plans)}   excluded at import: {len(loaded.excluded)}")
    echo(f"assumed round trip per request: {rtt:.0f} ms (poller.yaml check.est_rtt_ms)")
    for driver in sorted(by_driver):
        echo(f"  {driver:<13} devices {by_driver[driver]:>3}   with block plan {planned[driver]:>3}")
    echo(
        f"blocks: {sum(dp.blocks for dp in plans)}   wire requests per full pass: "
        f"{sum(dp.wire_requests for dp in plans)}   bad_config tags: {sum(len(dp.invalid) for dp in plans)}"
    )
    if overloaded:
        echo(f"OVERLOAD (estimated load > 100%, cycles will overrun): {', '.join(overloaded)}")
    if loaded.warnings:
        echo(f"\n{len(loaded.warnings)} config warning(s):")
        for w in loaded.warnings[:MAX_WARNINGS_SHOWN]:
            echo(f"  - {w}")
        if len(loaded.warnings) > MAX_WARNINGS_SHOWN:
            echo(f"  … {len(loaded.warnings) - MAX_WARNINGS_SHOWN} more")
    return 0
