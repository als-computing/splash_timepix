# Centroider Tab — Scientist's Tutorial

**Purpose:** Find the best clustering parameters for your `.tpx3` data by running
a parameter sweep and comparing the resulting x-histograms side by side.

---

## Background — Why clustering parameters matter

The Timepix3 detector records individual pixel hits. Each photon or particle that
arrives produces one or more adjacent hits in both space and time. **Clustering**
groups those related hits into a single event called a **cluster**, whose centroid
gives sub-pixel position resolution.

Two parameters control how aggressively hits are merged:

| Parameter | What it controls | UI field |
|---|---|---|
| **eps-s** | Maximum pixel distance between hits in the same cluster | eps-s (pixels) |
| **eps-t** | Maximum time gap between hits in the same cluster (nanoseconds) | eps-t (ns) |

Choosing these values incorrectly leads to:
- **Too small** → clusters are split; one real event becomes many small clusters,
  broadening the peak and reducing count efficiency.
- **Too large** → unrelated hits are merged; clusters absorb neighbours, shifting
  centroids and smearing the peak.

The Centroider tab runs every combination of the values you specify across the full
eps-s × eps-t grid so you can compare the resulting x-histograms and pick the
best pair by eye — without having to run anything manually.

---

## Quick start

1. Open the **Centroider** tab in the application.
2. Click **Browse** and select a representative `.tpx3` file from your experiment.
3. Type a few candidate values into **eps-s (pixels)** and **eps-t (ns)**.
4. Press **Centroid** and watch the plot fill in as each run completes.
5. Choose the combination whose histogram peak is sharpest and best-resolved.

The whole process typically takes a few minutes for a 20 MB file with a 3 × 3 grid.

---

## The UI at a glance

```
┌─ Inputs ──────────────────────────────────────────────────────────────┐
│  TPX3 File   [ /path/to/data.tpx3                    ]  [ Browse ]   │
│  eps-s (pixels)  [ 1,2,3                             ]               │
│  eps-t (ns)      [ 5,10,20                           ]               │
│                                          [ Centroid ] [ ⏹ Stop ]     │
└───────────────────────────────────────────────────────────────────────┘
┌─ Progress ─────────────────────────────────────────────────────────────┐
│  [████████████░░░░░░░░░░░░░]  [3/10] s=2&t=10ns: ok  |  ETA: 42s    │
└───────────────────────────────────────────────────────────────────────┘
┌─ Combinations ─────────┐  ┌─ Summary plot ────────────────────────────┐
│         t=5ns t=10ns … │  │  [ Waterfall ]                            │
│  s=1  [ ] ok  [ ] ok   │  │                                           │
│  s=2  [ ] ok  [ ] …    │  │   counts vs y (dispersive axis)           │
│  s=3  [ ] …   [ ] …    │  │                                           │
└────────────────────────┘  └───────────────────────────────────────────┘
```

---

## Step-by-step walkthrough

### Step 1 — Select a TPX3 file

Click **Browse** and navigate to the `.tpx3` file you want to analyse. You can
also type or paste the path directly into the text box.

> **Tip:** Use a short, representative file from your experiment — you do not need
> the full dataset. A 20–50 MB file gives reliable statistics and runs in a few
> minutes per combination.

---

### Step 2 — Choose eps-s values (pixels)

Type a comma-separated list of positive integers into the **eps-s (pixels)** field.

```
1,2,3
```

Each integer is the maximum allowed pixel distance between two hits that belong to
the same cluster. Typical starting values for a Timepix3 detector are **1, 2, 3**.

| Value | Effect |
|---|---|
| 1 | Only immediately adjacent pixels are merged. Good for high-flux, sparse hits. |
| 2 | Merges hits within a 2-pixel radius. Standard starting point. |
| 3+ | Merges hits across a wider area. Useful for large charge-sharing clouds. |

---

### Step 3 — Choose eps-t values (nanoseconds)

