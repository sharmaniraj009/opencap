"""The licence gate (scripts/check_licenses.py, ADR-0007) rejects what it must and accepts what it may."""
import importlib.util
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("check_licenses", ROOT / "scripts/check_licenses.py")
cl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cl)
CFG = tomllib.loads((ROOT / "licenses/python-allow.toml").read_text())


def bad(name, lic):
    return cl.evaluate([{"Name": name, "Version": "1", "License": lic}], CFG)


def test_permissive_and_choices_pass():
    for lic in ("MIT License", "BSD License", "Apache-2.0 OR BSD-3-Clause", "EPL-2.0 OR BSD-3-Clause",
                "Mozilla Public License 2.0 (MPL 2.0)", "ISC License (ISCL)", "Apache-2.0 AND BSD-2-Clause"):
        assert not bad("pkg", lic), lic


def test_copyleft_unknown_and_noncommercial_fail():
    for lic in ("GNU General Public License v3 (GPLv3)", "AGPL-3.0", "UNKNOWN", "", "MIT AND GPL-3.0",
                "Commons Clause", "LGPL-3.0-or-later"):
        assert bad("pkg", lic), lic


def test_listed_exception_is_allowed_only_for_that_package():
    assert not bad("asyncua", "LGPL-3.0-or-later")
    assert bad("other", "LGPL-3.0-or-later")
