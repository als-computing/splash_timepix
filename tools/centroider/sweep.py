"""
tpx3 Sweep Optimizer
====================

Runs tpx3dump over every (eps-t, eps-s) combination sequentially,
writing one HDF5 per combo and reporting live progress with an adaptive ETA.

All outputs land in a deterministic run subdir: <output-dir>/<input_stem>_centroided/
(no timestamp, idempotent), alongside a luna.meta file recording the luna version
and every effective parameter for reproducibility.

Phases (driven by a single ProgressReporter):
  1. Clustered conversions  — one .h5 per (eps-t, eps-s) with --discard-pixel-data
  2. PixelHits baseline     — one .h5 with --disable-clustering (skippable via --no-baseline)
  3. Histograms             — one .csv per produced .h5 (opt-in via --histogram)

Typical usage::

    python tools/centroider/sweep.py \\
        -i SampleData/tpx3/sample21MB.tpx3 \\
        -o SampleData/h5s \\
        -t 20ns,100ns,500ns \\
        -s 1,2,3

Use --example to run with built-in SampleData defaults (no arguments needed).
Use --dry-run to preview the planned commands without executing them.
Use --histogram to also generate CSV histograms after conversion.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shlex
import subprocess
import sys
import textwrap
import time
from datetime import datetime
from itertools import product
from pathlib import Path
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Adjust path so runner / progress / histogramify can be imported when running
# this script directly (without installing anything).
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent))

from histogramify import load_summary, process_file as histogramify_file  # noqa: E402
from progress import ProgressReporter, _fmt_seconds  # noqa: E402
from runner import Combo, RunResult, build_command, run_one  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_TPX3DUMP = Path(__file__).parent.parent.parent / "ASI" / "tpx3dump"
_SAMPLEDATA_ROOT = Path("/home/tpx/Desktop/tpx3LOCAL/SampleData")
_EXAMPLE_INPUT = _SAMPLEDATA_ROOT / "tpx3" / "sample21MB.tpx3"
_EXAMPLE_OUTPUT = _SAMPLEDATA_ROOT / "h5s"
_EXAMPLE_EPS_T = ["20ns", "100ns", "500ns"]
_EXAMPLE_EPS_S = [1, 2, 3]

_VALID_UNIT_RE = re.compile(r"^[0-9]+(\.[0-9]+)?(s|ms|us|ns|ps|fs)$")

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_eps_t(raw: str) -> List[str]:
    """
    Parse and validate a comma-separated list of eps-t values.
    Each token must match a number followed by a valid time unit.
    Raises SystemExit on bad input.
    """
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if not tokens:
        _die("--eps-t must not be empty")
    bad = [t for t in tokens if not _VALID_UNIT_RE.match(t)]
    if bad:
        _die(
            f"Invalid --eps-t value(s): {bad}\n"
            "  Expected format: <number><unit>, e.g. 100ns, 0.5ms, 1s\n"
            "  Valid units: s, ms, us, ns, ps, fs"
        )
    return tokens


def _validate_eps_s(raw: str) -> List[int]:
    """
    Parse and validate a comma-separated list of eps-s values.
    Each token must be a positive integer.
    Raises SystemExit on bad input.
    """
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if not tokens:
        _die("--eps-s must not be empty")
    result = []
    bad = []
    for t in tokens:
        try:
            v = int(t)
            if v < 1:
                raise ValueError
            result.append(v)
        except ValueError:
            bad.append(t)
    if bad:
        _die(f"Invalid --eps-s value(s): {bad}  (must be positive integers, e.g. 1,2,3)")
    return result


def _validate_tpx3dump(path: Path) -> Path:
    if not path.exists():
        _die(f"tpx3dump not found at: {path}\n  Set --tpx3dump or the TPX3DUMP env var.")
    if not os.access(path, os.X_OK):
        _die(f"tpx3dump is not executable: {path}")
    return path


def _die(msg: str) -> None:
    sys.stderr.write(f"ERROR: {msg}\n")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Combo / filename helpers
# ---------------------------------------------------------------------------


def _output_filename(stem: str, eps_t: str, eps_s: int) -> str:
    """
    Produce a deterministic, filesystem-safe output filename.
    E.g. stem="test", eps_t="0.01s", eps_s=2  ->  "test_t0.01s_s2.h5"
    """
    return f"{stem}_t{eps_t}_s{eps_s}.h5"


def _build_combos(
    input_file: Path,
    output_dir: Path,
    eps_t_list: List[str],
    eps_s_list: List[int],
) -> List[Combo]:
    stem = input_file.stem
    combos = []
    for eps_t, eps_s in product(eps_t_list, eps_s_list):
        fname = _output_filename(stem, eps_t, eps_s)
        combos.append(Combo(eps_t=eps_t, eps_s=eps_s, output_file=output_dir / fname))
    return combos


# ---------------------------------------------------------------------------
# Summary output
# ---------------------------------------------------------------------------


def _write_summary(results: List[RunResult], output_dir: Path) -> Tuple[Path, Path]:
    """Write summary.json and summary.csv; return their paths."""
    rows = []
    for r in results:
        rows.append(
            {
                "eps_t": r.combo.eps_t,
                "eps_s": r.combo.eps_s,
                "output_file": str(r.combo.output_file),
                "status": r.status,
                "exit_code": r.exit_code,
                "wall_seconds": round(r.wall_seconds, 3),
                "reported_seconds": (
                    round(r.reported_seconds, 3) if r.reported_seconds is not None else None
                ),
                "output_bytes": r.output_bytes,
                "command": shlex.join(r.command) if r.command else "",
                "error_message": r.error_message,
            }
        )

    json_path = output_dir / "summary.json"
    csv_path = output_dir / "summary.csv"

    with open(json_path, "w") as f:
        json.dump(rows, f, indent=2)

    if rows:
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    return json_path, csv_path


def _eps_label(combo: Combo) -> str:
    """Human-readable label for a combo's eps values."""
    if combo.eps_t is None:
        return "PixelHits baseline"
    return f"eps_t={combo.eps_t} eps_s={combo.eps_s}"


