# PLAN — streaming parquet output and Tiled registration

Design document for writing Timepix3 event data to parquet files *during* acquisition and
registering those files, with their metadata, in a Tiled catalog. The writer lives inside
`tpx-stream` (`splash_timepix.app`) beside the existing ZMQ path; the Tiled registration is a
separate small tool the UI calls when a scan finishes. Everything is native to this repository:
`pyarrow` (parquet) and the `tiled` client are ordinary library dependencies, in the same way
`h5py` is today, and nothing at runtime depends on NSLS-II's `tpx3awkward` or `tpx3_pipeline`.

---

## 1. Purpose and success criteria

The criteria this work is measured against, as stated by the group lead:

| # | Criterion | Where this plan satisfies it |
|---|---|---|
| 1 | Data streaming of Timepix3 into parquet files (during acquisition, not post-hoc) | `ParquetSink` inside `tpx-stream`, §6 |
| 2 | Tiled is able to read the files | Plain, standard parquet in a layout Tiled's built-in parquet adapter already understands, §7–§8 |
| 3 | Metadata is taken out of the files and put into Tiled's database | `tpx-tiled-register` reads the parquet footers and registers that metadata on the node, §8 |

Non-goals, deliberately: a Prefect flow (the registration tool is shaped so one can call it
later), live visibility *inside Tiled* while the scan runs (§11, only if wanted), and replacing
the raw `.tpx3` — Serval keeps writing it and `tpx3dump`-based tools keep working.

---

## 2. Why parquet, why streaming, why inside `tpx-stream`

**Why parquet.** Today the only event-level record is the raw `.tpx3`, and the only way to
analyse it is `tpx3dump → .h5`. `.h5` is a fine container but the wrong shape for an event list:
every read pulls whole datasets, there is no column pruning, no predicate pushdown, no natural
partitioning. Parquet is columnar, compressed per column, carries min/max statistics per row
group, and a directory of parquet files is exactly what Dask and Tiled treat as one table. Since
`live-cli` delivers events time-sorted, each file covers a contiguous time range, so "events
between t₁ and t₂" skips whole files without opening them.

**Why streaming.** Converting once, while the data is already decoded in memory, is cheaper
than re-reading raw later (NSLS-II measured a 30 MB `.tpx3` at ~400 ms to load vs ~200 ms to
process). More importantly, completed files are readable before the scan ends, and a crash loses
at most one buffer instead of the whole scan.

**Who writes what.** Serval (ASI) writes the `.tpx3`; `acq.py:84-91` tells it to send its raw
stream to two destinations, a TCP socket consumed by `live-cli` and a directory. That does not
change — after this work every scan has *both* the `.tpx3` and the parquet files. We do not add
anything to ASI's software (Serval and `live-cli` are closed binaries); we add a second output to
our own `tpx-stream`, which already receives every decoded event.

**Why in-process.** The decoded events already exist as NumPy arrays in `data_callback`
(`src/splash_timepix/app.py:330`); `live-cli` has done the decoding, time-sorting and timestamp
extension for us. Inside `tpx-stream` the scan metadata is a dictionary we already build for the
ZMQ start message. The alternatives, and why they lost:

