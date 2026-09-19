# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Stress the journal append lock: N threads, one bullet each, count what landed.

The repro for #319. Same shape as
``tests/test_hooks.py::test_concurrent_appends_serialize`` with the thread and
trial counts turned up, a barrier so every writer hits the lock at once, and
per-trial wall time recorded — a trial over ~1 s means a writer fell into
``msvcrt.locking(LK_LOCK)``'s one-second retry sleep.

    python scripts/stress_append_lock.py [threads] [trials]

Exits 1 if any append was lost. Only meaningful on Windows: POSIX ``flock``
queues fairly in the kernel and never drops a line.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from omind import hooks

_NOW = datetime(2026, 9, 19, 14, 32)


def _trial(threads_n: int) -> tuple[int, float]:
    """Run one burst; return ``(lost_lines, seconds)``."""
    # Windows can hold the journal open a beat past the last close.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        omi = Path(tmp)
        barrier = threading.Barrier(threads_n)

        def worker(i: int) -> None:
            barrier.wait()
            bullet = f"- 14:32 [session t{i:04d}] PostToolUse Bash -> c{i} (ok)"
            hooks.append_entry(omi, bullet, _NOW)

        workers = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
        started = time.monotonic()
        for thread in workers:
            thread.start()
        for thread in workers:
            thread.join()
        elapsed = time.monotonic() - started
        text = next((omi / "Journal").glob("*.md")).read_text(encoding="utf-8")
        return threads_n - len(hooks.action_bullets(text)), elapsed


def main() -> int:
    threads_n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    trials = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    results = [_trial(threads_n) for _ in range(trials)]
    lost = sum(n for n, _ in results)
    print(
        f"threads={threads_n} trials={trials} | "
        f"trials_with_lost_appends={sum(1 for n, _ in results if n)} lost_lines={lost} | "
        f"trials_over_0.9s={sum(1 for _, s in results if s > 0.9)} "
        f"worst_trial={max(s for _, s in results):.2f}s"
    )
    return 1 if lost else 0


if __name__ == "__main__":
    sys.exit(main())
