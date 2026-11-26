"""`opencap browse`: automatic tag generation from a device's own tag list (#11, plan DEV-12).

Generation is an operator action that produces a tag list to review, never a rewrite of the
project: `browse_device()` connects the device's driver once, calls `Driver.browse()`, checks
that every generated tag plans cleanly, and closes. `write_tags()` writes the result as a JSON
tag list (the `Device.tags` shape of devices.json) or, once the KEPServerEX CSV writer (#10) is
on `main`, as a KEPServerEX tag CSV.

Read-only: `browse()` may use read and list services only (`Driver.browse`); for `ab_logix` it
uses the tag list uploaded by `connect()` and sends nothing else.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from opencap.config.models import Device, Tag
from opencap.core.channel_worker import safety_timeout_s
from opencap.drivers import get_driver_class
from opencap.drivers.base import BrowseResult, Driver


class BrowseError(Exception):
    """The device could not be browsed (no browse support, connect failed)."""


@dataclass
class BrowseReport:
    device_id: str
    tags: list[Tag] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    invalid: dict[str, str] = field(default_factory=dict)     # generated tags the planner rejects (a bug)

    def summary_lines(self) -> list[str]:
        lines = [f"{self.device_id}: {len(self.tags)} tags generated, {len(self.skipped)} skipped"]
        lines += [f"  skipped {name}: {reason}" for name, reason in self.skipped.items()]
        if self.invalid:
            lines.append(f"  {len(self.invalid)} generated tag(s) do not plan (please report this):")
            lines += [f"  invalid {name}: {reason}" for name, reason in self.invalid.items()]
        return lines


async def browse_device(device: Device, *, expand_arrays: bool = False,
                        driver_factory: type[Driver] | None = None) -> BrowseReport:
    """Connect `device`'s driver, browse, plan-check the result, close. Raises BrowseError."""
    try:
        cls = driver_factory or get_driver_class(device.driver)
    except LookupError as exc:
        raise BrowseError(str(exc)) from None
    if cls.browse is Driver.browse:
        raise BrowseError(f"driver {device.driver!r} does not support browse (automatic tag generation)")
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="browse") as executor:
        driver = cls(device, executor)
        budget = driver.connect_timeout_s() or safety_timeout_s(device.timeout_ms)
        try:
            await asyncio.wait_for(driver.connect(), budget)
            result: BrowseResult = await asyncio.wait_for(driver.browse(expand_arrays=expand_arrays),
                                                          safety_timeout_s(device.timeout_ms))
        except NotImplementedError as exc:
            raise BrowseError(f"driver {device.driver!r}: {exc}") from None
        except TimeoutError:
            raise BrowseError(f"{device.id}: no answer within {budget:.1f} s") from None
        except Exception as exc:                                    # noqa: BLE001 - reported to the operator
            raise BrowseError(f"{device.id}: {type(exc).__name__}: {exc}") from None
        finally:
            await driver.close()
        plan = driver.plan(result.tags)
    return BrowseReport(device.id, result.tags, dict(result.skipped), dict(plan.invalid))


def tags_json(tags: list[Tag]) -> str:
    """A JSON tag list in the `Device.tags` shape (defaults left out)."""
    return json.dumps([t.model_dump(mode="json", exclude_defaults=True) for t in tags], indent=1,
                      ensure_ascii=False) + "\n"


def write_tags(tags: list[Tag], out: Path) -> None:
    """`.json` -> JSON tag list; `.csv` -> KEPServerEX tag CSV (needs `opencap.config.kep_csv`, #10)."""
    suffix = out.suffix.lower()
    if suffix == ".csv":
        try:
            from opencap.config.kep_csv import write_kep_csv
        except ImportError:
            raise BrowseError("CSV output needs the KEPServerEX tag CSV writer (#10), which is not in this "
                              "build yet; write .json instead") from None
        write_kep_csv(tags, out)
        return
    if suffix != ".json":
        raise BrowseError(f"--out must end in .json or .csv, got {out.name!r}")
    out.write_text(tags_json(tags), encoding="utf-8")
