# PLAN — sub-pixel centroiding for MCP/phosphor Timepix3 data

Design document for a standalone tool. Intended to live in its own repository and be
consumed by `splash_timepix` later, not to be built inside it.

---

## 1. Purpose

ASI's `tpx3dump` does not compute a centroid. The `cx`/`cy` it writes into `Clusters`
are the integer coordinates of the single highest-ToT pixel in the cluster (verified
exactly on 267,138 clusters; see `ethan.md`). Its cluster record is a timewalk-correction
primitive, not a position primitive.

This tool computes the real thing: it groups pixel hits by cluster id and reduces each
group to one sub-pixel event position. Target precision on the dispersive axis is
~0.27 px (≈15 µm) against the ~1.28 px (≈70 µm) scatter we get today.

---

## 2. Our experimental setup

Photoelectrons from the sample are energy-dispersed and strike a **double-stack
microchannel plate**. The resulting electron avalanche is converted to visible light by a
**P47 (Y₂SiO₅:Ce) phosphor**, chosen to replace the P43 (Gd₂O₂S:Tb) screen that shipped
with the instrument because P43's millisecond-scale glow onset and decay are far too slow
for event-by-event Timepix3 timestamping. There is **no image intensifier** between the
phosphor and the sensor: the phosphor is optically coupled directly onto a bare
**Timepix3 chip**, which detects the visible photons of each flash. One incident
photoelectron therefore produces one roughly radially symmetric light spot spread over
tens of pixels, and the centre of that spot — not any individual pixel — is the estimator
of where the electron landed. The dispersive axis is the detector **y** axis, so position
precision on y converts directly into energy resolution.

Measured from our own data, one flash rises to peak ~180 ns after its first hit and decays
with a ~187 ns constant, with 90% of the light arriving within ~492 ns. That figure is the
phosphor convolved with the timewalk of the faint halo pixels and MCP recharge dynamics,
so it should not be quoted as a P47 constant — but it does set the timescale the clustering
has to span.

---

## 3. Why centroiding is valid for this geometry

