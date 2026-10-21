# Converting an existing `.tpx3` to parquet by replaying it

`live-cli` is the only decoder of the raw `.tpx3` stream we run, and it cannot be driven from
a script or a notebook, so converting an old file is a manual step in the UI. It is also step
11 of the acceptance checklist in [../README.md](../README.md#7-acceptance-checklist).

The result is a normal scan directory: the same tables, footers and `_scan_meta.json` as a
live scan, with `source.kind = "replay"` and `source.path` pointing at the `.tpx3`.

## Steps

1. **Start the UI** with Serval running (`tpx-ui`; the replay needs live-cli, not the
   detector). Parquet output is on unless `write_parquet` is false in `operator_prefs.json`.
2. Optionally fill in *ESAF* / *Sample* / *Notes*; they are stored in the parquet
   footers of the converted scan like for a live run.
3. Press **↺ Replay** and pick the `.tpx3` file in the dialog (`TPX3 Files (*.tpx3)`).
4. Watch the *Pipeline queues → Parquet* row: `writing · N rows · M files`. The replay runs at
   live-cli's replay speed, not at the original acquisition rate, so the wall time is not a
   measure of anything.
5. When live-cli reaches the end of the file the streaming server publishes the stop message,
   exits, and the UI registers the scan in Tiled (Logs tab: `Tiled: created tpx3/<slug>
   (complete)`). If Tiled is not running the files are still complete; the catch-up at the
   next UI start registers them.

## Where the files are

Replay output goes **next to the replayed file**, in the directory that holds the `.tpx3`:

```
<dir of the .tpx3>/
  sample21MB.tpx3                       the source, untouched
  sample21MB_<uuid8>_<ts>_avg.csv ...   the UI's averages, named after the source file's stem
  <scan_uuid>/                          the parquet scan directory
    events/ tdc/ control/ _scan_meta.json _tiled_registered.json
```

The directory must be writable; `tpx-stream` refuses to start otherwise (Logs tab shows the
preflight error). Copy the `.tpx3` to a writable location first if it lives on read-only
storage.

## Checking the conversion

```python
from splash_timepix.parquet_meta import read_scan_metadata
md = read_scan_metadata("<dir of the .tpx3>/<scan_uuid>")
md.status, md.source.kind, md.source.path, md.tables["events"].num_rows
```

The `events` row count equals the number of pixel packets in the `.tpx3` (`tpx3dump`'s pixel
count, or `tpx3parser`'s). TDC and control packets are counted in their own tables, so none of
the three equals the raw packet total on its own.

## Re-running

Replaying the same `.tpx3` again creates a **new** scan directory (a new uuid every time), and
the UI registers it as a new Tiled node. Delete the directory you do not want, and run
`tpx-tiled-register --unregistered --scan-root <dir>` only if you want the remaining one
(re)registered; deleting a directory does not remove its Tiled node — do that with the Tiled
client (`container[key].delete(recursive=True, external_only=True)` — `external_only` makes sure
Tiled never deletes the parquet files themselves).
