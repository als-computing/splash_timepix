"""
Progress reporting with adaptive ETA for the tpx3 sweep optimizer.

Renders an in-place progress bar on TTY stderr; falls back to plain
newline-per-update output when stderr is not a terminal (e.g. piped to a file).
"""

from __future__ import annotations

import sys
import time
from typing import Optional


def _fmt_seconds(seconds: float) -> str:
    """Format a duration as Xh Ym Zs, Ym Zs, or Zs."""
    if seconds < 0:
        return "0s"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


class ProgressReporter:
    """
    Tracks and prints progress for a fixed-length sequence of tpx3dump runs.

    Usage::

        reporter = ProgressReporter(total=9)
        reporter.start()
        for combo in combos:
            reporter.begin_run(combo_label)
            result = run_one(...)
            reporter.finish_run(result.wall_seconds, result.status)
        reporter.done()
    """

    BAR_WIDTH = 30

    def __init__(self, total: int) -> None:
        self.total = total
        self._completed = 0
        self._skipped = 0
        self._failed = 0
        self._cumulative_wall: float = 0.0
        self._sweep_start: float = 0.0
        self._current_label: str = ""
        self._current_start: float = 0.0
        self._is_tty: bool = sys.stderr.isatty()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Call once before the first run."""
        self._sweep_start = time.monotonic()
        self._print_line(f"Starting sweep of {self.total} combination(s)...")

    def begin_run(self, label: str) -> None:
        """Call just before launching tpx3dump for one combo."""
        self._current_label = label
        self._current_start = time.monotonic()
        idx = self._completed + self._skipped + self._failed + 1
        self._render(idx, label, eta_str="estimating...")

    def finish_run(self, wall_seconds: float, status: str) -> None:
        """Call after a run completes with its wall time and status string."""
        if status == "ok":
            self._cumulative_wall += wall_seconds
            self._completed += 1
        elif status == "skipped":
            self._skipped += 1
        else:
            self._failed += 1

        eta_str = self._compute_eta()
        done_so_far = self._completed + self._skipped + self._failed
        status_tag = {"ok": "OK", "skipped": "SKIP", "failed": "FAIL"}.get(status, status.upper())

        summary = (
            f"[{status_tag}] {self._current_label}  "
            f"wall={_fmt_seconds(wall_seconds)}  "
            f"ETA remaining: {eta_str}"
        )
        # Clear the bar line, then print the completed-run summary
        if self._is_tty:
            sys.stderr.write("\r" + " " * 120 + "\r")
        self._print_line(summary)

        # Render updated bar for next run (if any remaining)
        if done_so_far < self.total:
            self._render(done_so_far + 1, "", eta_str=eta_str)

    def done(self) -> None:
        """Call after the last run; clears the bar and prints total elapsed."""
        if self._is_tty:
            sys.stderr.write("\r" + " " * 120 + "\r")
        total_wall = time.monotonic() - self._sweep_start
        self._print_line(
            f"Sweep complete — {self._completed} ok, {self._skipped} skipped, "
            f"{self._failed} failed  |  total elapsed: {_fmt_seconds(total_wall)}"
        )
        sys.stderr.flush()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_eta(self) -> str:
        """Return ETA string based on average of completed runs so far."""
        if self._completed == 0:
            return "estimating..."
        avg = self._cumulative_wall / self._completed
        remaining = self.total - (self._completed + self._skipped + self._failed)
        if remaining <= 0:
            return "done"
        return _fmt_seconds(avg * remaining)

    def _render(self, next_idx: int, label: str, eta_str: str) -> None:
        """Render the progress bar line (in-place on TTY, new line otherwise)."""
        done = self._completed + self._skipped + self._failed
        pct = done / self.total if self.total else 1.0
        filled = int(self.BAR_WIDTH * pct)
        bar = "█" * filled + "░" * (self.BAR_WIDTH - filled)
        elapsed = _fmt_seconds(time.monotonic() - self._sweep_start)

        combo_hint = f" | next: {label}" if label else ""
        line = (
            f"  [{done}/{self.total}] [{bar}] {int(pct*100):3d}%"
            f"  elapsed: {elapsed}  ETA: {eta_str}{combo_hint}"
        )

        if self._is_tty:
            sys.stderr.write("\r" + line)
            sys.stderr.flush()
        else:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()

    def _print_line(self, text: str) -> None:
        """Print a plain line to stderr (preceded by newline on TTY to avoid overlap)."""
        sys.stderr.write(text + "\n")
        sys.stderr.flush()