Type a comma-separated list of integers (no unit needed — nanoseconds are assumed)
into the **eps-t (ns)** field.

```
5,10,20
```

Each value is the maximum allowed time gap between hits that belong to the same
cluster.

> **Important:** Only nanosecond values are accepted. Larger time windows (µs, ms)
> cause the clustering algorithm to run dramatically slower and will merge
> unrelated hits. Stick to the **5 – 500 ns range** for practical sweeps.

| Value | Effect |
|---|---|
| 5 ns | Tight time window; only very fast charge clouds grouped. |
| 20 ns | Typical value for most experiments. |
| 100 ns | Broad window; useful if charge collection is slow. |
| 500 ns | Very broad; likely to over-cluster at high flux. |

---

### Step 4 — Press Centroid

Pressing **Centroid** starts the sweep. The button changes to **Centroiding…**
and the inputs are locked until the sweep finishes or is stopped.

The tool runs every (eps-s, eps-t) combination sequentially, **plus one extra
PixelHits baseline run** (clustering disabled) that serves as the "raw hits"
reference. For a 3 × 3 grid this means 10 total runs.

**Progress bar** — fills as each run completes. The label shows the current
combination, its status, and the estimated time remaining.

**ETA** — computed as a running average of completed run times. It displays
"estimating…" until the first run finishes.

---

### Step 5 — Watch the combinations grid

The **Combinations** panel on the left shows a table where:
- **Rows** correspond to eps-s values.
- **Columns** correspond to eps-t values.
- Each cell shows the status of that run and a **show** checkbox.

| Status label | Meaning |
|---|---|
| `queued` | Not yet started. |
| `running…` | tpx3dump is currently processing this combination. |
| `ok (7.3s)` | Completed successfully. Wall time shown in parentheses. |
| `cached` | Output file already existed from a previous run; skipped. |
| `failed` | tpx3dump returned a non-zero exit code. |

Once a run finishes successfully, its **show** checkbox becomes enabled and its
histogram curve appears in the plot.

---

### Step 6 — Read the summary plot

The **Summary plot** on the right overlays one x-histogram curve per
combination, plus the grey PixelHits baseline.

- **X axis:** y pixel position (the dispersive axis of the detector — the
  physically meaningful direction for your experiment).
- **Y axis:** counts (number of events at each position).
- **Grey curve:** raw PixelHits baseline (before any clustering). Use this as
  your reference for what the signal looks like without merging.
- **Coloured curves:** one per (eps-s, eps-t) combination. Colours are assigned
  in order of completion and stay consistent within a sweep.

**What to look for:**

- A good set of parameters produces a **sharp, well-separated peak** (or peaks)
  with low background between them.
- If the peak is broader than the PixelHits baseline, eps-s or eps-t is too
  large and unrelated hits are being merged.
- If the peak is about the same width as the baseline, clustering is not helping
  (eps values may be too small).
- The PixelHits baseline typically shows a smoother, broader distribution because
  it has no sub-pixel centroid precision.

**Interacting with the plot:**
- **Scroll** to zoom in/out.
- **Right-click → View All** to reset zoom.
- **Drag** to pan.
- Uncheck a **show** checkbox in the Combinations grid to hide that curve.

---

### Step 7 — Waterfall mode (optional)

If many curves overlap and are hard to distinguish, toggle the **Waterfall**
button above the plot.

Waterfall mode offsets each visible curve vertically by a fixed step (1/10th of
the visible range), so overlapping histograms separate into a stacked display.
The relative shape of each curve is preserved; only the vertical position changes.
Toggle it off to return to the normal overlaid view.

---

### Step 8 — Choose the best parameters

Compare the curves and identify the combination that gives the sharpest,
best-resolved histogram. Note down the eps-s and eps-t values of that combination
— these are the parameters to use for your full data reduction.

