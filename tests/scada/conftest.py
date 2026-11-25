"""Fixtures and reporting for the L1 SCADA compatibility suite (tests/scada/README.md).

The suite is opt-in: it starts the engine as a child process and runs external clients, so
it only runs when selected with `-m scada` (or OPENCAP_SCADA_L1=1). `pytest -m "not acceptance"`
(the default CI run) reports it as skipped.

    pytest -m scada tests/scada --junitxml=out/junit.xml        # all built clients
    OPENCAP_SCADA_REQUIRE=milo,open62541,asyncua pytest -m scada tests/scada   # missing client = fail
    OPENCAP_SCADA_SUMMARY=out/summary.md pytest -m scada tests/scada            # + markdown matrix
"""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.scada import harness

SCENARIO_ORDER: list[str] = []          # filled from @pytest.mark.l1("...") in collection order
_OUTCOMES: dict[tuple[str, str], str] = {}
_ENABLED = False                         # the suite was selected (-m scada): report the matrix


def pytest_configure(config):
    config.addinivalue_line("markers", "scada: L1 SCADA compatibility suite (opt-in: -m scada)")
    config.addinivalue_line("markers", "l1(name): the L1 scenario a test belongs to (matrix row)")


def _selected(config) -> bool:
    return "scada" in (config.getoption("-m") or "") or os.environ.get("OPENCAP_SCADA_L1") == "1"


def pytest_collection_modifyitems(config, items):
    global _ENABLED
    selected = _ENABLED = _selected(config)
    skip = pytest.mark.skip(reason="L1 SCADA suite is opt-in: run `pytest -m scada tests/scada` "
                                   "(tests/scada/README.md)")
    for item in items:
        if item.get_closest_marker("scada") is None:
            continue
        if not selected:
            item.add_marker(skip)
        mark = item.get_closest_marker("l1")
        if mark and mark.args[0] not in SCENARIO_ORDER:
            SCENARIO_ORDER.append(mark.args[0])


def required_clients() -> set[str]:
    return {c.strip() for c in os.environ.get("OPENCAP_SCADA_REQUIRE", "").split(",") if c.strip()}


@pytest.fixture(params=harness.CLIENTS)
def client(request) -> str:
    name = request.param
    if harness.client_command(name) is None:
        message = f"{name} client not built: {harness.build_hint(name)}"
        if name in required_clients():
            pytest.fail(message + " (required by OPENCAP_SCADA_REQUIRE)")
        pytest.skip(message)
    return name


@dataclass
class L1:
    engine: harness.EngineProcess
    plant: harness.Plant
    pki: harness.Pki
    ns: int
    workdir: Path

    @property
    def endpoint(self) -> str:
        return self.engine.endpoint

    def args(self, *extra: str) -> list[str]:
        return [*harness.common_args(self.endpoint, self.pki, self.ns), *extra]

    def write_lines(self, name: str, lines: list[str]) -> Path:
        path = self.workdir / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


@pytest.fixture(scope="session")
def l1(request, tmp_path_factory) -> L1:
    import asyncio

    workdir = tmp_path_factory.mktemp("scada-l1")
    plant = harness.Plant.start()
    engine = None
    try:
        pki = harness.make_client_pki(workdir / "client-pki")
        port = harness.stable_port()
        config_dir = harness.write_engine_config(workdir, plant.devices, port, pki)
        engine = harness.EngineProcess(config_dir, request.config.getoption("--engine"), workdir,
                                       f"opc.tcp://127.0.0.1:{port}/opencap")
        engine.start()
        ns = asyncio.run(_namespace_index(engine.endpoint))
        yield L1(engine=engine, plant=plant, pki=pki, ns=ns, workdir=workdir)
    finally:
        if engine is not None:
            engine.stop()
        plant.stop()


async def _namespace_index(endpoint: str) -> int:
    from asyncua import Client

    async with Client(endpoint, timeout=5) as c:
        return (await c.get_namespace_array()).index(harness.NS_URI)


_RUNS: dict[tuple[str, str, tuple[str, ...]], harness.ClientRun] = {}


@pytest.fixture
def run_scenario(l1):
    """`run_scenario(client, scenario, *args) -> ClientRun`, memoized per session so several
    tests can assert on one client run (e.g. read -> values, datatypes, EURange)."""

    def run(client: str, scenario: str, *extra: str, cache: bool = True, **kw) -> harness.ClientRun:
        key = (client, scenario, extra)
        if cache and key in _RUNS:
            return _RUNS[key]
        result = harness.run_client(client, scenario, l1.args(*extra), **kw)
        if cache:
            _RUNS[key] = result
        return result

    return run


# --- matrix report ---------------------------------------------------------------------------

def pytest_runtest_logreport(report):
    item_marks = getattr(report, "_l1", None)
    if item_marks is None or not _ENABLED:
        return
    scenario, client = item_marks
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        if hasattr(report, "wasxfail"):
            outcome = "xfail" if report.skipped else "xpass"
        else:
            outcome = report.outcome
        _OUTCOMES[(scenario, client)] = outcome


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    mark = item.get_closest_marker("l1")
    callspec = getattr(item, "callspec", None)
    if mark is not None and callspec is not None and "client" in callspec.params:
        report._l1 = (mark.args[0], callspec.params["client"])


SYMBOL = {"passed": "pass", "failed": "**FAIL**", "skipped": "skip", "xfail": "xfail (pending)",
          "xpass": "**XPASS**"}


def summary_markdown() -> str:
    by_scenario: dict[str, dict[str, str]] = defaultdict(dict)
    for (scenario, client), outcome in _OUTCOMES.items():
        by_scenario[scenario][client] = outcome
    lines = ["# L1 SCADA compatibility results", "",
             "| Scenario | " + " | ".join(harness.CLIENTS) + " |",
             "|---|" + "---|" * len(harness.CLIENTS)]
    for scenario in SCENARIO_ORDER:
        row = by_scenario.get(scenario, {})
        lines.append(f"| {scenario} | " + " | ".join(SYMBOL.get(row.get(c, ""), "—") for c in harness.CLIENTS)
                     + " |")
    lines += ["", ("xfail (pending) = a known server gap with an open issue; the test turns into a failure "
                   "(XPASS) once the server is fixed, so the marker must be removed then.")]
    return "\n".join(lines) + "\n"


def pytest_sessionfinish(session, exitstatus):
    if not _OUTCOMES:
        return
    target = os.environ.get("OPENCAP_SCADA_SUMMARY")
    if target:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_text(summary_markdown(), encoding="utf-8")


def pytest_terminal_summary(terminalreporter):
    if _OUTCOMES:
        terminalreporter.write_sep("-", "L1 SCADA matrix")
        terminalreporter.write_line(summary_markdown())
