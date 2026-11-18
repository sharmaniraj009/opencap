"""plan/checklist.md is generated from the feature files (plan/README.md): it must not drift."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_plan_checklist_is_up_to_date():
    done = subprocess.run([sys.executable, str(ROOT / "plan" / "build_checklist.py"), "--check"],
                          capture_output=True, text=True, cwd=ROOT)
    assert done.returncode == 0, ("plan/checklist.md is out of date; run: python plan/build_checklist.py\n"
                                  + done.stdout + done.stderr)
