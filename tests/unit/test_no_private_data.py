"""Guard: no real site data may ever be committed to this public repository.

- no file from `.private/` and no generated site config is tracked;
- no RFC1918 / private IP address appears in a tracked file (use 192.0.2.x, 198.51.100.x or
  203.0.113.x, the documentation ranges, or 127.0.0.1);
- when a real KEPServer export is present locally (`.private/fixtures/project.json`), none of
  its site names appears in a tracked file.
"""

from __future__ import annotations

import ipaddress
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOC_NETS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
IP_RE = re.compile(r"(?<![\d.])(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?![\d.])")
FORBIDDEN_PATHS = (".private/", "config/devices.json", "config/devices_map.yaml",
                   "tests/fixtures/project.json", "tests/fixtures/kep2poller_output.json")
GENERIC_SITE_NAMES = {"SIMULATOR", "ALLEN_BRADLEY", "Test", "Test API", "Test_OPC1", "MQTT1"}


def tracked_files() -> list[Path]:
    if shutil.which("git") and (ROOT / ".git").exists():
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
        return [ROOT / p for p in out.decode().split("\0") if p]
    skip = {".private", ".venv", ".git", ".pytest_cache", "__pycache__", "reports", "data"}
    return [p for p in ROOT.rglob("*") if p.is_file() and not (set(p.relative_to(ROOT).parts) & skip)
            and not p.name.endswith(".egg-info") and p.suffix not in {".pyc", ".sqlite"}]


def _texts():
    for p in tracked_files():
        try:
            yield p, p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue


def test_no_private_paths_tracked():
    rels = [p.relative_to(ROOT).as_posix() for p in tracked_files()]
    bad = [r for r in rels if any(r == f or r.startswith(f) for f in FORBIDDEN_PATHS)]
    assert not bad, f"private files are tracked: {bad}"


def test_no_private_ip_addresses():
    hits = []
    for p, text in _texts():
        for ip in IP_RE.findall(text):
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if addr.is_loopback or addr.is_unspecified or any(addr in n for n in DOC_NETS):
                continue
            if addr.is_private:
                hits.append(f"{p.relative_to(ROOT)}: {ip}")
    assert not hits, "private IP addresses in tracked files (use 192.0.2.x):\n" + "\n".join(hits[:20])


def test_no_real_site_names():
    export = ROOT / ".private" / "fixtures" / "project.json"
    if not export.exists():
        pytest.skip("no private KEPServer export here; site-name check runs only where it exists")
    channels = json.loads(export.read_text(encoding="utf-8-sig"))["project"]["channels"]
    names = {c["common.ALLTYPES_NAME"] for c in channels} - GENERIC_SITE_NAMES
    names = sorted((n for n in names if len(n) >= 5), key=len, reverse=True)
    patterns = [re.compile(rf"(?<![A-Za-z0-9_]){re.escape(n)}(?![A-Za-z0-9_])") for n in names]
    hits = [f"{p.relative_to(ROOT)}: {rx.pattern}" for p, text in _texts() for rx in patterns if rx.search(text)]
    assert not hits, "real site names in tracked files:\n" + "\n".join(hits[:20])