| Option | Verdict |
|---|---|
| Convert the finished `.tpx3` after the scan | Simplest, and useful as a backfill tool for existing files — but it is not *streaming*, so it fails criterion 1 on its own. We get it for free anyway via replay mode. |
| Tail the `.tpx3` while Serval writes it | Streaming, no change to our software, but fragile: partial-chunk handling, end-of-file detection, heartbeat state across chunks. Nothing it offers that the next option does not. |
| Standalone daemon fed by a third Serval raw destination (NSLS-II's shape) | Clean separation, but needs a second decoder *and* a side channel for scan UUID and TDC/binning metadata (NSLS-II use EPICS; we have none). Right choice only if sharing code with NSLS-II outweighs the fastest route to the criteria. |
| Sink inside `tpx-stream` (this plan) | Reuses `live-cli`'s decoding, owns the metadata, one process for the UI to supervise. |

**What stays as it is.** ZMQ remains the live view: ~1 s latency, binned histograms, consumed by
the browser UI and the Latent Space Explorer. Parquet is the durable, unbinned record. They are
different data products, not competitors. Post-hoc conversion of existing `.tpx3` files is not a
deliverable, but it falls out for free: replay mode streams a `.tpx3` through `live-cli` down the
same TCP path, so replaying a file with parquet enabled converts it.

---

## 3. Input contract — what exists today

- `live-cli` → TCP → `SocketDataServer` (`src/splash_timepix/socket_server.py`). 12-byte packets
  with **56-bit timestamps at 3840 MHz** (260.4167 ps per tick), already time-sorted and already
  extended by `live-cli`. 2⁵⁶ ticks ≈ 217 days, so no rollover handling is needed in practice;
  we still record whether each row group was monotonic (§7).
- `_data_processor` (`socket_server.py:335-377`) parses a batch with
  `PacketParser.parse_batch` into a `BatchParseResult` (`src/splash_timepix/parser.py:121-175`):
  `pixel_x/y/tot` (int32), `pixel_timestamp` (uint64), `tdc_timestamp/channel/edge`,
  `control_timestamp/subtype`, plus `*_indices` for ordering. All arrays are copies (`astype`,
  mask indexing), not views of the TCP buffer. One inconsistency to code around:
  `_empty_result` returns int64 timestamps (`parser.py:418,429`) where `parse_batch` returns
  uint64; the sink coerces every column explicitly.
- `data_callback` is called synchronously on the processor thread; it bins into the histogram
  and `emit_flush_if_due` hands arrays to `zmq_worker` via `xyt_queue`.
- Scan lifecycle: `scan_name` (uuid4) is assigned only in the main loop's client-connect branch
  (`app.py:~910-968`), which also resets counters and queues `TimePixStart`. There are three stop
  sites (`app.py:~871-907`, `~973-1010`, the `finally` at `~1149-1171`) that each build
  `TimePixStop` (`src/splash_timepix/schemas.py:66`). Heartbeat stats come from
  `_queue_stats_for_heartbeat` (`app.py:183-196`).
- UI: `start_streaming_server` (`src/splash_timepix/ui/workers.py:337-375`) launches
  `python -m splash_timepix.app` and does **not** pass the output directory. `_save_on_stop`
  (`src/splash_timepix/ui/main.py:417-488`) writes CSV/PNG/`_meta.json` with the slug
  `{counter:06d}_{uuid8}_{YYYYMMDDTHHMMSS}` and renames the newest `.tpx3` to it.

---

## 4. Architecture

```
live-cli ──TCP──> SocketDataServer ──BatchParseResult──> data_callback ──> histogram ──> xyt_queue ──> zmq_worker ──> ZMQ  (live view, unchanged)
                                                              │
                                                              └── sink.offer(result)      never raises; never blocks with the default on_full=drop
                                                                        │
                                                              ParquetSink writer thread      ── finished file ──> closer thread
                                                                        │   row groups; roll at size                  fsync → os.replace → fsync(dir)
                                                                        ▼
                      <parquet_dir>/<scan_uuid>/ events/part-000000.parquet …  tdc/part-000000.parquet …  control/…  _scan_meta.json
                                                                        │
                      UI, when the streaming process exits:   tpx-tiled-register <scan_dir> ──HTTP──> Tiled catalog
                                                                                                        (SQLite catalog served on the
                                                                                                         acquisition PC, localhost:8000)
```

Three properties the diagram encodes:

1. **The ZMQ path cannot be slowed** (with the default `drop` policy, §6.1; the operator can
   choose `block` and accept the opposite trade). `offer()` is a non-blocking enqueue; everything heavy
   happens on the writer thread, and the heavy parts (`np.concatenate`, Arrow encoding, zstd)
   release the GIL in C. Closing a finished file (`fsync` of hundreds of MB can take longer than
   the queue can absorb at peak rate) happens on a separate closer thread, so the writer opens
   the next file immediately.
2. **Readers never see a partial file, and a directory is always readable as a table.** A
   parquet file's footer is written at close, so an open file is unreadable by design; we write
   `.part-000000.parquet.tmp` (leading dot), `fsync`, `os.replace` to `part-000000.parquet`,
   then `fsync` the directory so the rename itself is durable. Each table has its own
   subdirectory and the sidecar is `_scan_meta.json`: pyarrow, Dask and DuckDB skip files that
   start with `.` or `_`, so `pandas.read_parquet("<scan>/events")` works at any moment during
   the scan.
3. **The directory is owned by `tpx-stream` for its whole life.** It is named by the scan UUID
   and never renamed. The human-readable slug lives in Tiled (as the node key) and in the UI's
   `_meta.json`, which also records the directory path.

---

## 5. Phase 0 — pre-existing defects the sink would expose

Reviewing the lifecycle turned up three defects that exist today and would bite a file writer
harder than they bite the ZMQ path. They are fixed first, as a separate PR.

- **`task_done()` is skipped when `data_callback` raises.** In `_data_processor`
  (`socket_server.py:366-375`) the exception handler logs but never calls `task_done()`, so
  `unfinished_tasks` stays >0 and every subsequent `wait_for_idle` blocks its full 5 s. Fix: move
  `task_done()` into a `finally`.
- **Events arrive before the scan exists.** `client_connected = True` is set at
  `socket_server.py:235` and batches flow to `data_callback` immediately, but `scan_name`,
  the counter reset and `TimePixStart` happen in the main loop's 1 s poll — and not at all
  during the 2 s `wait_after_start` sleep (`app.py:838-840`). Up to a few seconds of data reach
  the callback with no scan. A connect→disconnect→connect inside one tick is also missed. Fix:
  add `on_client_connect` / `on_client_disconnect` callbacks to `SocketDataServer` (fired at
  `:235` and `:238-239`) and move scan start/stop bookkeeping into them.
- **No SIGTERM handler.** The UI `terminate()`s the streaming process and `kill()`s it 5 s later
  (`ui/workers.py:455-458`; 6 s safety net at `ui/main.py:412`). Today that just drops a stop
  message; with a writer it corrupts the file being closed. Fix: SIGTERM → `KeyboardInterrupt`
  so the existing `finally` path runs. In that `finally`, call `server.stop()` *before*
  `sink.stop()` — the client may still be connected, and `wait_for_idle` is not quiescence
  (`socket_server.py:196-199`). This is Linux-only by nature: on Windows `terminate()` is a
  hard kill with no signal, which is acceptable because the acquisition PC runs Linux.

The move of the existing tests into `tests/{core,streaming,ui}/` (§9) lands as its own no-op
commit *before* these fixes, so the Phase 0 diff shows behaviour changes only, not renames.

---

## 6. Phase 1 — `ParquetSink`

New module `src/splash_timepix/parquet_sink.py`.

### 6.1 API

```python
sink = ParquetSink(parquet_dir, roll_bytes=512 * 2**20, row_group_rows=1_000_000,
                   queue_max_rows=20_000_000, on_full="drop")
sink.start(scan_name, metadata)      # creates <parquet_dir>/<scan_name>/{events,tdc,control}/, opens writers
sink.offer(result)                   # BatchParseResult; non-blocking; safe before start() and after stop()
sink.stop(final_counts, timeout=30)  # final_counts = the TimePixStop fields; drain, close, write _scan_meta.json, join
sink.stats()                         # rows/files/dropped/queue depth/status, for the heartbeat
```

`offer()` coerces the columns to their on-disk dtypes (`x, y, tot` → uint16, timestamps →
uint64; a cheap `astype` on the processor thread) and enqueues only those — not `*_indices`, and
not the int32 originals, so the memory bound below is honest. The queue is bounded by **rows**
rather than batch count because batch size varies from one packet (the 0.1 s partial flush in
`socket_server.py:306-319`) to `callback_batch_size`. 20 M rows × 14 B ≈ 280 MB worst case.
Batches that arrive before `start()` are held in a bounded pending list — belt and braces with
the Phase 0 callbacks. `start()`/`stop()` run on the main thread while `offer()` runs on the
processor thread, so state changes happen under a lock (an atomic swap of the active writer
set); a batch can never be offered to a writer that is mid-close.

**Policy on a full queue** (`on_full`, CLI `--parquet-on-full`):

- `drop` (default): drop the batch, increment `dropped_batches`/`dropped_rows`, log once per
  few seconds, and mark the scan `incomplete`. The live view is never slowed.
- `block`: block `offer()` until there is room, which slows the ZMQ path. For an experiment
  where the parquet is the record this is the right trade; the operator chooses.

In either case the `.tpx3` written by Serval is complete, so an `incomplete` scan can be
regenerated losslessly: replay the `.tpx3` with parquet enabled (§2, §9) and re-register. The
plan does not try to make the sink infallible; it makes the failure visible and recoverable.

The whole body of `offer()` is wrapped so it can never raise into `data_callback`. On any
`OSError` (disk full, permission) the sink closes what it can, sets status `error`, and turns
`offer()` into a counted no-op for the rest of the scan.

### 6.2 Writer thread

- `queue.get(timeout=0.5)` so time-based flushes fire during silence.
- Per table, append arrays to a list; write a row group when pending ≥ `row_group_rows`, or
  (≥ 65 k rows and ≥ 1 s since the last write), or ≥ 10 s regardless. One `np.concatenate`, one
  `pa.Table.from_arrays`, one `write_table` per row group — no per-batch Arrow work.
- We open the file ourselves (`f = open(tmp, "wb")`, `pq.ParquetWriter(f, schema, …)`) so
  `f.tell()` is the exact byte count for the roll decision. When a file rolls, the writer hands
  the open `ParquetWriter` to the **closer thread** and opens the next file at once; the closer
  writes the footer, `f.flush(); os.fsync(f.fileno())`, `os.replace(tmp, final)`, then
  `os.fsync` on the directory. fsync at close only, never per row group. The closer is a
  one-slot hand-off: if the previous close has not finished when the next roll comes, the writer
  waits — at that point the disk is genuinely too slow and the queue is the right place to feel it.
- **Each table rolls on its own byte size.** `tdc` and `control` are orders of magnitude
  smaller than `events`; rolling them on the events trigger would triple the file count with
  near-empty files. Time alignment between tables comes from the `t` min/max statistics in
  every footer, not from matching partition indices.
- File names are `part-{index:06d}.parquet`. Six digits because at peak rate a 512 MB file
  rolls every few seconds and four digits would be exhausted within a shift.
- Default roll size **512 MB**. At full rate (~40 Mhit/s, ~14 B/hit raw) a 200 MB roll fires
  every one to two seconds and produces thousands of files per hour; 512 MB–1 GB is the Dask
  sweet spot. The roll check is `bytes >= roll_bytes or seconds >= roll_seconds` from day one,
  with `roll_seconds=None` in Phase 1, so Phase 5's time-based roll is a configuration change,
  not a redesign.
- Encoding: `compression="zstd"`, dictionary encoding on `x, y, tot`, `DELTA_BINARY_PACKED` on
  `t` (sorted input makes deltas tiny; expect `t` to shrink from 8 B to 1–2 B per row, roughly
  halving disk bandwidth). In pyarrow this is the list form `use_dictionary=["x", "y", "tot"]`
  together with `column_encoding={"t": "DELTA_BINARY_PACKED"}`; the exact flags are part of
  the §13 spike.
- **Per-file footer values are written at close.** `t_first`, `t_last`, `num_rows`,
  `t_monotonic` and `dropped_rows_so_far` (§7) are unknown when the file is opened, so the
  closer adds them with `ParquetWriter.add_key_value_metadata()` (pyarrow ≥ 15) just before the
  footer; the per-scan part of the KV block, including the `--parquet-meta-json` content (§6.3),
  goes into the same call, so nothing has to be known when a file is opened. `t_monotonic`
  is `np.all(np.diff(t) >= 0)` across every row group of the file. `sorting_columns` is **not**
  declared in Phase 1: in pyarrow it is a writer-level flag that applies to every row group of
  the file and must be chosen before the first row is written, so it cannot be set honestly
  after the fact. Consumers get time locality from the `t` row-group statistics that parquet
  writes regardless; whether `live-cli` is monotonic enough to declare it unconditionally is a
  §13 question.
- `stop()` drains the queue completely (bounded by `timeout`), closes writers through the
  closer, writes `_scan_meta.json` atomically, joins both threads. The atomic JSON write reuses
  the existing tmp → fsync → `os.replace` helper in `ui/preferences.py:319-357` (with its tmpfs
  fallback), lifted into a shared module rather than copied.

### 6.3 Wiring into `tpx-stream`

- Flags: `--parquet-dir PATH` (sink disabled when absent), `--parquet-roll-mb 512`,
  `--parquet-row-group-rows 1000000`, `--parquet-on-full drop|block`, `--parquet-source`
  (§7) and `--parquet-meta-json PATH` — a JSON file the sink embeds in every footer. It
  carries two sub-dicts written by two different processes: `user`, the operator-entered
  values (sample, proposal, notes), written by the UI before it launches `tpx-stream`; and
  `detector`, a snapshot of Serval's `/dashboard`, `/detector/config` and `/detector/chips`
  taken with the existing `ServalClient` (`serval_client/lib.py:55-74,125-134`) — detector
  type, chip IDs, Serval version, bias and thresholds — merged into the same file by `acq.py`
  right after it has loaded the pixel configuration and DACs and applied the trigger settings
  (`serval_client/acq.py:59-72`), because that is the first moment the values are the ones the
  scan actually runs with. The sink therefore reads the file lazily, on the first row-group
  write, and caches it: data cannot flow before `acq.py` has started the acquisition, so the
  file is complete by then. In replay and simulator runs `acq.py` never runs and `detector` is
  absent. `tpx-stream` has no other way to learn any of this, and without it the metadata
  reaching Tiled is purely instrumental. Startup refuses to run if `--parquet-dir` is not
  writable, and `shutil.disk_usage` below a threshold is surfaced in the heartbeat.
- `sink.offer(result)` is the first line of both `data_callback` and
  `data_callback_alignment`. Alignment mode ignores TDCs in its binning; the sink does not.
- `sink.start()` in the connect callback with `scan_dir = parquet_dir / scan_name`;
  `sink.stop()` at every stop site after `wait_for_idle` (and after `server.stop()` in
  `finally`), unconditionally — not under the `start_message_sent` guard.
- Heartbeat gains `parquet_rows`, `parquet_files`, `parquet_dropped`, `parquet_q_rows`,
  `parquet_q_max`, `parquet_status` (`heartbeat.py:172-176` merges extra keys);
  `HeartbeatStatus` in `ui/workers.py` is extended so the Engineering tab can show them.
- `TimePixStop` gains optional `parquet_dir`, `parquet_status`, `parquet_rows` and
  `parquet_dropped_rows` (all backward compatible). `parquet_dir` is how the UI learns
  *exactly* which directory belongs to the run — never "the newest one"; the other three let it
  warn the operator about an `incomplete` scan the moment it stops.
- The UI passes `--parquet-dir` only in `start` and `replay` modes (preview, alignment and the
  simulator are throwaway).
- **Default state: on, with three independent switches.** Parquet is written by default
  alongside the `.tpx3`; both are outputs of every scan from the first release. Each layer can
  be turned off without touching the others and without a code change:

  | Layer | Off when | Effect |
  |---|---|---|
  | Parquet writing | UI "Write parquet" unchecked (`write_parquet = false`), or `--parquet-dir` omitted on the command line | `tpx-stream` behaves exactly as today; only the `.tpx3` is written |
  | Tiled registration | `tiled_uri` preference cleared (it ships as `http://localhost:8000`, §8.3) | parquet files are written and usable with pandas/Dask; nothing is registered, nothing is attempted |
  | Catch-up at UI startup | follows `tiled_uri`; also `--no-catch-up` on the UI command line for a quick start | scans without `_tiled_registered.json` stay unregistered until `tpx-tiled-register --unregistered` is run by hand |

  Dropping the `.tpx3` later is a separate decision (§13): it needs the parquet path proven
  over months, because today the `.tpx3` is both the `tpx3dump` input and the recovery path for
  an `incomplete` scan.

### 6.4 Implementation notes (Phase 1, as built)

Where the code departs from the text above, the code is right and this records why:

- `--parquet-roll-mb` is a float (tests roll at 0.25 MB; the UI preference stays an integer
  clamped to 64–4096) and `--parquet-queue-max-rows` exists as a flag so the drop/block
  policies can be exercised end to end.
- After a roll the writer opens the next file **immediately**, so a `.tmp` exists whenever a
  scan is in progress; an eagerly opened file that never receives a row is discarded at stop
  rather than left as an empty trailing part. A table that never received any row still gets
  one schema-only file, so every table directory is a valid dataset.
- The roll check runs after each row-group write; between writes a file can exceed
  `roll_bytes` by at most one row group.
- The metadata model is split into `ScanFields` (per scan), `ParquetScanMetadata` (per file,
  the footer document) and `ScanSummary` (`_scan_meta.json`); `read_scan_metadata()` returns
  one of the latter two depending on whether it is given a file or a directory, and a missing
  sidecar yields a summary rebuilt from the footers with `status="incomplete"`.
- The scan lifecycle in `app.py` tracks `scan_open` rather than relying on
  `start_message_sent`, so `sink.stop()` runs for every scan — including `--plot` runs that
  publish nothing on ZMQ.
- The "does not slow ZMQ" test compares the server's **CPU time per wire packet** with and
  without the sink, not `total_cycles`: the in-process simulator paces itself by wall clock and
  the packet count it manages in a fixed window varies by 2× between runs, independent of the
  server. Measured on the development machine: 24–33 µs per packet in both configurations;
  the sink itself costs ~12 ns per hit in `offer()` and the whole process ran at 0.32 cores
  while writing 2 Mhit/s at 3.8 B/hit (`tools/bench_parquet_sink.py`). No writer-thread
  operation holds the GIL for longer than the idle baseline (0.7 ms), so the sink cannot starve
  the TCP reader.
- The ZMQ worker now publishes pending control messages before an event it has just taken
  from the queue (a start queued while the worker was blocked could previously be overtaken by
  the first flush), and `_wait_for_xyt_drain` waits on `unfinished_tasks` rather than
  `empty()` so a stop is never queued before the last event has actually been sent.

---

## 7. Output schema

One directory per scan, `<parquet_dir>/<scan_uuid>/`, one subdirectory per table:

| Path | Columns | Notes |
|---|---|---|
| `events/part-NNNNNN.parquet` | `x:uint16, y:uint16, tot:uint16, t:uint64` | `tot` in 25 ns ticks (10-bit, `parser.py:371`); `t` in 3840 MHz ticks |
| `tdc/part-NNNNNN.parquet` | `t:uint64, channel:uint8, edge:uint8` | one row per TDC edge, tiny |
| `control/part-NNNNNN.parquet` | `t:uint64, subtype:uint8` | shutter open/close, heartbeat, timestamp — the only post-hoc way to find exposure windows |
| `_scan_meta.json` | — | completion marker and cross-check, written last |
| `_tiled_registered.json` | — | written by `tpx-tiled-register` on success (§8.3) |

A table directory is a valid parquet dataset on its own: `pandas.read_parquet("<scan>/events")`,
`dask.dataframe.read_parquet`, DuckDB and Tiled all read it without a file list, and the
leading `.`/`_` on temporary and sidecar files keeps them out of discovery (§4).

Decisions baked in:

- **Why three tables, and why many files per table.** The three packet kinds have different
  columns (`x, y, tot` / `channel, edge` / `subtype`) and rates that differ by four to six
  orders of magnitude; one table would need nullable columns on every row and would bury a few
  hundred TDC edges under tens of millions of events. Separate tables are also what NSLS-II
  settled on (§14). Many files rather than one growing file because a parquet file is only
  readable once its footer is written at close: one file per scan would be unreadable until the
  scan ends and lost entirely on a crash, whereas rolling at 512 MB makes every completed file
  readable immediately and bounds the loss to the file being written. A directory of files is
  still one table to pandas, Dask, DuckDB and Tiled, so the consumer never notices the split.
- **Native ticks, no float `t_ns` column.** Units go in Arrow field metadata
  (`unit="tick"`, `clock_hz=3840000000`) and consumers compute `t * 1e9 / clock_hz`. We store
  the exact integers `clock_hz=3840000000` and `tot_clock_hz=40000000`, **not** the truncated
  `TIMESTAMP_PS_PER_TICK = 260.41666` from `parser.py:26` — that constant is 2.6×10⁻⁸ off,
  ~0.5 s over a full 56-bit epoch.
- **Lower-case names, our units.** NSLS-II use `ToT` in ns and `t` in 1.5625 ns units. Same
  column name with a different unit is the worse footgun; renaming is trivial for a consumer,
  silent unit confusion is not.
- **File-level metadata is one JSON document** under the key `splash_timepix` in the parquet
  key/value block, so numbers stay numbers when they reach Tiled search. It is produced by
  dumping a pydantic model (`ParquetScanMetadata`, the same pattern as `schemas.py`), which is
  also the schema the registration tool validates against. Contents:

  | Key | Type | Source | In every file? |
  |---|---|---|---|
  | `schema_version` | int | constant, starts at 1 | yes |
  | `scan_name` | str (uuid4) | `tpx-stream` connect callback | yes |
  | `table` | `events \| tdc \| control` | sink | yes |
  | `file_index` | int | sink | yes |
  | `start_time_iso` | str (UTC) | connect callback | yes |
  | `mode` | `timing \| alignment` | `--alignment` | yes |
  | `tdc_frequency_hz`, `tdc_channel`, `tdc_edge`, `t_delta_ns`, `t_cycle_ns`, `n_bins`, `collapse_y` | float/int/str/bool | `tpx-stream` CLI flags (`static_metadata`, `app.py:228-241`) | yes |
  | `detector_size_x`, `detector_size_y` | int | `SimulatorConfig` defaults today (256×256) | yes |
  | `clock_hz` = 3840000000, `tot_clock_hz` = 40000000 | int | constants | yes |
  | `splash_timepix_version`, `pyarrow_version`, `hostname` | str | `importlib.metadata`, `socket.gethostname()` | yes |
  | `t_first`, `t_last`, `t_monotonic`, `num_rows`, `dropped_rows_so_far` | int/bool | sink, per file | yes |
  | `t_first_s`, `t_last_s` | float | `t / clock_hz`; `t` can exceed 2⁵³, which JavaScript clients (including Tiled's web UI) round silently, so a float copy exists for display and range queries | yes |
  | `first_batch_wall_time_iso` | str (UTC) | host clock when the first batch arrived; `start_time_iso` is the *connect* time. Together with `t_first` this anchors ticks to wall clock to within network latency | yes |
  | `writer_config` | `{roll_bytes, row_group_rows, compression, encodings, on_full}` | sink | yes |
  | `source` | `{"kind": "live" \| "replay" \| "simulator", "path": …}` | `--parquet-source` flag, passed by the UI (only the UI knows the replay file) | yes |
  | `user` | free-form dict (sample, proposal, notes, …) | `--parquet-meta-json`, written by the UI from operator-entered fields (§6.3) | yes, may be empty |
  | `detector` | `{detector_type, chip_ids, serval_version, bias_voltage, thresholds, config: {…}, snapshot_time_iso}` | `--parquet-meta-json`; `acq.py` snapshots Serval's `/dashboard`, `/detector/config` and `/detector/chips` through `ServalClient` after applying the configuration and just before `start_acquisition` (§6.3, §8.3). The whole config dict is kept so nothing has to be chosen now; the named fields are lifted to the top of the sub-dict so they are searchable | yes; empty for replay and simulator runs |

  Not in the files because nothing we run can learn it: the `live-cli` version (closed binary,
  no `--version` output we parse). Sensor temperature appears in the snapshot if Serval reports
  it in `/dashboard`.

- **`_scan_meta.json`** repeats the per-scan part of that model and adds what is known only at
  stop: `stop_time_iso`, `acquisition_duration_s`, per-table row counts and file lists,
  `dropped_rows` total, status (`complete | incomplete | error`), and the `TimePixStop` fields.
  It documents that `TimePixStop.total_packets` counts *binned* pixels (`event_count`,
  `app.py:441`) and will not equal the parquet row count.
- **Reader helpers ship with the writer**: `read_scan_metadata(path)` returns the validated
  model from a file or a scan directory, so consumers (and the registration tool) never parse
  the KV block by hand; `open_scan(path)` returns the three tables as pyarrow datasets with a
  `to_seconds()` convenience, so nobody re-derives the unit conversion.
- **The format gets its own document.** Analysts should not read a plan to learn the schema.
  When Phase 1 lands, this section becomes `info/outputs/parquet/format.md`, versioned with
  `schema_version`, and the plan links to it (§10).

---

## 8. Phase 2 — Tiled registration

New module `src/splash_timepix/tiled_register.py`, console script `tpx-tiled-register`.

```
tpx-tiled-register <scan_dir> --uri http://localhost:8000 [--path tpx3] [--key <slug>] [--allow-incomplete]
```

The API key is read from `TILED_API_KEY` (or `--api-key`), never from `operator_prefs.json`.

### 8.1 Metadata comes from the files

This is criterion 3 taken literally. The tool reads `pq.read_metadata(f).metadata[b"splash_timepix"]`
from every file and aggregates, then adds what only the files know: `num_rows` per table and per
file, `num_row_groups`, file sizes, and `t_first`/`t_last` from the row-group statistics on `t`.
`_scan_meta.json` is used as the **completion marker** and as a cross-check (mismatch → warning);
a scan without it is refused unless `--allow-incomplete`, in which case only footer-complete files
are registered and `status` is set accordingly. Provenance is added: `registered_by`,
`register_time`, `splash_timepix_version` (via `importlib.metadata`). UI-only knowledge — the
energy calibration `pixel_per_ev` / `ev_at_mid`, `_meta.json` totals, and `raw_tpx3_path`, the
final slug-renamed name of the `.tpx3` from `_save_on_stop` — is merged in at registration time
under a `ui` sub-dict. `raw_tpx3_path` is what links the raw and parquet records of the same scan
inside Tiled.

### 8.2 Tree shape

```
<path>/
  <slug>/                 container, spec tpx3_scan, full scan metadata (this is what search returns)
    events                table, spec tpx3_events, N partitions = N files
    tdc                   table, spec tpx3_tdc
    control               table, spec tpx3_control
```

Each table is registered with the low-level `client.new(structure_family="table",
data_sources=[DataSource(management="external", mimetype="application/x-parquet",
structure=TableStructure(schema without per-file KV, npartitions=N), assets=[Asset(data_uri=
"file:///…/events/part-000000.parquet", parameter="data_uris", num=0), …])], key=…, metadata=…)` —
i.e. exactly the shape Tiled uses for its own partitioned tables, so the server's built-in
`ParquetDatasetAdapter` serves it with no custom adapter. Re-running on the same scan is
idempotent and does not destroy what people added in Tiled: if a node with our spec exists and
its file set is unchanged, the tool calls `update_metadata` and keeps the node; it deletes and
recreates only when the file set differs (a re-run after `--allow-incomplete`, say) or with
`--force`, because data sources cannot be updated in place.

Three things to know about Tiled here. `readable_storage` is enforced at **read** time, not at
registration, so a mis-pathed scan registers fine and fails on `.read()`; the tool therefore
reads back `read_partition(0)` after registering and fails loudly. `Key(...)` searches match
the direct children of the node being searched, so clients search `<path>` to find scans. And
`.read()` on a table streams the *whole* table over HTTP as Arrow IPC — fine for a notebook
looking at one partition, wrong for a 100 GB scan. Tiled is the discovery and metadata layer;
analysis at scale asks Tiled for the asset paths (`include_data_sources`) and reads the files
directly with pyarrow or Dask.

### 8.3 UI hook and preferences

- Trigger: `_on_process_stopped("streaming")` (`ui/main.py:541`) for every mode with parquet
  enabled — the streaming process exiting means `sink.stop()` has finished (given Phase 0). Runs in
  a `QThread`; failures are logged, never fatal. `_save_on_stop` writes `parquet_dir` from
  `TimePixStop` into `_meta.json`; the Tiled key is the slug built from `scan_name` already in
  `_last_metadata` (`ui/operator_tab.py:1220-1221`). If `tiled_uri` is empty, registration is
  skipped silently; parquet files are useful without Tiled.
- **Catch-up.** Because failure is non-fatal, there must be a record of what never made it in.
  On success the tool writes `_tiled_registered.json` (node URL, time, metadata hash) into the
  scan directory; `tpx-tiled-register --scan-root <parquet_dir> --unregistered` walks the root
  and registers every scan without that marker. The UI runs this once at startup when
  `tiled_uri` is set, so a Tiled outage during a shift heals itself the next morning. This is
  Phase 2, not Phase 5.
- Preferences: `write_parquet` (bool, default **true**, §6.3), `parquet_roll_mb` (int, clamped 64–4096, default 512),
  `tiled_uri` (str, default `http://localhost:8000` — the service of §8.4; a failed registration
  is logged and healed by catch-up, so the default costs nothing while the service is not yet
  installed, and clearing the field turns registration off, §6.3), `tiled_path` (str, default
  `tpx3`). Each must be added to both `default_preferences()` and
  `validate_and_clamp()` (`ui/preferences.py:59,167`) or it is silently dropped on load; bump
  `PREFERENCES_VERSION`.
- Operator tab: a "Write parquet" checkbox beside the output directory field; the parquet root
  is the same output directory. Three optional text fields — **sample**, **proposal**,
  **notes** — are new (the tab has none today, `ui/operator_tab.py`); `start_streaming_server`
  writes them to a JSON file in the output directory and passes it as `--parquet-meta-json`, so
  they reach every footer as the `user` sub-dict (§6.3, §7). The same file carries the
  `detector` snapshot, but the UI does not take it: when `tpx-stream` is launched
  (`ui/main.py:249`) the detector still holds the previous scan's configuration — the
  `.bpc`/`.dacs` files and the trigger settings are loaded only when `acq.py` runs, two steps
  later (`ui/main.py:376`, via `ui/workers.py:430`). So `start_acquisition` passes the same
  file to `acq.py` as `--meta-json`, and `acq.py` writes the `detector` sub-dict into it
  between `update_detector_config` and `start_acquisition` with an atomic read-merge-write (the
  shared helper from §6.2). If a Serval GET fails the sub-dict is empty and the acquisition
  proceeds. The UI deletes the file once the streaming process has exited; its content lives
  on in every footer and in `_scan_meta.json`. The file size limit
  (`parquet_roll_mb`) and the Tiled URI/path live in the Engineering tab as advanced settings;
  `start_streaming_server` also forwards `--parquet-roll-mb` and `--parquet-source`.

### 8.4 The Tiled server runs on the acquisition PC

Decision: the Tiled server runs on the same machine as the UI and `tpx-stream`, at least for
the first releases. That settles three things at once — `readable_storage` is the output
directory, assets are `file://` URIs on a local disk with no shared filesystem involved, and
the UI talks to `http://localhost:8000`. The same command serves development, the integration
tests and production:

```
tiled serve catalog --init --api-key "$TILED_API_KEY" --read <output_dir> <catalog.db>
```

On the acquisition PC it runs as a **systemd user service** (`tiled-catalog.service`, shipped in
`info/outputs/parquet/tiled.md`), with the catalog at
`~/.local/share/splash_timepix/tiled/catalog.db` and the API key in an environment file
(`~/.config/splash_timepix/tiled.env`, mode 600) that both the unit and the UI's launcher read,
so `TILED_API_KEY` is set in one place. Running as the same user as the UI means no file
ownership or permission question arises. The catalog file is backed up with the data directory;
losing it loses only the registrations, and `tpx-tiled-register --unregistered` after deleting
the `_tiled_registered.json` markers rebuilds them from the files. `tiled serve directory
<data_dir>` is a quick way to peek at files but does not persist metadata, so it is not the target.
Moving the server to a beamline host later changes `tiled_uri`, `readable_storage` and the
mount path in the assets, nothing in the files.

### 8.5 Dependencies

- Core dependency: `pyarrow>=16`.
- Extra `tiled`: `tiled[minimal-client]>=0.2.19,<0.3` (the full client extra drags in numba,
  which lags new Pythons — relevant to the 3.14 CI leg).
- Extra `tiled-test`: `tiled[minimal-server,minimal-client]`, `dask[dataframe]`, `pandas`.
- `requires-python` moves to `>=3.10` (`app.py:204` already uses `str | None`; pyarrow 26 needs it).

### 8.6 Implementation notes (Phase 2, as built)

- All API names were confirmed against tiled 0.2.19: `Container.new(structure_family,
  data_sources, key=, metadata=, specs=)`, `TableStructure.from_schema(schema, npartitions)`,
  `DataSource(management=Management.external, mimetype="application/x-parquet", …)`,
  `Asset(data_uri, is_directory=False, parameter="data_uris", num=i)`, `update_metadata()`,
  `delete(recursive=True, external_only=True)`, `include_data_sources()`, `read_partition(i)`,
  `tiled.catalog.from_uri(..., readable_storage=[...], init_if_not_exists=True)`,
  `build_app`, `Context.from_app`, `from_context`. The write scopes required are
  `write:metadata`, `create:node` and `register`; the single-user `--api-key` has them all.
- The server flag is `--read` / `-r`, not `--readable-storage`. API keys must be alphanumeric
  (`openssl rand -hex 32`).
- Tiled's `minimal-server` extra cannot import `tiled.server.app` (missing `ragged`,
  `sparse`, `ndindex`, …); the `tiled-test` extra therefore uses `tiled[server]`.
- The scan container is created with `create_container(key, metadata, specs=["tpx3_scan"])`
  and the three tables with `new(...)` beneath it. The node key defaults to the UI slug
  (found via the matching `*_meta.json` beside the scan directory, which also yields
  `raw_tpx3_path` and `energy_calibration` for the `ui` block), else the scan uuid.
- Idempotency key is a `files_signature` (sorted `(table, filename, size)`) stored under
  `registration` in the node metadata: same signature → `update_metadata`, different or
  `--force` → delete and recreate.
- The UI hook fires 3 s after the streaming process exits (so `_save_on_stop` has written the
  `_meta.json`), on a `QThread`; the start-up catch-up runs 5 s after launch over the output
  directory preference, skipped with `tpx-ui --no-catch-up` or an empty `tiled_uri`.

---

## 9. Phase 3 — validation

This is the part that decides whether it worked.

**Test layout.** `tests/` is flat today (eleven files). New tests go into subfolders from the
start and the existing files move in Phase 0, so the tree becomes:

```
tests/
  conftest.py            shared fixtures stay at the root (server, simulator, streaming_rig, ports)
  core/                  test_parser.py, test_socket_server.py, test_simulator*.py
  streaming/             test_start_stop_*.py, test_flush_burstiness.py, test_main_workflow.py
  ui/                    test_preferences.py, test_log_manager.py, test_single_instance.py
  parquet/               test_parquet_sink.py, test_parquet_streaming.py, test_scan_metadata.py
  tiled/                 test_tiled_register.py, test_register_cli.py, test_tiled_server_cli.py,
                         conftest.py (in-process Tiled server fixture, importorskip)
```

(`tests/ui/` also gains `test_save_on_stop.py`; `tests/core/` gains `test_serval_snapshot.py`.)

Every subfolder has an `__init__.py` (the existing `from tests.conftest import …` style keeps
working and pytest tolerates duplicate basenames); `testpaths = ["tests"]` already recurses. The
Tiled fixture lives in `tests/tiled/conftest.py` so the heavy imports are only attempted there.

**Unit — `tests/parquet/test_parquet_sink.py`.** Build real `BatchParseResult`s with
`PacketParser().parse_batch()` over `PacketBuilder` packets (`src/splash_timepix/simulator.py:38-91`),
plus one `_empty_result()` case for the int64/uint64 trap. Assert: schema and dtypes; roll at a
tiny `roll_bytes`, each table on its own byte count (§6.2), so `events` has more partitions
than `tdc` and the `t` ranges in the footers still cover the same span; per-file footer values
(`t_first`, `t_last`, `num_rows`) match the file's contents; no `.tmp` remains and every `.parquet` opens
with plain `pyarrow`/`pandas`; drop counting sets `incomplete`; `stop()` drains everything that
was queued; `offer()` before `start()` is buffered; an injected `OSError` yields `error` status
and no exception; `t_monotonic` is false for a deliberately shuffled batch.

**Unit — `tests/parquet/test_scan_metadata.py`.** `ParquetScanMetadata` round-trips through the
KV block and `_scan_meta.json`; `read_scan_metadata()` works on a file and on a directory;
`open_scan()` returns the three tables and `to_seconds()` matches `t / clock_hz`; an unknown
`schema_version` is rejected with a clear error.

**Integration — `tests/parquet/test_parquet_streaming.py`** (markers `integration`, `slow`).
`streaming_rig` (`tests/conftest.py:213`) gains a `parquet_dir` kwarg. Assert: the parquet
contents, sorted by `t`, are **exactly** the packets the simulator sent — every `x, y, tot, t`
and every TDC edge, not just the row count (and never `stop.total_packets`);
`TimePixStop.parquet_dir` is present and is the directory on disk; `on_full="block"` loses
nothing and `on_full="drop"` with a tiny queue reports `incomplete` with a non-zero
`dropped_rows`; `kill -9` mid-run leaves only footer-complete files, a leftover
`.part-*.parquet.tmp` that pyarrow discovery ignores, and no `_scan_meta.json`, and the
registrar refuses without `--allow-incomplete`. The "does not slow ZMQ" claim is
checked with a **tolerance**, `total_cycles` within 10 % with and without `--parquet-dir`,
and marked `slow`; an exact comparison of two runs on a shared CI runner would be flaky by
construction. The precise measurement belongs to the benchmark script below.

**Tiled — `tests/tiled/test_tiled_register.py`** (marker `integration`; `pytest.importorskip` on
`tiled.server.app` and `dask.dataframe`). In-process server via
`Context.from_app(build_app(in_memory(writable_storage=tmp/"w", readable_storage=[scan_root])))`
and `from_context`, used as a context manager — not `tiled.client.simple`, which spawns a real
uvicorn thread. Assert: `client[path][slug]["events"].read()` returns the expected columns and
row count; `client[path].search(Key("scan_name") == uuid)` and a numeric query
(`Key("tdc_frequency_hz") > 0`) both find the container; registering twice yields one node; a
scan root outside `readable_storage` fails at the read-back with a clear message.

Two tests are written specifically so that criterion 3 cannot pass by accident:

- **Metadata comes from the files, not a sidecar.** Register a scan directory from which
  `_scan_meta.json` has been deleted (`--allow-incomplete`). Assert the node's metadata is
  complete, that every value equals what `pq.read_metadata(f).metadata[b"splash_timepix"]`
  returns, and that a search on a key that exists *only* in the footers — `t_first`, derived from
  row-group statistics — finds the scan. Tiled queries do not support Python's chained
  comparison, so the range query is two chained searches:
  `client[path].search(Key("t_first") <= t).search(Key("t_last") >= t)`.
- **Metadata is in the database, not in memory.** Use a SQLite file catalog
  (`from_uri("sqlite:///<tmp>/catalog.db", init_if_not_exists=True)`) instead of `in_memory`.
  Register, tear the app down, build a *new* app on the same `catalog.db`, and run the same
  searches: they must still find the scan with the same metadata. This is the literal
  "Tiled put that metadata into its database".

The remaining criterion-specific tests, each named for the claim it pins down:

- **Criterion 1 — readable mid-scan** (`test_parquet_streaming.py`). With a tiny `roll_bytes`,
  *while the simulator is still sending*, assert that at least one `part-*.parquet` exists and
  opens with pyarrow, and that `pandas.read_parquet("<scan>/events")` succeeds even though a
  `.part-*.parquet.tmp` is present in the directory. This is the test that distinguishes
  streaming from post-hoc writing, and it proves the leading-dot naming does its job.
- **Criterion 2 — Tiled equals the files** (`test_tiled_register.py`). What
  `client[...]["events"].read()` returns is identical to `pq.read_table("<scan>/events")`;
  dtypes survive the trip (uint16 and uint64 through Arrow IPC into pandas); `read_partition(i)`
  works for every `i` and the partition count equals the file count.
- **Criterion 2 — the real server recipe works** (`tests/tiled/test_tiled_server_cli.py`, marker
  `slow`). Launch `tiled serve catalog` as a subprocess with the exact command from §8.4,
  register over HTTP with the API key, read back. The in-process app is faster for everything
  else, but the recipe operators will actually run must be exercised at least once.
- **Criterion 3 — operator and detector metadata flow end to end.** Write a
  `--parquet-meta-json` with a sample name and a `detector` block shaped like a Serval
  snapshot, run a scan through `streaming_rig`, register, and find it with
  `Key("user.sample") == "..."` and `Key("detector.serval_version") == "..."`. This is the
  UI → footer → Tiled chain for the metadata people will actually search on. The snapshot
  itself is a unit test of `acq.py` with `ServalClient` mocked
  (`tests/core/test_serval_snapshot.py`): the three GETs become the `detector` sub-dict merged
  into a file that already holds `user`, and a connection error yields an empty one without
  stopping the acquisition.
- **Criterion 3 — catch-up.** Register against an unreachable URI: non-zero exit, no
  `_tiled_registered.json`. Then run `--unregistered` against a working server: the scan is
  found and the marker exists.
- **Criterion 3 — idempotency both ways.** Re-register an unchanged scan: node id unchanged,
  metadata someone added in Tiled survives. Register with `--allow-incomplete`, finish the scan,
  re-register: the node now has more partitions.

Outside the criteria but needed so the feature cannot rot silently:
`tests/ui/test_preferences.py` round-trips the four new keys through `validate_and_clamp()`;
`tests/core/test_serval_snapshot.py` covers the `detector` snapshot with a mocked `ServalClient`;
`tests/tiled/test_register_cli.py` checks `tpx-tiled-register` exit codes and the refusal
without `_scan_meta.json`; `tests/ui/test_save_on_stop.py` asserts `parquet_dir` from
`TimePixStop` lands in `_meta.json`. The replay conversion of a `.tpx3` cannot run in CI
(`live-cli` is a closed binary) and is a named step of the acceptance checklist in the manual (§10).

**Traceability.** One row per criterion, so the question "do we meet it?" has a one-glance
answer:

| Criterion | Satisfied by | Proven by |
|---|---|---|
| 1. Stream Timepix3 data into parquet during acquisition | `ParquetSink` in `tpx-stream` (§6) | `tests/parquet/test_parquet_streaming.py`: a completed file is readable while the simulator is still sending; contents equal the packets sent exactly; `block` loses nothing, `drop` reports it; ZMQ throughput within tolerance |
| 2. Tiled can read the files | standard parquet, Tiled's built-in `ParquetDatasetAdapter` (§7–8) | `tests/tiled/test_tiled_register.py`: `.read()` equals `pq.read_table` on the same directory, dtypes intact, every partition readable; `test_tiled_server_cli.py`: the documented server recipe works over HTTP; mis-pathed storage fails loudly |
| 3. Metadata out of the files, into Tiled's database | `tpx-tiled-register` reads parquet footers (§8.1) | sidecar deleted → metadata still complete and searchable; server restarted on the same SQLite catalog → still searchable; operator-entered `user.sample` and detector `detector.serval_version` searchable; catch-up registers what an outage missed; re-registration keeps the node |

One interpretation, decided for now: we **register** the files (Tiled serves them from where
they are) rather than **upload** them through Tiled into its own storage. "Tiled should be able
to read the file" points at registration, and NSLS-II rejected streaming through Tiled as a slow
intermediary. If "ingest" turns out to mean upload to the group lead, the registration tool is
the only piece that changes: the files, the metadata model and the tests of §9 stay as they are.

**CI.** `.github/workflows/test.yml` installs the `tiled-test` extra on 3.12 and 3.13 so the
Tiled tests actually run rather than skip; 3.14 may skip while wheels lag.

**End-to-end without the detector.** Everything up to the real count rate can be exercised on
any machine with the repo's own tooling:

| Method | Exercises | How |
|---|---|---|
| Replay mode | the real `live-cli → tpx-stream` path, real packet format and timestamps | UI → "Select Replay File" → `live-cli --source-files <file>.tpx3` (`ui/workers.py:420-421`). `SampleData/tpx3/sample21MB.tpx3` for a quick run, `sample1.1GB.tpx3` (~66 M hits) for a realistic load. The resulting directory is the acceptance fixture for the Tiled steps below. |
| Simulator | `tpx-stream` alone at a chosen pixel/TDC rate, including control packets | `tpx-sim` / the `streaming_rig` fixture; this is what the integration tests use. |
| Unit tests | the sink, the metadata model and the registration tool in isolation | no network, no hardware. |

What replay cannot tell us: `live-cli`'s replay speed is not the live count rate, and Serval's
`.tpx3` write is not happening alongside.

**Benchmark script — `tools/bench_parquet_sink.py`.** Drives synthetic `BatchParseResult`s
into a `ParquetSink` at a chosen rate (default the 40 Mhit/s peak) for a chosen duration, with
no socket and no `tpx-stream`, and prints writer throughput, compressed bytes per hit, roll
interval at the configured size, queue high-water mark and drops. Run on the acquisition PC's
data disk it answers the disk-bandwidth and roll-size questions in §13 before any detector
time, and run with and without the sink it gives the precise cost of `offer()` that the
tolerance test above only bounds. Only the interaction with Serval's concurrent `.tpx3` write
still needs the real detector.

**Manual acceptance, on the acquisition PC.** One real scan with parquet enabled; open the
directory with `pandas.read_parquet`; register it against the Tiled service on the acquisition
PC (§8.4); run the three
queries above from a notebook; confirm the ZMQ live view and the Latent Space Explorer behaved
exactly as before. Measure compressed bytes per hit and disk bandwidth at the highest count
rate we run — these numbers set the final `roll_bytes` default and confirm that parquet can
stay on by default (§6.3). The full checklist, with expected results per step, lives in the
manual (§10) so it can be repeated after every release.

---

## 10. Documentation and examples

Documentation is a phase of its own (Phase 4, §12) rather than a line item inside each code
phase, for two reasons: the manual, the demonstration notebook and the acceptance checklist
all span Phases 1–3 and cannot be finished until the last of them lands, and a phase with a
named deliverable is what gets reviewed and signed off, whereas "docs in the same PR" is what
gets trimmed when the PR is late. The exceptions are the two documents a developer needs while
the code is being written: `format.md` lands with Phase 1 because the schema is being decided
there, and the server recipe in `tiled.md` lands with Phase 2 because Phase 2 cannot be
developed without it.

Everything a user of this feature needs lives under one sub-folder, so `info/` can keep
growing by data product rather than by file:

```
info/outputs/parquet/
  README.md                      the manual: operator and analyst guide (contents below)
  format.md                      the format reference — §7 promoted, versioned with schema_version
  tiled.md                       Tiled on the acquisition PC: systemd unit, env file, API key, searching, large scans
  acceptance/<date>.md           one record per acceptance run on the real detector (Phase 4)
  examples/
    demo_for_review.ipynb        the three criteria, one section each — the demonstration for the group lead
    demo_helpers.py              starts and stops tpx-sim, tpx-stream and the dev Tiled server for the notebook
    convert_tpx3_by_replay.md    manual steps: UI replay of SampleData/tpx3/sample21MB.tpx3 (live-cli cannot be scripted)
```

**The manual (`README.md`)** has seven parts: what you get (layout, which modes write parquet,
the relationship to the `.tpx3`); enabling it (checkbox, roll size, Tiled URI and path,
operator metadata fields, what the Engineering tab shows during a scan); command line
(`tpx-stream` flags, converting an existing `.tpx3` by replay, `tpx-tiled-register`, catch-up);
reading the data; Tiled (pointer to `tiled.md`); troubleshooting (`incomplete` and `error`,
dropped rows, disk full, leftover `.tmp` after a crash, registration failed, the
`readable_storage` read error); and the acceptance checklist, every step with its expected
result, so the group lead's criteria can be demonstrated in one sitting and repeated after
every release. "Reading the data" is where the analysis recipes live, as short snippets rather
than standalone scripts: `open_scan()` and ticks to seconds, exposure windows from the
`control` table, assigning events to their TDC cycle, the two-step time-range search, a large
scan through Dask via the Tiled asset paths, and printing every footer field. They are useful,
but they prove no criterion, so they do not need files of their own to maintain.

**The example is the notebook.** `demo_for_review.ipynb` is written for the group lead and
has one section per criterion, each headed with the criterion verbatim:

1. *Data streaming of Timepix3 into parquet files.* Start `tpx-sim` and `tpx-stream` with
   parquet on and a small roll size; list the scan directory twice, ten seconds apart, so files
   appear while data is still flowing; read a completed file with pandas while a `.tmp` is
   present; stop the scan and check the row count against the simulator's sent count and
   `_scan_meta.json`.
2. *Tiled should be able to read the file.* Start a dev Tiled server on a throw-away catalog,
   register the scan, read it back through Tiled, and show pyarrow and Tiled returning the same
   row count, columns and dtypes.
3. *Taking metadata out of files, and having Tiled put that metadata into its database.* Print
   the `splash_timepix` block as `pq.read_metadata` sees it in a footer next to the Tiled node's
   metadata; search by `user.sample` and by time range; restart the Tiled server on the same
   catalog file and repeat the searches.

The notebook generates its own data, needs only the repo's own tools (no detector, no `live-cli`,
nothing beyond localhost) and runs in under five minutes. It is kept **executed**, outputs
committed, so it reads as evidence without being run; "Run all" regenerates it. The process
plumbing is in `demo_helpers.py` so the cells show only what the reader is meant to check.
`convert_tpx3_by_replay.md` is the one item outside the notebook: it is a step of the acceptance
checklist, and `live-cli` cannot run from a cell in CI.

