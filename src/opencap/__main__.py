"""opencap CLI (PLAN.md section 9)."""

from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer(add_completion=False, no_args_is_help=True, help="opencap: read-only PLC poller → MQTT")

CONFIG_OPT = typer.Option(Path("config"), "--config", "-c", help="Config directory")


@app.command("import-kep")
def import_kep_cmd(
    project: Path = typer.Argument(..., exists=True, dir_okay=False, help="KEPServer project.json export"),
    config_dir: Path = CONFIG_OPT,
    reports_dir: Path = typer.Option(Path("reports"), "--reports", help="Where to write review reports"),
) -> None:
    """Import a KEPServer project.json into config/devices.json (+ reports)."""
    from opencap.config.kep_import import import_kep, write_outputs

    result = import_kep(project)
    written = write_outputs(result, config_dir=config_dir, reports_dir=reports_dir)
    typer.echo(result.summary_text())
    for path in written:
        typer.echo(f"wrote {path}")


@app.command("check")
def check_cmd(
    config_dir: Path = CONFIG_OPT,
    site: str | None = typer.Option(None, "--site", help="Only this channel/site"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Print every block"),
) -> None:
    """Validate config, print the block plan and estimated cycle time per site."""
    from opencap.core.check import run_check

    raise typer.Exit(run_check(config_dir, site=site, verbose=verbose))


def _find_device(config_dir: Path, device_id: str):
    from opencap.config.loader import load_config

    loaded = load_config(config_dir)
    for channel in loaded.channels:
        for device in channel.devices:
            if device.id == device_id:
                return loaded, channel, device
    typer.echo(f"unknown device {device_id!r} (ids look like '<channel>/<device>')", err=True)
    raise typer.Exit(1)


@app.command("read")
def read_cmd(
    device_id: str = typer.Argument(..., help="'<channel>/<device>', e.g. 'Plant A/PLC1'"),
    tag: str | None = typer.Option(None, "--tag", help="Only print this tag"),
    config_dir: Path = CONFIG_OPT,
) -> None:
    """One read of a device (all its tags), print values. No MQTT."""
    import asyncio

    from opencap.core.cache import ValueCache
    from opencap.core.channel_worker import ChannelWorker
    from opencap.core.registry import plan_device
    from opencap.logsetup import configure_logging

    configure_logging("WARNING")
    _, channel, device = _find_device(config_dir, device_id)
    plan = plan_device(device)
    if not plan.ok:
        typer.echo(f"{device_id}: {plan.error}", err=True)
        raise typer.Exit(1)
    single = channel.model_copy(update={"devices": [device]})

    async def go():
        cache = ValueCache()
        cache.register(device)
        worker = ChannelWorker(single, {device.id: plan}, cache)
        try:
            return await worker.read_once(device.id), worker.device_status(device.id).get("last_error")
        finally:
            await worker.stop()

    values, last_error = asyncio.run(go())
    names = [tag] if tag else sorted(values)
    for name in names:
        tv = values.get(name)
        if tv is None:
            typer.echo(f"{name:<40} (no such tag)")
            continue
        typer.echo(f"{name:<40} {tv.quality:<10} {tv.value!r}")
    bad = sum(1 for tv in values.values() if tv.quality != "good")
    if bad and last_error:
        typer.echo(f"{device_id}: {last_error}", err=True)
    raise typer.Exit(0 if bad == 0 else 3)


@app.command("identity")
def identity_cmd(device_id: str, config_dir: Path = CONFIG_OPT) -> None:
    """Vendor/model/firmware of a device, if its protocol reports it."""
    import asyncio
    import json
    from concurrent.futures import ThreadPoolExecutor

    from opencap.drivers import get_driver_class

    _, _, device = _find_device(config_dir, device_id)
    try:
        driver_cls = get_driver_class(device.driver)
    except LookupError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    async def go():
        with ThreadPoolExecutor(max_workers=1) as ex:
            driver = driver_cls(device, ex)
            await driver.connect()
            try:
                return await driver.identity()
            finally:
                await driver.close()

    typer.echo(json.dumps(asyncio.run(go()), indent=2, default=str))


@app.command("info")
def info_cmd(
    config_dir: Path = CONFIG_OPT,
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of text"),
) -> None:
    """Support summary, safe to paste into a public issue: versions, platform, drivers, north
    interfaces and the config revision. Never an IP address, host name or secret."""
    import json

    from opencap.core.support import render_text, support_info

    poller_cfg, revision = None, None
    try:
        from opencap.config.loader import load_config
        from opencap.config.store import ConfigStore

        poller_cfg = load_config(config_dir).poller
        revision = ConfigStore(config_dir).revision()
    except Exception:       # a broken or missing config still gets the build information
        pass
    info = support_info(poller_cfg, revision)
    typer.echo(json.dumps(info, indent=2) if as_json else render_text(info))


@app.command("run")
def run_cmd(
    config_dir: Path = CONFIG_OPT,
    site: list[str] = typer.Option(None, "--site", help="Poll only this channel/site (repeatable); for per-site cut-over"),
) -> None:
    """Run the poller: poll PLCs, publish to MQTT, serve the admin API."""
    import asyncio

    from opencap.config.loader import load_config
    from opencap.core.runtime import Poller, event_loop_factory
    from opencap.logsetup import configure_logging

    loaded = load_config(config_dir)
    known = {c.name for c in loaded.channels}
    unknown = [s for s in (site or []) if s not in known]
    if unknown:
        typer.echo(f"unknown site(s): {', '.join(unknown)}", err=True)
        raise typer.Exit(1)
    configure_logging(loaded.poller.log_level)
    try:
        asyncio.run(Poller(config_dir, loaded=loaded, only_channels=site or None).run_forever(),
                    loop_factory=event_loop_factory())
    except KeyboardInterrupt:
        # Ctrl+C where the loop has no signal handlers (Windows, e.g. a WinSW service stop):
        # asyncio cancelled run_forever(), which has already stopped the poller cleanly.
        pass


def main() -> None:
    app()


if __name__ == "__main__":
    main()
