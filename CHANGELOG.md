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
- `splash_timepix.atomic_io`: shared tmp → fsync → rename helpers (used by the UI
  preferences, the parquet sink and `acq.py`).

### Changed

- Scan lifecycle: per-scan reset and the start message now happen in the server's
  client-connect callback, before the first byte is read; the stop message is published from
  the disconnect callback. `tpx-stream` handles SIGTERM like Ctrl+C so the shutdown path runs.
- `SocketDataServer`: `task_done()` is always called; new `set_client_callbacks()`.
- `zmq_worker`: control messages are published before an event taken from the queue, so a
  start can no longer be overtaken by the first flush.
- `requires-python` is now `>=3.10`; `pyarrow>=16` is a core dependency.
- Tests moved into `tests/{core,streaming,ui,parquet}/`.