# ---------------------------------------------------------------------------
# Run directory + reproducibility metadata
# ---------------------------------------------------------------------------


def _run_dir_for(output_parent: Path, input_file: Path) -> Path:
    """
    Deterministic per-input run directory: ``<output_parent>/<input_stem>_centroided``.

    No timestamp, so re-running the same input writes to the same folder
    (idempotent) and keeps the parent directory clean.
    """
    return output_parent / f"{input_file.stem}_centroided"


def _get_luna_version(tpx3dump: Path) -> str:
    """Return the tpx3dump/luna version string (e.g. 'Tpx3Dump 0.3.2'), or 'unknown'."""
    try:
        proc = subprocess.run(
            [str(tpx3dump), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = (proc.stdout or proc.stderr).strip()
        return out.splitlines()[0].strip() if out else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _write_meta(
    run_dir: Path,
    input_file: Path,
    output_parent: Path,
    tpx3dump: Path,
    luna_version: str,
    eps_t_list: List[str],
    eps_s_list: List[int],
    extra_args: List[str],
    clustered_extra: List[str],
    baseline_extra: List[str],
    args: argparse.Namespace,
) -> Path:
    """
    Write ``luna.meta`` (JSON) into *run_dir* capturing everything needed to
    unambiguously reproduce this run: luna version, the exact command line,
    and every effective parameter / flag (not just user-supplied ones).
    """
    meta = {
        "luna_version": luna_version,
        "tpx3dump_path": str(tpx3dump),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "command_line": shlex.join(sys.argv),
        "input_file": str(input_file.resolve()),
        "output_parent": str(output_parent.resolve()),
        "run_dir": str(run_dir.resolve()),
        "parameters": {
            "eps_t": eps_t_list,
            "eps_s": eps_s_list,
            "log_level": args.log_level,
            "discard_pixel_data": not args.keep_pixel_data,
            "baseline_pixelhits": not args.no_baseline,
            "histogram": args.histogram,
            "skip_existing": args.skip_existing,
            "keep_going": args.keep_going,
            "extra_args": extra_args,
        },
        "tpx3dump_flags": {
            "clustered": clustered_extra,
            "baseline": baseline_extra,
        },
    }
    meta_path = run_dir / "luna.meta"
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    return meta_path


def _print_final_report(results: List[RunResult]) -> None:
    """Print a human-readable final summary table to stdout."""
    ok = [r for r in results if r.status == "ok"]
    skipped = [r for r in results if r.status == "skipped"]
    failed = [r for r in results if r.status == "failed"]

    total_wall = sum(r.wall_seconds for r in results)

    print("\n" + "=" * 70)
    print("  SWEEP SUMMARY")
    print("=" * 70)
    print(f"  Completed : {len(ok)}")
    print(f"  Skipped   : {len(skipped)}")
    print(f"  Failed    : {len(failed)}")
    print(f"  Total wall: {_fmt_seconds(total_wall)}")

    clustered_ok = [r for r in ok if r.combo.eps_t is not None]
    if clustered_ok:
        fastest = min(clustered_ok, key=lambda r: r.wall_seconds)
        slowest = max(clustered_ok, key=lambda r: r.wall_seconds)
        print(
            f"  Fastest   : eps_t={fastest.combo.eps_t} eps_s={fastest.combo.eps_s}"
            f"  ({_fmt_seconds(fastest.wall_seconds)})"
        )
        print(
            f"  Slowest   : eps_t={slowest.combo.eps_t} eps_s={slowest.combo.eps_s}"
            f"  ({_fmt_seconds(slowest.wall_seconds)})"
        )

    if results:
        col_t = max(len(_eps_label(r.combo)) for r in results)
        header = f"  {'label':<{col_t}}  {'status':>7}  {'wall':>8}  {'tpx3dump':>9}  output"
        print("\n" + header)
        print("  " + "-" * (len(header) - 2))
        for r in results:
            rep = f"{r.reported_seconds:.1f}s" if r.reported_seconds is not None else "  n/a  "
            size = f"{r.output_bytes / 1e9:.2f} GB" if r.output_bytes else "—"
            label = _eps_label(r.combo)
            print(
                f"  {label:<{col_t}}  {r.status:>7}"
                f"  {_fmt_seconds(r.wall_seconds):>8}  {rep:>9}  {size}"
            )

    if failed:
        print("\n  Failed runs:")
        for r in failed:
            print(f"    {_eps_label(r.combo)}: {r.error_message or 'see logs'}")

    print("=" * 70)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sweep.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent(
            """\
            tpx3 Sweep Optimizer
            --------------------
            Run tpx3dump over every (eps-t, eps-s) combination and write
            one HDF5 file per combo into --output-dir.

            Quick start (uses built-in SampleData defaults):
              python sweep.py --example
              python sweep.py --example --dry-run
              python sweep.py --example --histogram

            Full usage:
              python sweep.py -i file.tpx3 -o out/ -t 20ns,100ns,500ns -s 1,2,3
            """
        ),
    )

    # ---- input / output ----
    io_grp = parser.add_argument_group("Input / output")
    io_grp.add_argument("-i", "--input", metavar="PATH", help="Path to the .tpx3 input file")
    io_grp.add_argument(
        "-o",
        "--output-dir",
        metavar="PATH",
        help="Directory for HDF5 outputs and summary files (created if missing)",
    )

    # ---- sweep parameters ----
    sweep_grp = parser.add_argument_group("Sweep parameters")
    sweep_grp.add_argument(
        "-t",
        "--eps-t",
        metavar="LIST",
        help="Comma-separated time gaps with units, e.g. '20ns,100ns,500ns'. "
        "Valid units: s, ms, us, ns, ps, fs",
    )
    sweep_grp.add_argument(
        "-s",
        "--eps-s",
        metavar="LIST",
        help="Comma-separated pixel distances (positive ints), e.g. '1,2,3'",
    )

    # ---- converter ----
    conv_grp = parser.add_argument_group("Converter")
    conv_grp.add_argument(
        "--tpx3dump",
        metavar="PATH",
        default=os.environ.get("TPX3DUMP", str(_DEFAULT_TPX3DUMP)),
        help="Path to the tpx3dump executable (default: $TPX3DUMP or built-in path)",
    )
    conv_grp.add_argument(
        "--extra-args",
        metavar="ARGS",
        default="",
        help="Extra flags forwarded to every tpx3dump invocation",
    )
    conv_grp.add_argument(
        "--log-level",
        metavar="LEVEL",
        default="warn",
        choices=["off", "trace", "debug", "info", "warn", "error"],
        help="tpx3dump log level (default: warn — keeps console output clean)",
    )

    # ---- behaviour ----
    beh_grp = parser.add_argument_group("Behaviour")
    beh_grp.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip combos whose output .h5 already exists (resume support)",
    )
    beh_grp.add_argument(
        "--keep-going",
        action="store_true",
        help="On a failed run, log and continue instead of aborting",
    )
    beh_grp.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned commands and exit without running tpx3dump",
    )
    beh_grp.add_argument(
        "--example",
        action="store_true",
        help="Use built-in SampleData defaults (overrides -i/-o/-t/-s if not supplied)",
    )

    # ---- post-processing ----
    post_grp = parser.add_argument_group("Post-processing")
    post_grp.add_argument(
        "--histogram",
        action="store_true",
        help="Generate CSV histograms (histogramify) for every produced .h5 after conversion",
    )
    post_grp.add_argument(
        "--no-baseline",
        action="store_true",
        help="Skip the PixelHits baseline run (--disable-clustering)",
    )
    post_grp.add_argument(
        "--keep-pixel-data",
        action="store_true",
        help="Do NOT add --discard-pixel-data to clustered runs (keeps PixelHits in every .h5)",
    )

    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # ------------------------------------------------------------------
    # Apply --example defaults where CLI args are missing
    # ------------------------------------------------------------------
    if args.example:
        if not args.input:
            args.input = str(_EXAMPLE_INPUT)
        if not args.output_dir:
            args.output_dir = str(_EXAMPLE_OUTPUT)
        if not args.eps_t:
            args.eps_t = ",".join(_EXAMPLE_EPS_T)
        if not args.eps_s:
            args.eps_s = ",".join(str(s) for s in _EXAMPLE_EPS_S)

    # ------------------------------------------------------------------
    # Require -i / -o / -t / -s (unless satisfied by --example)
    # ------------------------------------------------------------------
    missing = [
        flag
        for flag, val in [
            ("-i/--input", args.input),
            ("-o/--output-dir", args.output_dir),
            ("-t/--eps-t", args.eps_t),
            ("-s/--eps-s", args.eps_s),
        ]
        if not val
    ]
    if missing:
        parser.print_usage(sys.stderr)
        _die(
            f"Missing required argument(s): {', '.join(missing)}\n"
            "  Tip: use --example to run with SampleData defaults."
        )

    # ------------------------------------------------------------------
    # Validate inputs
    # ------------------------------------------------------------------
    input_file = Path(args.input)
    if not input_file.exists():
        _die(f"Input file not found: {input_file}")
    if input_file.suffix.lower() != ".tpx3":
        sys.stderr.write(f"WARNING: input file does not have .tpx3 extension: {input_file}\n")

    # The -o/--output-dir is treated as the PARENT. All outputs go into a
    # deterministic per-input run subdir so the parent stays clean.
    output_parent = Path(args.output_dir)
    run_dir = _run_dir_for(output_parent, input_file)

    tpx3dump = _validate_tpx3dump(Path(args.tpx3dump))
    eps_t_list = _validate_eps_t(args.eps_t)
    eps_s_list = _validate_eps_s(args.eps_s)

    extra_args = shlex.split(args.extra_args) if args.extra_args.strip() else []

    # ------------------------------------------------------------------
    # Build combo lists (everything lives inside run_dir)
    # ------------------------------------------------------------------
    combos = _build_combos(input_file, run_dir, eps_t_list, eps_s_list)
    n_clustered = len(combos)

    baseline_h5 = run_dir / f"{input_file.stem}_PixelHits.h5"
    run_baseline = not args.no_baseline

    # Per-phase extra args
    clustered_extra = extra_args + ([] if args.keep_pixel_data else ["--discard-pixel-data"])
    baseline_extra = extra_args + ["--disable-clustering"]

    # Total task count spans all three phases
    n_baseline = 1 if run_baseline else 0
    n_conv = n_clustered + n_baseline
    n_hist = n_conv if args.histogram else 0
    total = n_conv + n_hist

    # ------------------------------------------------------------------
    # Print header
    # ------------------------------------------------------------------
    print("tpx3 Sweep Optimizer")
    print(f"  Input       : {input_file}")
    print(f"  Output parent: {output_parent}")
    print(f"  Run dir     : {run_dir}")
    print(f"  tpx3dump    : {tpx3dump}")
    print(f"  eps-t values: {eps_t_list}")
    print(f"  eps-s values: {eps_s_list}")
    print(f"  Combinations: {n_clustered}  +  {n_baseline} baseline  =  {n_conv} conversions")
    if args.histogram:
        print(f"  Histograms  : {n_hist} CSV files (--histogram)")
    if not args.keep_pixel_data:
        print("  --discard-pixel-data: ON for clustered runs (saves disk space)")
    if extra_args:
        print(f"  Extra args  : {extra_args}")
    if args.skip_existing:
        print("  --skip-existing is ON")
    if args.keep_going:
        print("  --keep-going is ON")
    print()

    # ------------------------------------------------------------------
    # Dry run
    # ------------------------------------------------------------------
    if args.dry_run:
        print("DRY RUN — planned commands:\n")
        print(f"  Would create run dir: {run_dir}")
        print(f"  Would write metadata: {run_dir / 'luna.meta'}\n")
        idx = 1
        for combo in combos:
            cmd = build_command(tpx3dump, combo, input_file, args.log_level, clustered_extra)
            print(f"  [{idx}/{total}] {_eps_label(combo)}  (clustered)")
            print(f"         output: {combo.output_file}")
            print(f"         cmd:    {shlex.join(cmd)}")
            print()
            idx += 1
        if run_baseline:
            baseline_combo = Combo(eps_t=None, eps_s=None, output_file=baseline_h5)
            cmd = build_command(tpx3dump, baseline_combo, input_file, args.log_level, baseline_extra)
            print(f"  [{idx}/{total}] PixelHits baseline  (--disable-clustering)")
            print(f"         output: {baseline_h5}")
            print(f"         cmd:    {shlex.join(cmd)}")
            print()
            idx += 1
        if args.histogram:
            all_h5 = [c.output_file for c in combos] + ([baseline_h5] if run_baseline else [])
            for h5_path in all_h5:
                print(f"  [{idx}/{total}] histogram: {h5_path.name}  ->  {h5_path.stem}.csv")
                idx += 1
        return 0

    # ------------------------------------------------------------------
    # Create run directory and write reproducibility metadata
    # ------------------------------------------------------------------
    run_dir.mkdir(parents=True, exist_ok=True)
    luna_version = _get_luna_version(tpx3dump)
    meta_path = _write_meta(
        run_dir=run_dir,
        input_file=input_file,
        output_parent=output_parent,
        tpx3dump=tpx3dump,
        luna_version=luna_version,
        eps_t_list=eps_t_list,
        eps_s_list=eps_s_list,
        extra_args=extra_args,
        clustered_extra=clustered_extra,
        baseline_extra=baseline_extra,
        args=args,
    )

    # ------------------------------------------------------------------
    # Phase 1 + 2: run conversions
    # ------------------------------------------------------------------
    reporter = ProgressReporter(total=total)
    reporter.start()
    results: List[RunResult] = []
    produced_h5: List[Path] = []  # track files actually produced for histogram phase

    # Phase 1 — clustered conversions (--discard-pixel-data by default)
    for combo in combos:
        label = _eps_label(combo)

        if args.skip_existing and combo.output_file.exists():
            reporter.begin_run(label)
            skipped_result = RunResult(combo=combo, status="skipped")
            reporter.finish_run(0.0, "skipped")
            results.append(skipped_result)
            produced_h5.append(combo.output_file)
            continue

        reporter.begin_run(label)
        result = run_one(
            tpx3dump=tpx3dump,
            combo=combo,
            input_file=input_file,
            log_level=args.log_level,
            extra_args=clustered_extra,
        )
        reporter.finish_run(result.wall_seconds, result.status)
        results.append(result)

        if result.status == "ok":
            produced_h5.append(combo.output_file)
        elif result.status == "failed":
            sys.stderr.write(f"  ERROR: {result.error_message}\n")
            if not args.keep_going:
                sys.stderr.write("  Aborting sweep (use --keep-going to continue on failure).\n")
                reporter.done()
                _write_summary(results, run_dir)
                return 1

    # Phase 2 — PixelHits baseline (--disable-clustering)
    if run_baseline:
        baseline_combo = Combo(eps_t=None, eps_s=None, output_file=baseline_h5)
        label = "PixelHits baseline"

        if args.skip_existing and baseline_h5.exists():
            reporter.begin_run(label)
            skipped_result = RunResult(combo=baseline_combo, status="skipped")
            reporter.finish_run(0.0, "skipped")
            results.append(skipped_result)
            produced_h5.append(baseline_h5)
        else:
            reporter.begin_run(label)
            result = run_one(
                tpx3dump=tpx3dump,
                combo=baseline_combo,
                input_file=input_file,
                log_level=args.log_level,
                extra_args=baseline_extra,
            )
            reporter.finish_run(result.wall_seconds, result.status)
            results.append(result)

            if result.status == "ok":
                produced_h5.append(baseline_h5)
            elif result.status == "failed":
                sys.stderr.write(f"  ERROR (baseline): {result.error_message}\n")
                if not args.keep_going:
                    sys.stderr.write("  Aborting (use --keep-going to continue on failure).\n")
                    reporter.done()
                    _write_summary(results, run_dir)
                    return 1

    # ------------------------------------------------------------------
    # Write partial summary.json so histogramify can read labels
    # ------------------------------------------------------------------
    _write_summary(results, run_dir)

    # ------------------------------------------------------------------
    # Phase 3 — histograms (in-process, one step per .h5)
    # ------------------------------------------------------------------
    hist_failed = 0
    if args.histogram:
        summary_data = load_summary(run_dir)
        for h5_path in produced_h5:
            label = f"histogram: {h5_path.name}"
            reporter.begin_run(label)
            t0 = time.monotonic()
            csv_path, err = histogramify_file(h5_path, run_dir, summary_data)
            wall = time.monotonic() - t0
            if csv_path is not None:
                reporter.finish_run(wall, "ok")
            else:
                reporter.finish_run(wall, "failed")
                hist_failed += 1
                sys.stderr.write(f"  WARNING: histogram failed for {h5_path.name}: {err}\n")

    reporter.done()

    # ------------------------------------------------------------------
    # Write final summary and report
    # ------------------------------------------------------------------
    json_path, csv_path = _write_summary(results, run_dir)
    _print_final_report(results)
    print("\n  Run dir written to:")
    print(f"    {run_dir}")
    print("  Files:")
    print(f"    {meta_path.name}  (luna {luna_version}, reproducibility metadata)")
    print(f"    {json_path.name}")
    print(f"    {csv_path.name}")

    failed = sum(1 for r in results if r.status == "failed")
    return 1 if (failed or hist_failed) else 0


if __name__ == "__main__":
    sys.exit(main())
