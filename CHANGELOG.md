# Changelog

Notable changes to `splash_timepix`. The parquet `schema_version` is listed next to the
package version that introduced it so the two stay traceable to each other.

## Unreleased

### Added

- **Streaming parquet output** (parquet `schema_version` 1). `tpx-stream --parquet-dir DIR`
  writes every scan's raw events, TDC edges and control packets to
  `DIR/<scan_uuid>/{events,tdc,control}/part-NNNNNN.parquet` while the acquisition runs,
  with the scan configuration, operator metadata and a Serval detector snapshot embedded
  in every file footer. Format reference: `info/outputs/parquet/format.md`.
  New flags: `--parquet-roll-mb`, `--parquet-row-group-rows`, `--parquet-queue-max-rows`,
  `--parquet-on-full drop|block`, `--parquet-source`, `--parquet-source-path`,
  `--parquet-meta-json`.
- `splash_timepix.parquet_meta`: `read_scan_metadata()`, `open_scan()` and the
  `ParquetScanMetadata` / `ScanSummary` models.
- `TimePixStop` gains optional `parquet_dir`, `parquet_status`, `parquet_rows`,
  `parquet_dropped_rows`.
- Heartbeat gains `parquet_*` fields when the sink is enabled.
- `acq.py --meta-json PATH` merges a Serval detector snapshot into the parquet metadata file.
- `tools/bench_parquet_sink.py` measures the sink's throughput and cost on a given disk.
- **Tiled registration**: `tpx-tiled-register <scan_dir>` registers a scan's parquet files in
  a Tiled catalog in place (no copy), with the node metadata built from the parquet footers
  and row-group statistics; idempotent re-runs, `--allow-incomplete`, `--force`, and
  `--unregistered --scan-root DIR` catch-up driven by the `_tiled_registered.json` marker.
  The UI registers every finished Start/Replay scan and runs the catch-up at start-up
  (`tpx-ui --no-catch-up`). New preferences `tiled_uri` (empty = off) and `tiled_path`; the
  API key is taken from `TILED_API_KEY` and never stored. Extras `tiled` and `tiled-test`.
  Server recipe: `info/outputs/parquet/tiled.md`.
- `_meta.json` written by the UI now carries `energy_calibration`, `parquet` (dir/status from
  the stop message) and `stop_message`.
- `splash_timepix.atomic_io`: shared tmp → fsync → rename helpers (used by the UI
  preferences, the parquet sink and `acq.py`).
- Documentation under `info/outputs/parquet/`: the operator/analyst manual (`README.md`, with
  the acceptance checklist), the executed demonstration notebook
  `examples/demo_for_review.ipynb` (+ `demo_helpers.py`) covering the three criteria on
  localhost with the simulator, and `examples/convert_tpx3_by_replay.md`.
- Tests: `tests/parquet/` (sink unit tests, metadata readers, streaming round-trip incl.
  `kill -9` mid-scan), `tests/tiled/` (in-process server, CLI exit codes, `tiled serve catalog`
  recipe), `tests/ui/test_save_on_stop.py`, `tests/core/test_serval_snapshot.py`;
  `tests/ui/conftest.py` creates one offscreen `QApplication` for the package.

### Changed

- Scan lifecycle: per-scan reset and the start message now happen in the server's
  client-connect callback, before the first byte is read; the stop message is published from
  the disconnect callback. `tpx-stream` handles SIGTERM like Ctrl+C so the shutdown path runs.
- `SocketDataServer`: `task_done()` is always called; new `set_client_callbacks()`.
- `zmq_worker`: control messages are published before an event taken from the queue, so a
  start can no longer be overtaken by the first flush.
- `requires-python` is now `>=3.10`; `pyarrow>=16` is a core dependency.
- Tests moved into `tests/{core,streaming,ui,parquet}/`.
- Parquet output has no switch in the UI: it is on by default and controlled only by the
  `write_parquet` preference. The operator field *Proposal* is now *ESAF* (`user.esaf`).
