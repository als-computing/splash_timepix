# Parquet scan format — `schema_version` 1

Reference for the parquet files `tpx-stream` writes during an acquisition. The code that
defines this format is `src/splash_timepix/parquet_meta.py` (schemas, metadata model, reader
helpers) and `src/splash_timepix/parquet_sink.py` (the writer). The design rationale is in
`info/plans/parquet_streaming_tiled.md` §6–§7; this page is the contract analysts can rely on.

## Layout

One directory per scan, named by the scan's UUID (the `scan_name` of the ZMQ start/stop
messages), one sub-directory per table:

```
<output_dir>/<scan_uuid>/
  events/part-000000.parquet, part-000001.parquet, …
  tdc/part-000000.parquet, …
  control/part-000000.parquet, …
  _scan_meta.json                 written last; its presence means the scan finished
  _tiled_registered.json          written by tpx-tiled-register after a successful registration
```

- A table directory is a valid parquet dataset on its own:
  `pandas.read_parquet("<scan>/events")`, `pyarrow.dataset.dataset("<scan>/events")`,
  `dask.dataframe.read_parquet`, DuckDB and Tiled all read it without a file list.
- Files are written as `.part-NNNNNN.parquet.tmp` and renamed when complete. Readers skip names
  that start with `.` or `_`, so a directory is readable at any moment during the scan and a
  crash leaves at most one `.tmp` file, which is simply ignored.
- Every table has at least one file, even when it is empty (a schema-only file), so every
  table directory is always a dataset.
- Files roll at `--parquet-roll-mb` (default 512 MB), each table on its own size: `tdc` and
  `control` are tiny and usually have one file while `events` has many. Partition indices do
  **not** line up across tables; use the `t` statistics in each footer for time alignment.

## Tables

All integers are little-endian; no column is nullable.

| Table | Column | Type | Meaning |
|---|---|---|---|
| `events` | `x` | uint16 | pixel column, 0–255 on a single Timepix3 chip |
| | `y` | uint16 | pixel row |
| | `tot` | uint16 | time over threshold in **25 ns ticks** (10-bit value, 0–1023) |
| | `t` | uint64 | time of arrival in **3840 MHz ticks** (56-bit value, already extended by `live-cli`) |
| `tdc` | `t` | uint64 | TDC edge time, same clock as `events.t` |
| | `channel` | uint8 | TDC channel, 1 or 2 |
| | `edge` | uint8 | 0 = rising, 1 = falling |
| `control` | `t` | uint64 | control packet time, same clock |
| | `subtype` | uint8 | 0 = shutter open, 1 = shutter close, 2 = heartbeat, 3 = timestamp |

Timestamps are stored in native ticks, never as floats, so nothing is rounded. Convert with the
exact clocks:

```python
t_seconds = t / 3_840_000_000          # 260.4166… ps per tick
tot_ns    = tot * 25                    # 40 MHz ToT clock
```

The clocks are also attached as Arrow field metadata (`unit="tick"`, `clock_hz="3840000000"`
on `t`; `clock_hz="40000000"` on `tot`) and repeated in the JSON footer block, so a file is
self-describing. Rows are in arrival order; `live-cli` sorts by time before sending, and every
file records whether its `t` column was monotonic (`t_monotonic`). Parquet row-group statistics
(min/max of `t`) are written, so time-range queries skip row groups and whole files.

Column names are lower-case and the units are ours. NSLS-II's `tpx3awkward` uses `ToT` in ns
and `t` in 1.5625 ns units; the names differ here on purpose so the units cannot be confused.

## Footer metadata

Every parquet file carries one JSON document under the key `splash_timepix` in the parquet
key/value metadata. It is the serialised `ParquetScanMetadata` model and validates against it
(`read_file_metadata(path)`). Numbers are JSON numbers, so they stay numbers when the
registration tool copies them into Tiled.

Per-scan fields (identical in every file of the scan):

