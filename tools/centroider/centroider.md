# Tpx3 Sweep Optimizer — Architecture & Developer Guide

This document explains what the optimizer does, why it exists, how the code is
structured, and how to run it.  It is intended for anyone joining this project
who needs to understand the full pipeline from raw `.tpx3` data to x-histogram
CSVs.

---

## Background & Objective

A **Timepix3 (Tpx3)** detector records individual pixel hits: for each photon
or particle that arrives, the detector registers the pixel coordinates `(x, y)`,
a timestamp (time-of-arrival, ToA), and a time-over-threshold (ToT) proxy for
energy.  The raw data format is `.tpx3` — a binary stream of these hit records.

The raw data is not immediately useful for most analyses.  The key processing
step is **clustering**: nearby hits in both space and time are grouped into
**clusters**, where each cluster represents a single physical event (one
photon, one particle track).  The cluster is summarised by its centroid
position `(cx, cy)`, giving sub-pixel position resolution.

The clustering result depends on two algorithm parameters:

| Parameter | Meaning | CLI flag |
|---|---|---|
| `eps-t` | Maximum time gap between hits in the same cluster | `--eps-t` (e.g. `100ns`) |
| `eps-s` | Maximum pixel distance between hits in the same cluster | `--eps-s` (e.g. `2`) |

Different values of these parameters produce different cluster assignments.
**The Sweep Optimizer exists to run the clustering over a grid of
`(eps-t, eps-s)` combinations automatically**, so we can later compare results
and choose the best parameters for a given experiment.

---

## The External Tool: `tpx3dump` (Luna)

All actual `.tpx3 → .h5` conversion and clustering is done by a compiled Rust
binary called `tpx3dump`, part of the **Luna** software package (version
`Tpx3Dump 0.3.2` at the time of writing):

```
/home/tpx/Desktop/tpx3LOCAL/software/luna/0.4.3/bin/tpx3dump
```

The Python code in `tools/centroider/` does **not** reimplement clustering.
It is a **wrapper and orchestrator** that calls `tpx3dump` repeatedly with
different parameters and handles progress reporting, file organisation, and
post-processing.

### What `tpx3dump` produces

`tpx3dump` writes an HDF5 (`.h5`) file containing two compound datasets:

- **`PixelHits`** — every raw hit as recorded. Columns include `x`, `y`, `tot`,
  `toa`.  Present when clustering is disabled (`--disable-clustering`).
- **`Clusters`** — one row per cluster. Columns include the centroid `cx`, `cy`,
  cluster size, total ToT, and cluster time.  Present when clustering runs
  (the default).

The detector is **256 × 256 pixels**, so `x` and `y` are in `[0, 255]`.
Cluster centroids `cx`, `cy` are floats (sub-pixel precision).

### Key `tpx3dump` flags used by the optimizer

| Flag | Effect |
|---|---|
| `--eps-t VALUE` | Time gap threshold for clustering (e.g. `100ns`) |
| `--eps-s VALUE` | Pixel distance threshold for clustering (integer) |
| `--discard-pixel-data` | Write only the `Clusters` dataset; omit `PixelHits` (saves disk space) |
| `--disable-clustering` | Skip clustering; write only `PixelHits` (the "raw" baseline) |
| `-g LEVEL` | Log verbosity: `off`, `warn`, `info`, etc. |

---

## Pipeline Overview

The full pipeline has three sequential phases, all orchestrated by `sweep.py`
under a single progress bar:

```
  .tpx3 file
      │
      ├─── Phase 1: Clustered conversions ──────────────────────────────────────────┐
      │    For every (eps-t, eps-s) combination:                                    │
      │    tpx3dump process --eps-t T --eps-s S --discard-pixel-data               │
      │    → <stem>_tT_sS.h5   (contains only Clusters dataset)                   │
      │                                                                             │
      ├─── Phase 2: PixelHits baseline ────────────────────────────────────────────┤
      │    tpx3dump process --disable-clustering                                    │
      │    → <stem>_PixelHits.h5   (contains only PixelHits dataset)               │
      │                                                                             │
      └─── Phase 3: Histograms (optional, --histogram) ────────────────────────────┘
           For each .h5 produced above:
           histogramify.py reads Clusters['cx'] or PixelHits['x']
           → <stem>.csv  (x-axis histogram, one column per file)
```

All output files land in a **deterministic run directory**:

```
<output-parent>/<input_stem>_centroided/
```

---

## Phase 1 — Clustered Conversions

### What happens

`sweep.py` builds the Cartesian product of all `eps-t` × `eps-s` values
supplied by the user and runs `tpx3dump` once per combination, sequentially.

For `eps-t = [20ns, 100ns, 500ns]` and `eps-s = [1, 2, 3]` this produces
**9 runs → 9 HDF5 files**:

```
sample21MB_t20ns_s1.h5
sample21MB_t20ns_s2.h5
sample21MB_t20ns_s3.h5
sample21MB_t100ns_s1.h5
...
sample21MB_t500ns_s3.h5
```

