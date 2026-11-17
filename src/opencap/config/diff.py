"""Diffs of config data: field changes for the audit log, and the import dry-run report."""

from __future__ import annotations

import difflib
from typing import Any

MAX_CHANGES = 200          # per audit entry / per device in a report
MAX_TEXT_DIFF_LINES = 400


def _keyed(items: list[Any]) -> dict[str, Any] | None:
    """Lists of named objects (tags, channels, devices) are compared by name/id, not position."""
    if not items or not all(isinstance(i, dict) for i in items):
        return None
    for key in ("id", "name"):
        if all(key in i for i in items):
            names = [str(i[key]) for i in items]
            if len(set(names)) == len(names):
                return dict(zip(names, items))
    return None


def changes(old: Any, new: Any, path: str = "", out: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Leaf-level differences as [{"path": "tags/FLOW_1/scan_ms", "old": 100, "new": 1000}].

    A missing side is None. Mappings recurse; lists of named dicts are matched by id/name.
    """
    out = [] if out is None else out
    if isinstance(old, dict) and isinstance(new, dict):
        for key in [*old, *(k for k in new if k not in old)]:
            sub = f"{path}/{key}" if path else str(key)
            if key not in new:
                out.append({"path": sub, "old": old[key], "new": None})
            elif key not in old:
                out.append({"path": sub, "old": None, "new": new[key]})
            else:
                changes(old[key], new[key], sub, out)
        return out
    if isinstance(old, list) and isinstance(new, list) and old != new:
        ko, kn = _keyed(old), _keyed(new)
        if ko is not None and kn is not None:
            return changes(ko, kn, path, out)
    if old != new:
        out.append({"path": path, "old": old, "new": new})
    return out


def capped(items: list[dict[str, Any]], limit: int = MAX_CHANGES) -> dict[str, Any]:
    return {"changes": items[:limit], "truncated": max(0, len(items) - limit)}


def _devices(df: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {d["id"]: d for c in df.get("channels", []) for d in c.get("devices", [])}


def devices_file_diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """What replacing devices.json `old` with `new` changes, by channel, device and tag."""
    old_ch = {c["name"]: c for c in old.get("channels", [])}
    new_ch = {c["name"]: c for c in new.get("channels", [])}
    old_dev, new_dev = _devices(old), _devices(new)

    added = [{"id": i, "driver": d.get("driver"), "host": d.get("host"), "tags": len(d.get("tags", []))}
             for i, d in new_dev.items() if i not in old_dev]
    removed = [{"id": i, "driver": d.get("driver"), "host": d.get("host"), "tags": len(d.get("tags", []))}
               for i, d in old_dev.items() if i not in new_dev]
    changed = []
    tag_totals = {"added": sum(t["tags"] for t in added), "removed": sum(t["tags"] for t in removed), "changed": 0}
    for dev_id, nd in new_dev.items():
        od = old_dev.get(dev_id)
        if od is None or od == nd:
            continue
        fields = changes({k: v for k, v in od.items() if k != "tags"}, {k: v for k, v in nd.items() if k != "tags"})
        ot = {t["name"]: t for t in od.get("tags", [])}
        nt = {t["name"]: t for t in nd.get("tags", [])}
        tags_changed = [{"name": n, "changes": changes(ot[n], nt[n])} for n in nt if n in ot and ot[n] != nt[n]]
        entry = {
            "id": dev_id,
            "fields": fields[:MAX_CHANGES],
            "tags_added": [n for n in nt if n not in ot],
            "tags_removed": [n for n in ot if n not in nt],
            "tags_changed": tags_changed[:MAX_CHANGES],
            "tags_changed_total": len(tags_changed),
        }
        tag_totals["added"] += len(entry["tags_added"])
        tag_totals["removed"] += len(entry["tags_removed"])
        tag_totals["changed"] += len(tags_changed)
        changed.append(entry)
    channels_changed = [n for n in new_ch if n in old_ch
                        and {k: v for k, v in new_ch[n].items() if k != "devices"}
                        != {k: v for k, v in old_ch[n].items() if k != "devices"}]
    excluded_old = {(e.get("channel"), e.get("device")) for e in old.get("excluded", [])}
    excluded_new = {(e.get("channel"), e.get("device")) for e in new.get("excluded", [])}
    return {
        "summary": {
            "channels": {"added": len([n for n in new_ch if n not in old_ch]),
                         "removed": len([n for n in old_ch if n not in new_ch]),
                         "changed": len(channels_changed)},
            "devices": {"added": len(added), "removed": len(removed), "changed": len(changed),
                        "unchanged": len(new_dev) - len(added) - len(changed)},
            "tags": tag_totals,
            "excluded": {"added": len(excluded_new - excluded_old), "removed": len(excluded_old - excluded_new)},
        },
        "channels_added": [n for n in new_ch if n not in old_ch],
        "channels_removed": [n for n in old_ch if n not in new_ch],
        "channels_changed": channels_changed,
        "devices_added": added,
        "devices_removed": removed,
        "devices_changed": changed,
        "unchanged": old == new,
    }


def text_diff(old: str | None, new: str | None, name: str) -> dict[str, Any]:
    """Unified diff of a text file (for the YAML files of an import)."""
    lines = list(difflib.unified_diff(
        (old or "").splitlines(keepends=True), (new or "").splitlines(keepends=True),
        fromfile=f"{name} (current)" if old is not None else "/dev/null",
        tofile=f"{name} (imported)" if new is not None else "/dev/null",
    ))
    return {
        "unchanged": old == new,
        "created": old is None and new is not None,
        "diff": "".join(lines[:MAX_TEXT_DIFF_LINES]),
        "truncated": max(0, len(lines) - MAX_TEXT_DIFF_LINES),
    }
