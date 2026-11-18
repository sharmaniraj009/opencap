"""Value cache (PLAN.md sections 3 and 4).

One `ValueCache` per process. ChannelWorkers write to it, the publisher and the admin API
read from it. Everything runs on one event loop, so there is no locking.

Rules (contract §2):
- A good value replaces value, quality and `source_ts`.
- Any other quality keeps the last value and the last good `source_ts`; only the quality
  changes. A tag that was never read good has value `None` and `source_ts` `None`.
- `version()` bumps on every `update()` and `register()`, and on an `expire()` that
  changed something, so a reader can tell whether the snapshot changed.
- Reads of an unknown device are lenient (`{}`, 0, `None`); writes raise `KeyError`.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from opencap.config.models import Quality

if TYPE_CHECKING:
    from opencap.config.models import Device


@dataclass(slots=True)
class TagValue:
    value: Any
    quality: Quality
    source_ts: datetime | None     # time the reply arrived (UTC, tz-aware); None = never read


def _copy_value(value: Any) -> Any:
    # Scalars are immutable; arrays come back as (nested) lists.
    return copy.deepcopy(value) if isinstance(value, (list, dict)) else value


class ValueCache:
    def __init__(self) -> None:
        self._tags: dict[str, dict[str, TagValue]] = {}
        self._versions: dict[str, int] = {}
        self._last_good: dict[str, datetime | None] = {}

    # --- writes ---------------------------------------------------------------------

    def register(self, device: Device) -> None:
        """Pre-fill every enabled tag with `TagValue(None, "stale", None)`.

        Idempotent: on a second call (config reload) tags that still exist keep their
        value and quality, new tags are added as stale and removed tags are dropped.
        """
        old = self._tags.get(device.id, {})
        self._tags[device.id] = {
            t.name: old.get(t.name) or TagValue(None, "stale", None) for t in device.tags if t.enabled
        }
        self._last_good.setdefault(device.id, None)
        self._bump(device.id)

    def update(self, device_id: str, values: Mapping[str, TagValue]) -> None:
        """Apply one batch of results. Names that are not registered are ignored."""
        tags = self._tags[device_id]
        newest = self._last_good.get(device_id)
        for name, tv in values.items():
            entry = tags.get(name)
            if entry is None:
                continue
            if tv.quality == "good":
                tags[name] = TagValue(_copy_value(tv.value), "good", tv.source_ts)
                if tv.source_ts is not None and (newest is None or tv.source_ts > newest):
                    newest = tv.source_ts
            else:
                entry.quality = tv.quality      # keep last value and last good ts
        self._last_good[device_id] = newest
        self._bump(device_id)

    def expire(self, device_id: str, now: datetime, max_age_s: Mapping[str, float], *,
               read_at: Mapping[str, datetime] | None = None) -> None:
        """Good tags whose last good read is older than `max_age_s[name]` become "stale".

        The age is measured from `source_ts`, or from `read_at[name]` when given: the time the
        worker last read the tag good, for drivers whose `source_ts` is the device's own change
        time (subscriptions, D-045). Tags missing from `max_age_s`, and tags that are already
        bad, are left alone.
        """
        tags = self._tags[device_id]
        changed = False
        for name, max_age in max_age_s.items():
            entry = tags.get(name)
            if entry is None or entry.quality != "good":
                continue
            ref = read_at.get(name) if read_at is not None else None
            if ref is None:
                ref = entry.source_ts
            if ref is None:
                continue
            if (now - ref).total_seconds() > max_age:
                entry.quality = "stale"
                changed = True
        if changed:
            self._bump(device_id)

    def unregister(self, device_id: str) -> None:
        self._tags.pop(device_id, None)
        self._versions.pop(device_id, None)
        self._last_good.pop(device_id, None)

    # --- reads ----------------------------------------------------------------------

    def snapshot(self, device_id: str) -> dict[str, TagValue]:
        """Copy of every registered tag of the device (empty if not registered)."""
        return {
            name: TagValue(_copy_value(tv.value), tv.quality, tv.source_ts)
            for name, tv in self._tags.get(device_id, {}).items()
        }

    def get(self, device_id: str, name: str) -> TagValue | None:
        tv = self._tags.get(device_id, {}).get(name)
        return None if tv is None else TagValue(_copy_value(tv.value), tv.quality, tv.source_ts)

    def version(self, device_id: str) -> int:
        return self._versions.get(device_id, 0)

    def last_good(self, device_id: str) -> datetime | None:
        """Newest good `source_ts` seen on the device."""
        return self._last_good.get(device_id)

    def devices(self) -> list[str]:
        return list(self._tags)

    def __contains__(self, device_id: object) -> bool:
        return device_id in self._tags

    def _bump(self, device_id: str) -> None:
        self._versions[device_id] = self._versions.get(device_id, 0) + 1
