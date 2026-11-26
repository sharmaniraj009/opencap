"""`opencap run` on Windows (WinSW service, deploy/windows/).

- Windows has no SIGHUP: the signal table must not reference it (reload is `POST /reload` there).
- aiomqtt/paho need `loop.add_reader`, which the default Windows ProactorEventLoop lacks, so
  `opencap run` uses a SelectorEventLoop on Windows and the default loop elsewhere.
"""

from __future__ import annotations

import asyncio
import signal
import types

import pytest

from opencap.core import runtime


def _poller() -> runtime.Poller:
    # The signal table needs no config: bypass __init__.
    return runtime.Poller.__new__(runtime.Poller)


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="SIGHUP is POSIX-only; test_signal_table_without_sighup covers Windows")
def test_signal_table_on_unix_includes_reload():
    table = _poller()._signal_handlers()
    assert [sig for sig, _ in table] == [signal.SIGTERM, signal.SIGINT, signal.SIGHUP]


def test_signal_table_without_sighup(monkeypatch):
    fake = types.SimpleNamespace(SIGTERM=signal.SIGTERM, SIGINT=signal.SIGINT)   # like Windows
    monkeypatch.setattr(runtime, "signal", fake)
    table = _poller()._signal_handlers()
    assert [sig for sig, _ in table] == [signal.SIGTERM, signal.SIGINT]


def test_event_loop_factory(monkeypatch):
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    assert runtime.event_loop_factory() is asyncio.SelectorEventLoop
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    assert runtime.event_loop_factory() is None


def test_event_loop_factory_runs_a_loop():
    factory = runtime.event_loop_factory()

    async def main() -> str:
        return type(asyncio.get_running_loop()).__name__

    assert asyncio.run(main(), loop_factory=factory)


def test_run_exits_cleanly_after_ctrl_c_without_signal_handlers(tmp_path, monkeypatch):
    """Windows: Ctrl+C reaches asyncio as KeyboardInterrupt after the poller has stopped
    (asyncio cancels run_forever(), whose finally calls stop()). `opencap run` exits 0."""
    from typer.testing import CliRunner

    from opencap.__main__ import app
    from opencap.config.models import Device, Tag
    from tests.helpers.configdir import write_config

    dev = Device(id="P/D", channel="P", name="D", driver="modbus_tcp", host="192.0.2.1", port=502,
                 tags=[Tag(name="T", address="400001", dtype="uint16")])
    config_dir = write_config(tmp_path, [dev])
    ran = []

    def fake_run(coro, **kwargs):
        coro.close()
        ran.append(kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(asyncio, "run", fake_run)
    result = CliRunner().invoke(app, ["run", "--config", str(config_dir)])
    assert ran and "loop_factory" in ran[0]
    assert result.exit_code == 0, result.output
