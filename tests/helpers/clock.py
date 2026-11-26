"""The Python engine's cycle-time clock, for tests that check measured durations.

`ChannelWorker` measures poll cycles with `time.monotonic()`. With Python 3.12 on Windows that
clock ticks every ~15.6 ms, so a cycle of a few ms against a local simulator measures 0 ms (or
one tick). Tests that check a measured cycle time accept that only when this flag is set.
#199 moves the measurement to `time.perf_counter()` and removes this module and its uses.
"""

from __future__ import annotations

import time

COARSE_CYCLE_CLOCK = time.get_clock_info("monotonic").resolution > 1e-3
