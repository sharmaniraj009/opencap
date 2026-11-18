from __future__ import annotations

import gc
import json
import os
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
