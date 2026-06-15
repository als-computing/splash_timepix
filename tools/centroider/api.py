"""
Programmatic API for the tpx3 sweep optimizer.
=============================================

An argv-free, print-free entry point that mirrors ``sweep.main`` but is meant
to be driven in-process (e.g. from a GUI worker thread) rather than the CLI.

It reuses the orchestration helpers from :mod:`sweep`, the single-run wrapper
from :mod:`runner`, and the histogram readers from :mod:`histogramify`, and
reports progress through an optional ``progress_callback`` instead of the TTY
:class:`progress.ProgressReporter` (which stays as-is for CLI use).

Typical usage::

    from api import run_sweep, ProgressEvent

    def on_progress(event: ProgressEvent) -> None:
        print(f"[{event.index}/{event.total}] {event.label} {event.status or ''}")

    result = run_sweep(
        input_file="sample.tpx3",
        output_parent="h5s",
        eps_t_list="20ns,100ns,500ns",
        eps_s_list="1,2,3",
        progress_callback=on_progress,
    )

Nothing here writes to stdout/stderr, raises ``SystemExit``, or parses argv, so
it is safe to call from a long-lived process.
"""

from __future__ import annotations

import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, List, Optional, Sequence, Tuple, Union

import numpy as np

# ---------------------------------------------------------------------------
# Make the sibling modules importable when this file is loaded directly
# (without installing anything), mirroring sweep.py's own path shim.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent))

import sweep as _sweep  # noqa: E402
from histogramify import histogramify_clusters, histogramify_pixelhits, label_for, load_summary  # noqa: E402
from runner import Combo, RunResult, run_one  # noqa: E402

# ---------------------------------------------------------------------------
# Validation (raises ValueError instead of calling sys.exit, so callers in a
# long-lived process can handle bad input gracefully).
# ---------------------------------------------------------------------------

_VALID_UNIT_RE = re.compile(r"^[0-9]+(\.[0-9]+)?ns$")

EpsT = Union[str, Sequence[str]]
EpsS = Union[str, Sequence[Union[str, int]]]


def _normalize_eps_t(value: EpsT) -> List[str]:
    """Accept a comma-separated string or a sequence; return validated tokens.

    Only nanosecond values are accepted (e.g. ``20ns``, ``100ns``). Larger
    units (ms, s, …) would make each tpx3dump run impractically slow, so they
    are rejected explicitly.
    """
    if isinstance(value, str):
        tokens = [t.strip() for t in value.split(",") if t.strip()]
    else:
        tokens = [str(t).strip() for t in value if str(t).strip()]
    if not tokens:
        raise ValueError("eps-t must not be empty")
    bad = [t for t in tokens if not _VALID_UNIT_RE.match(t)]
    if bad:
        raise ValueError(
            f"Invalid eps-t value(s): {bad}. "
            "Only nanosecond values are accepted, e.g. 20ns, 100ns, 500ns"
        )
    return tokens


def _normalize_eps_s(value: EpsS) -> List[int]:
    """Accept a comma-separated string or a sequence; return validated positive ints."""
    if isinstance(value, str):
        tokens = [t.strip() for t in value.split(",") if t.strip()]
    else:
        tokens = [str(t).strip() for t in value if str(t).strip()]
    if not tokens:
        raise ValueError("eps-s must not be empty")
    result: List[int] = []
    bad: List[str] = []
    for t in tokens:
        try:
            v = int(t)
            if v < 1:
                raise ValueError
            result.append(v)
        except ValueError:
            bad.append(t)
    if bad:
        raise ValueError(f"Invalid eps-s value(s): {bad} (must be positive integers, e.g. 1,2,3)")
    return result


# ---------------------------------------------------------------------------
# Result / progress payloads
# ---------------------------------------------------------------------------


@dataclass
class ProgressEvent:
    """One progress update emitted by :func:`run_sweep`.

    A ``begin`` event has ``status is None``; a ``finish`` event carries the
    run's ``status`` ("ok" | "failed" | "skipped"), ``wall_seconds`` and the
    produced ``h5_path`` (when available). A final event with ``phase == "done"``
    is emitted once the whole sweep finishes.
    """

    index: int  # 1-based task index across the whole sweep
    total: int
    label: str
    phase: str  # "clustered" | "baseline" | "done"
    eps_t: Optional[str] = None
    eps_s: Optional[int] = None
    status: Optional[str] = None  # None on begin; "ok"/"failed"/"skipped" on finish
    wall_seconds: float = 0.0
    eta_seconds: Optional[float] = None
    h5_path: Optional[Path] = None


@dataclass
class SweepResult:
    """Final outcome of a sweep."""

    run_dir: Path
    results: List[RunResult] = field(default_factory=list)
    baseline_h5: Optional[Path] = None


ProgressCallback = Callable[[ProgressEvent], None]