**Existing documents to update.** The README's documentation list gains the new folder;
`info/TESTING_GUIDE.md` describes the test sub-folders, the `tiled-test` extra and the `slow`
marker; `CHANGELOG` (or the release notes) records the parquet `schema_version` next to the
package version, so the two are traceable to each other.

**Phase 4 ends with the acceptance run.** The checklist in the manual is executed once on the
acquisition PC with the real detector, and the result — date, software version,
`schema_version`, measured bytes per hit and disk bandwidth, pass/fail per step — is recorded
in `info/outputs/parquet/acceptance/<date>.md`. That record, together with the executed
notebook, is what is shown to the group lead; the phase is not done until it exists.

### 10.1 Implementation notes (Phases 3 and 4, as built)

- **Tests.** 212 tests after Phase 4 (`pytest -m ""`, about 6 min; the default `-m "not slow"`
  selection is 197 tests in about 2.5 min). `tests/ui/conftest.py` creates one offscreen `QApplication`
  before any UI module is collected: `test_log_manager.py` only needs a `QCoreApplication`, but
  Qt cannot upgrade one to a `QApplication` later, and a widget built on a bare
  `QCoreApplication` aborts the whole pytest process (observed once `test_save_on_stop.py`
  was added).
- **Notebook.** `examples/demo_for_review.ipynb` runs in about one minute on localhost; it is
  generated and executed by a build script (nbformat + nbclient) and committed with its
  outputs. `demo_helpers.Demo` starts `tpx-stream` (parquet on, `--exit-on-disconnect`,
  roll 0.25 MB, row groups of 20 k rows so files appear every few seconds), `tpx-sim`
  (`--auto-start`, through a `python -c` wrapper that turns on INFO logging so the simulator's
  final "Sent events" line is available as ground truth) and `tiled serve catalog --init
  --api-key … --read <data dir>`; `restart_tiled()` restarts the server on the same catalog
  file for criterion 3.
