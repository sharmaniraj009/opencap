"""ValueCache (contract §2): register, update, snapshot, version, last_good, expire."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from opencap.config.models import Device, Tag
from opencap.core.cache import TagValue, ValueCache

T0 = datetime(2026, 11, 16, 10, 0, 0, tzinfo=UTC)


def dev(*names: str, disabled: tuple[str, ...] = ()) -> Device:
    tags = [Tag(name=n, address=f"4000{i + 1}", dtype="uint16") for i, n in enumerate(names)]
    tags += [Tag(name=n, address="40099", dtype="uint16", enabled=False) for n in disabled]
    return Device(id="S/D", channel="S", name="D", driver="modbus_tcp", host="h", port=502, tags=tags)


def good(v, ts=T0) -> TagValue:
    return TagValue(v, "good", ts)


def test_register_prefills_enabled_tags_as_stale():
    c = ValueCache()
    c.register(dev("A", "B", disabled=("OFF",)))
    snap = c.snapshot("S/D")
    assert set(snap) == {"A", "B"}
    assert all(tv == TagValue(None, "stale", None) for tv in snap.values())
    assert c.last_good("S/D") is None
    assert "S/D" in c and c.devices() == ["S/D"]


def test_good_update_stores_value_quality_and_ts():
    c = ValueCache()
    c.register(dev("A", "B"))
    c.update("S/D", {"A": good(1.5)})
    assert c.snapshot("S/D")["A"] == good(1.5)
    assert c.snapshot("S/D")["B"].quality == "stale"
    assert c.last_good("S/D") == T0


def test_bad_update_keeps_last_value_and_last_good_ts():
    c = ValueCache()
    c.register(dev("A"))
    c.update("S/D", {"A": good(7)})
    for q in ("bad_comm", "bad_decode", "bad_config", "stale"):
        c.update("S/D", {"A": TagValue(None, q, T0 + timedelta(seconds=5))})
        assert c.get("S/D", "A") == TagValue(7, q, T0)
    c.update("S/D", {"A": good(8, T0 + timedelta(seconds=9))})
    assert c.get("S/D", "A") == TagValue(8, "good", T0 + timedelta(seconds=9))


def test_bad_update_on_never_read_tag_keeps_none():
    c = ValueCache()
    c.register(dev("A"))
    c.update("S/D", {"A": TagValue(None, "bad_comm", None)})
    assert c.get("S/D", "A") == TagValue(None, "bad_comm", None)


def test_version_bumps_on_every_update():
    c = ValueCache()
    assert c.version("S/D") == 0
    c.register(dev("A"))
    v0 = c.version("S/D")
    c.update("S/D", {"A": good(1)})
    c.update("S/D", {"A": good(1)})      # same value still bumps
    c.update("S/D", {})
    assert c.version("S/D") == v0 + 3


def test_snapshot_is_a_copy():
    c = ValueCache()
    c.register(dev("A", "ARR"))
    c.update("S/D", {"A": good(1), "ARR": good([1, 2, 3])})
    snap = c.snapshot("S/D")
    snap["A"].value = 99
    snap["A"].quality = "bad_comm"
    snap["ARR"].value.append(4)
    snap.pop("A")
    again = c.snapshot("S/D")
    assert again["A"] == good(1)
    assert again["ARR"].value == [1, 2, 3]


def test_update_does_not_alias_caller_objects():
    c = ValueCache()
    c.register(dev("A"))
    arr = [1, 2]
    tv = good(arr)
    c.update("S/D", {"A": tv})
    arr.append(3)
    tv.quality = "bad_comm"
    assert c.get("S/D", "A") == good([1, 2])


def test_unknown_names_are_ignored_unknown_device_raises_on_write():
    c = ValueCache()
    c.register(dev("A"))
    c.update("S/D", {"NOPE": good(1)})
    assert set(c.snapshot("S/D")) == {"A"}
    with pytest.raises(KeyError):
        c.update("X/Y", {"A": good(1)})
    with pytest.raises(KeyError):
        c.expire("X/Y", T0, {})
    assert c.snapshot("X/Y") == {} and c.version("X/Y") == 0 and c.last_good("X/Y") is None
    assert c.get("X/Y", "A") is None


def test_last_good_is_newest_good_ts():
    c = ValueCache()
    c.register(dev("A", "B"))
    c.update("S/D", {"A": good(1, T0 + timedelta(seconds=3)), "B": good(2, T0 + timedelta(seconds=1))})
    assert c.last_good("S/D") == T0 + timedelta(seconds=3)
    c.update("S/D", {"B": good(2, T0 + timedelta(seconds=2))})   # older than A's
    assert c.last_good("S/D") == T0 + timedelta(seconds=3)
    c.update("S/D", {"A": TagValue(None, "bad_comm", T0 + timedelta(seconds=60))})
    assert c.last_good("S/D") == T0 + timedelta(seconds=3)


def test_expire_marks_only_old_good_tags_stale():
    c = ValueCache()
    c.register(dev("OLD", "NEW", "BAD", "NEVER", "NOLIMIT"))
    c.update("S/D", {
        "OLD": good(1, T0),
        "NEW": good(2, T0 + timedelta(seconds=9)),
        "BAD": good(3, T0),
        "NOLIMIT": good(4, T0),
    })
    c.update("S/D", {"BAD": TagValue(None, "bad_comm", None)})
    v = c.version("S/D")
    ages = {"OLD": 3.0, "NEW": 3.0, "BAD": 3.0, "NEVER": 3.0}
    c.expire("S/D", T0 + timedelta(seconds=10), ages)
    snap = c.snapshot("S/D")
    assert snap["OLD"] == TagValue(1, "stale", T0)          # value and ts kept
    assert snap["NEW"].quality == "good"
    assert snap["BAD"].quality == "bad_comm"                # bad stays bad
    assert snap["NEVER"] == TagValue(None, "stale", None)
    assert snap["NOLIMIT"].quality == "good"                # not in max_age_s
    assert c.version("S/D") == v + 1
    c.expire("S/D", T0 + timedelta(seconds=10), ages)       # nothing changed: no bump
    assert c.version("S/D") == v + 1
    c.update("S/D", {"OLD": good(5, T0 + timedelta(seconds=11))})
    assert c.get("S/D", "OLD") == good(5, T0 + timedelta(seconds=11))


def test_expire_boundary_is_strictly_older():
    c = ValueCache()
    c.register(dev("A"))
    c.update("S/D", {"A": good(1, T0)})
    c.expire("S/D", T0 + timedelta(seconds=3), {"A": 3.0})
    assert c.get("S/D", "A").quality == "good"
    c.expire("S/D", T0 + timedelta(seconds=3, milliseconds=1), {"A": 3.0})
    assert c.get("S/D", "A").quality == "stale"


def test_register_again_keeps_values_adds_and_drops_tags():
    c = ValueCache()
    c.register(dev("A", "B"))
    c.update("S/D", {"A": good(1), "B": good(2)})
    c.register(dev("A", "C"))
    snap = c.snapshot("S/D")
    assert set(snap) == {"A", "C"}
    assert snap["A"] == good(1)
    assert snap["C"] == TagValue(None, "stale", None)
    c.unregister("S/D")
    assert "S/D" not in c
