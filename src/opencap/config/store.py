"""Config store: the read-modify-write layer of the config API (KEPServerEX Configuration API
equivalent).

Every change goes through one pipeline, under one lock:

1. read the four config files (`poller.yaml`, `devices.json`, `devices_map.yaml`,
   `overrides.yaml`) and compute the config **revision** (hash of their bytes). A caller
   that sends `if_match` gets `Conflict` when the files changed since it read them;
2. apply the change to plain data and validate the edited object with its pydantic model
   (errors keep a location relative to that object, for inline UI errors);
3. render the new file texts (devices.json in the importer's layout; the YAML files with
   their comments kept, see `yamledit`) and run the real loader (`load_config`) on them in
   a staging directory, so a change is accepted only if `POST /reload` would accept it;
4. build the block plan of the affected devices: a driver that rejects the device's params
   or an enabled tag's address is a validation error, not a surprise at reload;
5. write the changed files atomically (temp file + fsync + rename) and keep the previous
   version as `<file>.bak`.

Files are written one by one. Each write is atomic and every intermediate state loads
(unknown ids in the map/overrides are warnings), and `POST /reload` takes the same lock.
Writes never apply anything to the running poller: that is `POST /reload`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, get_args

from pydantic import BaseModel, ValidationError

from opencap import FIRM_VERSION
from opencap.config import yamledit
from opencap.config.diff import capped, changes, devices_file_diff, text_diff
from opencap.config.kep_import import (
    DEVICES_MAP_HEADER,
    OVERRIDES_TEMPLATE,
    devices_json_text,
    import_kep,
    stub_sub_device_id,
)
from opencap.config.loader import (
    DEVICES_FILE,
    MAP_FILE,
    OVERRIDES_FILE,
    POLLER_FILE,
    LoadedConfig,
    load_config,
)
from opencap.config.models import (
    Channel,
    Device,
    DeviceMap,
    DeviceMapEntry,
    DeviceOverride,
    DevicesFile,
    DType,
    Overrides,
    PollerConfig,
    PublishCfg,
    Tag,
)

CONFIG_FILES = (POLLER_FILE, DEVICES_FILE, MAP_FILE, OVERRIDES_FILE)
EXPORT_FORMAT = "opencap-config"
MAP_TEMPLATE = DEVICES_MAP_HEADER + "defaults: {}\ndevices: {}\n"
MAX_IMPORT_BYTES = 64 * 1024 * 1024
_MODEL_FILES = {"PollerConfig": POLLER_FILE, "DevicesFile": DEVICES_FILE, "DeviceMap": MAP_FILE,
                "Overrides": OVERRIDES_FILE, "Device": f"{DEVICES_FILE} (after overrides)"}
_RESTART_SECTIONS = ("mqtt", "saf", "api")


# --- errors ---------------------------------------------------------------------------

class ConfigError(Exception):
    """Base error; `status` is the HTTP status the API answers with."""

    status = 400

    def __init__(self, message: str, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.errors = errors or []


class NotFound(ConfigError):
    status = 404


class Conflict(ConfigError):
    status = 409


class Invalid(ConfigError):
    """Validation failed. `errors` are pydantic-style {"loc": [...], "msg": ..., "type": ...}."""

    status = 422


def _err(loc: tuple | list, msg: str, type_: str = "value_error") -> dict[str, Any]:
    return {"loc": [str(p) if not isinstance(p, int) else p for p in loc], "msg": msg, "type": type_}


def _pyd_errors(exc: ValidationError, prefix: tuple = ()) -> list[dict[str, Any]]:
    # the input is left out on purpose: it can be a whole devices.json
    return [_err((*prefix, *e["loc"]), e["msg"], e["type"]) for e in exc.errors(include_url=False)]


def _validate(model: type[BaseModel], data: Any, prefix: tuple = ()) -> BaseModel:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise Invalid(f"invalid {model.__name__}", _pyd_errors(exc, prefix)) from None


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386 JSON merge patch: objects merge, `null` deletes a key, anything else replaces."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = merge_patch(out.get(key), value)
    return out


def _check_name(value: Any, what: str, loc: str = "name") -> str:
    if not isinstance(value, str) or not value.strip():
        raise Invalid(f"{what} name is required", [_err((loc,), f"{what} name is required", "missing")])
    if "/" in value:
        raise Invalid(f"'/' is not allowed in a {what} name",
                      [_err((loc,), f"'/' is not allowed in a {what} name (device ids are '<channel>/<device>')")])
    return value


def _check_text(data: dict[str, Any], key: str, what: str, prefix: tuple = ()) -> None:
    """The models accept empty strings; names, hosts and addresses must not be empty."""
    value = data.get(key)
    if isinstance(value, str) and not value.strip():
        raise Invalid(f"{what} must not be empty", [_err((*prefix, key), f"{what} must not be empty", "string_too_short")])


def _require_dict(body: Any, what: str) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise Invalid(f"{what} must be a JSON object", [_err((), f"{what} must be a JSON object", "dict_type")])
    return body


# --- atomic file writes ---------------------------------------------------------------

def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write(path: Path, text: str, *, backup: bool = True) -> None:
    """Write `text` to `path` atomically; the previous file (if any) becomes `<name>.bak`."""
    path = Path(path)
    tag = uuid.uuid4().hex[:8]
    tmp = path.with_name(f".{path.name}.{tag}.tmp")
    btmp = path.with_name(f".{path.name}.bak.{tag}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            shutil.copymode(path, tmp)
            if backup:
                try:
                    os.link(path, btmp)              # the old inode lives on as the .bak
                except OSError:
                    shutil.copy2(path, btmp)
                os.replace(btmp, path.with_name(path.name + ".bak"))
        os.replace(tmp, path)
    finally:
        for leftover in (tmp, btmp):
            if leftover.exists():
                leftover.unlink()
    _fsync_dir(path.parent)


# --- state ----------------------------------------------------------------------------

@dataclass
class _State:
    texts: dict[str, str | None]
    revision: str
    devices: dict[str, Any] | None          # normalized devices.json; None = file missing
    devices_error: str | None
    dmap: dict[str, Any]                    # devices_map.yaml as the loader reads it
    overrides: dict[str, Any]
    yaml_errors: dict[str, str]


@dataclass
class Result:
    """Outcome of one accepted change (what the API returns and the audit log records)."""

    action: str
    target: str
    revision: str
    changes: list[dict[str, Any]]
    truncated: int
    warnings: list[str]
    files: list[str]
    data: Any = None
    comments_lost: list[str] = field(default_factory=list)

    def body(self) -> dict[str, Any]:
        out = {"ok": True, "action": self.action, "target": self.target, "revision": self.revision,
               "files": self.files, "changes": self.changes, "truncated": self.truncated,
               "warnings": self.warnings}
        if self.comments_lost:
            out["comments_lost"] = self.comments_lost
        if self.data is not None:
            out["data"] = self.data
        return out


@dataclass
class DiskState:
    revision: str
    loaded: LoadedConfig | None
    error: str | None
    errors: list[dict[str, Any]] = field(default_factory=list)


class _Txn:
    """Mutable copy of the config for one change."""

    def __init__(self, state: _State) -> None:
        self.state = state
        if state.devices is None and state.devices_error is None:
            self.devices: dict[str, Any] = {"version": 1, "channels": [], "excluded": []}
        elif state.devices is None:
            raise Conflict(f"{DEVICES_FILE} is invalid; fix it by hand or import a project: {state.devices_error}")
        else:
            self.devices = copy.deepcopy(state.devices)
        for name, error in state.yaml_errors.items():
            raise Conflict(f"{name} is invalid; fix it by hand first: {error}")
        self.dmap = copy.deepcopy(state.dmap)
        self.overrides = copy.deepcopy(state.overrides)
        self.map_renames: yamledit.Renames = {}
        self.ov_renames: yamledit.Renames = {}
        self.raw_texts: dict[str, str] = {}               # files replaced verbatim (import)
        self.check_devices: set[str] = set()              # plan these effective devices
        self.check_tags: dict[str, set[str]] = {}         # ... and these tags must plan
        self.warnings: list[str] = []

    # devices.json ------------------------------------------------------------------
    def channel(self, name: str) -> dict[str, Any]:
        for ch in self.devices["channels"]:
            if ch["name"] == name:
                return ch
        raise NotFound(f"unknown channel {name!r}")

    def has_channel(self, name: str) -> bool:
        return any(ch["name"] == name for ch in self.devices["channels"])

    def device_ids(self) -> set[str]:
        return {d["id"] for ch in self.devices["channels"] for d in ch["devices"]}

    def device(self, dev_id: str) -> tuple[dict[str, Any], int]:
        for ch in self.devices["channels"]:
            for i, dev in enumerate(ch["devices"]):
                if dev["id"] == dev_id:
                    return ch, i
        raise NotFound(f"unknown device {dev_id!r}")

    def check(self, dev_id: str, tags: set[str] | None = None) -> None:
        self.check_devices.add(dev_id)
        if tags:
            self.check_tags.setdefault(dev_id, set()).update(tags)

    # devices_map.yaml / overrides.yaml ------------------------------------------------
    def _section(self, doc: dict[str, Any], key: str) -> dict[str, Any]:
        if not isinstance(doc.get(key), dict):
            doc[key] = {}
        return doc[key]

    def map_devices(self) -> dict[str, Any]:
        return self._section(self.dmap, "devices")

    def ov_devices(self) -> dict[str, Any]:
        return self._section(self.overrides, "devices")

    def ov_channels(self) -> dict[str, Any]:
        return self._section(self.overrides, "channels")

    @staticmethod
    def _rename_key(section: dict[str, Any], old: str, new: str) -> dict[str, Any]:
        return {(new if k == old else k): v for k, v in section.items()}

    def rename_device_refs(self, old_id: str, new_id: str) -> None:
        for doc, renames, fname in ((self.dmap, self.map_renames, MAP_FILE),
                                    (self.overrides, self.ov_renames, OVERRIDES_FILE)):
            section = doc.get("devices")
            if isinstance(section, dict) and old_id in section:
                if new_id in section:
                    raise Conflict(f"{fname} already has an entry for {new_id!r}; remove it first")
                doc["devices"] = self._rename_key(section, old_id, new_id)
                renames.setdefault(("devices",), {})[old_id] = new_id

    def drop_device_refs(self, dev_id: str) -> dict[str, Any]:
        removed = {}
        for doc, key in ((self.dmap, "map_entry"), (self.overrides, "override")):
            section = doc.get("devices")
            if isinstance(section, dict) and dev_id in section:
                removed[key] = section.pop(dev_id)
        return removed

    def rename_tag_override(self, dev_id: str, old: str, new: str) -> None:
        ov = self.ov_devices().get(dev_id)
        if isinstance(ov, dict) and isinstance(ov.get("tags"), dict) and old in ov["tags"]:
            if new in ov["tags"]:
                raise Conflict(f"{OVERRIDES_FILE} already has a tag override {new!r} for {dev_id!r}")
            ov["tags"] = self._rename_key(ov["tags"], old, new)
            self.ov_renames.setdefault(("devices", dev_id, "tags"), {})[old] = new

    def drop_tag_override(self, dev_id: str, tag: str) -> Any:
        ov = self.ov_devices().get(dev_id)
        if isinstance(ov, dict) and isinstance(ov.get("tags"), dict) and tag in ov["tags"]:
            return ov["tags"].pop(tag)
        return None


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True)


def _unset_dump(model: BaseModel) -> dict[str, Any]:
    """Only the keys the caller gave (keeps hand-owned YAML minimal)."""
    return model.model_dump(mode="json", exclude_unset=True)


def _device_summary(dev: dict[str, Any], dmap: dict[str, Any], ov: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": dev["id"], "channel": dev["channel"], "name": dev["name"], "driver": dev["driver"],
        "host": dev["host"], "port": dev["port"], "enabled": dev.get("enabled", True),
        "tags": len(dev.get("tags", [])),
        "sub_device_id": (dmap.get(dev["id"]) or {}).get("sub_device_id") if isinstance(dmap, dict) else None,
        "has_override": isinstance(ov, dict) and dev["id"] in ov,
    }


def _changed_tags(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> set[str]:
    """Enabled tags that are new or whose address/dtype/enabled changed: these must plan."""
    before = {t["name"]: t for t in old}
    out = set()
    for t in new:
        if not t.get("enabled", True):
            continue
        o = before.get(t["name"])
        if o is None or any(o.get(k) != t.get(k) for k in ("address", "dtype", "enabled")):
            out.add(t["name"])
    return out


def _safe_filename(name: str | None) -> str:
    base = Path(name or "project.json").name
    base = re.sub(r"[^A-Za-z0-9._ -]+", "_", base).strip(" .") or "project.json"
    return base[:100]


# --- the store ------------------------------------------------------------------------

class ConfigStore:
    """Read and change the config directory. Thread-safe; see the module docstring."""

    def __init__(self, config_dir: Path) -> None:
        self.dir = Path(config_dir)
        self.lock = threading.Lock()                 # held for every change and by POST /reload
        self._disk_cache: DiskState | None = None

    # reading ------------------------------------------------------------------------
    def _raw(self) -> tuple[dict[str, bytes | None], str]:
        raw: dict[str, bytes | None] = {}
        for name in CONFIG_FILES:
            try:
                raw[name] = (self.dir / name).read_bytes()
            except FileNotFoundError:
                raw[name] = None
        h = hashlib.sha256()
        for name in CONFIG_FILES:
            h.update(name.encode() + b"\0" + (b"\1" + raw[name] if raw[name] is not None else b"\0") + b"\0")
        return raw, h.hexdigest()[:16]

    def _read(self) -> _State:
        raw, revision = self._raw()
        texts = {n: (b.decode("utf-8-sig") if b is not None else None) for n, b in raw.items()}
        devices, devices_error = None, None
        if texts[DEVICES_FILE] is not None:
            try:
                devices = _dump(DevicesFile.model_validate(json.loads(texts[DEVICES_FILE])))
            except (ValueError, ValidationError) as exc:
                devices_error = str(exc).splitlines()[0] if isinstance(exc, ValidationError) else str(exc)
        yaml_errors: dict[str, str] = {}
        parsed: dict[str, dict[str, Any]] = {}
        for name in (MAP_FILE, OVERRIDES_FILE):
            try:
                parsed[name] = yamledit.parse(texts[name])
            except Exception as exc:  # noqa: BLE001 - yaml.YAMLError or a non-mapping top level
                parsed[name] = {}
                yaml_errors[name] = str(exc)
        return _State(texts=texts, revision=revision, devices=devices, devices_error=devices_error,
                      dmap=parsed[MAP_FILE], overrides=parsed[OVERRIDES_FILE], yaml_errors=yaml_errors)

    def revision(self) -> str:
        return self._raw()[1]

    @staticmethod
    def _check_if_match(if_match: str | None, state: _State) -> None:
        if if_match and if_match.strip().strip('"') != state.revision:
            raise Conflict(f"config changed since revision {if_match.strip().strip(chr(34))} "
                           f"(now {state.revision}); read it again and retry")

    def load_disk(self) -> DiskState:
        """The loader's result for the files on disk (cached by revision)."""
        cached = self._disk_cache
        if cached is not None and cached.revision == self.revision():
            return cached
        state = self._read()
        try:
            disk = DiskState(state.revision, self._load_texts(state.texts), None)
        except Invalid as exc:
            disk = DiskState(state.revision, None, exc.message, exc.errors)
        self._disk_cache = disk
        return disk

    def _devices_or_empty(self, state: _State) -> dict[str, Any]:
        if state.devices is not None:
            return state.devices
        if state.devices_error is not None:
            raise Conflict(f"{DEVICES_FILE} is invalid: {state.devices_error}")
        return {"version": 1, "channels": [], "excluded": []}

    def summary(self) -> dict[str, Any]:
        from opencap.drivers import DRIVERS

        state = self._read()
        disk = self.load_disk()
        files = {}
        for name in CONFIG_FILES:
            p = self.dir / name
            files[name] = ({"exists": True, "bytes": p.stat().st_size,
                            "modified": datetime.fromtimestamp(p.stat().st_mtime, UTC).isoformat(timespec="seconds")}
                           if p.exists() else {"exists": False})
        devices = state.devices or {"channels": []}
        return {
            "revision": state.revision,
            "config_dir": str(self.dir),
            "files": files,
            "valid": disk.error is None,
            "error": disk.error,
            "errors": disk.errors,
            "warnings": disk.loaded.warnings if disk.loaded else [],
            "counts": {
                "channels": len(devices["channels"]),
                "devices": sum(len(c["devices"]) for c in devices["channels"]),
                "tags": sum(len(d.get("tags", [])) for c in devices["channels"] for d in c["devices"]),
                "excluded": len((state.devices or {}).get("excluded", [])),
            },
            "drivers": list(DRIVERS),
            "dtypes": list(get_args(DType)),
        }

    def list_channels(self) -> list[dict[str, Any]]:
        state = self._read()
        ov = state.overrides.get("channels") if isinstance(state.overrides.get("channels"), dict) else {}
        return [{"name": c["name"], "enabled": c.get("enabled", True), "devices": len(c["devices"]),
                 "override": ov.get(c["name"])} for c in self._devices_or_empty(state)["channels"]]

    def get_channel(self, name: str) -> dict[str, Any]:
        state = self._read()
        dmap = state.dmap.get("devices") or {}
        ovd = state.overrides.get("devices") or {}
        ovc = state.overrides.get("channels") if isinstance(state.overrides.get("channels"), dict) else {}
        for c in self._devices_or_empty(state)["channels"]:
            if c["name"] == name:
                return {"name": name, "enabled": c.get("enabled", True), "override": ovc.get(name),
                        "devices": [_device_summary(d, dmap, ovd) for d in c["devices"]],
                        "revision": state.revision}
        raise NotFound(f"unknown channel {name!r}")

    def list_devices(self, channel: str | None = None) -> list[dict[str, Any]]:
        state = self._read()
        dmap = state.dmap.get("devices") or {}
        ovd = state.overrides.get("devices") or {}
        return [_device_summary(d, dmap, ovd) for c in self._devices_or_empty(state)["channels"]
                if channel is None or c["name"] == channel for d in c["devices"]]

    def get_device(self, dev_id: str) -> dict[str, Any]:
        state = self._read()
        for c in self._devices_or_empty(state)["channels"]:
            for d in c["devices"]:
                if d["id"] == dev_id:
                    return {"device": d, "map_entry": (state.dmap.get("devices") or {}).get(dev_id),
                            "override": (state.overrides.get("devices") or {}).get(dev_id),
                            "revision": state.revision}
        raise NotFound(f"unknown device {dev_id!r}")

    def get_tag(self, dev_id: str, tag: str) -> dict[str, Any]:
        dev = self.get_device(dev_id)
        for t in dev["device"]["tags"]:
            if t["name"] == tag:
                ov = ((dev["override"] or {}).get("tags") or {}).get(tag)
                return {"tag": t, "override": ov, "revision": dev["revision"]}
        raise NotFound(f"device {dev_id!r} has no tag {tag!r}")

    def get_map(self) -> dict[str, Any]:
        state = self._read()
        return {"defaults": state.dmap.get("defaults") or {}, "devices": state.dmap.get("devices") or {},
                "revision": state.revision}

    def get_map_entry(self, dev_id: str) -> dict[str, Any]:
        state = self._read()
        entry = (state.dmap.get("devices") or {}).get(dev_id)
        if entry is None:
            raise NotFound(f"{MAP_FILE} has no entry for {dev_id!r}")
        return {"id": dev_id, "entry": entry, "revision": state.revision}

    def get_overrides(self) -> dict[str, Any]:
        state = self._read()
        return {"channels": state.overrides.get("channels") or {}, "devices": state.overrides.get("devices") or {},
                "revision": state.revision}

    def get_device_override(self, dev_id: str) -> dict[str, Any]:
        state = self._read()
        ov = (state.overrides.get("devices") or {}).get(dev_id)
        if ov is None:
            raise NotFound(f"{OVERRIDES_FILE} has no override for device {dev_id!r}")
        return {"id": dev_id, "override": ov, "revision": state.revision}

    def get_channel_override(self, name: str) -> dict[str, Any]:
        state = self._read()
        ov = (state.overrides.get("channels") or {}).get(name)
        if ov is None:
            raise NotFound(f"{OVERRIDES_FILE} has no override for channel {name!r}")
        return {"name": name, "override": ov, "revision": state.revision}

    def export(self) -> dict[str, Any]:
        state = self._read()
        return {
            "format": EXPORT_FORMAT, "version": 1, "opencap": FIRM_VERSION,
            "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "revision": state.revision,
            "files": {n: t for n, t in state.texts.items() if t is not None},
        }

    # validation ---------------------------------------------------------------------
    def _load_texts(self, texts: dict[str, str | None]) -> LoadedConfig:
        """Run the real loader on these file texts (staged in a temporary directory)."""
        with tempfile.TemporaryDirectory(prefix="opencap-stage-") as tmp:
            stage = Path(tmp)
            for name, text in texts.items():
                if text is not None:
                    (stage / name).write_text(text, encoding="utf-8")
            try:
                return load_config(stage)
            except ValidationError as exc:
                where = _MODEL_FILES.get(exc.title, exc.title)
                raise Invalid(f"{where} does not validate", _pyd_errors(exc, (where,))) from None
            except (ValueError, FileNotFoundError) as exc:
                msg = str(exc).replace(f"{stage}{os.sep}", "")
                raise Invalid(msg, [_err((), msg)]) from None

    def _plan_check(self, loaded: LoadedConfig, txn: _Txn) -> tuple[list[dict[str, Any]], list[str]]:
        from opencap.core.registry import plan_device

        errors: list[dict[str, Any]] = []
        warnings: list[str] = []
        effective = {d.id: d for c in loaded.channels for d in c.devices}
        for dev_id in sorted(txn.check_devices):
            dev = effective.get(dev_id)
            if dev is None:
                continue
            try:
                dp = plan_device(dev)
            except Exception as exc:  # noqa: BLE001 - a driver that rejects the device's settings
                errors.append(_err(("params",), f"{dev_id}: driver {dev.driver!r} rejects the device: {exc}"))
                continue
            if dp.error:
                warnings.append(f"{dev_id}: {dp.error}; the device is kept but not polled")
                continue
            must = txn.check_tags.get(dev_id, set())
            for name in sorted(must & set(dp.invalid)):
                errors.append(_err(("address",), f"{name}: {dp.invalid[name]}", "bad_config"))
            others = sorted(set(dp.invalid) - must)
            if others:
                warnings.append(f"{dev_id}: {len(others)} tag(s) are bad_config and will not be read: "
                                + ", ".join(others[:5]) + (" …" if len(others) > 5 else ""))
        return errors, warnings

    # the change pipeline ------------------------------------------------------------
    def _run(self, action: str, target: str, fn: Callable[[_Txn], tuple[Any, Any, Any]], *,
             if_match: str | None = None, dry_run: bool = False) -> Result:
        with self.lock:
            state = self._read()
            self._check_if_match(if_match, state)
            txn = _Txn(state)
            before, after, data = fn(txn)
            texts = dict(state.texts)
            comments_lost: list[str] = []
            base = state.devices if state.devices is not None else {"version": 1, "channels": [], "excluded": []}
            if txn.devices != base and DEVICES_FILE not in txn.raw_texts:
                model = _validate(DevicesFile, txn.devices, (DEVICES_FILE,))
                texts[DEVICES_FILE] = devices_json_text(model)
            for name, doc, model, template, renames in (
                (MAP_FILE, txn.dmap, DeviceMap, MAP_TEMPLATE, txn.map_renames),
                (OVERRIDES_FILE, txn.overrides, Overrides, OVERRIDES_TEMPLATE, txn.ov_renames),
            ):
                if doc != (state.dmap if name == MAP_FILE else state.overrides) and name not in txn.raw_texts:
                    if state.texts[name] is None:      # new file: keep the template's sections
                        doc = {**yamledit.parse(template), **doc}
                    _validate(model, doc, (name,))
                    text, preserved = yamledit.render(state.texts[name], doc, file_name=name,
                                                      template=template, renames=renames)
                    texts[name] = text
                    if not preserved:
                        comments_lost.append(name)
            texts.update(txn.raw_texts)
            changed = [n for n in CONFIG_FILES if texts[n] != state.texts[n]]
            loaded = self._load_texts(texts)
            errors, plan_warnings = self._plan_check(loaded, txn)
            if errors:
                raise Invalid("the driver rejects this configuration", errors)
            if not dry_run:
                for name in changed:
                    atomic_write(self.dir / name, texts[name])
            new_state_rev = self.revision() if not dry_run else state.revision
            if not dry_run:
                self._disk_cache = DiskState(new_state_rev, loaded, None)
            diff = changes(before, after)
            capped_diff = capped(diff)
            warnings = [*txn.warnings, *plan_warnings, *loaded.warnings]
            warnings += [f"{n}: comments could not be kept; the old text is in {n}.bak" for n in comments_lost]
            return Result(action=action, target=target, revision=new_state_rev, changes=capped_diff["changes"],
                          truncated=capped_diff["truncated"], warnings=warnings, files=changed, data=data,
                          comments_lost=comments_lost)

    # channels -----------------------------------------------------------------------
    def create_channel(self, body: Any, *, if_match: str | None = None) -> Result:
        body = _require_dict(body, "channel")
        name = _check_name(body.get("name"), "channel")
        data = {**body, "devices": []}
        devices_in = body.get("devices") or []
        if not isinstance(devices_in, list):
            raise Invalid("devices must be a list", [_err(("devices",), "devices must be a list", "list_type")])
        for i, dev in enumerate(devices_in):
            dev = _require_dict(dev, f"devices[{i}]")
            dev_name = _check_name(dev.get("name"), "device", loc="name")
            derived = f"{name}/{dev_name}"
            if dev.get("id", derived) != derived or dev.get("channel", name) != name:
                raise Invalid("device id is '<channel>/<name>'",
                              [_err(("devices", i, "id"), f"device id must be {derived!r}")])
            data["devices"].append({"tags": [], **dev, "id": derived, "channel": name})
        ch = _validate(Channel, data)

        def apply(txn: _Txn):
            if txn.has_channel(name):
                raise Conflict(f"channel {name!r} already exists")
            clash = txn.device_ids() & {d.id for d in ch.devices}
            if clash:
                raise Conflict(f"device ids already exist: {sorted(clash)}")
            dumped = _dump(ch)
            txn.devices["channels"].append(dumped)
            for d in dumped["devices"]:
                txn.check(d["id"], _changed_tags([], d["tags"]))
            return None, dumped, dumped

        return self._run("channel.create", name, apply, if_match=if_match)

    def update_channel(self, name: str, patch: Any, *, if_match: str | None = None) -> Result:
        patch = _require_dict(patch, "channel")
        unknown = sorted(set(patch) - {"name", "enabled"})
        if unknown:
            raise Invalid("only name and enabled can be changed here",
                          [_err((k,), "not a channel setting (use the device endpoints for devices)",
                                "extra_forbidden") for k in unknown])
        new_name = _check_name(patch.get("name", name), "channel")

        def apply(txn: _Txn):
            ch = txn.channel(name)
            before = {k: v for k, v in ch.items() if k != "devices"}
            merged = merge_patch(before, patch)
            merged["name"] = new_name
            _validate(Channel, {**merged, "devices": []})
            if new_name != name:
                if txn.has_channel(new_name):
                    raise Conflict(f"channel {new_name!r} already exists")
                for dev in ch["devices"]:
                    old_id, new_id = dev["id"], f"{new_name}/{dev['name']}"
                    dev["channel"], dev["id"] = new_name, new_id
                    txn.rename_device_refs(old_id, new_id)
                ovc = txn.overrides.get("channels")
                if isinstance(ovc, dict) and name in ovc:
                    txn.overrides["channels"] = txn._rename_key(ovc, name, new_name)
                    txn.ov_renames.setdefault(("channels",), {})[name] = new_name
            ch["name"] = new_name
            ch["enabled"] = merged.get("enabled", True)
            after = {k: v for k, v in ch.items() if k != "devices"}
            return before, after, after

        return self._run("channel.update", name, apply, if_match=if_match)

    def delete_channel(self, name: str, *, if_match: str | None = None) -> Result:
        def apply(txn: _Txn):
            ch = txn.channel(name)
            txn.devices["channels"].remove(ch)
            removed: dict[str, Any] = {"channel": ch, "refs": {}}
            for dev in ch["devices"]:
                refs = txn.drop_device_refs(dev["id"])
                if refs:
                    removed["refs"][dev["id"]] = refs
            ovc = txn.overrides.get("channels")
            if isinstance(ovc, dict) and name in ovc:
                removed["channel_override"] = ovc.pop(name)
            return removed, None, None

        return self._run("channel.delete", name, apply, if_match=if_match)

    # devices ------------------------------------------------------------------------
    def _device_body(self, body: dict[str, Any], channel: str, name: str, *, given_id: Any) -> dict[str, Any]:
        _check_name(name, "device")
        _check_name(channel, "channel", loc="channel")
        _check_text(body, "host", "host")
        derived = f"{channel}/{name}"
        if given_id is not None and given_id != derived:
            raise Invalid("device id is '<channel>/<name>'",
                          [_err(("id",), f"the id is derived from channel and name: {derived!r}")])
        return {**body, "id": derived, "channel": channel, "name": name}

    def create_device(self, channel: str, body: Any, *, if_match: str | None = None) -> Result:
        body = _require_dict(body, "device")
        if body.get("channel", channel) != channel:
            raise Invalid("channel comes from the URL", [_err(("channel",), f"must be {channel!r}")])
        data = self._device_body({"tags": [], **body}, channel, body.get("name"), given_id=body.get("id"))
        dev = _validate(Device, data)

        def apply(txn: _Txn):
            ch = txn.channel(channel)
            if dev.id in txn.device_ids():
                raise Conflict(f"device {dev.id!r} already exists")
            dumped = _dump(dev)
            ch["devices"].append(dumped)
            txn.check(dev.id, _changed_tags([], dumped["tags"]))
            return None, dumped, dumped

        return self._run("device.create", dev.id, apply, if_match=if_match)

    def update_device(self, dev_id: str, body: Any, *, partial: bool, if_match: str | None = None) -> Result:
        body = _require_dict(body, "device")

        def apply(txn: _Txn):
            ch, idx = txn.device(dev_id)
            old = ch["devices"][idx]
            merged = merge_patch(old, body) if partial else dict(body)
            new_channel = merged.get("channel", old["channel"])
            new_name = merged.get("name", old["name"])
            data = self._device_body(merged, new_channel, new_name, given_id=body.get("id"))
            dev = _validate(Device, data)
            dumped = _dump(dev)
            if dev.id != dev_id:
                if dev.id in txn.device_ids():
                    raise Conflict(f"device {dev.id!r} already exists")
                if new_channel != old["channel"]:
                    if not txn.has_channel(new_channel):
                        raise Invalid(f"unknown channel {new_channel!r}",
                                      [_err(("channel",), f"unknown channel {new_channel!r}")])
                txn.rename_device_refs(dev_id, dev.id)
            if new_channel != old["channel"]:
                del ch["devices"][idx]
                txn.channel(new_channel)["devices"].append(dumped)
            else:
                ch["devices"][idx] = dumped
            txn.check(dev.id, _changed_tags(old["tags"], dumped["tags"]))
            return old, dumped, dumped

        return self._run("device.update", dev_id, apply, if_match=if_match)

    def delete_device(self, dev_id: str, *, if_match: str | None = None) -> Result:
        def apply(txn: _Txn):
            ch, idx = txn.device(dev_id)
            dev = ch["devices"].pop(idx)
            return {"device": dev, **txn.drop_device_refs(dev_id)}, None, None

        return self._run("device.delete", dev_id, apply, if_match=if_match)

    # tags ---------------------------------------------------------------------------
    def create_tags(self, dev_id: str, body: Any, *, if_match: str | None = None) -> Result:
        items = body if isinstance(body, list) else [_require_dict(body, "tag")]
        tags = []
        for i, item in enumerate(items):
            prefix = (i,) if isinstance(body, list) else ()
            tag = _dump(_validate(Tag, item, prefix))
            _check_text(tag, "name", "tag name", prefix)
            _check_text(tag, "address", "address", prefix)
            tags.append(tag)
        names = [t["name"] for t in tags]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise Invalid("duplicate tag names in the request", [_err(("name",), f"duplicate tag names {dupes}")])

        def apply(txn: _Txn):
            ch, idx = txn.device(dev_id)
            dev = ch["devices"][idx]
            existing = {t["name"] for t in dev["tags"]}
            clash = sorted(existing & set(names))
            if clash:
                raise Conflict(f"device {dev_id!r} already has tag(s) {clash}")
            dev["tags"].extend(tags)
            txn.check(dev_id, _changed_tags([], tags))
            return None, tags if isinstance(body, list) else tags[0], tags if isinstance(body, list) else tags[0]

        target = f"{dev_id}#{names[0]}" if len(names) == 1 else f"{dev_id} ({len(names)} tags)"
        return self._run("tag.create", target, apply, if_match=if_match)

    def update_tag(self, dev_id: str, tag: str, body: Any, *, partial: bool, if_match: str | None = None) -> Result:
        body = _require_dict(body, "tag")

        def apply(txn: _Txn):
            ch, idx = txn.device(dev_id)
            dev = ch["devices"][idx]
            pos = next((i for i, t in enumerate(dev["tags"]) if t["name"] == tag), None)
            if pos is None:
                raise NotFound(f"device {dev_id!r} has no tag {tag!r}")
            old = dev["tags"][pos]
            merged = merge_patch(old, body) if partial else {"name": tag, **body}
            new = _dump(_validate(Tag, merged))
            _check_text(new, "name", "tag name")
            _check_text(new, "address", "address")
            if new["name"] != tag:
                if any(t["name"] == new["name"] for t in dev["tags"]):
                    raise Conflict(f"device {dev_id!r} already has a tag {new['name']!r}")
                txn.rename_tag_override(dev_id, tag, new["name"])
            dev["tags"][pos] = new
            txn.check(dev_id, _changed_tags([old], [new]))
            return old, new, new

        return self._run("tag.update", f"{dev_id}#{tag}", apply, if_match=if_match)

    def delete_tag(self, dev_id: str, tag: str, *, if_match: str | None = None) -> Result:
        def apply(txn: _Txn):
            ch, idx = txn.device(dev_id)
            dev = ch["devices"][idx]
            pos = next((i for i, t in enumerate(dev["tags"]) if t["name"] == tag), None)
            if pos is None:
                raise NotFound(f"device {dev_id!r} has no tag {tag!r}")
            old = dev["tags"].pop(pos)
            ov = txn.drop_tag_override(dev_id, tag)
            return {"tag": old, **({"override": ov} if ov is not None else {})}, None, None

        return self._run("tag.delete", f"{dev_id}#{tag}", apply, if_match=if_match)

    # devices_map.yaml ---------------------------------------------------------------
    def put_map_defaults(self, body: Any, *, if_match: str | None = None) -> Result:
        body = _require_dict(body, "defaults")
        defaults = _unset_dump(_validate(PublishCfg, body))

        def apply(txn: _Txn):
            before = txn.dmap.get("defaults") or {}
            txn.dmap["defaults"] = defaults
            return before, defaults, defaults

        return self._run("map.defaults", "defaults", apply, if_match=if_match)

    def put_map_entry(self, dev_id: str, body: Any, *, partial: bool, if_match: str | None = None) -> Result:
        body = _require_dict(body, "map entry")

        def apply(txn: _Txn):
            if dev_id not in txn.device_ids():
                raise NotFound(f"unknown device {dev_id!r}")
            section = txn.map_devices()
            old = section.get(dev_id)
            if partial and old is None:
                raise NotFound(f"{MAP_FILE} has no entry for {dev_id!r} (use PUT to create it)")
            merged = merge_patch(old or {}, body) if partial else body
            entry = _unset_dump(_validate(DeviceMapEntry, merged))
            section[dev_id] = entry
            return old, entry, entry

        return self._run("map.update" if partial else "map.put", dev_id, apply, if_match=if_match)

    def delete_map_entry(self, dev_id: str, *, if_match: str | None = None) -> Result:
        def apply(txn: _Txn):
            section = txn.map_devices()
            if dev_id not in section:
                raise NotFound(f"{MAP_FILE} has no entry for {dev_id!r}")
            return section.pop(dev_id), None, None

        return self._run("map.delete", dev_id, apply, if_match=if_match)

    # overrides.yaml -----------------------------------------------------------------
    def put_device_override(self, dev_id: str, body: Any, *, partial: bool, if_match: str | None = None) -> Result:
        body = _require_dict(body, "override")

        def apply(txn: _Txn):
            if dev_id not in txn.device_ids():
                raise NotFound(f"unknown device {dev_id!r}")
            section = txn.ov_devices()
            old = section.get(dev_id)
            if partial and old is None:
                raise NotFound(f"{OVERRIDES_FILE} has no override for {dev_id!r} (use PUT to create it)")
            merged = merge_patch(old or {}, body) if partial else body
            ov = _unset_dump(_validate(DeviceOverride, merged))
            section[dev_id] = ov
            ch, idx = txn.device(dev_id)
            known = {t["name"] for t in ch["devices"][idx]["tags"]}
            tag_ovs = ov.get("tags") or {}
            txn.check(dev_id, {n for n, t in tag_ovs.items()
                               if n in known and t.get("enabled") is not False
                               and any(k in t for k in ("address", "dtype", "enabled"))})
            return old, ov, ov

        return self._run("override.update" if partial else "override.put", dev_id, apply, if_match=if_match)

    def delete_device_override(self, dev_id: str, *, if_match: str | None = None) -> Result:
        def apply(txn: _Txn):
            section = txn.ov_devices()
            if dev_id not in section:
                raise NotFound(f"{OVERRIDES_FILE} has no override for {dev_id!r}")
            txn.check(dev_id)
            return section.pop(dev_id), None, None

        return self._run("override.delete", dev_id, apply, if_match=if_match)

    def put_channel_override(self, name: str, body: Any, *, if_match: str | None = None) -> Result:
        body = _require_dict(body, "channel override")
        unknown = sorted(set(body) - {"enabled"})
        if unknown or not isinstance(body.get("enabled"), bool):
            errs = [_err((k,), "only 'enabled' can be overridden per channel", "extra_forbidden") for k in unknown]
            if not isinstance(body.get("enabled"), bool):
                errs.append(_err(("enabled",), "enabled must be true or false", "bool_type"))
            raise Invalid("invalid channel override", errs)

        def apply(txn: _Txn):
            if not txn.has_channel(name):
                raise NotFound(f"unknown channel {name!r}")
            section = txn.ov_channels()
            old = section.get(name)
            section[name] = {"enabled": body["enabled"]}
            return old, section[name], section[name]

        return self._run("override.channel", name, apply, if_match=if_match)

    def delete_channel_override(self, name: str, *, if_match: str | None = None) -> Result:
        def apply(txn: _Txn):
            section = txn.ov_channels()
            if name not in section:
                raise NotFound(f"{OVERRIDES_FILE} has no override for channel {name!r}")
            return section.pop(name), None, None

        return self._run("override.channel_delete", name, apply, if_match=if_match)

    # import -------------------------------------------------------------------------
    def import_config(self, raw: bytes, *, dry_run: bool = True, filename: str | None = None,
                      if_match: str | None = None) -> dict[str, Any]:
        """Import a KEPServer `project.json` or an opencap export. Returns the dry-run report
        (always) and, unless `dry_run`, the `Result` of writing it under `"result"`."""
        if len(raw) > MAX_IMPORT_BYTES:
            raise Invalid("upload too large", [_err((), f"the upload is larger than {MAX_IMPORT_BYTES} bytes")])
        try:
            doc = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise Invalid("not JSON", [_err((), f"the upload is not JSON: {exc}", "json_invalid")]) from None
        if isinstance(doc, dict) and isinstance(doc.get("project"), dict):
            return self._import_kep(raw, filename, dry_run=dry_run, if_match=if_match)
        if isinstance(doc, dict) and doc.get("format") == EXPORT_FORMAT:
            return self._import_export(doc, dry_run=dry_run, if_match=if_match)
        raise Invalid("unknown file", [_err((), "expected a KEPServer project.json (top-level 'project') "
                                                f"or an opencap export (format '{EXPORT_FORMAT}')")])

    def _import_kep(self, raw: bytes, filename: str | None, *, dry_run: bool, if_match: str | None) -> dict[str, Any]:
        state = self._read()
        try:
            poller = PollerConfig.model_validate(yamledit.parse(state.texts[POLLER_FILE]))
            defaults = poller.defaults
        except Exception:  # noqa: BLE001 - a broken poller.yaml fails later in validation
            defaults = None
        with tempfile.TemporaryDirectory(prefix="opencap-import-") as tmp:
            path = Path(tmp) / _safe_filename(filename)
            path.write_bytes(raw)
            try:
                result = import_kep(path, defaults=defaults)
            except Exception as exc:  # noqa: BLE001 - malformed export
                raise Invalid("cannot import this project", [_err((), f"KEPServer import failed: {exc}")]) from None
        new_devices = _dump(result.devices_file)
        stub_ids: list[str] = []

        def apply(txn: _Txn):
            before = txn.devices
            txn.devices = new_devices
            section = txn.map_devices()
            for ch in result.devices_file.channels:
                single = len(ch.devices) == 1
                for dev in ch.devices:
                    if dev.id not in section:
                        section[dev.id] = {"sub_device_id": stub_sub_device_id(ch.name, dev.name, single), "stub": True}
                        stub_ids.append(dev.id)
            if state.texts[OVERRIDES_FILE] is None:
                txn.raw_texts[OVERRIDES_FILE] = OVERRIDES_TEMPLATE
            txn.check_devices.update(d.id for c in result.devices_file.channels for d in c.devices)
            return {"generated_from": before.get("generated_from")}, {"generated_from": new_devices.get("generated_from")}, None

        res = self._run("config.import_kep", _safe_filename(filename), apply, if_match=if_match, dry_run=dry_run)
        report = {
            "kind": "kepserver",
            "dry_run": dry_run,
            "revision": state.revision,
            "devices_json": devices_file_diff(state.devices or {"channels": []}, new_devices),
            "devices_map": {"stubs_added": stub_ids, "created": state.texts[MAP_FILE] is None},
            "overrides": {"created": state.texts[OVERRIDES_FILE] is None},
            "files": res.files,
            "warnings": res.warnings,
            "import_summary": result.summary,
            "problems": result.problems,
        }
        if not dry_run:
            res.data = {"devices_json": report["devices_json"]["summary"], "stubs_added": stub_ids,
                        "problems": len(result.problems)}
            report["result"] = res
            report["revision"] = res.revision
        return report

    def _import_export(self, doc: dict[str, Any], *, dry_run: bool, if_match: str | None) -> dict[str, Any]:
        files = doc.get("files")
        if not isinstance(files, dict) or not files:
            raise Invalid("no files", [_err(("files",), "an opencap export needs a 'files' object")])
        bad = [n for n in files if n not in CONFIG_FILES]
        bad += [n for n, t in files.items() if n in CONFIG_FILES and not isinstance(t, str)]
        if bad:
            raise Invalid("unknown files", [_err(("files", n), f"not a config file or not text; expected {CONFIG_FILES}")
                                            for n in bad])
        state = self._read()

        def apply(txn: _Txn):
            txn.raw_texts.update(files)
            txn.check_devices.update(txn.device_ids())
            if DEVICES_FILE in files:
                try:
                    imported = DevicesFile.model_validate(json.loads(files[DEVICES_FILE]))
                    txn.check_devices.update(d.id for c in imported.channels for d in c.devices)
                except (ValueError, ValidationError):
                    pass                                   # the staged load reports it
            return None, None, None

        old_state_ok = state.devices_error is None and not state.yaml_errors
        if old_state_ok:
            res = self._run("config.import", "export", apply, if_match=if_match, dry_run=dry_run)
        else:
            res = self._run_replace(files, if_match=if_match, dry_run=dry_run)
        report: dict[str, Any] = {"kind": "opencap-export", "dry_run": dry_run, "revision": state.revision,
                                  "files": res.files, "warnings": res.warnings, "text": {}}
        if DEVICES_FILE in files:
            try:
                new_devices = _dump(DevicesFile.model_validate(json.loads(files[DEVICES_FILE])))
                report["devices_json"] = devices_file_diff(state.devices or {"channels": []}, new_devices)
            except (ValueError, ValidationError):
                pass
        for name in (POLLER_FILE, MAP_FILE, OVERRIDES_FILE):
            if name in files:
                report["text"][name] = text_diff(state.texts[name], files[name], name)
        if POLLER_FILE in files:
            try:
                old_p = PollerConfig.model_validate(yamledit.parse(state.texts[POLLER_FILE]))
                new_p = PollerConfig.model_validate(yamledit.parse(files[POLLER_FILE]))
                restart = [s for s in _RESTART_SECTIONS if getattr(old_p, s) != getattr(new_p, s)]
                if restart:
                    report["warnings"].insert(0, f"{POLLER_FILE}: changes to {', '.join(restart)} take effect "
                                                 "after a restart, not on reload")
            except Exception:  # noqa: BLE001
                pass
        if not dry_run:
            report["result"] = res
            report["revision"] = res.revision
        return report

    def _run_replace(self, files: dict[str, str], *, if_match: str | None, dry_run: bool) -> Result:
        """Replace whole files when the current ones cannot even be parsed (restore a backup)."""
        with self.lock:
            state = self._read()
            self._check_if_match(if_match, state)
            texts = {**state.texts, **files}
            loaded = self._load_texts(texts)
            changed = [n for n in CONFIG_FILES if texts[n] != state.texts[n]]
            if not dry_run:
                for name in changed:
                    atomic_write(self.dir / name, texts[name])
            rev = self.revision() if not dry_run else state.revision
            return Result(action="config.import", target="export", revision=rev, changes=[], truncated=0,
                          warnings=list(loaded.warnings), files=changed)