- **Simulator limit.** `PacketSimulator.generate_stream` keeps a Python list of pending events
  and pops from its front; above roughly 30 k packets/s (less on a loaded machine) the backlog
  grows faster than it drains and a 20 s run can take minutes. The notebook therefore streams
  at 15 k hits/s. This does not affect the real detector path; the sink's own ceiling is
  measured by `tools/bench_parquet_sink.py`.
- **Acceptance run.** Not performed in this session: it needs the detector and `live-cli`. The
  checklist in the manual (§7) is ready to be executed and recorded in
  `info/outputs/parquet/acceptance/<date>.md`.

---

## 11. Phase 5 — live visibility in Tiled (only if asked for)

Nothing in the layout changes, but five things do: the roll rule gains a time term (10–30 s),
the container is created at scan start with `status: running` and partitions are appended as
files roll (or a Tiled directory watcher does it), metadata is finalised at stop, small files are
compacted at scan end, and — the real cost — Tiled becomes a runtime dependency during
acquisition. Registration failures must then be non-fatal by construction; the Phase 2 catch-up
(`--unregistered`, §8.3) already covers an outage, and would gain a "finalise running scans"
step. Consumers must tolerate a table whose
partition count grows between reads; `status` is how they know a scan is finished.

---

## 12. Phasing