Sub-pixel reconstruction by ToT-weighted centre of mass is standard for hybrid pixel
detectors. Khalil et al. resolved 4.4 ± 0.86 µm steps inside a 55 µm CdTe pixel
([J. Synchrotron Rad. 25, 2018](https://doi.org/10.1107/s1600577518013838)); a Timepix4
study in a TEM measured MTF at Nyquist improving by 2.12× at 100 keV and 3.16× at 200 keV
after clustering and centroiding ([arXiv:2411.16258](https://arxiv.org/html/2411.16258v1));
and centroiding of intensified TPX3CAM data is routine in the velocity-map-imaging
community. The known failure modes are documented and we should test for them rather than
assume immunity: sub-threshold charge loss distorts both the centre of mass and the cluster
energy ([JINST 19 C01026](https://iopscience.iop.org/article/10.1088/1748-0221/19/01/C01026/pdf)),
and centroids can pile up non-uniformly within a pixel because only a discrete set of
cluster shapes is possible, which requires a flat-field style sub-pixel response correction
and costs DQE ([Front. Phys. 11:1123787, 2023](https://www.frontiersin.org/journals/physics/articles/10.3389/fphy.2023.1123787/full)).
That second artifact is strongest for clusters of one to four pixels and should be weak for
our tens-of-pixels light spots, but it is cheap to measure and section 8 does so.

**The concern that a charged particle deposits ionisation along an extended trajectory and
so activates multiple pixels in a way that displaces the ToT centroid from the true entry
point does not apply to us, because we image phosphor light rather than detecting the
photoelectron directly in the silicon.**

---

## 4. Input contract

### 4.1 Our existing `.h5` files cannot be ingested

This is a correction to the original sketch. Checked directly:

| File | Datasets | Usable? |
|---|---|---|
| `000000_t10ns_s2.h5` (clustered) | `Clusters`, `ExposureTimeBoundaries`, `TDCEvents` | **No** — `PixelHits` was stripped |
| `000000_PixelHits.h5` (baseline) | `PixelHits`, `ExposureTimeBoundaries`, `TDCEvents` | **No** — clustering was off, every `cid` is −1 |

`splash_timepix`'s `api.run_sweep()` passes `--discard-pixel-data` on the clustered runs and
`--disable-clustering` on the baseline, so no file we currently produce contains pixel hits
*and* cluster ids together. Everything must be regenerated from the raw `.tpx3`. Nothing
needs re-measuring — the raw files hold all of it.

### 4.2 Required `tpx3dump` invocation

```bash
tpx3dump process \
  -i <raw>.tpx3 \
  -o <out>.h5 \
  --eps-s 2 \
  --eps-t 200ns \
  --tof-tdc-reference TDC1Rising \
  -g warn
# note: NO --discard-pixel-data, NO --disable-clustering
```

Three points the tool must document and validate on ingest:

- **`--eps-t` must span the flash.** At 10 ns we fragment each event into ~12 pieces
  (267,138 clusters vs 22,248 at convergence, while the count of real events stays at
  ~14,800 throughout). 200 ns is where it converges; 500 ns gives the same answer, so there
  is margin. Re-confirm at production count rates, where pile-up risk is higher.
- **`--tof-tdc-reference` is currently never passed**, so `tof` is −1 in every file we have.
  Without it we lose the time-resolved axis, which is the point of the instrument.
- **The `toa` LSB is 15.625 ns.** Any `eps-t` below that is meaningless. The tool should
  refuse or warn.

### 4.3 What we rely on

`PixelHits` with dtype `(toa, cid, tot, dtoa, x, y, tof)`. Confirmed on a 200 ns run that
`cid` is **dense over `0..N_clusters-1` with no negative values**, so grouping needs no
sort — `np.bincount` over `cid` is O(N) and fully vectorised. The tool should still handle
`cid < 0` defensively in case other `tpx3dump` settings emit unclustered hits.

---

## 5. Architecture

```
raw .tpx3
   |  tpx3dump  (external, invoked by our CLI wrapper or run by hand)
   v
<name>.h5   PixelHits[toa, cid, tot, dtoa, x, y, tof] + Clusters + TDCEvents
   |
   |  ingest: chunked read, validate schema + provenance attrs
   v
group by cid   (bincount-based, no sort)
   |
   |  position estimator  (pluggable)     -> x, y
   |  time estimator      (pluggable)     -> t
   |  quality metrics                     -> size, sum_tot, sigma, err
   v
<name>_events.h5   Events[cid, x, y, t, tof, size, sum_tot, sigma_x, sigma_y, err_x, err_y]
```

Keep a pure library core with no Qt and no UI dependency, and put a thin CLI on top. The
library is what `splash_timepix` will import later; the CLI is what we use for the
validation campaign in section 8.

Estimators should be a registry keyed by name so that "more than one option of the math" is
a first-class concept rather than a branch, and so a run's provenance can record exactly
which one produced the file.

---

## 6. The estimators

### 6.1 Position

| Name | Definition | Why |
|---|---|---|
| `max_tot` | coordinates of the highest-ToT pixel | Reproduces ASI. **Control** — every comparison is against this. |
| `mean` | unweighted mean of member pixel coordinates | Immune to ToT nonlinearity. |
| `tot_weighted` | Σ(x·ToT)/Σ(ToT) | **Default.** Standard centre of mass. |
| `tot_power(p)` | Σ(x·ToTᵖ)/Σ(ToTᵖ) | `p` sharpens or flattens the weighting; a knob against ToT nonlinearity. `p=1` is `tot_weighted`. |
| `tot_offset(c)` | weight by max(ToT − c, 0) | Compensates the threshold cutting into the signal, which is what biases the centre of mass. |
| `gaussian_fit` | 2D Gaussian fit to the ToT map | Best expected precision, slowest; a reference to bound how much the cheap estimators lose. |
| `weighted_median` | ToT-weighted geometric median | Robust to a second flash overlapping the first. |

### 6.2 Time

Worth stating explicitly: **for the time coordinate, ASI's approach is the right one and we
should keep it.** The highest-ToT pixel has the least timewalk, which is exactly why Luna
uses it as the T₀ estimator.

| Name | Definition | Note |
|---|---|---|
| `ctoa` | ToA of the highest-ToT pixel | **Default.** Matches ASI; least timewalk. |
| `first` | earliest ToA in the cluster | On a phosphor flash the leading edge may carry the best timing, but it is biased by photon statistics and by timewalk on dim pixels. Worth measuring. |
| `tot_weighted_toa` | Σ(ToA·ToT)/Σ(ToT) | Expected to be worse; include for completeness. |

`tof` should be carried through from the member pixels of the chosen reference pixel, not
recomputed, so that it stays consistent with whatever `--tof-tdc-reference` was used.

---

## 7. Output schema

The original sketch was `cid, x, y, t`. That is the right core, but it is too thin to
support the validation in section 8 or any downstream quality cut, and regenerating from
the source file is expensive. Proposed:

| Column | dtype | Meaning |
|---|---|---|
| `cid` | `int64` | cluster id, traceable back to the source `.h5` |
| `x` | `float32` | centroid, pixel units, sub-pixel |
| `y` | `float32` | centroid, pixel units, sub-pixel — **dispersive axis** |
| `t` | `int64` | event time, picoseconds (matches `toa` units) |
| `tof` | `int64` | time relative to TDC reference, −1 if unavailable |
| `size` | `uint32` | member pixel count |
| `sum_tot` | `uint64` | total cluster ToT — intensity proxy, and the basis for quality cuts |
| `sigma_x`, `sigma_y` | `float32` | ToT-weighted RMS spot extent — pile-up / double-hit discriminator |
| `err_x`, `err_y` | `float32` | σ/√size, the per-event position uncertainty |

`float32` is ample for position: a 24-bit mantissa over a 0–256 range resolves far below the
~0.01 px we could ever claim. `t` must stay `int64` — values are ~7×10¹² ps and float64
would start losing picoseconds.

Root attributes for provenance, non-negotiable if we want to compare runs months apart:
source filename and hash, `tpx3dump` version and full argv, `eps_s`, `eps_t`, estimator
names for position and time, tool version, timestamp.

Keeping one row per event in a single compound dataset makes the file trivially convertible
to a DataFrame and keeps it appendable for chunked writing.

---

## 8. Validation — the part that decides whether this worked

Doing the arithmetic is easy; proving it improved resolution is the actual work. None of
this requires new beamtime.

1. **Sub-pixel uniformity.** Histogram the fractional part of `x` and `y` within a pixel.
   A flat distribution means the resolution gain is real. Peaks at particular fractions mean
   we are seeing the discrete-cluster-shape artifact, and any apparent gain is partly
   fictitious. This is how the Timepix4 study bounded itself to 2×2 sub-pixel splitting, it
   is cheap, and it should gate everything else.
2. **Au 4f doublet.** The 4f₇/₂–4f₅/₂ splitting is a known 3.67 eV. Measure peak FWHM and the
   resolved splitting for each estimator against the `max_tot` control on the same raw file.
   This is our real figure of merit.
3. **Estimator sweep.** All of section 6 on one dataset, one table, same binning.
4. **Closure test.** Rebin centroided positions to 1 px and confirm the profile matches the
   raw pixel-hit profile. If it does not, the estimator is introducing a shift.
5. **`eps-t` re-confirmation** at production count rate, checking that mean cluster size and
   event count still converge and that we are not merging distinct electrons.
6. **Knife-edge MTF**, if we can arrange the measurement. The standard quantitative answer,
   and directly comparable to the published numbers in section 3.

---

## 9. Implementation notes

- **Scale.** A 546 MB `.tpx3` yields ~66.5M pixel hits; the `.h5` runs to a few GB. Read
  `PixelHits` in chunks, but chunk boundaries must not split a cluster — either read cluster-
  aligned ranges or buffer the trailing partial cluster into the next chunk.
- **Grouping.** With dense non-negative `cid`, everything is `np.bincount` with `weights=`.
  Sums, ToT-weighted sums and second moments all come out in a handful of O(N) passes. No
  Python-level loop over clusters, and no sort.
- **Guard against zero weight.** `sum_tot` can be 0 if every member ToT is 0; fall back to
  the unweighted mean rather than emitting NaN.
- **`gaussian_fit` is the one estimator that needs a per-cluster loop.** Keep it out of the
  default path and expect it to be orders of magnitude slower; it exists as a reference.
- Reference implementation sketch of the core reduction is in `ethan.md` §7.

---

## 10. Phasing

| Phase | Deliverable |
|---|---|
| 1 | Ingest + schema validation + `max_tot` control, reproducing ASI's `cx`/`cy` bit-for-bit. Proves the grouping is correct before any new maths. |
| 2 | `tot_weighted` and `mean`, full output schema, provenance attributes. |
| 3 | Validation harness: sub-pixel uniformity, closure test, estimator comparison table. |
| 4 | Au 4f figure of merit on production data; pick the default estimator on evidence. |
| 5 | Remaining estimators; CLI polish; hand the library API to `splash_timepix`. |

Phase 1 is worth doing properly. If we cannot reproduce ASI's answer exactly from our own
grouping, we do not understand the data well enough to improve on it.

---

## 11. Open questions and risks

- **Are we sure the big clusters are single flashes and not merged electrons?** The evidence
  says yes at the count rate of the test file (event count and mean size both converge as
  `eps-t` grows to 500 ns), but this must be rechecked at production rates. `sigma_x`/`sigma_y`
  in the output exist partly to detect it after the fact.
- **We depend on `tpx3dump`'s `cid`.** Its `eps-t` semantics are opaque — we observed hits
  32 ns apart grouped under a nominal 10 ns setting, and the 15.625 ns LSB makes small values
  ill-defined. Acceptable for v1; writing our own clustering is the fallback if the grouping
  turns out not to be what we want.
- **ToT nonlinearity near threshold is unquantified for our system.** This is what `tot_power`
  and `tot_offset` are for, but choosing `p` or `c` honestly needs a calibration we do not
  currently have.
- **Sub-threshold halo loss** biases spot size more than centre for a symmetric flash, but
  that symmetry is an assumption. Test 8.1 is the check.
- **Estimator for `t` on a phosphor flash is genuinely open.** `ctoa` is the defensible
  default; whether the leading edge beats it is an empirical question we should answer rather
  than argue.
