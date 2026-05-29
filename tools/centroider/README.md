# tpx3 Sweep Optimizer

A standalone command-line tool that wraps `tpx3dump` and runs it over every
`(eps-t, eps-s)` combination, producing one HDF5 file per combo with live
progress and an adaptive ETA.

Optionally generates a CSV histogram per HDF5 file and a PixelHits baseline
for before/after comparison — all driven by a single progress bar.

No extra Python packages required for the sweep itself.
`histogramify.py` additionally needs `h5py` and `numpy` (install via `.venv`).

---

## Quick start

The fastest way to try it is the convenience script, which uses the 21 MB
sample file and 9 combinations by default:

```bash
# Preview — prints planned commands without running anything
bash tools/centroider/run_example.sh --dry-run

# Full sweep (9 combinations + 1 baseline, ~few minutes)
bash tools/centroider/run_example.sh

# Sweep + generate histogram CSVs in one pass
bash tools/centroider/run_example.sh --histogram
```

Or use the built-in `--example` preset directly:

```bash
python tools/centroider/sweep.py --example --dry-run
python tools/centroider/sweep.py --example
python tools/centroider/sweep.py --example --histogram
```

---

## Full usage

```
python tools/centroider/sweep.py [OPTIONS]

Input / output:
  -i, --input PATH        Path to the .tpx3 input file
  -o, --output-dir PATH   Directory for HDF5 outputs and summary files

Sweep parameters:
  -t, --eps-t LIST        Comma-separated time gaps (e.g. 20ns,100ns,500ns)
  -s, --eps-s LIST        Comma-separated pixel distances (e.g. 1,2,3)

Converter:
  --tpx3dump PATH         Path to tpx3dump binary (default: $TPX3DUMP env or built-in path)
  --extra-args "ARGS"     Extra flags forwarded to every tpx3dump call
  --log-level LEVEL       tpx3dump log verbosity: off/trace/debug/info/warn/error (default: warn)

Behaviour:
  --skip-existing         Skip combos whose .h5 output already exists (resume)
  --keep-going            Continue sweep if a run fails (default: abort on first failure)
  --dry-run               Print planned commands and exit without running
  --example               Fill in SampleData defaults for any missing -i/-o/-t/-s

Post-processing:
  --histogram             Generate CSV histograms (histogramify) for every produced .h5
  --no-baseline           Skip the PixelHits baseline run (--disable-clustering)
  --keep-pixel-data       Do NOT pass --discard-pixel-data to clustered runs
```

---

## Example: the 9-file sweep + baseline + histograms

```bash
python tools/centroider/sweep.py \
    -i /home/tpx/Desktop/tpx3LOCAL/SampleData/tpx3/sample21MB.tpx3 \
    -o /home/tpx/Desktop/tpx3LOCAL/SampleData/h5s \
    -t 20ns,100ns,500ns \
    -s 1,2,3 \
    --histogram
```

> **Important:** Keep `--eps-t` in the **ns to low-µs range** for Tpx3 data.
> The DFSCluster algorithm becomes exponentially slower as eps-t grows, because
> it tries to merge more hits into bigger and bigger clusters.
> Values above ~100µs can make a single run take minutes or hang entirely.
>
> Measured scaling on `sample21MB.tpx3`:
> - 20ns → ~7s · 100ns → ~9s · 500ns → ~8s · 1µs → ~9s · 10µs → ~12s · 100µs → ~28s · 1ms+ → hangs

---

## Run directory & reproducibility

To avoid mixing outputs into the folder you point `-o` at, the tool treats
`-o/--output-dir` as a **parent** and writes everything into a deterministic
per-input subfolder:

```
<output-dir>/<input_stem>_centroided/
```

The name has **no timestamp**, so re-running the same input writes to the same
folder (idempotent) and the parent directory stays clean.

Each run directory contains a `luna.meta` JSON file capturing everything needed
to unambiguously recreate the run:

```json
{
  "luna_version": "Tpx3Dump 0.3.2",
  "tpx3dump_path": "/home/tpx/.../luna/0.4.3/bin/tpx3dump",
  "generated_at": "2026-05-29T14:20:00-07:00",
  "command_line": "tools/centroider/sweep.py --example --histogram",
  "input_file": "/home/tpx/.../SampleData/tpx3/sample21MB.tpx3",
  "output_parent": "/home/tpx/.../SampleData/h5s",
  "run_dir": "/home/tpx/.../SampleData/h5s/sample21MB_centroided",
  "parameters": {
    "eps_t": ["20ns", "100ns", "500ns"],
    "eps_s": [1, 2, 3],
    "log_level": "warn",
    "discard_pixel_data": true,
    "baseline_pixelhits": true,
    "histogram": true,
    "skip_existing": false,
    "keep_going": false,
    "extra_args": []
  },
  "tpx3dump_flags": {
    "clustered": ["--discard-pixel-data"],
    "baseline": ["--disable-clustering"]
  }
}
```

---

## Pipeline phases

`sweep.py` drives all three phases under a **single progress bar** (`[k/N]`):

| Phase | Task | Flag |
|---|---|---|
| 1 — clustered conversions | `tpx3dump ... --eps-t T --eps-s S --discard-pixel-data` | always |
| 2 — PixelHits baseline | `tpx3dump ... --disable-clustering` → `<stem>_PixelHits.h5` | default on; `--no-baseline` to skip |
| 3 — histograms | `histogramify` in-process → one `.csv` per `.h5` | opt-in via `--histogram` |

---

## Outputs

### Per-combination HDF5 files (clustered, Phase 1)

Named `<input_stem>_t<eps_t>_s<eps_s>.h5`, written into `--output-dir`.

By default these contain **only the `Clusters` dataset** (`--discard-pixel-data`),
which saves significant disk space.  Pass `--keep-pixel-data` to retain `PixelHits`
in each clustered file.