| Phase | Deliverable |
|---|---|
| 0a | Move existing tests into `tests/{core,streaming,ui}/` — a no-op commit on its own |
| 0b | `SocketDataServer` connect/disconnect callbacks; `task_done` in `finally`; SIGTERM handler; stop ordering in `finally` |
| 1 | `ParquetScanMetadata` model + `read_scan_metadata()` / `open_scan()`; `ParquetSink` with closer thread, per-table rolling and footer values at close; `--parquet-dir`, `--parquet-roll-mb`, `--parquet-on-full`, `--parquet-source`, `--parquet-meta-json`; shared atomic-write helper; heartbeat stats; `TimePixStop` parquet fields; UI checkbox (on by default), sample/proposal/notes fields, `acq.py --meta-json` writing the Serval `detector` snapshot into the meta JSON, roll-size setting, `write_parquet`/`parquet_roll_mb` preferences and arg plumbing; `tools/bench_parquet_sink.py`; `info/outputs/parquet/format.md`; changelog entry |
| 2 | `tpx-tiled-register` with update-vs-recreate idempotency, `_tiled_registered.json` marker and `--unregistered` catch-up; UI hook on streaming exit and catch-up at startup (`--no-catch-up`); `tiled_uri`/`tiled_path` preferences; dependency extras; `info/outputs/parquet/tiled.md` with the server recipe (completed in Phase 4) |
| 3 | Unit, streaming-integration (exact round-trip, readable mid-scan) and Tiled-integration tests under `tests/parquet/` and `tests/tiled/`, including the real-server recipe test; UI and CLI tests; CI extra |
| 4 | **Documentation and demonstration** (§10): the manual `info/outputs/parquet/README.md` with the reading-the-data snippets and acceptance checklist; `tiled.md` completed (systemd unit, env file, searching, large scans); `demo_for_review.ipynb` + `demo_helpers.py`, executed and committed; `convert_tpx3_by_replay.md`; README and `TESTING_GUIDE.md` updates; the acceptance run on the acquisition PC recorded in `acceptance/<date>.md` |
| 5 | Live-in-Tiled, after the group lead confirms it is wanted |

