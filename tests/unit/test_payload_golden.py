"""The golden payload file the Rust engine is tested against is what Python produces now.

If this fails after a deliberate change to `opencap.north.payload`, regenerate the file
(`.venv/bin/python -m tests.helpers.payload_golden`) and make the Rust engine match
(`cd engine && cargo test -p opencap-north --test payload_golden`).
"""

from __future__ import annotations

from tests.helpers.payload_golden import GOLDEN_PATH, render


def test_golden_payload_file_is_current():
    assert GOLDEN_PATH.exists(), f"missing {GOLDEN_PATH}; run python -m tests.helpers.payload_golden"
    assert GOLDEN_PATH.read_text(encoding="utf-8") == render(), (
        "engine golden payload cases are stale: run `python -m tests.helpers.payload_golden`")
