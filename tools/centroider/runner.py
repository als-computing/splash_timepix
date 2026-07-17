"""
Run a single tpx3dump process invocation and capture timing + outcome.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


# Pattern emitted by tpx3dump on every successful run, e.g.:
#   [... main] [INFO]: Full tpx3dump run took 47.181461748s
_TIMING_RE = re.compile(r"Full tpx3dump run took\s+([\d.]+)s")


@dataclass
class Combo:
    """One (eps_t, eps_s) combination, or a baseline run when both are None."""

    eps_t: Optional[str]  # e.g. "100ns", "0.5s"; None for --disable-clustering baseline
    eps_s: Optional[int]  # pixels; None for --disable-clustering baseline
    output_file: Path


@dataclass
class RunResult:
    """Outcome of one tpx3dump invocation."""

    combo: Combo
    status: str  # "ok" | "failed" | "skipped"
    exit_code: int = 0
    wall_seconds: float = 0.0
    reported_seconds: Optional[float] = None  # parsed from tpx3dump log line
    output_bytes: int = 0
    stdout: str = ""
    stderr: str = ""
    error_message: str = ""
    command: List[str] = field(default_factory=list)


def build_command(
    tpx3dump: Path,
    combo: Combo,
    input_file: Path,
    log_level: str = "info",
    extra_args: Optional[List[str]] = None,
) -> List[str]:
    """Return the full tpx3dump argv list for a single combo."""
    cmd = [
        str(tpx3dump),
        "process",
        "-i", str(input_file),
        "-o", str(combo.output_file),
        "-g", log_level,
    ]
    if combo.eps_t is not None:
        cmd += ["--eps-t", combo.eps_t]
    if combo.eps_s is not None:
        cmd += ["--eps-s", str(combo.eps_s)]
    if extra_args:
        cmd.extend(extra_args)
    return cmd


def _kill_proc_group(proc: subprocess.Popen) -> None:
    """Terminate the process group (SIGTERM then SIGKILL). Falls back to plain
    terminate/kill when the process group cannot be resolved (e.g. already dead)."""
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        # Give the process group up to 3 s to exit gracefully.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.05)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        # Process already gone, or pgid lookup failed — try direct signals.
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def run_one(
    tpx3dump: Path,
    combo: Combo,
    input_file: Path,
    log_level: str = "info",
    extra_args: Optional[List[str]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> RunResult:
    """
    Execute one tpx3dump conversion and return a RunResult.

    Stdout + stderr are captured (tpx3dump writes its log to stderr).
    The wall-clock time is measured around the subprocess; the tpx3dump-
    reported time (from 'Full tpx3dump run took Xs') is also parsed when
    available, as it excludes Python / subprocess overhead.

    If *cancel_event* is set while the process is running the process group
    is killed and a ``RunResult`` with ``status="cancelled"`` is returned.
    """
    cmd = build_command(tpx3dump, combo, input_file, log_level, extra_args)

    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  # own process group so we can kill it cleanly
        )
    except OSError as exc:
        return RunResult(
            combo=combo,
            status="failed",
            exit_code=-1,
            wall_seconds=time.monotonic() - t0,
            command=cmd,
            error_message=str(exc),
        )

    # Poll until the process finishes or the cancel_event fires.
    while proc.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            _kill_proc_group(proc)
            stdout, stderr = proc.communicate()
            # Remove the partially-written output file — it will be corrupted.
            try:
                if combo.output_file.exists():
                    combo.output_file.unlink()
            except OSError:
                pass
            return RunResult(
                combo=combo,
                status="cancelled",
                exit_code=proc.returncode or -1,
                wall_seconds=time.monotonic() - t0,
                command=cmd,
                stdout=stdout or "",
                stderr=stderr or "",
                error_message="Cancelled by user",
            )
        time.sleep(0.05)

    stdout, stderr = proc.communicate()
    wall = time.monotonic() - t0

    # tpx3dump writes its log lines to stderr
    combined_log = (stderr or "") + (stdout or "")
    reported = _parse_reported_time(combined_log)

    output_bytes = 0
    if combo.output_file.exists():
        output_bytes = combo.output_file.stat().st_size

    status = "ok" if proc.returncode == 0 else "failed"
    error_message = ""
    if proc.returncode != 0:
        # Surface the last few lines of stderr as the error summary
        last_lines = (stderr or "").strip().splitlines()
        error_message = "\n".join(last_lines[-5:]) if last_lines else "non-zero exit"

    return RunResult(
        combo=combo,
        status=status,
        exit_code=proc.returncode,
        wall_seconds=wall,
        reported_seconds=reported,
        output_bytes=output_bytes,
        stdout=stdout or "",
        stderr=stderr or "",
        error_message=error_message,
        command=cmd,
    )


def _parse_reported_time(log_text: str) -> Optional[float]:
    """Extract seconds from 'Full tpx3dump run took X.Xs' in captured output."""
    match = _TIMING_RE.search(log_text)
    if match:
        return float(match.group(1))
    return None
