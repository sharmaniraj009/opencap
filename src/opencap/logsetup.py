"""structlog setup: JSON lines on stdout (PLAN.md section 9)."""

from __future__ import annotations

import logging
import os
import sys

import structlog


def configure_logging(level: str | None = None, *, json: bool = True) -> None:
    level_name = (level or os.environ.get("OPENCAP_LOG_LEVEL") or "INFO").upper()
    numeric = logging.getLevelNamesMapping().get(level_name, logging.INFO)
    logging.basicConfig(level=numeric, stream=sys.stdout, format="%(message)s")
    # Library chatter (pymodbus, paho, uvicorn access) stays at WARNING unless DEBUG is asked for.
    for noisy in ("pymodbus", "pycomm3", "paho", "aiomqtt", "uvicorn.access", "amqtt", "asyncio"):
        logging.getLogger(noisy).setLevel(numeric if numeric <= logging.DEBUG else logging.WARNING)
    renderer = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )
