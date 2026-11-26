"""Registry: sites, devices, tags and their block plans (PLAN.md section 3).

Plans are built once at startup and on config reload. Building a plan never
touches the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from opencap.config.models import Channel, Device
from opencap.drivers import get_driver_class
from opencap.drivers.base import Plan


@dataclass
class RateGroupPlan:
    rate_ms: int
    tag_count: int
    plan: Plan


@dataclass
class DevicePlan:
    device: Device
    groups: list[RateGroupPlan] = field(default_factory=list)
    error: str | None = None          # set when no plan could be built (e.g. driver not implemented yet)

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def enabled_tags(self) -> int:
        return sum(g.tag_count for g in self.groups)

    @property
    def blocks(self) -> int:
        return sum(len(g.plan.blocks) for g in self.groups)

    @property
    def wire_requests(self) -> int:
        """Round trips for one pass over every rate group."""
        return sum(g.plan.wire_requests for g in self.groups)

    @property
    def invalid(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for g in self.groups:
            out.update(g.plan.invalid)
        return out

    def est_cycle_ms(self, rtt_ms: float, delay_ms: float = 0.0) -> float:
        """Time for one sequential pass over every rate group. `delay_ms` is the idle time the
        channel keeps around each request (`request_delay_ms`)."""
        return self.wire_requests * (rtt_ms + delay_ms)

    def est_load(self, rtt_ms: float, delay_ms: float = 0.0) -> float:
        """Fraction of wall time the channel spends on the wire for this device (1.0 = saturated)."""
        return sum(g.plan.wire_requests * (rtt_ms + delay_ms) / g.rate_ms for g in self.groups)

    def request_delay_ms(self, inter_device_delay_ms: int = 0, shared_channel: bool = False) -> float:
        """Idle time per request: the device's inter-request delay, plus the channel's inter-device
        delay when other devices share the channel (an upper bound: it counts every request as
        a switch between devices, which is what a busy channel does)."""
        return self.device.inter_request_delay_ms + (inter_device_delay_ms if shared_channel else 0)


def effective_connect_timeout_ms(device: Device) -> int | None:
    """What bounds one connect attempt of this device: the explicit `connect_timeout_ms`, else the
    driver's own budget (`Driver.connect_timeout_s`), else `timeout_ms`. None for a driver without a
    connection to open (melsec_mc over UDP) or one that is not available."""
    if device.driver == "melsec_mc" and str(device.params.get("protocol", "tcp")).lower() == "udp":
        return None
    if device.connect_timeout_ms is not None:
        return device.connect_timeout_ms
    try:
        budget_s = get_driver_class(device.driver)(device).connect_timeout_s()
    except Exception:   # noqa: BLE001 - missing library or bad params: fall back to the plain timeout
        budget_s = None
    return round(budget_s * 1000) if budget_s else device.timeout_ms


def plan_device(device: Device) -> DevicePlan:
    result = DevicePlan(device=device)
    try:
        driver_cls = get_driver_class(device.driver)
    except LookupError as exc:
        result.error = str(exc)
        return result
    driver = driver_cls(device)
    for rate_ms, tags in device.rate_groups().items():
        result.groups.append(RateGroupPlan(rate_ms=rate_ms, tag_count=len(tags), plan=driver.plan(tags)))
    return result


@dataclass
class Registry:
    channels: list[Channel]
    plans: dict[str, DevicePlan]      # key = device id

    @classmethod
    def build(cls, channels: list[Channel]) -> "Registry":
        plans: dict[str, DevicePlan] = {}
        for channel in channels:
            for device in channel.devices:
                plans[device.id] = plan_device(device)
        return cls(channels=channels, plans=plans)

    def device(self, device_id: str) -> Device:
        return self.plans[device_id].device
