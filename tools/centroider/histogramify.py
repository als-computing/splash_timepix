"""
histogramify.py
===============

Turns sweep-generated .h5 files into per-file CSV histograms over the
cluster x-centroid (or raw pixel-x for the PixelHits baseline).

Two modes, auto-detected by filename suffix:

  *_PixelHits.h5  ->  reads PixelHits['x'], fixed bins 0..255 (256 rows always)
  all others      ->  reads Clusters['cx'], np.unique(return_counts=True)

Can be imported and called in-process by sweep.py, or used standalone::

    .venv/bin/python3.12 tools/centroider/histogramify.py
    .venv/bin/python3.12 tools/centroider/histogramify.py -i SampleData/h5s
    .venv/bin/python3.12 tools/centroider/histogramify.py -i SampleData/h5s -o SampleData/h5s
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import textwrap
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

try:
    import h5py
except ImportError:
    sys.exit(
        "ERROR: h5py is not installed.\n"
        "  Run: .venv/bin/python3.12 -m pip install h5py"
    )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SAMPLEDATA_DEFAULT = Path("/home/tpx/Desktop/tpx3LOCAL/SampleData/h5s")

# Matches sweep output names: <stem>_t<eps_t>_s<eps_s>
_SWEEP_NAME_RE = re.compile(r"_t(?P<eps_t>[^_]+)_s(?P<eps_s>\d+)$")

# ---------------------------------------------------------------------------
# Core processing functions (importable)
# ---------------------------------------------------------------------------


def histogramify_clusters(h5_path: Path) -> Tuple[np.ndarray, np.ndarray]:
    """
    Read ``Clusters['cx']`` from an HDF5 file.

    Returns ``(xs, counts)`` where *xs* are unique float cluster-x centroids
    sorted ascending and *counts* are the number of clusters at each x.
    Each cluster contributes 1; y and t dimensions are collapsed.
    """
    with h5py.File(h5_path, "r") as f:
        cx = f["Clusters"]["cx"][:]
    cx = np.asarray(cx, dtype=float)
    xs, counts = np.unique(cx, return_counts=True)
    return xs, counts.astype(np.int64)


def histogramify_pixelhits(h5_path: Path) -> Tuple[np.ndarray, np.ndarray]:
    """
    Read ``PixelHits['x']`` from an HDF5 file.

    Returns ``(xs, counts)`` where *xs* is ``np.arange(256)`` (always 256
    rows) and *counts* is the pixel-hit count at each x.  Bins with no hits
    are 0.  y and time dimensions are collapsed.
    """
    with h5py.File(h5_path, "r") as f:
        x = f["PixelHits"]["x"][:]
    x = np.asarray(x, dtype=int)
    counts = np.bincount(x, minlength=256)[:256]
    xs = np.arange(256, dtype=int)
    return xs, counts.astype(np.int64)


def label_for(h5_path: Path, summary: Optional[List[Dict]] = None) -> str:
    """
    Build the count-column header string for a given ``.h5`` file.

    - Files whose stem ends with ``_PixelHits`` return ``"PixelHits"``.
    - Clustered files return ``"s=<eps_s>&t=<eps_t>"`` — sourced from
      *summary* (a parsed ``summary.json`` list) when available, otherwise
      parsed from the filename convention ``<stem>_t<eps_t>_s<eps_s>.h5``.
    """
    stem = h5_path.stem
    if stem.endswith("_PixelHits"):
        return "PixelHits"

    # Try summary.json lookup first (canonical source of truth)
    if summary:
        for row in summary:
            row_file = Path(row.get("output_file", ""))
            if row_file.name == h5_path.name:
                eps_t = row.get("eps_t")
                eps_s = row.get("eps_s")
                if eps_t and eps_s is not None:
                    return f"s={eps_s}&t={eps_t}"

    # Fallback: parse from filename
    m = _SWEEP_NAME_RE.search(stem)
    if m:
        return f"s={m.group('eps_s')}&t={m.group('eps_t')}"

    return stem


def write_csv(out_path: Path, xs: np.ndarray, counts: np.ndarray, label: str) -> None:
    """Write a two-column CSV with header ``x,<label>``."""
    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["x", label])
        for x_val, count_val in zip(xs.tolist(), counts.tolist()):
            writer.writerow([x_val, count_val])


def process_file(
    h5_path: Path,
    output_dir: Path,
    summary: Optional[List[Dict]] = None,
) -> Tuple[Optional[Path], Optional[str]]:
    """
    Process one ``.h5`` file into a CSV histogram.

    Returns ``(csv_path, None)`` on success or ``(None, error_message)`` on
    failure (missing dataset / column, I/O error, etc.).
    """
    stem = h5_path.stem
    is_pixelhits = stem.endswith("_PixelHits")

    try:
        if is_pixelhits:
            xs, counts = histogramify_pixelhits(h5_path)
        else:
            xs, counts = histogramify_clusters(h5_path)
    except KeyError as exc:
        return None, f"missing dataset or column {exc}"
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)

    col_label = label_for(h5_path, summary)
    out_path = output_dir / (stem + ".csv")
    write_csv(out_path, xs, counts, col_label)
    return out_path, None


# ---------------------------------------------------------------------------
# Summary JSON loader (used by both standalone CLI and sweep.py)
# ---------------------------------------------------------------------------


def load_summary(input_dir: Path) -> Optional[List[Dict]]:
    """Load ``summary.json`` from *input_dir* if it exists; return parsed list or None."""
    p = input_dir / "summary.json"
    if p.exists():
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# CLI (standalone mode)
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="histogramify.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent(
            f"""\
            histogramify — generate per-file CSV histograms from sweep .h5 outputs.

            Mode is auto-detected by filename:
              *_PixelHits.h5  ->  PixelHits['x'], fixed bins 0..255 (column: PixelHits)
              all others      ->  Clusters['cx'], unique float centroids (column: s=?&t=?)

            Example:
              .venv/bin/python3.12 tools/centroider/histogramify.py
              .venv/bin/python3.12 tools/centroider/histogramify.py \\
                  -i {_SAMPLEDATA_DEFAULT}
            """
        ),
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    parser.add_argument(
        "-i",
        "--input-dir",
        metavar="PATH",
        default=str(_SAMPLEDATA_DEFAULT),
        help=f"Directory containing .h5 files (default: {_SAMPLEDATA_DEFAULT})",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        metavar="PATH",
        default=None,
        help="Directory for CSV outputs (default: same as --input-dir)",
    )
    parser.add_argument(
        "--glob",
        metavar="PATTERN",
        default="*.h5",
        help="Glob pattern for input files (default: *.h5)",
    )

    args = parser.parse_args(argv)

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir) if args.output_dir else input_dir

    if not input_dir.exists():
        sys.stderr.write(f"ERROR: input-dir does not exist: {input_dir}\n")
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)

    h5_files = sorted(input_dir.glob(args.glob))
    h5_files = [f for f in h5_files if not f.stem.startswith("summary")]

    if not h5_files:
        sys.stderr.write(f"WARNING: no files matching '{args.glob}' in {input_dir}\n")
        return 0

    summary = load_summary(input_dir)

    print("histogramify")
    print(f"  Input dir : {input_dir}")
    print(f"  Output dir: {output_dir}")
    print(f"  Files     : {len(h5_files)}")
    print()

    written: List[Path] = []
    skipped: List[Tuple[Path, str]] = []

    t_start = time.monotonic()
    for h5_path in h5_files:
        csv_path, err = process_file(h5_path, output_dir, summary)
        if csv_path is not None:
            written.append(csv_path)
            print(f"  [OK]   {h5_path.name}  ->  {csv_path.name}")
        else:
            skipped.append((h5_path, err or "unknown error"))
            print(f"  [SKIP] {h5_path.name}: {err}")

    elapsed = time.monotonic() - t_start
    print(f"\n  CSVs written : {len(written)}  (elapsed: {elapsed:.1f}s)")
    if skipped:
        print(f"  Skipped      : {len(skipped)}")
        for path, reason in skipped:
            print(f"    {path.name}: {reason}")

    return 1 if skipped else 0


if __name__ == "__main__":
    sys.exit(main())
