# Parquet output and Tiled — operator and analyst manual

`splash_timepix` writes every Timepix3 scan to parquet files **while the acquisition runs** and
registers the finished scan in a Tiled catalog, where its metadata is searchable. This manual
covers using the feature; the file format is in [format.md](format.md), the Tiled server
set-up in [tiled.md](tiled.md), and the design in `info/plans/parquet_streaming_tiled.md`.

1. [What you get](#1-what-you-get)
2. [Enabling and configuring](#2-enabling-and-configuring)
3. [Command line](#3-command-line)
4. [Reading the data](#4-reading-the-data)
5. [Tiled](#5-tiled)
6. [Troubleshooting](#6-troubleshooting)
7. [Acceptance checklist](#7-acceptance-checklist)

---

## 1. What you get

For every **Start** or **Replay** run (parquet on by default), next to the
`.tpx3` that Serval writes:

```
<output dir>/
  000042_2f1c9a7b_20261014T153012.tpx3            raw file (Serval), renamed to the slug at save
  000042_2f1c9a7b_20261014T153012_avg.csv/.png    averaged heatmap (UI)
  000042_2f1c9a7b_20261014T153012_energy.csv      energy axis (UI)
  000042_2f1c9a7b_20261014T153012_time.csv        time axis (UI)
  000042_2f1c9a7b_20261014T153012_meta.json       UI metadata, now incl. parquet dir/status
  2f1c9a7b-....-..../                             parquet scan directory, named by the scan uuid
    events/part-000000.parquet …                  x, y, tot, t   — every pixel hit on the wire
    tdc/part-000000.parquet …                     t, channel, edge
    control/part-000000.parquet …                 t, subtype     (shutter, heartbeat, timestamp)
    _scan_meta.json                               written last = the scan finished
    _tiled_registered.json                        written by the Tiled registration
```

- The parquet files are the **unbinned event record** of the scan in native detector ticks;
  the ZMQ live view (binned histograms) is unchanged. They are readable while the scan runs:
  a file appears every `parquet_roll_mb` (default 512 MB) and is complete the moment it has its
  final name.
- The `.tpx3` keeps being written by Serval and `tpx3dump`-based tools keep working. If a
  parquet scan is ever reported `incomplete`, the `.tpx3` is the recovery path (§6).
- Preview, Simulator and Alignment runs do not write parquet.
- Roughly 4–8 bytes per hit on disk (synthetic data compresses to ~4 B/hit; real data is
  expected higher). Nobody deletes anything automatically: the operator owns disk housekeeping,
  and the Operator tab shows free space in the Parquet row when it drops below 20 GB.

## 2. Enabling and configuring

**Operator tab**

- There is deliberately **no parquet switch in the UI**. Parquet output is on for every Start
  and Replay run unless `write_parquet` is set to `false` in the preferences file (below).
- *ESAF*, *Sample*, *Notes* — free text, stored in every parquet footer as `user.esaf`,
  `user.sample`, `user.notes` and searchable in Tiled. Fill them in before pressing Start.
- *Pipeline queues → Parquet* row during a run: `writing · 12.3M rows · 4 files`, plus
  `N dropped` in yellow if the writer fell behind, `error` in red if the disk failed.

**Preferences** (`~/.config/splash_timepix/operator_prefs.json`, editable by hand while the UI
is closed):

| Key | Default | Meaning |
|---|---|---|
| `write_parquet` | `true` | parquet output on/off for Start and Replay runs (no UI switch) |
| `parquet_roll_mb` | `512` | file roll size, 64–4096 MB |
| `tiled_uri` | `"http://localhost:8000"` | Tiled server; empty string turns registration off |
| `tiled_path` | `"tpx3"` | container under which scans are registered |

The Tiled **API key is never stored in preferences**. Set `TILED_API_KEY` in the environment
the UI runs in (see [tiled.md](tiled.md)).

**What happens during a run** (Start mode): the UI writes the operator fields to a hidden
metadata file in the output directory, launches `tpx-stream --parquet-dir <output dir>`,
starts `live-cli`, then `acq.py --meta-json <file>`, which snapshots the detector state from
Serval (type, chip IDs, Serval version, bias, thresholds, full config) into the same file just
before the acquisition starts. The sink embeds that file in every footer. When the streaming
server exits the UI deletes the metadata file and, three seconds later, registers the scan in
Tiled. At start-up the UI registers anything left unregistered in the output directory.

## 3. Command line

**Streaming server** (`tpx-stream`, what the UI launches):

```
--parquet-dir DIR             enable the parquet output under DIR/<scan_uuid>/
--parquet-roll-mb 512         file roll size (float; tests use 0.25)
--parquet-row-group-rows 1000000
--parquet-queue-max-rows 20000000   rows the writer may fall behind by (~14 B/row of memory)
--parquet-on-full drop|block  drop (default): never slows the live view, scan marked incomplete
                              block: never loses rows, may slow the ZMQ path
--parquet-source live|replay|simulator   provenance recorded in the files
--parquet-source-path PATH    the replayed .tpx3 (replay mode)
--parquet-meta-json PATH      JSON with `user` / `detector` objects for the footers
```

**Converting an existing `.tpx3`**: use the UI's *Replay* (live-cli cannot be scripted; steps
in [examples/convert_tpx3_by_replay.md](examples/convert_tpx3_by_replay.md)). The parquet
directory lands next to the `.tpx3`, with `source.kind = "replay"` and `source.path` set.

**Registration** (`tpx-tiled-register`; key from `TILED_API_KEY`):

```bash
tpx-tiled-register <output dir>/<scan_uuid>                 # one scan
tpx-tiled-register <scan_dir> --allow-incomplete            # crashed scan: footer-complete files only
tpx-tiled-register --unregistered --scan-root <output dir>  # catch-up: everything without a marker
tpx-tiled-register <scan_dir> --force --key my-run          # recreate under a chosen key
```

Exit codes: 0 ok · 1 failed (connection, key, read-back) · 2 refused (incomplete without the
flag, usage).

**Benchmark** (no detector needed; run on the data disk):

```bash
python tools/bench_parquet_sink.py --dir /data/bench --rate 40e6 --seconds 20
python tools/bench_parquet_sink.py --dir /data/bench --rate 40e6 --seconds 20 --no-sink
```

Prints bytes per hit, roll interval, writer throughput, queue high-water mark, drops, `offer()`
latency and CPU cores used.

## 4. Reading the data

```python
from splash_timepix.parquet_meta import open_scan, read_scan_metadata

scan = open_scan("/data/2f1c9a7b-...")          # three pyarrow datasets, lazily
summary = scan.metadata                          # ScanSummary (or None if never finished)
events = scan.read("events")                     # pyarrow.Table
t_s = scan.to_seconds(events["t"])               # exact clock from the footer (3840 MHz)
tot_ns = events["tot"].to_numpy() * 25           # 40 MHz ToT clock
```

Plain pandas: `pd.read_parquet("/data/2f1c9a7b-.../events")`. Dask, DuckDB and Tiled read the
same directories.

**Exposure windows** from the control table (subtype 0 = shutter open, 1 = shutter close):

```python
ctl = scan.read("control").to_pandas()
opens = ctl.loc[ctl.subtype == 0, "t"].to_numpy()
closes = ctl.loc[ctl.subtype == 1, "t"].to_numpy()
```

**Assign events to their TDC cycle** (rising edges on the channel the scan used):

```python
import numpy as np
md = summary
tdc = scan.read("tdc").to_pandas()
edges = tdc[(tdc.edge == 0) & ((md.tdc_channel == 0) | (tdc.channel == md.tdc_channel))]["t"].to_numpy()
t = events["t"].to_numpy()
cycle = np.searchsorted(edges, t, side="right") - 1     # -1 = before the first trigger
t_in_cycle_s = (t - edges[np.clip(cycle, 0, None)]) / md.clock_hz
```

**Time-range query without reading everything** — row-group statistics prune files and row
groups:

```python
import pyarrow.compute as pc
sub = scan.read("events", filter=(pc.field("t") >= t0) & (pc.field("t") < t1))
```

**A large scan through Dask**, with the file list from Tiled (§5):

```python
import dask.dataframe as dd
ddf = dd.read_parquet("/data/2f1c9a7b-.../events")      # one partition per file
```

**Every footer field**: `read_scan_metadata(path_to_one_file).model_dump()`.

## 5. Tiled

See [tiled.md](tiled.md) for the server. From a notebook:

```python
from tiled.client import from_uri
from tiled.queries import Key

c = from_uri("http://localhost:8000")["tpx3"]
c.search(Key("user.sample") == "Cu foil")
c.search(Key("detector.serval_version") == "3.3.2")
c.search(Key("t_first") <= t).search(Key("t_last") >= t)
scan = c["000042_2f1c9a7b_20261014T153012"]
scan.metadata["tables"]["events"]["num_rows"]
scan["events"].read_partition(0)
```

Tiled serves the files in place; `.read()` streams the whole table over HTTP, so for big scans
take the paths from `scan.include_data_sources()["events"].data_sources()[0].assets` (or
`scan.metadata["registration"]["scan_dir"]`) and read the files directly.

## 6. Troubleshooting

| Symptom | Meaning | What to do |
|---|---|---|
| Stop message / `_scan_meta.json` says `incomplete`, `dropped_rows > 0` | The writer fell behind and `--parquet-on-full drop` dropped batches; the live view was protected. | The `.tpx3` is complete: Replay it with parquet on to regenerate, then `tpx-tiled-register --force`. Consider a faster disk, a larger `--parquet-queue-max-rows`, or `--parquet-on-full block` for runs where parquet is the record. |
| `error` status, Parquet row red | An I/O error (disk full, permission); the sink gave up for this scan, the live view continued. | Check free space / permissions. Completed files are valid; `.tmp` files are not. Replay to regenerate. |
| `tpx-stream` refuses to start: "--parquet-dir … is not writable" | Preflight check. | Fix the directory or set `write_parquet` to `false` in the preferences file. |
| A `.part-*.parquet.tmp` file is left in a table directory | The process was killed while writing that file (or it is still running). | Ignore it: readers skip dot-files. Delete it once the scan is definitely over. No `_scan_meta.json` → the scan did not finish; register with `--allow-incomplete` if the completed files are useful. |
| `pandas.read_parquet` on an *empty* table directory fails | Should not happen: every table always has at least one (possibly empty) file. | Report it. |
| Log: "Tiled registration failed … Connection refused" | Tiled server down or wrong `tiled_uri`. | Files are intact. Start the service; the UI catch-up at the next start, or `tpx-tiled-register --unregistered`, registers it. |
| Registration fails with 401 / "not authenticated" | `TILED_API_KEY` missing or wrong in the UI's environment. | Load `~/.config/splash_timepix/tiled.env` before launching (tiled.md). |
| "registered, but Tiled cannot read events … --read" | The scan directory is outside the server's `--read` locations. | Add the output directory to the service's `--read` and restart it; re-register with `--force`. |
| Searches find the scan but `.read()` is slow | Expected for large tables over HTTP. | Read the files directly (§4, §5). |

## 7. Acceptance checklist

To be run once on the acquisition PC with the real detector after each release; record the
result in `acceptance/<date>.md` (date, software version, `schema_version`, measured bytes per
hit and disk bandwidth, pass/fail per step).

| # | Step | Expected |
|---|---|---|
| 1 | Tiled service running (`systemctl --user status tiled-catalog`); `TILED_API_KEY` set in the UI's environment | active; `echo $TILED_API_KEY` non-empty in the launcher shell |
| 2 | Operator tab: ESAF/Sample/Notes filled; press **Start** at the highest count rate used in operation | Parquet row shows `writing`, rows increasing, 0 dropped |
| 3 | While acquiring, in a terminal: `pd.read_parquet("<output>/<uuid>/events")` after the first roll | Returns rows; a `.part-*.tmp` is present alongside |
| 4 | Stop. Check `_scan_meta.json`, the Logs tab stop line, and the `_meta.json` `parquet` block | `status: complete`, `parquet.dir` equals the scan directory |
| 5 | `tables.events.num_rows` vs `tpx3dump` pixel count of the renamed `.tpx3` | equal (TDC/control counted separately) |
| 6 | Logs tab: "Tiled: created tpx3/<slug> (complete)"; notebook: `search(Key("user.sample") == …)` | finds the scan; `scan["events"].read_partition(0)` returns a DataFrame with uint16/uint64 dtypes |
| 7 | `search(Key("detector.serval_version") == …)` and `Key("detector.bias_voltage")` present | detector snapshot came through `acq.py` |
| 8 | Restart the Tiled service; repeat step 6 | still found (SQLite catalog) |
| 9 | Live view and Latent Space Explorer during steps 2–4 | behave exactly as before parquet existed |
| 10 | `tools/bench_parquet_sink.py --dir <output> --rate <peak>` and the Parquet row during step 2 | bytes/hit and MB/s recorded; no drops at the peak rate → parquet stays on by default |
| 11 | Replay the `.tpx3` from step 4 with parquet on (examples/convert_tpx3_by_replay.md) | a second scan directory with `source.kind = "replay"` and the same `events` row count |
| 12 | Stop the Tiled service, run one short scan, start the service, restart the UI | catch-up registers the scan (Logs: "catch-up: 1 registered") |
