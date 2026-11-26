"""Prometheus metrics (PLAN.md section 9). One registry for the whole process.

Import the metric objects from here; never create metrics elsewhere.
"""

from __future__ import annotations

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


def render() -> bytes:
    return generate_latest(REGISTRY)