# ---------------------------------------------------------------------------
# Internal ETA tracker (in-process analogue of progress.ProgressReporter)
# ---------------------------------------------------------------------------


class _EtaTracker:
    def __init__(self, total: int) -> None:
        self.total = total
        self._completed = 0
        self._skipped = 0
        self._failed = 0
        self._cumulative_wall = 0.0

    def finish(self, wall_seconds: float, status: str) -> None:
        if status == "ok":
            self._cumulative_wall += wall_seconds
            self._completed += 1
        elif status == "skipped":
            self._skipped += 1
        elif status == "cancelled":
            pass  # Don't count cancelled runs against ETA
        else:
            self._failed += 1

    def eta_seconds(self) -> Optional[float]:
        if self._completed == 0:
            return None
        avg = self._cumulative_wall / self._completed
        remaining = self.total - (self._completed + self._skipped + self._failed)
        if remaining <= 0:
            return 0.0
        return avg * remaining


# ---------------------------------------------------------------------------
# Main programmatic entry point
# ---------------------------------------------------------------------------


def run_sweep(
    input_file: Union[str, Path],
    output_parent: Union[str, Path],
    eps_t_list: EpsT,
    eps_s_list: EpsS,
    tpx3dump: Optional[Union[str, Path]] = None,
    progress_callback: Optional[ProgressCallback] = None,
    run_baseline: bool = True,
    skip_existing: bool = True,
    keep_going: bool = True,
    keep_pixel_data: bool = False,
    log_level: str = "warn",
    cancel_event: Optional[threading.Event] = None,
) -> SweepResult:
    """Run tpx3dump over every (eps-t, eps-s) combo, reporting progress in-process.

    Parameters mirror the CLI in :mod:`sweep`. ``eps_t_list`` / ``eps_s_list``
    accept either a comma-separated string (``"20ns,100ns"``) or a sequence.
    ``progress_callback`` is invoked on every begin/finish and once on
    completion. Returns a :class:`SweepResult`.

    Raises ``ValueError`` / ``FileNotFoundError`` on bad inputs (never calls
    ``sys.exit``).
    """
    input_file = Path(input_file)
    output_parent = Path(output_parent)
    tpx3dump = Path(tpx3dump) if tpx3dump else Path(_sweep._DEFAULT_TPX3DUMP)

    if not input_file.exists():
        raise FileNotFoundError(f"Input file not found: {input_file}")
    if not tpx3dump.exists():
        raise FileNotFoundError(
            f"tpx3dump not found at: {tpx3dump}. Set the tpx3dump argument or the TPX3DUMP env var."
        )

    eps_t_values = _normalize_eps_t(eps_t_list)
    eps_s_values = _normalize_eps_s(eps_s_list)

    run_dir = _sweep._run_dir_for(output_parent, input_file)
    combos = _sweep._build_combos(input_file, run_dir, eps_t_values, eps_s_values)
    baseline_h5 = run_dir / f"{input_file.stem}_PixelHits.h5"

    clustered_extra = [] if keep_pixel_data else ["--discard-pixel-data"]
    baseline_extra = ["--disable-clustering"]

    n_baseline = 1 if run_baseline else 0
    total = len(combos) + n_baseline

    run_dir.mkdir(parents=True, exist_ok=True)

    # Reproducibility metadata (best-effort; never fatal for the GUI use case).
    try:
        luna_version = _sweep._get_luna_version(tpx3dump)
        meta_args = SimpleNamespace(
            log_level=log_level,
            keep_pixel_data=keep_pixel_data,
            no_baseline=not run_baseline,
            histogram=False,
            skip_existing=skip_existing,
            keep_going=keep_going,
        )
        _sweep._write_meta(
            run_dir=run_dir,
            input_file=input_file,
            output_parent=output_parent,
            tpx3dump=tpx3dump,
            luna_version=luna_version,
            eps_t_list=eps_t_values,
            eps_s_list=eps_s_values,
            extra_args=[],
            clustered_extra=clustered_extra,
            baseline_extra=baseline_extra,
            args=meta_args,
        )
    except Exception:  # noqa: BLE001 - metadata is non-essential
        pass

    tracker = _EtaTracker(total)
    results: List[RunResult] = []

    def _emit(event: ProgressEvent) -> None:
        if progress_callback is not None:
            progress_callback(event)

    def _begin(index: int, label: str, phase: str, eps_t, eps_s) -> None:
        _emit(
            ProgressEvent(
                index=index,
                total=total,
                label=label,
                phase=phase,
                eps_t=eps_t,
                eps_s=eps_s,
                status=None,
                eta_seconds=tracker.eta_seconds(),
            )
        )

    def _finish(index, label, phase, eps_t, eps_s, status, wall, h5_path) -> None:
        tracker.finish(wall, status)
        _emit(
            ProgressEvent(
                index=index,
                total=total,
                label=label,
                phase=phase,
                eps_t=eps_t,
                eps_s=eps_s,
                status=status,
                wall_seconds=wall,
                eta_seconds=tracker.eta_seconds(),
                h5_path=h5_path,
            )
        )

    index = 0

    def _is_cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    # ---- Phase 1: clustered conversions ----
    for combo in combos:
        if _is_cancelled():
            _sweep._write_summary(results, run_dir)
            _emit(ProgressEvent(index=index, total=total, label="cancelled", phase="done", status="cancelled"))
            return SweepResult(run_dir=run_dir, results=results, baseline_h5=None)

        index += 1
        label = f"s={combo.eps_s}&t={combo.eps_t}"
        _begin(index, label, "clustered", combo.eps_t, combo.eps_s)

        if skip_existing and combo.output_file.exists():
            results.append(RunResult(combo=combo, status="skipped"))
            _finish(index, label, "clustered", combo.eps_t, combo.eps_s, "skipped", 0.0, combo.output_file)
            continue

        result = run_one(
            tpx3dump=tpx3dump,
            combo=combo,
            input_file=input_file,
            log_level=log_level,
            extra_args=clustered_extra,
            cancel_event=cancel_event,
        )
        results.append(result)
        produced = combo.output_file if result.status in ("ok", "skipped") else None
        _finish(index, label, "clustered", combo.eps_t, combo.eps_s, result.status, result.wall_seconds, produced)

        if result.status == "cancelled":
            _sweep._write_summary(results, run_dir)
            _emit(ProgressEvent(index=index, total=total, label="cancelled", phase="done", status="cancelled"))
            return SweepResult(run_dir=run_dir, results=results, baseline_h5=None)

        if result.status == "failed" and not keep_going:
            _sweep._write_summary(results, run_dir)
            _emit(ProgressEvent(index=index, total=total, label="aborted", phase="done", status="failed"))
            return SweepResult(run_dir=run_dir, results=results, baseline_h5=None)

    # ---- Phase 2: PixelHits baseline ----
    if run_baseline:
        if _is_cancelled():
            _sweep._write_summary(results, run_dir)
            _emit(ProgressEvent(index=index, total=total, label="cancelled", phase="done", status="cancelled"))
            return SweepResult(run_dir=run_dir, results=results, baseline_h5=None)

        index += 1
        baseline_combo = Combo(eps_t=None, eps_s=None, output_file=baseline_h5)
        label = "PixelHits baseline"
        _begin(index, label, "baseline", None, None)

        if skip_existing and baseline_h5.exists():
            results.append(RunResult(combo=baseline_combo, status="skipped"))
            _finish(index, label, "baseline", None, None, "skipped", 0.0, baseline_h5)
        else:
            result = run_one(
                tpx3dump=tpx3dump,
                combo=baseline_combo,
                input_file=input_file,
                log_level=log_level,
                extra_args=baseline_extra,
                cancel_event=cancel_event,
            )
            results.append(result)
            produced = baseline_h5 if result.status in ("ok", "skipped") else None
            _finish(index, label, "baseline", None, None, result.status, result.wall_seconds, produced)

            if result.status == "cancelled":
                _sweep._write_summary(results, run_dir)
                _emit(ProgressEvent(index=index, total=total, label="cancelled", phase="done", status="cancelled"))
                return SweepResult(run_dir=run_dir, results=results, baseline_h5=None)

    _sweep._write_summary(results, run_dir)

    produced_baseline = baseline_h5 if (run_baseline and baseline_h5.exists()) else None
    _emit(ProgressEvent(index=total, total=total, label="done", phase="done", status="ok"))

    return SweepResult(run_dir=run_dir, results=results, baseline_h5=produced_baseline)


# ---------------------------------------------------------------------------
# Plot data loading
# ---------------------------------------------------------------------------


def load_histogram(
    h5_path: Union[str, Path],
    summary: Optional[List[dict]] = None,
) -> Tuple[np.ndarray, np.ndarray, str]:
    """Read an x-histogram straight from a sweep ``.h5`` file.

    Auto-detects baseline vs clustered files by the ``_PixelHits`` suffix and
    returns ``(xs, counts, label)`` where *label* is the legend/column string
    (``"PixelHits"`` or ``"s=<eps_s>&t=<eps_t>"``).
    """
    h5_path = Path(h5_path)
    if h5_path.stem.endswith("_PixelHits"):
        xs, counts = histogramify_pixelhits(h5_path)
    else:
        xs, counts = histogramify_clusters(h5_path)
    label = label_for(h5_path, summary)
    return xs, counts, label


def load_run_summary(run_dir: Union[str, Path]) -> Optional[List[dict]]:
    """Load the ``summary.json`` written into *run_dir*, if present."""
    return load_summary(Path(run_dir))
