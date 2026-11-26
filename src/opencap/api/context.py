"""State shared by the REST API and the UI: poller, auth, config store, audit log."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from starlette.concurrency import run_in_threadpool

from opencap.api.audit import AuditLog
from opencap.api.auth import Auth, Principal
from opencap.api.events import RefusalLog
from opencap.config.models import ApiCfg, PollerConfig
from opencap.config.store import ConfigStore, Result

if TYPE_CHECKING:
    from opencap.core.runtime import Poller

# poller.yaml settings that `POST /reload` does not apply (the process must restart)
RESTART_FIELDS = ("instance", "log_level", "mqtt", "saf", "api")


class PendingTracker:
    """Does the config on disk differ from the config the poller runs? (cached per revision)"""

    def __init__(self, poller: Any, store: ConfigStore | None) -> None:
        self.poller = poller
        self.store = store
        self._key: tuple[str, Any] | None = None
        self._value: dict[str, Any] | None = None

    def status(self) -> dict[str, Any]:
        running = getattr(self.poller, "loaded", None)
        if self.store is None or running is None:
            return {"available": False, "pending_reload": False}
        disk = self.store.load_disk()
        if self._key is not None and self._key[0] == disk.revision and self._key[1] is running:
            return self._value
        out: dict[str, Any] = {"available": True, "revision": disk.revision, "disk_valid": disk.error is None,
                               "error": disk.error, "channels": {"added": [], "removed": [], "changed": []},
                               "poller_changed": [], "restart_needed": []}
        if disk.loaded is None:
            out["pending_reload"] = True       # a reload would fail and keep the running config
        else:
            old = {c.name: c.model_dump_json() for c in running.channels}
            new = {c.name: c.model_dump_json() for c in disk.loaded.channels}
            out["channels"] = {
                "added": [n for n in new if n not in old],
                "removed": [n for n in old if n not in new],
                "changed": [n for n in new if n in old and old[n] != new[n]],
            }
            changed_fields = [f for f in PollerConfig.model_fields
                              if getattr(running.poller, f) != getattr(disk.loaded.poller, f)]
            out["poller_changed"] = changed_fields
            out["restart_needed"] = [f for f in changed_fields if f in RESTART_FIELDS]
            out["pending_reload"] = bool(any(out["channels"].values()) or changed_fields)
        self._key, self._value = (disk.revision, running), out
        return out


def _cause(poller: Any, principal: Principal) -> dict[str, str]:
    """`who` and `via` for the `config_reloaded` line, when the poller accepts them (LOG-R15)."""
    try:
        accepts = "who" in inspect.signature(poller.reload).parameters
    except (TypeError, ValueError):
        accepts = False
    return {"who": principal.name, "via": principal.via} if accepts else {}


@dataclass
class ApiContext:
    poller: "Poller"
    cfg: ApiCfg
    auth: Auth
    store: ConfigStore | None
    audit: AuditLog
    tracker: PendingTracker = field(init=False)
    identity_cache: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict)
    refusals: RefusalLog = field(default_factory=RefusalLog)       # api.request_refused, rate limited

    def __post_init__(self) -> None:
        self.tracker = PendingTracker(self.poller, self.store)
        if self.store is not None:
            self.store.on_commit = self._audit_commit

    def _audit_commit(self, result: Result, writer: Any, detail: dict[str, Any] | None) -> None:
        """Called by the store with its lock held, right after the files are written. If the
        audit entry cannot be written the store rolls the change back (no unaudited changes)."""
        who = writer if isinstance(writer, Principal) else Principal("local", "admin", "api")
        self.audit.record(who, result.action, result.target, revision=result.revision,
                          files=result.files, changes=result.changes, truncated=result.truncated,
                          detail=detail)

    async def reload(self, principal: Principal) -> dict[str, list[str]]:
        """`poller.reload()` under the store lock (no half-written multi-file change), audited."""
        lock = self.store.lock if self.store is not None else None
        if lock is not None:
            # poll instead of blocking a worker thread: a cancelled request must never leave
            # the lock taken (there is no await between the acquire and the try)
            while not lock.acquire(blocking=False):
                await asyncio.sleep(0.02)
        try:
            changed = await self.poller.reload(**_cause(self.poller, principal))
        finally:
            if lock is not None:
                lock.release()
        revision = await run_in_threadpool(self.store.revision) if self.store is not None else None
        detail = {k: v for k, v in changed.items() if k != "unchanged" and v}
        await run_in_threadpool(lambda: self.audit.record(principal, "reload", "poller", revision=revision,
                                                          detail=detail or None))
        return changed