### PixelHits baseline HDF5 (Phase 2)

Named `<input_stem>_PixelHits.h5`.  Contains the `PixelHits` dataset (unsorted,
unclastered) and is used as the "before clustering" reference for histogram comparisons.
Skip with `--no-baseline`.

### Histogram CSVs (Phase 3, requires `--histogram`)

One CSV per HDF5, same stem, written into `--output-dir`:

**Clustered files** → `<stem>.csv`:

```
x,s=1&t=20ns
0.5,3
1.0,12
1.5,7
...
```

- `x`: unique cluster-x centroid (float), sorted ascending.
- count column: number of clusters at that centroid (y and t collapsed).
- Column header: `s=<eps_s>&t=<eps_t>` (from `summary.json` or filename).

**PixelHits baseline** → `<stem>_PixelHits.csv`:

```
x,PixelHits
0,1024
1,2310
...
255,640
```

- `x`: fixed integer pixel column, every value `0..255` (256 rows always).
- count column: raw pixel-hit count at that x (y and t collapsed).

### summary.json / summary.csv

One row per conversion with:

| Field | Description |
|---|---|
| `eps_t` | Time gap used (null for baseline) |
| `eps_s` | Pixel distance used (null for baseline) |
| `output_file` | Full path to the HDF5 |
| `status` | `ok`, `failed`, or `skipped` |
| `wall_seconds` | Measured elapsed time (Python) |
| `reported_seconds` | Time reported by tpx3dump itself |
| `output_bytes` | HDF5 file size |
| `exit_code` | tpx3dump exit code |
| `command` | Full command that was run |
| `error_message` | Last lines of stderr on failure |

### Completion signal

`sweep.py` exits with code `0` when all phases complete successfully,
non-zero if any conversion or histogram step failed.  The final summary
table and `summary.json`/`summary.csv` are the machine-readable "done" signal.

---

## Output directory layout (example)

```
SampleData/h5s/                       # parent (-o), kept clean
  sample21MB_centroided/              # deterministic run dir (<input_stem>_centroided)
    luna.meta                         # reproducibility metadata (luna version + params)
    sample21MB_t20ns_s1.h5           # Clusters only (--discard-pixel-data)
    sample21MB_t20ns_s2.h5
    sample21MB_t20ns_s3.h5
    sample21MB_t100ns_s1.h5
    sample21MB_t100ns_s2.h5
    sample21MB_t100ns_s3.h5
    sample21MB_t500ns_s1.h5
    sample21MB_t500ns_s2.h5
    sample21MB_t500ns_s3.h5
    sample21MB_PixelHits.h5           # PixelHits baseline (--disable-clustering)
    sample21MB_t20ns_s1.csv          # Histogram CSVs (--histogram)
    sample21MB_t20ns_s2.csv
    ...
    sample21MB_PixelHits.csv
    summary.json
    summary.csv
```

---

## Post-processing standalone: histogramify.py

`histogramify.py` can also be run on its own against any existing `.h5` directory:

```bash
# Point -i at a sweep run dir (where the .h5 files live)
.venv/bin/python3.12 tools/centroider/histogramify.py \
    -i /home/tpx/Desktop/tpx3LOCAL/SampleData/h5s/sample21MB_centroided
```

```
histogramify.py [OPTIONS]

  -i, --input-dir PATH   Directory containing .h5 files (default: SampleData/h5s)
  -o, --output-dir PATH  Directory for CSV outputs (default: same as --input-dir)
  --glob PATTERN         Glob pattern for input files (default: *.h5)
```

Mode is auto-detected:
- `*_PixelHits.h5` → `PixelHits['x']` mode, fixed bins `0..255`
- all others → `Clusters['cx']` mode, unique float centroids

Requires: `h5py`, `numpy` (install into `.venv` with `pip install h5py`).

---

## Progress bar example

```
Starting sweep of 20 combination(s)...
  [OK] eps_t=20ns eps_s=1  wall=7s  ETA remaining: 2m 9s
  [OK] eps_t=20ns eps_s=2  wall=7s  ETA remaining: 2m 2s
  [2/20] [███░░░░░░░░░░░░░░░░░░░░░░░░░░░]  10%  elapsed: 14s  ETA: 2m 2s | next: eps_t=20ns eps_s=3
  ...
  [OK] PixelHits baseline  wall=5s  ETA remaining: 3s
  [OK] histogram: sample21MB_t20ns_s1.h5  wall=0s  ETA remaining: done
  ...
Sweep complete — 20 ok, 0 skipped, 0 failed  |  total elapsed: 2m 45s
```

---

## Resume a partial sweep

If the sweep was interrupted, use `--skip-existing` to skip already-converted
files and only run the missing combinations:

```bash
python tools/centroider/sweep.py \
    -i SampleData/tpx3/sample21MB.tpx3 \
    -o SampleData/h5s \
    -t 20ns,100ns,500ns -s 1,2,3 \
    --skip-existing
```

---

## Override paths in run_example.sh

The convenience script respects these env vars:

```bash
TPX3_INPUT=/path/to/other.tpx3 \
H5_OUTPUT_DIR=/path/to/output \
TPX3DUMP=/path/to/tpx3dump \
  bash tools/centroider/run_example.sh
```

---

## File layout

```
tools/centroider/
  sweep.py          — CLI entry point and orchestration loop (all 3 phases)
  runner.py         — run_one(): subprocess wrapper, timing, RunResult dataclass
  progress.py       — ProgressReporter: adaptive ETA, TTY bar, non-TTY fallback
  histogramify.py   — histogram CSVs from .h5 files (importable + standalone CLI)
  run_example.sh    — zero-arg convenience script
  README.md         — this file
```
