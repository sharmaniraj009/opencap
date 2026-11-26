"""Staged project imports: the KEPServerEX import wizard's API (#33).

    POST   /api/v1/project/import?stage=true     upload, run the importer, keep its output, answer the report
    GET    /api/v1/project/import/{import_id}    the staged report again
    POST   /api/v1/project/import/{import_id}/apply   write it (If-Match, ?apply=true reloads)
    DELETE /api/v1/project/import/{import_id}    discard it

Privacy (D-010, D-087): staging lives under the config directory, in `.import-staging/`
(directory 0700, files 0600), never in the system temp dir. The uploaded export exists only while
the importer reads it (a 0600 temporary copy inside the staging directory) and is deleted before
the report is returned; what stays is the importer's output, which holds env-var names instead
of credentials. Nothing here logs the upload or the report. A staged import is removed when it is
applied or discarded, or `TTL_S` after it was made (checked on every call, by a timer while the
API runs, and when the API starts).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opencap.config.store import ConfigStore, NotFound, StorageError

STAGING_DIR = ".import-staging"
STAGED_FILE = "staged.json"
TTL_S = 3600
_ID = re.compile(r"^[0-9a-f]{32}$")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class Staging:
    """Staged imports of one config directory."""

    def __init__(self, store: ConfigStore, *, ttl_s: float | None = None, clock=None) -> None:
        self.store = store
        self.root = store.dir / STAGING_DIR
        self.ttl_s = TTL_S if ttl_s is None else ttl_s
        self.clock = clock or time.time

    # --- files -------------------------------------------------------------------------
    def _dir(self, import_id: str) -> Path:
        if not _ID.match(import_id):
            raise NotFound("unknown or expired import")
        return self.root / import_id

    def _private_dir(self, path: Path) -> None:
        path.mkdir(mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)                 # mkdir's mode is masked by the umask

    def _need_writable(self) -> None:
        w = self.store.writable()
        if not w["writable"]:
            raise StorageError(f"imports are staged in the config directory, which is read-only: {w['reason']}")

    def _load(self, import_id: str) -> dict[str, Any]:
        self.sweep()
        try:
            return json.loads((self._dir(import_id) / STAGED_FILE).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise NotFound("unknown or expired import: upload the project again") from None

    def _remove(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)

    # --- operations (blocking: run them in a worker thread) ------------------------------
    def create(self, raw: bytes, *, filename: str | None, who: str) -> dict[str, Any]:
        """Run the importer into a new staging directory; the report, with `import_id`."""
        self._need_writable()
        self.sweep()
        self._private_dir(self.root)
        import_id = secrets.token_hex(16)
        work = self.root / import_id
        self._private_dir(work)
        try:
            report, staged = self.store.stage_import(raw, filename=filename, work_dir=work)
            now = self.clock()
            report.update({"import_id": import_id, "staged": True, "applied": False,
                           "created_at": _iso(now), "expires_at": _iso(now + self.ttl_s)})
            doc = {"created": now, "expires": now + self.ttl_s, "who": who, "report": report, "staged": staged}
            path = work / STAGED_FILE
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
        except BaseException:
            self._remove(work)
            raise
        return report

    def report(self, import_id: str) -> dict[str, Any]:
        return self._load(import_id)["report"]

    def apply(self, import_id: str, *, if_match: str | None) -> dict[str, Any]:
        """Write the staged import (normal atomic write, revision, audit); the staging goes."""
        doc = self._load(import_id)
        report = self.store.apply_staged(doc["staged"], if_match=if_match)
        self._remove(self._dir(import_id))
        report["import_id"] = import_id
        return report

    def discard(self, import_id: str) -> None:
        path = self._dir(import_id)
        self.sweep()
        if not path.is_dir():
            raise NotFound("unknown or expired import")
        self._remove(path)

    def sweep(self) -> list[str]:
        """Remove expired staged imports (and leftovers without a readable staged.json)."""
        removed: list[str] = []
        try:
            entries = list(self.root.iterdir())
        except OSError:
            return removed
        now = self.clock()
        for path in entries:
            if not path.is_dir():
                continue
            try:
                expires = json.loads((path / STAGED_FILE).read_text(encoding="utf-8"))["expires"]
            except (OSError, ValueError, KeyError, TypeError):
                # being created right now, or a crash left it: judge by the directory's age
                try:
                    expires = path.stat().st_mtime + self.ttl_s
                except OSError:
                    continue
            if now >= expires:
                self._remove(path)
                removed.append(path.name)
        return removed

    def schedule_sweep(self) -> None:
        """While the API runs, sweep again when the newest staged import expires."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.call_later(self.ttl_s + 1, lambda: loop.run_in_executor(None, self.sweep))