### Filename convention

```
<input_stem>_t<eps_t>_s<eps_s>.h5
```

The double underscore (`__`) separates the input stem from the parameter
suffix; `t` and `s` prefix each value.  This is deterministic and
filesystem-safe for all valid `eps-t` unit strings.

### `--discard-pixel-data` is on by default

Because the downstream analysis only needs the `Clusters` dataset (to build
x-histograms), the clustered `.h5` files are generated with
`--discard-pixel-data`.  This drops the `PixelHits` table from the clustered
files and can reduce their size substantially (raw pixel hits are the bulk of
the data).  Pass `--keep-pixel-data` to disable this default.

### Performance note

The Luna `DFSCluster` algorithm's runtime grows dramatically with `eps-t`.
Observed scaling on a 21 MB sample file:

| eps-t | ~runtime |
|---|---|
| 20 ns | 5–7 s |
| 100 ns | 7–10 s |
| 500 ns | 7–10 s |
| 1 µs | ~10 s |
| 10 µs | ~15 s |
| 100 µs | ~30 s |
| 1 ms | hangs |

Keep `eps-t` in the **ns to low-µs range** for practical sweeps.

---

## Phase 2 — PixelHits Baseline

### Why we create this file

The clustered `.h5` files do **not** contain `PixelHits` (because of
`--discard-pixel-data`).  But we need a reference representing the data
**before any clustering** — i.e. the raw detector response — so we can compare:

> How does the x-distribution of raw pixel hits compare to the x-distribution
> of cluster centroids under different (eps-t, eps-s) settings?

The baseline run answers this.  It uses `--disable-clustering`, which makes
`tpx3dump` skip the clustering step entirely and write only `PixelHits`.

### Output file

```
<input_stem>_PixelHits.h5
```

This file is detected by `histogramify.py` via its `_PixelHits` suffix and
processed differently from the clustered files (see Phase 3).

Skip the baseline with `--no-baseline` if you only care about clustered
outputs.

---

## Phase 3 — Histogram CSVs

### Why x-histograms

We collapse the 2D cluster/hit data onto the **x-axis only**, producing a 1D
histogram (counts vs. x position).  This gives a simple, fast-to-compute
profile of the spatial distribution along x, suitable for comparing clustering
parameter choices without handling full 2D arrays.

### Two histogram modes (auto-detected by filename)

**Clustered files** (`*_t*_s*.h5`):

- Read `Clusters['cx']` — the float x-centroid of each cluster.
- Group by unique `cx` value and count occurrences
  (`numpy.unique(cx, return_counts=True)`).
- The x-axis differs per file (cluster positions depend on the parameters).
- Output CSV: one row per unique centroid value.

```
x,s=1&t=100ns
28.0,4
31.0,1
32.0,3
...
```

**PixelHits baseline** (`*_PixelHits.h5`):

- Read `PixelHits['x']` — integer pixel x-coordinate of every raw hit.
- Bin into **fixed** bins `0..255` (256 rows always present, even if count is 0)
  using `numpy.bincount`.
- The x-axis is always `0, 1, 2, …, 255` — no variation between files.

```
x,PixelHits
0,0
1,0
2,1024
...
255,640
```

### Column header format

The count column header encodes the parameters, so a single CSV is
self-describing:

- Clustered: `s=<eps_s>&t=<eps_t>` (e.g. `s=2&t=500ns`)
- Baseline: `PixelHits`

The label is sourced from `summary.json` if available (canonical), falling back
to parsing the filename.

### Phase 3 is opt-in

Add `--histogram` to `sweep.py` to enable it.  It can also be run standalone
at any time:

```bash
.venv/bin/python3.12 tools/centroider/histogramify.py \
    -i SampleData/h5s/sample21MB_centroided
```

---

## Run Directory & Reproducibility

### Directory layout

`-o/--output-dir` is treated as a **parent**.  All outputs for one input file
land in a deterministic subdirectory:

```
<output-dir>/
  <input_stem>_centroided/     ← created automatically, never clutters parent
    luna.meta                  ← reproducibility metadata (JSON)
    sample21MB_t20ns_s1.h5
    sample21MB_t20ns_s2.h5
    ...
    sample21MB_PixelHits.h5
    sample21MB_t20ns_s1.csv   ← (if --histogram)
    ...
    sample21MB_PixelHits.csv
    summary.json               ← one row per tpx3dump run (timing, status, command)
    summary.csv
```

The name has **no timestamp**, so re-running the same input writes to the same
folder (idempotent) and the parent directory stays clean across many
experiments.

### `luna.meta` — the reproducibility file

Written at the start of every real run, this JSON captures:

- `luna_version` — `tpx3dump --version` output (e.g. `"Tpx3Dump 0.3.2"`)
- `tpx3dump_path` — absolute path to the binary used
- `generated_at` — ISO-8601 timestamp with timezone
- `command_line` — the exact Python invocation that produced this run
- `input_file`, `output_parent`, `run_dir` — resolved absolute paths
- `parameters` — **all effective** values (not just what was passed on the
  command line): `eps_t`, `eps_s`, `log_level`, `discard_pixel_data`,
  `baseline_pixelhits`, `histogram`, `skip_existing`, `keep_going`, `extra_args`