Phase 0 is a PR of its own and is worth doing even if nothing else ships. Phases 1 and 2 are
independent enough to be reviewed separately; Phase 3 tests land with the code they test.
Phase 4 is the milestone shown to the group lead: it closes when the notebook runs end to end
and the acceptance record exists, so "done" has a definition that does not depend on who is
asked.

---

## 13. Open questions and risks

- **Disk bandwidth at full rate.** ~40 Mhit/s is ~560 MB/s raw; zstd plus delta encoding should
  halve it, but the acquisition PC's disk has to sustain that alongside Serval's `.tpx3` write.
  `tools/bench_parquet_sink.py` (§9) measures the sink's half on the real disk without the
  detector; only the concurrent Serval write needs a live run. Parquet is on by default (§6.3);
  if the benchmark shows the disk cannot keep up, the default flips to off until it can.
- **Disk housekeeping.** Parquet roughly doubles the on-disk footprint of every scan and nobody
  owns deletion today. The first release states the policy plainly — the operator deletes, the
  UI shows free space and the heartbeat warns below a threshold — and the manual says so. A
  retention tool is a later decision.
- **When, if ever, does the `.tpx3` go away?** Both outputs are on by default for now. Dropping
  the raw file needs: parquet proven over months of real scans, a replacement for every
  `tpx3dump`-based tool, and acceptance that an `incomplete` parquet scan would then have no
  recovery path. Revisit after the first beamtime with parquet on.
