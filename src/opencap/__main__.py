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
    from opencap.core.runtime import ApiStartError, Poller, event_loop_factory
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
    except ApiStartError:
        raise typer.Exit(1) from None      # api.bind_failed is logged: no process without its API (LOG-29)
    except KeyboardInterrupt:
        # Ctrl+C where the loop has no signal handlers (Windows, e.g. a WinSW service stop):
        # asyncio cancelled run_forever(), which has already stopped the poller cleanly.
        pass


tags_app = typer.Typer(no_args_is_help=True, help="Tag lists as KEPServerEX-compatible CSV files")
app.add_typer(tags_app, name="tags")


@tags_app.command("export")
def tags_export_cmd(
    device_id: str = typer.Argument(..., help="'<channel>/<device>', e.g. 'Plant A/PLC1'"),
    out: Path = typer.Argument(..., dir_okay=False, help="CSV file to write"),
    config_dir: Path = CONFIG_OPT,
) -> None:
    """Write a configured device's tags as a KEPServerEX tag CSV."""
    from opencap.config.kep_csv import write_kep_csv

    _, _, device = _find_device(config_dir, device_id)
    write_kep_csv(device.tags, out)
    typer.echo(f"wrote {len(device.tags)} tags to {out}")


@tags_app.command("import")
def tags_import_cmd(
    csv_file: Path = typer.Argument(..., exists=True, dir_okay=False, help="KEPServerEX tag CSV"),
    driver: str = typer.Option(..., "--driver", help="opencap driver the tags belong to, e.g. modbus_tcp"),
    out: Path = typer.Option(..., "--out", help="JSON tag list to write"),
    default_type: str | None = typer.Option(None, "--default-type", help="opencap dtype for 'Default' rows, e.g. int16"),
) -> None:
    """Convert a KEPServerEX tag CSV to a JSON tag list (nothing is written if a row fails)."""
    import json

    from opencap.config.kep_csv import KepCsvError, read_kep_csv

    try:
        tags, warnings = read_kep_csv(csv_file, driver, default_dtype=default_type)
    except KepCsvError as exc:
        for warning in exc.warnings:
            typer.echo(f"warning: {warning}", err=True)
        for error in exc.errors:
            typer.echo(f"error: {error}", err=True)
        typer.echo(f"{len(exc.errors)} row(s) failed; {out} not written", err=True)
        raise typer.Exit(1) from None
    for warning in warnings:
        typer.echo(f"warning: {warning}", err=True)
    out.write_text(json.dumps([t.model_dump(mode="json", exclude_defaults=True) for t in tags], indent=2) + "\n",
                   encoding="utf-8")
    typer.echo(f"wrote {len(tags)} tags to {out}")


@app.command("browse")
def browse_cmd(
    device_id: str = typer.Argument(..., help="'<channel>/<device>', e.g. 'Plant A/PLC1'"),
    out: Path | None = typer.Option(None, "--out", dir_okay=False, help="tags.json or tags.csv (default: JSON on stdout)"),
    expand_arrays: bool = typer.Option(False, "--expand-arrays", help="One tag per array element"),
    config_dir: Path = CONFIG_OPT,
) -> None:
    """Generate tags from the device's own tag list (read-only; ab_logix). Writes a list to review."""
    import asyncio

    from opencap.core.browse import BrowseError, browse_device, tags_json, write_tags
    from opencap.logsetup import configure_logging

    configure_logging("WARNING")
    _, _, device = _find_device(config_dir, device_id)
    try:
        report = asyncio.run(browse_device(device, expand_arrays=expand_arrays))
        if out is not None:
            write_tags(report.tags, out)
    except BrowseError as exc:
        typer.echo(f"browse failed: {exc}", err=True)
        raise typer.Exit(1) from None
    if out is None:
        typer.echo(tags_json(report.tags), nl=False)
    for line in report.summary_lines():
        typer.echo(line, err=True)
    if out is not None:
        typer.echo(f"wrote {len(report.tags)} tags to {out}", err=True)
    raise typer.Exit(3 if report.invalid else 0)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