> **Rule of thumb:** start with the smallest values that produce a
> noticeably sharper peak than the PixelHits baseline. Going larger than
> necessary costs runtime and degrades centroid accuracy at high flux.

---

## Stopping a sweep mid-run

Press **⏹ Stop** at any time to interrupt the sweep. The application will:

1. Send a termination signal to the currently running `tpx3dump` process.
2. Wait up to 3 seconds for it to exit cleanly; if it does not, send a
   force-kill signal.
3. Delete the partially-written output `.h5` file (it would be corrupted).
4. Return the already-completed curves to the plot.

The status bar will show how many runs completed, how many were cached, and how
many were cancelled.

You can immediately re-run with adjusted parameters after stopping — press
**Centroid** again.

> **On app close:** if a sweep is running when you close the application, the
> active `tpx3dump` process is killed automatically before the window closes.

---

## Output files

All outputs land in a single subdirectory next to your `.tpx3` file:

```
/path/to/data_centroided/
  data_t5ns_s1.h5          ← clustered HDF5, one per combination
  data_t5ns_s2.h5
  ...
  data_PixelHits.h5         ← raw hits baseline (no clustering)
  summary.json              ← timing, status, and command for each run
  summary.csv               ← same data in CSV format
  luna.meta                 ← full reproducibility record (parameters + software version)
```

**HDF5 files:** each clustered `.h5` contains a `Clusters` dataset with columns
including the centroid positions `cx` (x) and `cy` (y). The PixelHits baseline
contains a `PixelHits` dataset with the raw hit coordinates.

**summary.json / summary.csv:** produced after each run, so even a partially
completed sweep leaves a readable record of what finished.

**luna.meta:** records the exact `tpx3dump` version, binary path, all parameters,
and a timestamp so any run can be reproduced exactly at a later date.

---

## Caching — re-running without redoing work

If you run the sweep a second time with the same file and parameter values, any
`.h5` file that already exists on disk is **skipped automatically** and its curve
is loaded directly — marked as `cached` in the grid. This means:

- You can safely re-open the application and re-run to reload the plot from a
  previous session.
- You can add new eps-s or eps-t values to an existing sweep; only the new
  combinations run, the old ones are reused.
- To force a complete re-run (e.g. after updating the software), delete the
  `_centroided/` directory and press Centroid again.

---

## Frequently asked questions

**Q: How do I pick my initial parameter range?**

Start with eps-s = `1,2,3` and eps-t = `5,10,20`. This 3 × 3 grid covers the
most common practical range and completes in a few minutes. Once you see a clear
trend in the plot, narrow or expand from there.

**Q: The PixelHits baseline always appears last — is that normal?**

Yes. The tool runs all clustered combinations first (Phase 1), then the PixelHits
baseline (Phase 2). The grey baseline curve appears in the plot only after the
last run finishes.

**Q: A run shows "failed" in the grid. What happened?**

Hover over the cell to see the status, or check the application log for the
`tpx3dump` error message. Common causes are: the input file is corrupted, the
disk is full, or tpx3dump is not installed at the expected path.

**Q: Can I run the sweep on multiple files?**

Not simultaneously from the UI — run one file at a time. For batch processing
of many files, use the command-line tool directly:

```bash
python tools/centroider/sweep.py \
    -i /path/to/data.tpx3 \
    -o /path/to/output \
    -t 5ns,10ns,20ns \
    -s 1,2,3
```

**Q: Where is tpx3dump?**

The application uses the binary bundled with the software:
```
<repo>/ASI/tpx3dump
```
No additional installation is needed; it is found automatically. To use a
different build, set the `TPX3DUMP` environment variable to its full path
before launching (applies to both the UI and the command-line sweep tool).

**Q: Why are ms or s not accepted as time units?**

At those timescales the clustering algorithm merges thousands of unrelated hits,
making each run take many minutes or hang entirely. Nanoseconds are the only
physically meaningful range for Timepix3 data.