- **Roll size.** 512 MB is a reasoned default, not a measured one; set it from the benchmark's
  compressed-bytes-per-hit.
- **Tiled 0.2.19 API names** (`TableStructure.from_schema`, `delete_contents`,
  `update_metadata`, `include_data_sources`, the `register` scope on API keys) and the pyarrow
  writer details (`use_dictionary` list form with `column_encoding`,
  `ParquetWriter.add_key_value_metadata()` for the at-close footer values, §6.2) were taken
  from documentation, not run here — a half-day spike against a local `tiled serve catalog` confirms
  them before Phase 2 starts; the pyarrow half can run during Phase 1.
- **Does `live-cli` ever emit non-monotonic timestamps across batch boundaries?** Phase 1
  records `t_monotonic` per file and declares no `sorting_columns` (§6.2). If a few beamtimes
  of footers show it is always true, `sorting_columns` can be declared unconditionally and
  consumers can rely on it.
- **How much of `_meta.json` should migrate into Tiled?** The energy calibration is the obvious
  candidate; the averaged histogram CSV/PNG could become a second node per scan later.
- **Detector identity — which Serval fields to promote.** The snapshot in the `detector`
  sub-dict (§7) keeps the whole `/detector/config` dict, so nothing is lost, but only the lifted
  fields are convenient to search. The first list (type, chip IDs, Serval version, bias,
  thresholds) is a guess at what analysts will filter on; adjust after the first beamtime.
