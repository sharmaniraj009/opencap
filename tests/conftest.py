from __future__ import annotations

import asyncio
import gc
import json
import os
import sys
from pathlib import Path

import pytest
import structlog

FIXTURES = Path(__file__).parent / "fixtures"


def pytest_addoption(parser):
    parser.addoption(
        "--engine", choices=("python", "rust"), default=os.environ.get("OPENCAP_ENGINE", "python"),
        help="engine the conformance (acceptance) tests drive: the Python reference Poller (default) "
             "or the Rust engine binary from engine/ (see tests/helpers/engine.py)",
    )


# --- the Rust port gap: suites the chosen engine cannot run yet ---------------------------------
# `@pytest.mark.drivers("ab_logix", ...)` names the PLC drivers a suite polls. With --engine=rust
# a suite is skipped when the binary does not implement one of them ("driver not ported", or
# "driver not compiled in" when its crate exists but the feature is off), and an acceptance suite
# that does not drive the engine through `engine_factory` (it reaches into the Python Poller) is
# skipped too. Both are listed at the end of the run and, on CI, in the job summary.
_PORT_GAP = pytest.StashKey[dict]()


def pytest_configure(config):
    config.stash[_PORT_GAP] = {}


def _suite_drivers(item) -> tuple[str, ...]:
    return tuple(d for m in item.iter_markers("drivers") for d in m.args)


def pytest_runtest_setup(item):
    if item.config.getoption("--engine") != "rust":
        return
    acceptance = item.get_closest_marker("acceptance") is not None
    drivers = _suite_drivers(item)
    if not acceptance and not drivers:
        return
    from tests.helpers.engine import NOT_AGNOSTIC, unsupported_reason

    gap = item.config.stash[_PORT_GAP]
    gap[item.nodeid] = {"drivers": drivers, "outcome": None, "reason": ""}
    plugins = [m.kwargs["plugin"] for m in item.iter_markers("drivers") if "plugin" in m.kwargs]
    reason = unsupported_reason("rust", drivers, plugins[0] if plugins else None)   # raises with no binary
    if reason is None and "engine_factory" not in getattr(item, "fixturenames", ()):
        reason = f"{NOT_AGNOSTIC}: drives the Python engine directly, not through engine_factory"
    if reason is None:
        return
    gap[item.nodeid].update(outcome="skipped", reason=reason)
    pytest.skip(reason)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    report = (yield).get_result()
    entry = item.config.stash.get(_PORT_GAP, {}).get(item.nodeid)
    if entry is None or entry["outcome"] in ("skipped", "failed"):
        return                                     # first verdict wins: our skip, or a failure
    if report.failed:
        entry["outcome"] = "failed"
    elif report.skipped:
        entry["outcome"] = "skipped"
        entry["reason"] = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
    elif report.when == "call":
        entry["outcome"] = "passed"


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    gap = config.stash.get(_PORT_GAP, {})
    if config.getoption("--engine") != "rust" or not gap:
        return
    terminalreporter.section("Rust port gap")
    for nodeid, e in sorted(gap.items()):
        terminalreporter.write_line(f"{(e['outcome'] or '?'):8} {nodeid}  {e['reason']}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        from tests.helpers.engine import port_gap_markdown

        with open(summary, "a", encoding="utf-8") as f:
            f.write(port_gap_markdown(gap))


@pytest.fixture(scope="session")
def event_loop_policy():
    """Async tests on Windows run on the selector loop, as `opencap run` does (D-031): aiomqtt
    (paho) needs `add_reader`/`add_writer`, which the default proactor loop lacks. The selector
    loop cannot start asyncio subprocesses on Windows, so tests start child processes with
    `subprocess` in a worker thread (`run_process` in tests/helpers/engine.py), on any loop."""
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.get_event_loop_policy()


@pytest.fixture
def no_gc():
    """For tests with wall-clock bounds: collect first, then no automatic collection while the
    test runs. A full collection after the asyncua and config-API tests takes ~300-400 ms here
    (more on CI runners) and stalls the event loop; CI saw a 0.4 s gap in a 90 ms
    channel-isolation bound and a UDP reply miss a 300 ms timeout because of it. The pause
    hits every channel at once, so it says nothing about what these tests check."""
    gc.collect()
    gc.disable()
    try:
        yield
    finally:
        gc.enable()


@pytest.fixture
def engine_factory(request, tmp_path):
    """`factory(config_dir) -> engine` for the engine chosen with --engine / OPENCAP_ENGINE."""
    from tests.helpers.engine import make_engine_factory

    return make_engine_factory(request.config.getoption("--engine"), tmp_path)


@pytest.fixture(autouse=True)
def _restore_structlog_config():
    """Tests that call `configure_logging()` change structlog's global config; restore it so
    tests that capture log events (e.g. `structlog.testing.capture_logs`) see INFO lines."""
    saved = structlog.get_config()
    yield
    structlog.configure(**saved)
# Real site data lives outside version control, in the git-ignored `.private/` folder (or wherever
# OPENCAP_PRIVATE_DIR points). Tests that need it are marked `real_project` and skip without it.
PRIVATE = Path(os.environ.get("OPENCAP_PRIVATE_DIR", Path(__file__).resolve().parent.parent / ".private"))
REAL_PROJECT = Path(os.environ.get("OPENCAP_KEP_PROJECT", PRIVATE / "fixtures" / "project.json"))
KEP2POLLER_OUTPUT = PRIVATE / "fixtures" / "kep2poller_output.json"


@pytest.fixture(scope="session")
def real_project_path() -> Path:
    if not REAL_PROJECT.exists():
        pytest.skip(f"real KEPServer export not present at {REAL_PROJECT} (set OPENCAP_KEP_PROJECT)")
    return REAL_PROJECT


@pytest.fixture(scope="session")
def real_project(real_project_path: Path) -> dict:
    # KEPServer writes a UTF-8 BOM
    return json.loads(real_project_path.read_text(encoding="utf-8-sig"))


@pytest.fixture(scope="session")
def kep2poller_output() -> dict:
    if not KEP2POLLER_OUTPUT.exists():
        pytest.skip(f"kep2poller output not present at {KEP2POLLER_OUTPUT}")
    return json.loads(KEP2POLLER_OUTPUT.read_text(encoding="utf-8"))