- `tpx3dump_flags` — per-phase flags actually forwarded to the binary

This means you can reproduce any run exactly, even months later, by reading
`luna.meta` and re-invoking `sweep.py` with those parameters.

---

## Code Architecture

```
tools/centroider/
  sweep.py          orchestrator — CLI, all three phases, single progress bar
  runner.py         one tpx3dump invocation — builds command, runs subprocess, captures timing
  progress.py       ProgressReporter — adaptive ETA, TTY bar, non-TTY fallback
  histogramify.py   CSV histogram generation — importable by sweep.py, also standalone CLI
  run_example.sh    zero-argument convenience script using SampleData defaults
  README.md         reference: usage, flags, output formats, examples
```

### Module responsibilities

**`sweep.py`** is the single entry point.  It:
1. Parses and validates all CLI arguments.
2. Computes the total task count across all three phases up front, so
   `ProgressReporter` can display an accurate bar from the start.
3. Derives the run dir and writes `luna.meta` before any conversions start.
4. Calls `run_one()` from `runner.py` for each conversion (Phases 1 & 2).
5. Calls `histogramify.process_file()` in-process for each histogram (Phase 3).
6. Writes `summary.json` and `summary.csv` after every phase (so partial
   results survive an abort).

**`runner.py`** is a pure subprocess wrapper.  It knows nothing about sweeps
or progress.  It builds the `tpx3dump` command, runs it, captures stdout/stderr,
measures wall time, and parses the `"Full tpx3dump run took Xs"` log line from
the binary's own timing report.  Returns a `RunResult` dataclass.

**`progress.py`** owns all terminal output during the sweep.  It renders a
`[k/N] [████░░░░]  55%  elapsed: 58s  ETA: 3s` bar on TTY stderr (in-place,
using `\r`) and falls back to line-by-line output when stderr is not a
terminal (e.g. when piped to a log file).  ETA is an adaptive running
average of completed task times.

**`histogramify.py`** is designed to be both importable (called in-process by
`sweep.py` in Phase 3, keeping the progress bar in control of the whole
pipeline) and runnable standalone (for post-hoc processing of existing `.h5`
files).  Auto-detects histogram mode from the filename suffix.

---

## Inputs

| Argument | Flag | Example | Description |
|---|---|---|---|
| Input file | `-i` | `SampleData/tpx3/sample21MB.tpx3` | The raw Tpx3 binary file |
| Output parent | `-o` | `SampleData/h5s` | Parent dir; run subdir created inside it |
| Time gaps | `-t` | `20ns,100ns,500ns` | Comma-separated, with unit (ns/µs/ms/s) |
| Space gaps | `-s` | `1,2,3` | Comma-separated positive integers (pixels) |
| tpx3dump path | `--tpx3dump` | (built-in default) | Override if luna is in a different location |

All other flags are optional.  Run `python tools/centroider/sweep.py --help` for
the full list.

---

## Outputs

| File | When created | Contents |
|---|---|---|
| `luna.meta` | Start of every run | JSON reproducibility record |
| `<stem>_t<T>_s<S>.h5` | Phase 1 | HDF5 with `Clusters` dataset only |
| `<stem>_PixelHits.h5` | Phase 2 | HDF5 with `PixelHits` dataset only |
| `<stem>_t<T>_s<S>.csv` | Phase 3 (`--histogram`) | x-histogram for clustered file |
| `<stem>_PixelHits.csv` | Phase 3 (`--histogram`) | x-histogram for baseline file |
| `summary.json` | After each phase | Per-run timing, status, command, file size |
| `summary.csv` | After each phase | Same as above in CSV format |

---

## Quick Start

```bash
# Preview planned commands (no files created)
python tools/centroider/sweep.py --example --dry-run --histogram

# Full run: 9 combos + baseline + histograms (~2 min on sample21MB.tpx3)
python tools/centroider/sweep.py --example --histogram

# Resume after an interruption (skip already-converted files)
python tools/centroider/sweep.py --example --histogram --skip-existing

# Convenience script (same as --example)
bash tools/centroider/run_example.sh --histogram
```

---

## Dependencies

| Package | Used by | Notes |
|---|---|---|
| `tpx3dump` (Luna) | `runner.py` | External binary, not a Python package |
| `numpy` | `histogramify.py` | Already in `.venv` |
| `h5py` | `histogramify.py` | Install: `.venv/bin/python3.12 -m pip install h5py` |
| Python stdlib | everything else | `argparse`, `csv`, `json`, `subprocess`, `pathlib` |

The sweep itself (`sweep.py`, `runner.py`, `progress.py`) uses **only the
Python standard library** — no extra packages needed to run conversions.
`h5py` + `numpy` are required only for the histogram phase (`--histogram`).