- **`globalTimestampInterval`.** Irrelevant to this design (`live-cli` extends timestamps for
  us), but the NSLS-II notes say ASI detectors need it set explicitly for any decoder that reads
  the raw `.tpx3` — i.e. `tools/tpx3parser` and anything built on `subpixel_centroiding.md`.
  Check our Serval configuration once.

---

## 14. What we took from the NSLS-II repositories

We read `NSLS2/tpx3awkward` (decoder/clusterer, cloned under `Playground/`) and
`MTakahashi-KWH/tpx3_pipeline` (their streaming writer). Nothing from them is a runtime
dependency here, but several of their choices shaped this plan:

- **Config embedded in the parquet schema metadata** (`tpx3awkward/processing/files.py:160-168`
  stores the processing config as JSON under one key) — adopted as the `splash_timepix` KV
  block in §7, with a pydantic model and a reader helper in place of their
  `read_parquet_config()`.
- **Separate tables for photon events and TDC events** — adopted; we add `control`.
- **Rolling whole files, not one growing file** — their `file_worker.py` writes one parquet
  per 50 MB raw buffer and only completed files are visible, which is the same guarantee we get
  from `.tmp → os.replace`. We improve on it with `ParquetWriter` row groups (so a file is not
  one giant row group), a closer thread so `fsync` never stalls the writer, and one
  subdirectory per table so each is a self-contained dataset.
- **Filenames tied to the scan identity** and a collision guard (`buff_{sid}_{scan}_{index}`,
  uuid6 suffix on collision) — ours is the scan-UUID directory plus `file_index`; collisions are
  impossible by construction, so no suffix.
- **~200 MB partitions for Dask** — the reasoning is sound; we start at 512 MB because at our
  peak rate 200 MB rolls every one to two seconds, and make it configurable (§6.2, §13).
- **Event-level live stream as Arrow IPC over ZMQ** (`file_worker.py:63-73`,
  `stream_dispatch.py`) — *not* adopted now; our ZMQ carries binned histograms, which is what
  the browser UI and the Latent Space Explorer consume. Recorded as the obvious design if a
  consumer ever wants unbinned events live: `pa.RecordBatch` → `pa.ipc.new_stream` on a second
  ZMQ port, with `SNDHWM` so slow subscribers drop rather than block.
- **Their centroided table schema** (`t, xc, yc, ToT_max, ToT_sum, n`, `schemas.py:54-61`) is
  a reasonable target for the output of `subpixel_centroiding.md` if that tool writes parquet
  too, so the two formats line up for anyone comparing beamlines. Their clustering defaults
  (`radius 3`, `time_window 0.3 µs`) are for direct detection and do not transfer to our
  phosphor geometry.
- **What we deliberately did not copy:** the EPICS IOC and Bluesky/Ophyd trigger (we have no
  EPICS; the UI plays that role), multi-process shared-memory buffers (pickling 280 KB batches
  to a child costs more than the GIL contention it avoids; the heavy work already releases the
  GIL), and their column units (`ToT` in ns, `t` in 1.5625 ns ticks) — see §7.