| Key | Type | Meaning |
|---|---|---|
| `schema_version` | int | this document: `1` |
| `scan_name` | str | uuid4, equals the directory name and the ZMQ `scan_name` |
| `start_time_iso` | str | UTC time the TCP client connected |
| `mode` | `timing` \| `alignment` | acquisition mode of `tpx-stream` |
| `tdc_frequency_hz`, `tdc_channel`, `tdc_edge`, `t_delta_ns`, `t_cycle_ns`, `n_bins`, `collapse_y` | | the live-view binning settings `tpx-stream` ran with |
| `detector_size_x`, `detector_size_y` | int | 256 × 256 |
| `clock_hz`, `tot_clock_hz` | int | 3840000000, 40000000 |
| `splash_timepix_version`, `pyarrow_version`, `hostname` | str | provenance |
| `writer_config` | object | `roll_bytes`, `roll_seconds`, `row_group_rows`, `compression`, `encodings`, `on_full` |
| `source` | object | `{"kind": "live" \| "replay" \| "simulator", "path": …}` |
| `user` | object | operator-entered fields from the UI (`sample`, `esaf`, `notes`); may be empty |
| `detector` | object | Serval snapshot taken by `acq.py` just before the acquisition starts: `/dashboard`, `/detector/config`, `/detector/chips`; empty for replay and simulator runs |

Per-file fields:

| Key | Type | Meaning |
|---|---|---|
| `table` | `events` \| `tdc` \| `control` | |
| `file_index` | int | the `NNNNNN` in the file name |
| `num_rows` | int | rows in this file |
| `t_first`, `t_last` | int \| null | min/max of `t` in this file (ticks); null for an empty file |
| `t_first_s`, `t_last_s` | float \| null | the same in seconds, for clients that cannot hold 64-bit integers (JavaScript) |
| `t_monotonic` | bool \| null | whether `t` never decreased within the file |
| `first_batch_wall_time_iso` | str \| null | host clock when the first batch of the scan arrived; with `t_first` of the first file this anchors ticks to wall clock |
| `dropped_rows_so_far` | int | rows the sink had dropped (queue full) when this file closed |

## `_scan_meta.json`

Written atomically after the last file is closed. It repeats the per-scan fields and adds:

| Key | Meaning |
|---|---|
| `status` | `complete` — every offered row was written; `incomplete` — rows were dropped (`on_full=drop`) or the stop timed out; `error` — the writer hit an I/O error and gave up |
| `stop_time_iso`, `acquisition_duration_s` | when and how long |
| `tables.<name>.num_rows`, `.num_files`, `.files`, `.t_first`, `.t_last` | per-table totals |
| `dropped_rows`, `dropped_batches`, `error` | what went wrong, if anything |
| `stop_message` | the ZMQ `TimePixStop` fields. **`total_packets` there counts binned pixels** (those inside a TDC window) and will not equal `tables.events.num_rows`, which counts every pixel on the wire |

If the file is missing, the scan did not finish (crash, kill): the completed files are still
valid, and `read_scan_summary(dir)` rebuilds a summary from their footers with
`status="incomplete"`.

## Reading

```python
from splash_timepix.parquet_meta import open_scan, read_scan_metadata

scan = open_scan("/data/2f1c…")           # the three tables as pyarrow datasets
events = scan.read("events")               # pyarrow.Table; columns=[...] and filter= accepted
t_s = scan.to_seconds(events["t"])         # float seconds, exact clock from the footer
meta = read_scan_metadata("/data/2f1c…")   # ScanSummary (directory) or ParquetScanMetadata (file)
```

Plain pandas works too: `pd.read_parquet("/data/2f1c…/events")`.

## Versioning

`schema_version` increments on any incompatible change to the tables or the footer document.
Readers refuse a version they do not know with a clear error. The package version that
introduced each schema version is recorded in `CHANGELOG.md`.
