#!/usr/bin/env python3
"""Validate tpx3parser HDF5 output against ASI tpx3dump ground-truth.

Usage:
    python validate.py --ours output.h5 --ref tpx3dump_ref.h5 [--n-mismatch 20]
    python validate.py --ours output.h5 --ref tpx3dump_ref.h5 --datasets PixelHits TDCEvents
    python validate.py --ours output.h5 --ref tpx3dump_ref.h5 --summary-only

Uses the repo virtual environment (h5py, numpy).

Exit codes:
    0  all checks passed
    1  mismatch detected
    2  input error
"""

import argparse
import sys
from pathlib import Path

try:
    import h5py
    import numpy as np
except ImportError:
    print("ERROR: h5py and numpy required. Activate the repo venv first.", file=sys.stderr)
    sys.exit(2)


CHECKED_DATASETS = ["PixelHits", "TDCEvents", "ExposureTimeBoundaries"]
SENTINEL_FIELDS = {
    "PixelHits": ["toa", "x", "y", "tot"],
    "TDCEvents": ["tdc_type", "trigger_count", "timestamp"],
    "ExposureTimeBoundaries": None,
}

COL_NEG1 = {"cid", "dtoa", "tof"}  # always -1 when clustering disabled


def open_h5(path: str) -> h5py.File:
    try:
        return h5py.File(path, "r")
    except OSError as e:
        print(f"ERROR opening {path}: {e}", file=sys.stderr)
        sys.exit(2)


def validate(ours_path: str, ref_path: str, datasets: list[str],
             n_mismatch: int, summary_only: bool, strict_etb: bool = False) -> bool:
    all_ok = True

    with open_h5(ours_path) as f_ours, open_h5(ref_path) as f_ref:
        ours_keys = set(f_ours.keys())
        ref_keys = set(f_ref.keys())

        # Dataset presence check.
        for ds in datasets:
            if ds not in ours_keys and ds not in ref_keys:
                continue
            if ds in ref_keys and ds not in ours_keys:
                print(f"FAIL  {ds}: present in ref but missing in ours")
                all_ok = False
                continue
            if ds not in ref_keys:
                # Extra dataset in ours — OK if it's one we generate.
                continue

        for ds in datasets:
            if ds not in ref_keys:
                continue
            if ds not in ours_keys:
                continue

            arr_ours = f_ours[ds][:]
            arr_ref = f_ref[ds][:]

            # Shape check.
            if arr_ours.shape != arr_ref.shape:
                # ExposureTimeBoundaries requires --exposure-interval-ms to reproduce exactly.
                # Treat a shape mismatch there as a warning, not a failure.
                severity = "WARN " if ds == "ExposureTimeBoundaries" else "FAIL "
                print(f"{severity} {ds}: shape mismatch — ours {arr_ours.shape} vs ref {arr_ref.shape}"
                      + (" (use --exposure-interval-ms to match)" if ds == "ExposureTimeBoundaries" else ""))
                if ds != "ExposureTimeBoundaries":
                    all_ok = False
                continue

            # Dtype check: compare field names, ignoring padding fields (names starting with _).
            if arr_ours.dtype.names is not None:
                ours_public = [n for n in arr_ours.dtype.names if not n.startswith('_')]
                ref_public = [n for n in arr_ref.dtype.names if not n.startswith('_')]
                if set(ours_public) != set(ref_public):
                    print(f"FAIL  {ds}: dtype field names differ\n"
                          f"      ours (public): {ours_public}\n"
                          f"      ref  (public): {ref_public}")
                    all_ok = False
                    continue

            # Per-field comparison.
            if arr_ours.dtype.names is not None:
                fields_to_check = SENTINEL_FIELDS.get(ds) or list(arr_ours.dtype.names)
                for field in fields_to_check:
                    if field not in arr_ours.dtype.names:
                        continue
                    o = arr_ours[field]
                    r = arr_ref[field]
                    if np.array_equal(o, r):
                        if not summary_only:
                            print(f"OK    {ds}.{field}  ({len(o):,} rows)")
                    else:
                        bad = np.flatnonzero(o != r)
                        print(f"FAIL  {ds}.{field}  {len(bad):,} mismatches "
                              f"({len(bad) / len(o) * 100:.3f}%)")
                        if not summary_only:
                            n_show = min(n_mismatch, len(bad))
                            for idx in bad[:n_show]:
                                print(f"      row {idx}: ours={o[idx]}  ref={r[idx]}")
                        all_ok = False

                # Also check -1 sentinel fields are correct in ours.
                for field in COL_NEG1:
                    if field not in arr_ours.dtype.names:
                        continue
                    o = arr_ours[field]
                    if not np.all(o == -1):
                        bad_count = int((o != -1).sum())
                        print(f"FAIL  {ds}.{field}  expected all -1 but "
                              f"{bad_count:,} are not")
                        all_ok = False
                    elif not summary_only:
                        print(f"OK    {ds}.{field}  all -1 ({len(o):,} rows)")
            else:
                # Plain array (ExposureTimeBoundaries).
                o = arr_ours
                r = arr_ref
                is_etb = (ds == "ExposureTimeBoundaries")
                if np.array_equal(o, r):
                    if not summary_only:
                        print(f"OK    {ds}  ({len(o)} values)")
                else:
                    bad = np.flatnonzero(o != r)
                    severity = "FAIL " if (not is_etb or strict_etb) else "WARN "
                    etb_note = " (provide --exposure-interval-ms to tpx3parser; use --strict-etb to fail)" if is_etb else ""
                    print(f"{severity} {ds}  {len(bad)} mismatches{etb_note}")
                    if not summary_only:
                        for idx in bad[:n_mismatch]:
                            print(f"      [{idx}]: ours={o[idx]}  ref={r[idx]}")
                    if not is_etb or strict_etb:
                        all_ok = False

    return all_ok


def main():
    parser = argparse.ArgumentParser(
        description="Validate tpx3parser .h5 output against ASI tpx3dump ground-truth"
    )
    parser.add_argument("--ours", required=True, help="Path to tpx3parser output .h5")
    parser.add_argument("--ref", required=True, help="Path to ASI tpx3dump reference .h5")
    parser.add_argument(
        "--datasets", nargs="+", default=CHECKED_DATASETS,
        help=f"Datasets to check (default: {CHECKED_DATASETS})"
    )
    parser.add_argument(
        "--n-mismatch", type=int, default=20,
        help="Max mismatched rows to print per field (default: 20)"
    )
    parser.add_argument(
        "--summary-only", action="store_true",
        help="Print only FAIL lines, suppress OK lines"
    )
    parser.add_argument(
        "--strict-etb", action="store_true",
        help="Treat ExposureTimeBoundaries mismatches as failures (default: warning only)"
    )
    args = parser.parse_args()

    for p in (args.ours, args.ref):
        if not Path(p).exists():
            print(f"ERROR: file not found: {p}", file=sys.stderr)
            sys.exit(2)

    print(f"Comparing:\n  ours : {args.ours}\n  ref  : {args.ref}\n")
    ok = validate(args.ours, args.ref, args.datasets, args.n_mismatch,
                  args.summary_only, strict_etb=args.strict_etb)

    if ok:
        print("\nAll checks PASSED.")
        sys.exit(0)
    else:
        print("\nValidation FAILED — see mismatches above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
