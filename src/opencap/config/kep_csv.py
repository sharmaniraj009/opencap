"""KEPServerEX-compatible tag CSV import and export (issue #10, FR-2, FR-9).

KEPServerEX users keep a device's tag list as a CSV file (Tag Management, "Export CSV" /
"Import CSV"). This module reads and writes that file so tags can be edited in a spreadsheet
and moved between KEPServerEX and opencap.

Header written by `write_kep_csv` (this exact spelling and order, comma separated, CRLF line
ends, a quoted field where needed)::

    Tag Name,Address,Data Type,Respect Data Type,Client Access,Scan Rate,Scaling,Raw Low,Raw High,
    Scaled Low,Scaled High,Scaled Data Type,Clamp Low,Clamp High,Eng Units,Description,Negate Value

(one line; wrapped here). Value spellings, as KEPServerEX writes them:

- ``Data Type``: Default, String, Boolean, Char, Byte, Short, Word, Long, DWord, Float, Double,
  BCD, LBCD, Date, LLong, QWord. Mapped with the same tables as `kep_import.py`
  (``KEP_DTYPES`` etc.): Char and Byte become int16 / uint16 with a warning (no 8-bit dtype),
  BCD / LBCD / Date are imported **disabled** (no decoder yet), Default takes the device's
  default type (``default_dtype``; without one the row fails). Array types ("Word Array") fail.
- ``Respect Data Type``: ``1`` written; ignored on read (opencap always serves the tag's type).
- ``Client Access``: ``Read Only`` written. ``Read/Write`` is imported as read-only with a
  warning (ADR-0008: opencap never writes).
- ``Scan Rate``: milliseconds; empty or 0 = the device's rate (``Tag.scan_ms`` None).
- ``Scaling``: ``None`` | ``Linear`` | ``Square Root``; ``Raw Low/High``, ``Scaled Low/High``,
  ``Clamp Low/High`` and ``Negate Value`` (``0``/``1`` written; ``1``, ``yes``, ``true`` read as
  set) and ``Eng Units`` map to `Scale`. ``Scaled Data Type`` is written as ``Double`` and
  ignored on read. Scaling columns are empty when a tag has no scaling.
- ``Description``: free text.
- ``Tag Name``: a group path is dot-joined (``Group.Tag``), which is also opencap's tag name.

Read tolerances: a UTF-8 BOM, column order, column-name case and spaces, extra columns (ignored),
missing optional columns, blank lines. ``Tag Name``, ``Address`` and ``Data Type`` are required.

opencap extension (not KEPServerEX): an ``Enabled`` column is appended **only** when a tag is
disabled, so a disabled tag survives write then read; delete the column before importing the file
into KEPServerEX. Round trip: ``write`` then ``read`` returns identical tags. ``read`` then
``write`` gives the same CSV except where the import had to change something it warned about
(Char -> Short, Read/Write -> Read Only).

A row that cannot be imported is reported with its line number in `KepCsvError.errors`; the
other rows are still checked, and no partial result is returned.
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import get_args

from opencap.config.kep_import import DISABLED_TYPES, KEP_DTYPES, KEP_TYPE_NAMES, WIDENED_TYPES
from opencap.config.models import RESERVED_GROUP, Device, DriverName, DType, Scale, Tag

COLUMNS = (
    "Tag Name", "Address", "Data Type", "Respect Data Type", "Client Access", "Scan Rate", "Scaling",
    "Raw Low", "Raw High", "Scaled Low", "Scaled High", "Scaled Data Type", "Clamp Low", "Clamp High",
    "Eng Units", "Description", "Negate Value",
)
ENABLED_COLUMN = "Enabled"           # opencap extension, written only when a tag is disabled
REQUIRED = ("Tag Name", "Address", "Data Type")

# dtype -> the KEPServerEX name written for it (the inverse of KEP_DTYPES on the types it decodes).
DTYPE_TO_KEP = {
    "bool": "Boolean", "int16": "Short", "uint16": "Word", "int32": "Long", "uint32": "DWord",
    "int64": "LLong", "uint64": "QWord", "float32": "Float", "float64": "Double", "string": "String",
}
_KEP_NAME_TO_CODE = {name.lower(): code for code, name in KEP_TYPE_NAMES.items()}
_SCALING_NAMES = {"": None, "none": None, "linear": "linear", "square root": "sqrt", "sqrt": "sqrt"}
_TRUE = {"1", "yes", "true", "y", "on"}
_FALSE = {"0", "no", "false", "n", "off", ""}


@dataclass(frozen=True)
class RowError:
    line: int
    message: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.message}"


class KepCsvError(ValueError):
    """The file could not be imported: `errors` lists every failing row (and file-level problems
    at line 1 or 0). `warnings` holds what was noted before the failure."""

    def __init__(self, errors: list[RowError], warnings: list[str] | None = None) -> None:
        self.errors = errors
        self.warnings = warnings or []
        super().__init__("; ".join(str(e) for e in errors))


@dataclass
class _Reading:
    tags: list[Tag] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)


# --- read ---------------------------------------------------------------------------------------

def _norm(name: str) -> str:
    return " ".join(name.strip().lower().split())


def _flag(text: str, what: str) -> bool:
    t = text.strip().lower()
    if t in _TRUE:
        return True
    if t in _FALSE:
        return False
    raise ValueError(f"{what} must be 0/1 or yes/no, got {text!r}")


def _number(text: str, what: str, default: float) -> float:
    t = text.strip()
    if not t:
        return default
    try:
        value = float(t)
    except ValueError:
        raise ValueError(f"{what} {text!r} is not a number") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"{what} {text!r} is not finite")
    return value


def _check_address(driver: str, tag: Tag) -> str | None:
    """Reason when the driver's planner rejects the address with default parameters, else None.
    Best effort: a driver that needs parameters to plan is not judged."""
    from opencap.drivers import get_driver_class

    try:
        device = Device(id="csv/check", channel="csv", name="check", driver=driver, host="192.0.2.1",  # type: ignore[arg-type]
                        port=1, tags=[tag])
        return get_driver_class(driver)(device).plan([tag]).invalid.get(tag.name)
    except Exception:  # noqa: BLE001 - not judged
        return None


def _row_to_tag(row: dict[str, str], line: int, driver: str, default_dtype: str | None,
                warnings: list[str]) -> Tag:
    """One CSV row -> Tag, or ValueError with the reason. Warnings get the line number."""
    def warn(msg: str) -> None:
        warnings.append(f"line {line}: {msg}")

    name = row["Tag Name"].strip()
    address = row["Address"].strip()
    if not name:
        raise ValueError("empty Tag Name")
    if not address:
        raise ValueError(f"tag {name!r} has no Address")
    if any(part == "" for part in name.split(".")):
        raise ValueError(f"Tag Name {name!r} has an empty group level")
    if name.split(".", 1)[0].lower() == RESERVED_GROUP.lower():
        raise ValueError(f"Tag Name {name!r} uses the reserved group name {RESERVED_GROUP!r}")

    type_text = row["Data Type"].strip()
    enabled = True
    if _norm(type_text) in ("", "default"):
        if default_dtype is None:
            raise ValueError(f"tag {name!r}: Data Type 'Default' and no device default type to use")
        dtype = default_dtype
    else:
        if _norm(type_text).endswith(" array") or "[" in type_text:
            raise ValueError(f"tag {name!r}: array Data Type {type_text!r} is not supported")
        code = _KEP_NAME_TO_CODE.get(_norm(type_text))
        if code is None or code not in KEP_DTYPES:
            raise ValueError(f"tag {name!r}: unknown Data Type {type_text!r}")
        dtype = KEP_DTYPES[code]
        if code in WIDENED_TYPES:
            warn(f"tag {name!r}: KEPServerEX {KEP_TYPE_NAMES[code]} imported as {dtype} (no 8-bit dtype); verify")
        if code in DISABLED_TYPES:
            warn(f"tag {name!r}: KEPServerEX {KEP_TYPE_NAMES[code]} has no decoder yet; imported disabled")
            enabled = False
    if "Enabled" in row and row["Enabled"].strip():
        enabled = enabled and _flag(row["Enabled"], "Enabled")

    access = _norm(row.get("Client Access", ""))
    if access in ("read/write", "readwrite", "read write"):
        warn(f"tag {name!r}: Client Access 'Read/Write' imported as read-only (opencap never writes, ADR-0008)")
    elif access not in ("", "read only", "readonly"):
        warn(f"tag {name!r}: unknown Client Access {row['Client Access']!r}; imported as read-only")

    scan_text = row.get("Scan Rate", "").strip()
    scan_ms: int | None = None
    if scan_text:
        try:
            scan_ms = int(float(scan_text))
        except ValueError:
            raise ValueError(f"tag {name!r}: Scan Rate {scan_text!r} is not a number of milliseconds") from None
        if scan_ms < 0:
            raise ValueError(f"tag {name!r}: negative Scan Rate {scan_text!r}")
        scan_ms = scan_ms or None

    scale: Scale | None = None
    scaling = _norm(row.get("Scaling", ""))
    if scaling not in _SCALING_NAMES:
        raise ValueError(f"tag {name!r}: unknown Scaling {row['Scaling']!r} (None, Linear, Square Root)")
    kind = _SCALING_NAMES[scaling]
    if kind is not None:
        units = row.get("Eng Units", "").strip()
        defaults = Scale()
        scale = Scale(
            type=kind,  # type: ignore[arg-type]
            raw_lo=_number(row.get("Raw Low", ""), "Raw Low", defaults.raw_lo),
            raw_hi=_number(row.get("Raw High", ""), "Raw High", defaults.raw_hi),
            eng_lo=_number(row.get("Scaled Low", ""), "Scaled Low", defaults.eng_lo),
            eng_hi=_number(row.get("Scaled High", ""), "Scaled High", defaults.eng_hi),
            clamp_lo=_flag(row.get("Clamp Low", ""), "Clamp Low"),
            clamp_hi=_flag(row.get("Clamp High", ""), "Clamp High"),
            negate=_flag(row.get("Negate Value", ""), "Negate Value"),
            units=units or None,
        )
        if scale.raw_hi == scale.raw_lo:
            warn(f"tag {name!r}: scaling Raw High equals Raw Low; every read will be bad_decode")

    description = row.get("Description", "").strip()
    tag = Tag(name=name, address=address, dtype=dtype, scan_ms=scan_ms, scale=scale,  # type: ignore[arg-type]
              enabled=enabled, description=description or None)
    reason = _check_address(driver, tag)
    if reason:
        warn(f"tag {name!r}: address {address!r} is not valid for driver {driver}: {reason}")
    return tag


def parse_kep_csv(text: str, driver: str, *, default_dtype: str | None = None) -> _Reading:
    """Parse CSV text. Never raises for a bad row: see `_Reading.errors`."""
    out = _Reading()
    if driver not in get_args(DriverName):
        out.errors.append(RowError(0, f"unknown driver {driver!r} (one of: {', '.join(get_args(DriverName))})"))
        return out
    if default_dtype is not None and default_dtype not in get_args(DType):
        out.errors.append(RowError(0, f"unknown default type {default_dtype!r} (one of: {', '.join(get_args(DType))})"))
        return out
    if text.startswith("﻿"):
        text = text[1:]
    reader = csv.reader(io.StringIO(text, newline=""))
    header: list[str] | None = None
    seen: dict[str, int] = {}
    previous_end = 0
    try:
        for fields in reader:
            line = previous_end + 1                       # first physical line of this record
            previous_end = reader.line_num
            if not any(f.strip() for f in fields):
                continue
            if header is None:
                header = _header(fields, out.errors, line)
                if header is None:
                    return out
                continue
            row = {col: (fields[i] if i < len(fields) else "") for i, col in enumerate(header) if col}
            if len(fields) > len(header) and any(f.strip() for f in fields[len(header):]):
                out.errors.append(RowError(line, f"{len(fields)} fields, the header has {len(header)}"))
                continue
            try:
                tag = _row_to_tag(row, line, driver, default_dtype, out.warnings)
            except ValueError as exc:
                out.errors.append(RowError(line, str(exc)))
                continue
            if tag.name in seen:
                out.errors.append(RowError(line, f"duplicate Tag Name {tag.name!r} (first on line {seen[tag.name]})"))
                continue
            seen[tag.name] = line
            out.tags.append(tag)
    except csv.Error as exc:
        out.errors.append(RowError(reader.line_num, f"not valid CSV: {exc}"))
    if header is None and not out.errors:
        out.errors.append(RowError(1, "empty file: no header line"))
    return out


def _header(fields: list[str], errors: list[RowError], line: int) -> list[str] | None:
    by_norm = {_norm(c): c for c in (*COLUMNS, ENABLED_COLUMN)}
    header = [by_norm.get(_norm(f), "") for f in fields]    # unknown columns become "" (ignored)
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        errors.append(RowError(line, f"header lacks required column(s): {', '.join(missing)}"))
        return None
    dupes = sorted({c for c in header if c and header.count(c) > 1})
    if dupes:
        errors.append(RowError(line, f"header repeats column(s): {', '.join(dupes)}"))
        return None
    return header


def read_kep_csv(path: str | Path, driver: str, *, default_dtype: str | None = None) -> tuple[list[Tag], list[str]]:
    """Read a KEPServerEX tag CSV: ``(tags, warnings)``. Raises `KepCsvError` if any row failed."""
    text = Path(path).read_text(encoding="utf-8-sig")
    result = parse_kep_csv(text, driver, default_dtype=default_dtype)
    if result.errors:
        raise KepCsvError(result.errors, result.warnings)
    return result.tags, result.warnings


# --- write --------------------------------------------------------------------------------------

def _num(x: float) -> str:
    """Shortest text that reads back as the same float; whole numbers without '.0'."""
    return str(int(x)) if math.isfinite(x) and x == int(x) and abs(x) < 1e15 else repr(x)


def _row(tag: Tag, with_enabled: bool) -> list[str]:
    s = tag.scale
    row = [
        tag.name, tag.address, DTYPE_TO_KEP[tag.dtype], "1", "Read Only",
        str(tag.scan_ms) if tag.scan_ms else "",
        ("Linear" if s.type == "linear" else "Square Root") if s else "None",
        _num(s.raw_lo) if s else "", _num(s.raw_hi) if s else "",
        _num(s.eng_lo) if s else "", _num(s.eng_hi) if s else "",
        "Double" if s else "",
        ("1" if s.clamp_lo else "0") if s else "", ("1" if s.clamp_hi else "0") if s else "",
        (s.units or "") if s else "", tag.description or "",
        ("1" if s.negate else "0") if s else "",
    ]
    if with_enabled:
        row.append("1" if tag.enabled else "0")
    return row


def render_kep_csv(tags: list[Tag]) -> str:
    with_enabled = any(not t.enabled for t in tags)
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow((*COLUMNS, ENABLED_COLUMN) if with_enabled else COLUMNS)
    for tag in tags:
        writer.writerow(_row(tag, with_enabled))
    return buf.getvalue()


def write_kep_csv(tags: list[Tag], path: str | Path) -> None:
    """Write `tags` as a KEPServerEX tag CSV (UTF-8, CRLF, no BOM)."""
    Path(path).write_text(render_kep_csv(tags), encoding="utf-8", newline="")
