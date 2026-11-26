"""`opencap config migrate`: write a project v2 (`project.json` + `node.yaml`) from a v1 layout (#37,
ADR-0013).

The v1 layering (`overrides.yaml`, `devices_map.yaml`, `serial_ports`) is applied once, here, and the
result is written as `project.json`; `poller.yaml` becomes `node.yaml` unchanged (comments kept). The
load-time finalize steps (default topics, unmapped serial devices, warnings) stay in the loader for both
layouts, so a topic left to its default still follows `mqtt.topic_prefix`.

Before anything is written, the v2 files are loaded from a scratch directory and must give `Device`,
`Channel` and node models equal to the v1 load; otherwise nothing is written. Then `node.yaml` and
`project.json` are written atomically (project last: until it exists the directory still loads as v1),
and the v1 files are moved, not deleted, to `config/v1-backup/` so they stop producing "ignored" warnings.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from opencap.config.kep_import import devices_json_text
from opencap.config.loader import (
    DEVICES_FILE,
    NODE_FILE,
    POLLER_FILE,
    PROJECT_FILE,
    V1_FILES,
    LoadedConfig,
    _load_devices,
    is_v2,
    load_config,
    load_v1,
)
from opencap.config.models import ProjectV2

BACKUP_DIR = "v1-backup"
NODE_HEADER = ("# node.yaml (project v2): this node's settings, the fields poller.yaml had.\n"
               "# Written by `opencap config migrate`; see README \"Configuration reference\".\n")


class MigrationError(Exception):
    """The directory cannot be migrated (already v2, v1 does not load, results differ)."""


@dataclass
class Migration:
    config_dir: Path
    project: ProjectV2
    project_text: str
    node_text: str
    v1: LoadedConfig                    # what the v1 layout loads to now
    v2: LoadedConfig                    # what the written v2 files load to (equal channels and node)
    moved: list[str] = field(default_factory=list)

    @property
    def device_count(self) -> int:
        return sum(len(c.devices) for c in self.project.channels)

    def summary_lines(self) -> list[str]:
        tags = sum(len(d.tags) for c in self.project.channels for d in c.devices)
        lines = [f"v1 -> project v2: {len(self.project.channels)} channels, {self.device_count} devices, {tags} tags; "
                 "the v2 files load to the same devices and node settings"]
        lines += [f"  v1 warning: {w}" for w in self.v1.warnings]
        lines += [f"  v2 warning: {w}" for w in self.v2.warnings]
        return lines


def _compare(v1: LoadedConfig, v2: LoadedConfig) -> list[str]:
    """Differences that would make the migration change what runs; [] when equal."""
    problems: list[str] = []
    if v1.poller.model_dump() != v2.poller.model_dump():
        problems.append("node settings differ from poller.yaml")
    a = [(c.name, c.enabled) for c in v1.channels]
    b = [(c.name, c.enabled) for c in v2.channels]
    if a != b:
        problems.append(f"channels differ: {a} vs {b}")
    old = {d.id: d for c in v1.channels for d in c.devices}
    new = {d.id: d for c in v2.channels for d in c.devices}
    if list(old) != list(new):
        problems.append(f"device ids differ: {sorted(set(old) ^ set(new))}")
    problems += [f"device {dev_id!r} differs" for dev_id in old if dev_id in new and old[dev_id] != new[dev_id]]
    if [e.model_dump() for e in v1.excluded] != [e.model_dump() for e in v2.excluded]:
        problems.append("excluded devices differ")
    return problems


def plan_migration(config_dir: Path) -> Migration:
    """Everything `apply_migration` would write, verified; writes nothing. Raises MigrationError."""
    config_dir = Path(config_dir)
    if is_v2(config_dir):
        raise MigrationError(f"{config_dir / PROJECT_FILE} exists: already a project v2")
    if not (config_dir / DEVICES_FILE).exists():
        raise MigrationError(f"{config_dir / DEVICES_FILE} not found: nothing to migrate")
    try:
        v1 = load_config(config_dir)
        layered = load_v1(config_dir, for_project=True)
        source = _load_devices(config_dir / DEVICES_FILE)
    except (ValueError, FileNotFoundError) as exc:      # pydantic ValidationError is a ValueError
        raise MigrationError(f"the v1 config does not load: {exc}") from None
    project = ProjectV2(revision=1, generated_from=source.generated_from, generated_at=source.generated_at,
                        channels=layered.channels, excluded=layered.excluded)
    project_text = devices_json_text(project)
    poller = config_dir / POLLER_FILE
    node_text = poller.read_text(encoding="utf-8") if poller.exists() else NODE_HEADER + "{}\n"
    with tempfile.TemporaryDirectory(prefix="opencap-migrate-") as tmp:
        (Path(tmp) / PROJECT_FILE).write_text(project_text, encoding="utf-8")
        (Path(tmp) / NODE_FILE).write_text(node_text, encoding="utf-8")
        try:
            v2 = load_config(Path(tmp))
        except (ValueError, FileNotFoundError) as exc:
            raise MigrationError(f"the migrated project does not load: {exc}") from None
    problems = _compare(v1, v2)
    if problems:
        raise MigrationError("the migrated project would change what runs: " + "; ".join(problems))
    return Migration(config_dir, project, project_text, node_text, v1, v2)


def _backup_dir(config_dir: Path) -> Path:
    target = config_dir / BACKUP_DIR
    n = 1
    while target.exists():
        n += 1
        target = config_dir / f"{BACKUP_DIR}-{n}"
    return target


def apply_migration(m: Migration) -> Migration:
    """Write `node.yaml` then `project.json` (atomic), then move the v1 files to `v1-backup/`."""
    from opencap.config.store import atomic_write

    if is_v2(m.config_dir):
        raise MigrationError(f"{m.config_dir / PROJECT_FILE} appeared meanwhile: not overwritten")
    atomic_write(m.config_dir / NODE_FILE, m.node_text, backup=False)
    atomic_write(m.config_dir / PROJECT_FILE, m.project_text, backup=False)
    present = [name for name in V1_FILES if (m.config_dir / name).exists()]
    if present:
        backup = _backup_dir(m.config_dir)
        backup.mkdir()
        for name in present:
            shutil.move(str(m.config_dir / name), str(backup / name))
            m.moved.append(f"{name} -> {backup.name}/{name}")
    return m


def migrate(config_dir: Path, *, dry_run: bool = False) -> Migration:
    """`opencap config migrate [--dry-run]`."""
    m = plan_migration(config_dir)
    return m if dry_run else apply_migration(m)


__all__ = ["BACKUP_DIR", "Migration", "MigrationError", "apply_migration", "migrate", "plan_migration"]
