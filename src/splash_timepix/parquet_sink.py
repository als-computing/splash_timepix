"""Streaming parquet writer for ``tpx-stream``.

:class:`ParquetSink` sits beside the ZMQ path in :mod:`splash_timepix.app`: every
:class:`~splash_timepix.parser.BatchParseResult` that reaches ``data_callback`` is
also offered to the sink, which writes the raw events, TDC edges and control
packets of the scan to parquet files while the acquisition runs.

Design points (see ``info/plans/parquet_streaming_tiled.md`` §6):

* :meth:`ParquetSink.offer` never raises and, with the default ``on_full="drop"``,
  never blocks: it coerces the columns to their on-disk dtypes and enqueues them.
  Everything heavy happens on the **writer thread**.
* The writer thread appends batches per table and writes a parquet row group
  when enough rows (or enough time) have accumulated. Files roll at
  ``roll_bytes`` (and optionally ``roll_seconds``), each table on its own size.
* Finished files are handed to a **closer thread** that writes the footer,
  ``fsync``\\ s, renames ``.part-NNNNNN.parquet.tmp`` → ``part-NNNNNN.parquet``
  and ``fsync``\\ s the directory, so the writer never stalls on a close and a
  reader never sees a partial file.
* :meth:`ParquetSink.stop` drains the queue, closes every file, writes
  ``_scan_meta.json`` and reports the scan's status.
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from splash_timepix.atomic_io import atomic_write_json, fsync_dir, read_json
from splash_timepix.parquet_meta import (
    COLUMN_DTYPES,
    PART_FILENAME_FORMAT,
    SCAN_META_FILENAME,
    TABLE_SCHEMAS,
    TABLES,
    TMP_PREFIX,
    TMP_SUFFIX,
    ParquetScanMetadata,
    ScanFields,
    ScanSummary,
    SourceInfo,
    TableSummary,
    WriterConfig,
    environment_fields,
    ticks_to_seconds,
)

logger = logging.getLogger(__name__)

PathLike = Union[str, Path]

# Row-group policy: write when pending rows reach ROW_GROUP_ROWS, or when at least
# ROW_GROUP_MIN_TIMED_ROWS have waited ROW_GROUP_TIMED_S, or when anything has
# waited ROW_GROUP_MAX_WAIT_S. The first bound keeps row groups large at full rate;
# the other two keep latency bounded at low rates.
ROW_GROUP_MIN_TIMED_ROWS = 65_536
ROW_GROUP_TIMED_S = 1.0
ROW_GROUP_MAX_WAIT_S = 10.0

DEFAULT_ROLL_BYTES = 512 * 2**20
DEFAULT_ROW_GROUP_ROWS = 1_000_000
DEFAULT_QUEUE_MAX_ROWS = 20_000_000
DEFAULT_COMPRESSION = "zstd"
ENCODINGS = {"x": "RLE_DICTIONARY", "y": "RLE_DICTIONARY", "tot": "RLE_DICTIONARY", "t": "DELTA_BINARY_PACKED"}
PENDING_BEFORE_START_MAX_ROWS = 5_000_000
DROP_LOG_INTERVAL_S = 5.0

#: Bytes per event row on the queue (uint16 ×3 + uint64), for sizing ``queue_max_rows``.
EVENT_ROW_BYTES = 14


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def check_writable(parquet_dir: PathLike) -> Path:
    """Create ``parquet_dir`` if needed and prove it is writable; raise ``OSError`` otherwise."""
    d = Path(parquet_dir)
    d.mkdir(parents=True, exist_ok=True)
    probe = d / f".write-probe-{os.getpid()}"
    with open(probe, "wb") as fh:
        fh.write(b"ok")
    probe.unlink()
    return d


def free_bytes(path: PathLike) -> Optional[int]:
    try:
        return shutil.disk_usage(os.fspath(path)).free
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------


@dataclass
class _OpenFile:
    index: int
    tmp_path: Path
    final_path: Path
    fh: Any
    writer: pq.ParquetWriter
    opened_mono: float
    num_rows: int = 0
    t_first: Optional[int] = None
    t_last: Optional[int] = None
    last_t: Optional[int] = None
    monotonic: bool = True


@dataclass
class _CloseJob:
    open_file: _OpenFile
    kv: Dict[str, str]
    sink: "ParquetSink"
    done: Optional[threading.Event] = None


class _TableWriter:
    """Per-table buffering, row-group writing and file rolling (writer thread only)."""

    def __init__(self, sink: "ParquetSink", scan: "_ScanState", name: str):
        self.sink = sink
        self.scan = scan
        self.name = name
        self.schema = TABLE_SCHEMAS[name]
        self.columns = [f.name for f in self.schema]
        self.dir = scan.scan_dir / name
        self.pending: List[Dict[str, np.ndarray]] = []
        self.pending_rows = 0
        self.last_write_mono = time.monotonic()
        self.open_file: Optional[_OpenFile] = None
        self.next_index = 0
        # Running totals for the scan
        self.rows_total = 0
        self.files_closed = 0
        self.files: List[str] = []
        self.t_first: Optional[int] = None
        self.t_last: Optional[int] = None

    # -- buffering -----------------------------------------------------------

    def append(self, cols: Dict[str, np.ndarray], rows: int) -> None:
        if rows == 0:
            return
        self.pending.append(cols)
        self.pending_rows += rows

    def due(self, now: float) -> bool:
        if self.pending_rows == 0:
            return False
        if self.pending_rows >= self.sink.row_group_rows:
            return True
        waited = now - self.last_write_mono
        if self.pending_rows >= ROW_GROUP_MIN_TIMED_ROWS and waited >= ROW_GROUP_TIMED_S:
            return True
        return waited >= ROW_GROUP_MAX_WAIT_S

    # -- writing -------------------------------------------------------------

    def write_row_group(self, now: float) -> None:
        if self.pending_rows == 0:
            return
        if len(self.pending) == 1:
            merged = self.pending[0]
        else:
            merged = {c: np.concatenate([b[c] for b in self.pending]) for c in self.columns}
        self.pending = []
        rows = self.pending_rows
        self.pending_rows = 0
        self.last_write_mono = now

        if self.open_file is None:
            self._open()
        of = self.open_file
        assert of is not None

        t = merged["t"]
        if rows:
            first = int(t[0])
            last = int(t[-1])
            if of.monotonic:
                if of.last_t is not None and first < of.last_t:
                    of.monotonic = False
                elif rows > 1 and not bool(np.all(t[1:] >= t[:-1])):
                    of.monotonic = False
            lo = int(t.min())
            hi = int(t.max())
            of.t_first = lo if of.t_first is None else min(of.t_first, lo)
            of.t_last = hi if of.t_last is None else max(of.t_last, hi)
            of.last_t = last
            self.t_first = lo if self.t_first is None else min(self.t_first, lo)
            self.t_last = hi if self.t_last is None else max(self.t_last, hi)

        table = pa.Table.from_arrays([pa.array(merged[c]) for c in self.columns], schema=self.schema)
        of.writer.write_table(table, row_group_size=rows)
        of.num_rows += rows
        self.rows_total += rows

        if self._should_roll(of, now):
            self.roll(final=False)
            # Open the next file at once so a `.tmp` exists whenever a scan is in
            # progress and the next row group never waits on the closer.
            self._open()

    def _should_roll(self, of: _OpenFile, now: float) -> bool:
        if of.fh.tell() >= self.sink.roll_bytes:
            return True
        rs = self.sink.roll_seconds
        return rs is not None and (now - of.opened_mono) >= rs

    def _open(self) -> None:
        index = self.next_index
        self.next_index += 1
        final = self.dir / PART_FILENAME_FORMAT.format(index=index)
        tmp = self.dir / f"{TMP_PREFIX}{final.name}{TMP_SUFFIX}"
        fh = open(tmp, "wb")
        try:
            writer = pq.ParquetWriter(
                fh,
                self.schema,
                compression=self.sink.compression,
                use_dictionary=[c for c in self.columns if ENCODINGS.get(c) == "RLE_DICTIONARY"],
                column_encoding={c: e for c, e in ENCODINGS.items() if c in self.columns and e != "RLE_DICTIONARY"},
                write_statistics=True,
            )
        except Exception:
            fh.close()
            raise
        self.open_file = _OpenFile(
            index=index, tmp_path=tmp, final_path=final, fh=fh, writer=writer, opened_mono=time.monotonic()
        )

    def roll(self, final: bool) -> Optional[threading.Event]:
        """Hand the open file to the closer; open nothing new (the next write opens lazily)."""
        of = self.open_file
        if of is None:
            return None
        self.open_file = None
        kv = self.scan.file_metadata(self, of).to_kv()
        done = threading.Event() if final else None
        self.files.append(of.final_path.name)
        self.files_closed += 1
        self.sink._closer_submit(_CloseJob(open_file=of, kv=kv, sink=self.sink, done=done))
        return done

    def finish(self, now: float) -> Optional[threading.Event]:
        """Flush what is pending and close the file; guarantee at least one file per table."""
        self.write_row_group(now)
        of = self.open_file
        if of is not None and of.num_rows == 0 and self.files:
            # The file opened eagerly after the last roll never received a row:
            # discard it rather than leave an empty trailing part file.
            self.open_file = None
            self.next_index -= 1
            for closer in (of.writer.close, of.fh.close):
                try:
                    closer()
                except Exception:
                    pass
            try:
                of.tmp_path.unlink()
            except OSError:
                pass
        elif self.open_file is None and self.next_index == 0:
            # Nothing was ever written: emit an empty, schema-only file so the table
            # directory is still a valid dataset for pandas/Dask/Tiled.
            self._open()
        return self.roll(final=True)

    def summary(self) -> TableSummary:
        return TableSummary(
            num_rows=self.rows_total,
            num_files=len(self.files),
            files=list(self.files),
            t_first=self.t_first,
            t_last=self.t_last,
        )


@dataclass
class _ScanState:
    scan_name: str
    scan_dir: Path
    scan_fields: Dict[str, Any]
    start_mono: float
    sink: "ParquetSink"
    writers: Dict[str, _TableWriter] = field(default_factory=dict)
    dropped_rows: int = 0
    dropped_batches: int = 0
    rows_offered: int = 0
    status: str = "writing"
    error: Optional[str] = None
    first_batch_wall_iso: Optional[str] = None
    extra_meta_loaded: bool = False
    _last_drop_log: float = 0.0

    def __post_init__(self) -> None:
        for name in TABLES:
            self.writers[name] = _TableWriter(self.sink, self, name)

    def load_extra_meta(self) -> None:
        """Merge ``--parquet-meta-json`` (``user``/``detector``) into the scan fields, once."""
        if self.extra_meta_loaded:
            return
        self.extra_meta_loaded = True
        path = self.sink.meta_json_path
        if path is None:
            return
        doc = read_json(path, default=None)
        if not isinstance(doc, dict):
            if Path(path).exists():
                logger.warning("parquet meta JSON %s is not a JSON object; ignored", path)
            return
        for key in ("user", "detector"):
            value = doc.get(key)
            if isinstance(value, dict):
                self.scan_fields[key] = value

    def file_metadata(self, tw: _TableWriter, of: _OpenFile) -> ParquetScanMetadata:
        self.load_extra_meta()
        clock = int(self.scan_fields.get("clock_hz", 0)) or None
        return ParquetScanMetadata(
            **self.scan_fields,
            table=tw.name,  # type: ignore[arg-type]
            file_index=of.index,
            num_rows=of.num_rows,
            t_first=of.t_first,
            t_last=of.t_last,
            t_monotonic=of.monotonic if of.num_rows else None,
            t_first_s=ticks_to_seconds(of.t_first, clock) if (of.t_first is not None and clock) else None,
            t_last_s=ticks_to_seconds(of.t_last, clock) if (of.t_last is not None and clock) else None,
            first_batch_wall_time_iso=self.first_batch_wall_iso,
            dropped_rows_so_far=self.dropped_rows,
        )


@dataclass
class _Batch:
    scan: _ScanState
    cols: Dict[str, Dict[str, np.ndarray]]
    rows: int


@dataclass
class _StopCmd:
    scan: _ScanState
    final_counts: Optional[Dict[str, Any]]
    done: threading.Event
    result: Dict[str, Any] = field(default_factory=dict)


class _Shutdown:
    pass


# ---------------------------------------------------------------------------
# The sink
# ---------------------------------------------------------------------------


class ParquetSink:
    """Write ``BatchParseResult`` streams to per-scan parquet directories. See module docstring.

    Args:
        parquet_dir: Root directory; each scan gets ``<parquet_dir>/<scan_name>/``.
        roll_bytes: Close a file and start the next once it reaches this size.
        row_group_rows: Target rows per parquet row group.
        queue_max_rows: Bound on rows waiting for the writer thread (memory ≈ 14 B/row).
        on_full: ``"drop"`` (default) drops the batch and marks the scan incomplete;
            ``"block"`` makes :meth:`offer` wait for room (slows the caller).
        roll_seconds: Optional time-based roll in addition to ``roll_bytes``.
        meta_json_path: Optional JSON file whose ``user``/``detector`` objects are
            embedded in every footer (read lazily at the first row-group write).
        compression: Parquet compression codec.
    """

    def __init__(
        self,
        parquet_dir: PathLike,
        roll_bytes: int = DEFAULT_ROLL_BYTES,
        row_group_rows: int = DEFAULT_ROW_GROUP_ROWS,
        queue_max_rows: int = DEFAULT_QUEUE_MAX_ROWS,
        on_full: str = "drop",
        roll_seconds: Optional[float] = None,
        meta_json_path: Optional[PathLike] = None,
        compression: str = DEFAULT_COMPRESSION,
    ):
        if on_full not in ("drop", "block"):
            raise ValueError(f"on_full must be 'drop' or 'block', got {on_full!r}")
        self.parquet_dir = Path(parquet_dir)
        self.roll_bytes = int(roll_bytes)
        self.row_group_rows = int(row_group_rows)
        self.queue_max_rows = int(queue_max_rows)
        self.on_full = on_full
        self.roll_seconds = roll_seconds
        self.meta_json_path = Path(meta_json_path) if meta_json_path else None
        self.compression = compression

        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._queue: "queue.Queue[Union[_Batch, _StopCmd, _Shutdown]]" = queue.Queue()
        self._queued_rows = 0
        self._scan: Optional[_ScanState] = None
        self._last_scan: Optional[_ScanState] = None
        self._pending_before_start: List[Dict[str, Dict[str, np.ndarray]]] = []
        self._pending_rows = 0
        self._enabled = True
        self._error: Optional[str] = None
        self._closer_queue: "queue.Queue[Optional[_CloseJob]]" = queue.Queue(maxsize=1)
        self._writer_thread: Optional[threading.Thread] = None
        self._closer_thread: Optional[threading.Thread] = None
        self._offer_error_logged = False

    # -- public API ----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self, scan_name: str, metadata: Dict[str, Any]) -> Path:
        """Begin a scan: create ``<parquet_dir>/<scan_name>/{events,tdc,control}/``.

        ``metadata`` holds the per-scan fields of :class:`ScanFields` that the
        caller knows (mode, TDC settings, detector size, source…); the sink adds
        ``scan_name``, ``start_time_iso``, ``writer_config`` and the environment.
        Batches offered before this call are written to the new scan.
        """
        self._ensure_threads()
        scan_dir = self.parquet_dir / scan_name
        try:
            for table in TABLES:
                (scan_dir / table).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._fail(f"cannot create scan directory {scan_dir}: {e}")
            raise

        fields = dict(metadata)
        fields.setdefault("start_time_iso", _utc_now_iso())
        fields["scan_name"] = scan_name
        fields.update(environment_fields())
        fields["writer_config"] = WriterConfig(
            roll_bytes=self.roll_bytes,
            roll_seconds=self.roll_seconds,
            row_group_rows=self.row_group_rows,
            compression=self.compression,
            encodings=dict(ENCODINGS),
            on_full=self.on_full,  # type: ignore[arg-type]
        ).model_dump()
        source = fields.get("source")
        if source is None:
            fields["source"] = SourceInfo().model_dump()
        elif isinstance(source, SourceInfo):
            fields["source"] = source.model_dump()
        # Validate the per-scan part now so a wrong caller fails at start(), not at the first roll.
        fields = ScanFields.model_validate(fields).model_dump()

        scan = _ScanState(
            scan_name=scan_name, scan_dir=scan_dir, scan_fields=fields, start_mono=time.monotonic(), sink=self
        )
        with self._cv:
            self._scan = scan
            self._last_scan = scan
            pending, self._pending_before_start = self._pending_before_start, []
            self._pending_rows = 0
        for cols in pending:
            self._enqueue(scan, cols)
        logger.info("ParquetSink: scan %s → %s", scan_name, scan_dir)
        return scan_dir

    def offer(self, result: Any) -> None:
        """Enqueue a ``BatchParseResult``. Never raises; non-blocking unless ``on_full="block"``."""
        if not self._enabled:
            return
        try:
            cols, rows = self._coerce(result)
            if rows == 0:
                return
            with self._cv:
                scan = self._scan
                if scan is None:
                    if self._pending_rows + rows <= PENDING_BEFORE_START_MAX_ROWS:
                        self._pending_before_start.append(cols)
                        self._pending_rows += rows
                    return
                scan.rows_offered += rows
                if scan.first_batch_wall_iso is None:
                    scan.first_batch_wall_iso = _utc_now_iso()
            self._enqueue(scan, cols)
        except Exception:
            if not self._offer_error_logged:
                self._offer_error_logged = True
                logger.exception("ParquetSink.offer failed; further errors suppressed")

    def stop(self, final_counts: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Optional[Dict[str, Any]]:
        """Finish the current scan: drain, close files, write ``_scan_meta.json``.

        Returns a summary dict (``scan_dir``, ``status``, ``rows``, ``dropped_rows``,
        ``files``) or ``None`` when no scan was active. ``final_counts`` (the
        ``TimePixStop`` fields) are recorded in the sidecar.
        """
        with self._cv:
            scan = self._scan
            self._scan = None
        if scan is None:
            return None
        cmd = _StopCmd(scan=scan, final_counts=final_counts, done=threading.Event())
        if not self._enabled or self._writer_thread is None or not self._writer_thread.is_alive():
            # Writer is dead (disk error): report what we know without blocking.
            scan.status = "error"
            return self._summary_dict(scan)
        self._queue.put(cmd)
        if not cmd.done.wait(timeout):
            logger.warning("ParquetSink.stop: scan %s did not finish within %.0fs", scan.scan_name, timeout)
            scan.status = scan.status if scan.status == "error" else "incomplete"
            return self._summary_dict(scan)
        return cmd.result or self._summary_dict(scan)

    def shutdown(self, timeout: float = 10.0) -> None:
        """Stop the worker threads (after :meth:`stop`). Idempotent."""
        if self._scan is not None:
            self.stop(timeout=timeout)
        if self._writer_thread and self._writer_thread.is_alive():
            self._queue.put(_Shutdown())
            self._writer_thread.join(timeout)
        if self._closer_thread and self._closer_thread.is_alive():
            self._closer_queue.put(None)
            self._closer_thread.join(timeout)

    def stats(self) -> Dict[str, Any]:
        """Snapshot for the heartbeat (cheap; called from another thread)."""
        with self._lock:
            scan = self._scan or self._last_scan
            q_rows = self._queued_rows
        if scan is None:
            status = "error" if not self._enabled else "idle"
            rows = files = dropped = 0
        else:
            status = scan.status
            rows = scan.writers["events"].rows_total
            files = sum(w.files_closed for w in scan.writers.values())
            dropped = scan.dropped_rows
        out = {
            "parquet_status": status,
            "parquet_rows": rows,
            "parquet_files": files,
            "parquet_dropped": dropped,
            "parquet_q_rows": q_rows,
            "parquet_q_max": self.queue_max_rows,
        }
        fb = free_bytes(self.parquet_dir)
        if fb is not None:
            out["parquet_free_gb"] = round(fb / 1e9, 2)
        return out

    # -- internals: caller side ----------------------------------------------

    @staticmethod
    def _coerce(result: Any) -> "tuple[Dict[str, Dict[str, np.ndarray]], int]":
        """Columns per table in on-disk dtypes. ``_empty_result`` gives int64 timestamps; normalise."""
        ev = COLUMN_DTYPES["events"]
        td = COLUMN_DTYPES["tdc"]
        ct = COLUMN_DTYPES["control"]
        cols: Dict[str, Dict[str, np.ndarray]] = {}
        rows = 0
        if result.n_pixels:
            cols["events"] = {
                "x": np.asarray(result.pixel_x).astype(ev["x"], copy=False),
                "y": np.asarray(result.pixel_y).astype(ev["y"], copy=False),
                "tot": np.asarray(result.pixel_tot).astype(ev["tot"], copy=False),
                "t": np.asarray(result.pixel_timestamp).astype(ev["t"], copy=False),
            }
            rows += int(result.n_pixels)
        if result.n_tdc:
            cols["tdc"] = {
                "t": np.asarray(result.tdc_timestamp).astype(td["t"], copy=False),
                "channel": np.asarray(result.tdc_channel).astype(td["channel"], copy=False),
                "edge": np.asarray(result.tdc_edge).astype(td["edge"], copy=False),
            }
            rows += int(result.n_tdc)
        if result.n_control:
            cols["control"] = {
                "t": np.asarray(result.control_timestamp).astype(ct["t"], copy=False),
                "subtype": np.asarray(result.control_subtype).astype(ct["subtype"], copy=False),
            }
            rows += int(result.n_control)
        return cols, rows

    def _enqueue(self, scan: _ScanState, cols: Dict[str, Dict[str, np.ndarray]]) -> None:
        rows = sum(len(c["t"]) for c in cols.values())
        with self._cv:
            if self._queued_rows + rows > self.queue_max_rows:
                if self.on_full == "drop":
                    scan.dropped_rows += rows
                    scan.dropped_batches += 1
                    if scan.status == "writing":
                        scan.status = "incomplete"
                    now = time.monotonic()
                    if now - scan._last_drop_log >= DROP_LOG_INTERVAL_S:
                        scan._last_drop_log = now
                        logger.warning(
                            "ParquetSink: queue full (%d rows), dropped %d rows so far for scan %s",
                            self._queued_rows,
                            scan.dropped_rows,
                            scan.scan_name,
                        )
                    return
                while self._queued_rows + rows > self.queue_max_rows and self._enabled:
                    self._cv.wait(timeout=0.5)
                if not self._enabled:
                    return
            self._queued_rows += rows
        self._queue.put(_Batch(scan=scan, cols=cols, rows=rows))

    def _ensure_threads(self) -> None:
        if self._writer_thread is None or not self._writer_thread.is_alive():
            self._writer_thread = threading.Thread(target=self._writer_main, name="parquet-writer", daemon=True)
            self._writer_thread.start()
        if self._closer_thread is None or not self._closer_thread.is_alive():
            self._closer_thread = threading.Thread(target=self._closer_main, name="parquet-closer", daemon=True)
            self._closer_thread.start()

    def _fail(self, message: str) -> None:
        """Disable the sink after an unrecoverable I/O error; offer() becomes a counted no-op."""
        logger.error("ParquetSink disabled: %s", message)
        with self._cv:
            self._enabled = False
            self._error = message
            scan = self._scan or self._last_scan
            if scan is not None:
                scan.status = "error"
                scan.error = message
            self._cv.notify_all()

    def _summary_dict(self, scan: _ScanState) -> Dict[str, Any]:
        return {
            "scan_name": scan.scan_name,
            "scan_dir": str(scan.scan_dir),
            "status": scan.status,
            "rows": scan.writers["events"].rows_total,
            "rows_by_table": {n: w.rows_total for n, w in scan.writers.items()},
            "dropped_rows": scan.dropped_rows,
            "dropped_batches": scan.dropped_batches,
            "files": sum(len(w.files) for w in scan.writers.values()),
            "error": scan.error,
        }

    # -- internals: writer thread ---------------------------------------------

    def _writer_main(self) -> None:
        logger.info("ParquetSink writer thread started")
        while True:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                item = None
            try:
                if isinstance(item, _Shutdown):
                    break
                if isinstance(item, _StopCmd):
                    self._handle_stop(item)
                    continue
                if isinstance(item, _Batch):
                    self._handle_batch(item)
                # Time-based row-group flushes, also during silence.
                scan = self._scan
                if scan is not None and scan.status != "error":
                    now = time.monotonic()
                    for tw in scan.writers.values():
                        if tw.due(now):
                            tw.write_row_group(now)
            except OSError as e:
                self._fail(f"{type(e).__name__}: {e}")
                self._abandon_open_files()
            except Exception:
                logger.exception("ParquetSink writer thread: unexpected error")
                self._fail("unexpected error in writer thread (see log)")
                self._abandon_open_files()
        logger.info("ParquetSink writer thread finished")

    def _handle_batch(self, batch: _Batch) -> None:
        with self._cv:
            self._queued_rows -= batch.rows
            self._cv.notify_all()
        scan = batch.scan
        if scan.status == "error":
            return
        for table, cols in batch.cols.items():
            scan.writers[table].append(cols, len(cols["t"]))
        now = time.monotonic()
        for tw in scan.writers.values():
            if tw.due(now):
                tw.write_row_group(now)

    def _handle_stop(self, cmd: _StopCmd) -> None:
        scan = cmd.scan
        try:
            if scan.status != "error":
                now = time.monotonic()
                events: List[threading.Event] = []
                for tw in scan.writers.values():
                    ev = tw.finish(now)
                    if ev is not None:
                        events.append(ev)
                for ev in events:
                    ev.wait(timeout=60.0)
                if scan.status == "writing":
                    scan.status = "complete"
            self._write_sidecar(scan, cmd.final_counts)
        except OSError as e:
            self._fail(f"{type(e).__name__}: {e}")
            self._abandon_open_files()
        finally:
            cmd.result = self._summary_dict(scan)
            logger.info(
                "ParquetSink: scan %s %s — %d event rows, %d files, %d dropped rows",
                scan.scan_name,
                scan.status,
                cmd.result["rows"],
                cmd.result["files"],
                scan.dropped_rows,
            )
            cmd.done.set()

    def _write_sidecar(self, scan: _ScanState, final_counts: Optional[Dict[str, Any]]) -> None:
        scan.load_extra_meta()
        duration = time.monotonic() - scan.start_mono
        summary = ScanSummary(
            **scan.scan_fields,
            status=scan.status,  # type: ignore[arg-type]
            stop_time_iso=_utc_now_iso(),
            acquisition_duration_s=round(duration, 3),
            first_batch_wall_time_iso=scan.first_batch_wall_iso,
            tables={name: tw.summary() for name, tw in scan.writers.items()},
            dropped_rows=scan.dropped_rows,
            dropped_batches=scan.dropped_batches,
            error=scan.error,
            stop_message=dict(final_counts) if final_counts else None,
        )
        atomic_write_json(scan.scan_dir / SCAN_META_FILENAME, summary.model_dump())

    def _abandon_open_files(self) -> None:
        """After an error: close handles without producing files (the .tmp stays for forensics)."""
        scan = self._scan or self._last_scan
        if scan is None:
            return
        for tw in scan.writers.values():
            of = tw.open_file
            tw.open_file = None
            if of is None:
                continue
            for closer in (of.writer.close, of.fh.close):
                try:
                    closer()
                except Exception:
                    pass

    # -- internals: closer thread ---------------------------------------------

    def _closer_submit(self, job: _CloseJob) -> None:
        # One-slot hand-off: blocks the writer only if the previous close is still running.
        self._closer_queue.put(job)

    def _closer_main(self) -> None:
        while True:
            job = self._closer_queue.get()
            if job is None:
                break
            of = job.open_file
            try:
                of.writer.add_key_value_metadata(job.kv)
                of.writer.close()
                of.fh.flush()
                try:
                    os.fsync(of.fh.fileno())
                except OSError:
                    pass
                of.fh.close()
                os.replace(of.tmp_path, of.final_path)
                fsync_dir(of.final_path.parent)
                logger.debug("ParquetSink: closed %s (%d rows)", of.final_path, of.num_rows)
            except Exception as e:
                try:
                    of.fh.close()
                except Exception:
                    pass
                job.sink._fail(f"closing {of.tmp_path}: {type(e).__name__}: {e}")
            finally:
                if job.done is not None:
                    job.done.set()
