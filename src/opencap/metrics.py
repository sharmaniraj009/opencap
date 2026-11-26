"""Prometheus metrics (PLAN.md section 9). One registry for the whole process.

Import the metric objects from here; never create metrics elsewhere. Count the poll, publish and
drop counters through the `count_*` functions below, never with `.inc()` on the metric: they also
keep plain per-device totals that the system tags read (#238).
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from types import MappingProxyType

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

REGISTRY = CollectorRegistry(auto_describe=True)

# result: ok | timeout | conn_error | bad_config | error
POLL_REQUESTS = Counter(
    "poll_requests", "Wire requests to PLCs by result", ["device", "result"], registry=REGISTRY
)
POLL_FAILED_READS = Counter(
    "poll_failed_reads", "Block reads that ended bad_comm after all retries", ["device"], registry=REGISTRY
)
POLL_CYCLE = Histogram(
    "poll_cycle_seconds", "Duration of one rate-group cycle of a device", ["device"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60), registry=REGISTRY,
)
POLL_OVERRUN = Counter(
    "poll_overrun", "Cycles that took longer than their scan rate", ["device"], registry=REGISTRY
)
DEVICE_STATE = Gauge(
    "device_state", "Device state: 0 = ok, 1 = offline, 2 = demoted", ["device"], registry=REGISTRY
)
MQTT_PUBLISHED = Counter("mqtt_published", "Messages acknowledged by the broker", registry=REGISTRY)
SAF_QUEUE = Gauge("saf_queue_messages", "Messages waiting in store-and-forward", registry=REGISTRY)
SAF_DROPPED = Counter(
    "saf_dropped", "Messages dropped from store-and-forward because of the size cap", registry=REGISTRY
)

DEVICE_STATE_CODES = {"ok": 0, "offline": 1, "demoted": 2}


# --- plain totals beside the counters (#238) ---------------------------------------------------
# The system tags need every device's counters once per second and after every device cycle.
# `REGISTRY.get_sample_value` collects the whole registry on each call, so reading N devices that
# way cost O(N^2) per step: 77 % of the CPU at 10k tags. These totals are the same numbers (process
# lifetime, like the counters), read in O(1). A test checks that they match the registry.
_lock = threading.Lock()
_device_counts: dict[str, dict[str, int]] = {}
_totals: dict[str, int] = {"mqtt_published": 0, "saf_dropped": 0}
_NONE: Mapping[str, int] = MappingProxyType({})


def _bump(device: str, key: str, n: int = 1) -> None:
    with _lock:
        counts = _device_counts.setdefault(device, {})
        counts[key] = counts.get(key, 0) + n


def count_request(device: str, result: str) -> None:
    """One wire request of `device` with `result` (ok | timeout | conn_error | bad_config | error)."""
    POLL_REQUESTS.labels(device, result).inc()
    _bump(device, result)


def count_failed_read(device: str) -> None:
    POLL_FAILED_READS.labels(device).inc()
    _bump(device, "failed_reads")


def count_overrun(device: str) -> None:
    POLL_OVERRUN.labels(device).inc()
    _bump(device, "overruns")


def count_published(n: int) -> None:
    MQTT_PUBLISHED.inc(n)
    with _lock:
        _totals["mqtt_published"] += n


def count_saf_dropped(n: int) -> None:
    SAF_DROPPED.inc(n)
    with _lock:
        _totals["saf_dropped"] += n


def device_counts(device: str) -> Mapping[str, int]:
    """The totals of one device: keys are the request results, `failed_reads` and `overruns`;
    a key that was never counted is absent (read it as 0)."""
    with _lock:
        counts = _device_counts.get(device)
        return dict(counts) if counts is not None else _NONE


def total(name: str) -> int:
    """`mqtt_published` or `saf_dropped`."""
    with _lock:
        return _totals[name]


def render() -> bytes:
    return generate_latest(REGISTRY)
