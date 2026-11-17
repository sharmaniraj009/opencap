"""Audit log of configuration changes: who (token name), when, what. JSON lines, append-only.

One line per accepted change (config writes, imports, reloads):

    {"ts": "2026-11-17T10:15:00.123Z", "who": "alice", "role": "admin", "via": "ui",
     "action": "device.update", "target": "Site 001/8 PUMP", "revision": "3f2a...",
     "files": ["devices.json"], "changes": [{"path": "timeout_ms", "old": 3000, "new": 1500}]}

Rejected changes are not logged (nothing changed). The file rotates to `<name>.1` at
`MAX_BYTES`; `tail()` reads both, newest first.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opencap.api.auth import Principal
from opencap.core.health import iso_utc

MAX_BYTES = 16 * 1024 * 1024
_BLOCK = 64 * 1024


def _lines_reversed(path: Path) -> Iterator[str]:
    """The file's lines, last first, without reading it all."""
    try:
        f = open(path, "rb")
    except FileNotFoundError:
        return
    with f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        rest = b""
        while pos > 0:
            step = min(_BLOCK, pos)
            pos -= step
            f.seek(pos)
            chunk = f.read(step) + rest
            lines = chunk.split(b"\n")
            rest = lines[0]
            for line in reversed(lines[1:]):
                if line.strip():
                    yield line.decode("utf-8", "replace")
        if rest.strip():
            yield rest.decode("utf-8", "replace")


class AuditLog:
    def __init__(self, path: Path | str | None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()

    def record(self, who: Principal, action: str, target: str, *, revision: str | None = None,
               files: list[str] | None = None, changes: list[dict[str, Any]] | None = None,
               truncated: int = 0, detail: dict[str, Any] | None = None) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "ts": iso_utc(datetime.now(UTC)), "who": who.name, "role": who.role, "via": who.via,
            "action": action, "target": target,
        }
        if revision is not None:
            entry["revision"] = revision
        if files is not None:
            entry["files"] = files
        if changes is not None:
            entry["changes"] = changes
        if truncated:
            entry["truncated"] = truncated
        if detail:
            entry["detail"] = detail
        if self.path is None:
            return entry
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if self.path.stat().st_size + len(line) > MAX_BYTES:
                    os.replace(self.path, self.path.with_name(self.path.name + ".1"))
            except FileNotFoundError:
                pass
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
        return entry

    def tail(self, limit: int = 100, offset: int = 0, *, action: str | None = None,
             who: str | None = None, target: str | None = None) -> list[dict[str, Any]]:
        """Newest first. `action` matches a prefix ("device" matches "device.update")."""
        if self.path is None:
            return []
        out: list[dict[str, Any]] = []
        skipped = 0
        for path in (self.path, self.path.with_name(self.path.name + ".1")):
            for line in _lines_reversed(path):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if action and not str(entry.get("action", "")).startswith(action):
                    continue
                if who and entry.get("who") != who:
                    continue
                if target and target not in str(entry.get("target", "")):
                    continue
                if skipped < offset:
                    skipped += 1
                    continue
                out.append(entry)
                if len(out) >= limit:
                    return out
        return out
