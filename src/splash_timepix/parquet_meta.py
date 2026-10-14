"""On-disk format of a parquet scan: table schemas, footer metadata model and reader helpers.

Layout of one scan written by :class:`splash_timepix.parquet_sink.ParquetSink`::

    <parquet_dir>/<scan_uuid>/
        events/part-000000.parquet …   x:uint16, y:uint16, tot:uint16, t:uint64
        tdc/part-000000.parquet …      t:uint64, channel:uint8, edge:uint8
        control/part-000000.parquet …  t:uint64, subtype:uint8
        _scan_meta.json                completion marker and per-scan summary
        _tiled_registered.json         written by tpx-tiled-register on success

Every parquet file carries one JSON document under the key/value metadata key
``splash_timepix`` — a serialised :class:`ParquetScanMetadata`. Timestamps are
stored in native detector ticks; the clocks needed to convert them are part of
the metadata, both as Arrow field metadata and in the JSON block.

The human-readable description of this format lives in
``info/outputs/parquet/format.md`` and is versioned with :data:`SCHEMA_VERSION`.
"""

from __future__ import annotations

import importlib.metadata
import json
import socket
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

import numpy as np
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field

PathLike = Union[str, Path]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1
KV_KEY = "splash_timepix"
KV_KEY_BYTES = KV_KEY.encode()

#: Timestamp clock of the 56-bit ``t`` column: 3840 MHz, exactly.
CLOCK_HZ = 3_840_000_000
#: Time-over-threshold clock: 25 ns ticks (40 MHz), exactly.
TOT_CLOCK_HZ = 40_000_000

TABLES = ("events", "tdc", "control")
TableName = Literal["events", "tdc", "control"]

SCAN_META_FILENAME = "_scan_meta.json"
TILED_MARKER_FILENAME = "_tiled_registered.json"
PART_FILENAME_FORMAT = "part-{index:06d}.parquet"
TMP_PREFIX = "."
TMP_SUFFIX = ".tmp"


def _t_field() -> pa.Field:
    return pa.field("t", pa.uint64(), nullable=False, metadata={"unit": "tick", "clock_hz": str(CLOCK_HZ)})


TABLE_SCHEMAS: Dict[str, pa.Schema] = {
    "events": pa.schema(
        [
            pa.field("x", pa.uint16(), nullable=False, metadata={"unit": "pixel"}),
            pa.field("y", pa.uint16(), nullable=False, metadata={"unit": "pixel"}),
            pa.field("tot", pa.uint16(), nullable=False, metadata={"unit": "tick", "clock_hz": str(TOT_CLOCK_HZ)}),
            _t_field(),
        ]
    ),
    "tdc": pa.schema(
        [
            _t_field(),
            pa.field("channel", pa.uint8(), nullable=False),
            pa.field("edge", pa.uint8(), nullable=False, metadata={"0": "rising", "1": "falling"}),
        ]
    ),
    "control": pa.schema(
        [
            _t_field(),
            pa.field(
                "subtype",
                pa.uint8(),
                nullable=False,
                metadata={"0": "shutter_open", "1": "shutter_close", "2": "heartbeat", "3": "timestamp"},
            ),
        ]
    ),
}

#: NumPy dtype of every on-disk column, used by the sink to coerce batches.
COLUMN_DTYPES: Dict[str, Dict[str, np.dtype]] = {
    name: {f.name: schema.field(f.name).type.to_pandas_dtype() for f in schema}
    for name, schema in TABLE_SCHEMAS.items()
}


# ---------------------------------------------------------------------------
# Metadata models
# ---------------------------------------------------------------------------


class WriterConfig(BaseModel):
    """How the files of a scan were written (informational)."""

    roll_bytes: int
    roll_seconds: Optional[float] = None
    row_group_rows: int
    compression: str = "zstd"
    encodings: Dict[str, str] = Field(default_factory=dict)
    on_full: Literal["drop", "block"] = "drop"


class SourceInfo(BaseModel):
    """Where the packets came from."""

    kind: Literal["live", "replay", "simulator"] = "live"
    path: Optional[str] = None


class ScanFields(BaseModel):
    """The per-scan part of the metadata — identical in every file of a scan."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    scan_name: str = Field(..., description="uuid4 of the acquisition; also the directory name")
    start_time_iso: str = Field(..., description="UTC time the client connected")
    mode: Literal["timing", "alignment"] = "timing"

    tdc_frequency_hz: float
    tdc_channel: int
    tdc_edge: str
    t_delta_ns: float
    t_cycle_ns: float
    n_bins: int
    collapse_y: bool
    detector_size_x: int
    detector_size_y: int

    clock_hz: int = CLOCK_HZ
    tot_clock_hz: int = TOT_CLOCK_HZ

    splash_timepix_version: str = ""
    pyarrow_version: str = ""
    hostname: str = ""

    writer_config: WriterConfig
    source: SourceInfo = Field(default_factory=SourceInfo)
    #: Operator-entered values (sample, proposal, notes…) from ``--parquet-meta-json``.
    user: Dict[str, Any] = Field(default_factory=dict)
    #: Serval snapshot taken by ``acq.py`` from ``--parquet-meta-json``; empty for replay/simulator.
    detector: Dict[str, Any] = Field(default_factory=dict)


class ParquetScanMetadata(ScanFields):
    """The JSON document stored under ``splash_timepix`` in every parquet footer."""

    table: TableName
    file_index: int
    num_rows: int = 0
    t_first: Optional[int] = None
    t_last: Optional[int] = None
    t_monotonic: Optional[bool] = None
    #: Float copies for JavaScript clients, which round integers above 2**53.
    t_first_s: Optional[float] = None
    t_last_s: Optional[float] = None
    first_batch_wall_time_iso: Optional[str] = None
    dropped_rows_so_far: int = 0

    def to_kv(self) -> Dict[str, str]:
        """Key/value pair for ``ParquetWriter.add_key_value_metadata``."""
        return {KV_KEY: self.model_dump_json()}


class TableSummary(BaseModel):
    num_rows: int = 0
    num_files: int = 0
    files: List[str] = Field(default_factory=list)
    t_first: Optional[int] = None
    t_last: Optional[int] = None


class ScanSummary(ScanFields):
    """Content of ``_scan_meta.json``: per-scan fields plus what is known only at stop."""

    status: Literal["complete", "incomplete", "error"] = "complete"
    stop_time_iso: Optional[str] = None
    acquisition_duration_s: Optional[float] = None
    first_batch_wall_time_iso: Optional[str] = None
    tables: Dict[str, TableSummary] = Field(default_factory=dict)
    dropped_rows: int = 0
    dropped_batches: int = 0
    error: Optional[str] = None
    #: The ``TimePixStop`` fields as published on ZMQ. ``total_packets`` there counts
    #: *binned* pixels and will not equal the parquet row count.
    stop_message: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Helpers for writers
# ---------------------------------------------------------------------------


def package_version() -> str:
    try:
        return importlib.metadata.version("splash_timepix")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def environment_fields() -> Dict[str, str]:
    return {
        "splash_timepix_version": package_version(),
        "pyarrow_version": pa.__version__,
        "hostname": socket.gethostname(),
    }


def ticks_to_seconds(t: Union[int, float, np.ndarray], clock_hz: int = CLOCK_HZ) -> Union[float, np.ndarray]:
    """Convert ``t`` ticks to seconds as float64."""
    if isinstance(t, np.ndarray):
        return t.astype(np.float64) / float(clock_hz)
    return float(t) / float(clock_hz)


def is_sidecar_or_tmp(name: str) -> bool:
    """True for files parquet readers skip: leading ``.`` (temporary) or ``_`` (sidecar)."""
    return name.startswith(TMP_PREFIX) or name.startswith("_")


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


class ScanFormatError(ValueError):
    """Raised when a file or directory is not a splash_timepix parquet scan we understand."""


def read_kv_block(path: PathLike) -> Dict[str, Any]:
    """Return the raw ``splash_timepix`` JSON document from a parquet footer."""
    md = pq.read_metadata(path)
    kv = md.metadata or {}
    raw = kv.get(KV_KEY_BYTES)
    if raw is None:
        raise ScanFormatError(f"{path}: no '{KV_KEY}' key in the parquet footer")
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ScanFormatError(f"{path}: '{KV_KEY}' footer block is not JSON: {e}") from e
    version = doc.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ScanFormatError(
            f"{path}: schema_version {version!r} not supported by this splash_timepix "
            f"(expects {SCHEMA_VERSION}); upgrade the package or convert the file"
        )
    return doc


def read_file_metadata(path: PathLike) -> ParquetScanMetadata:
    """Validated footer metadata of one parquet file."""
    return ParquetScanMetadata.model_validate(read_kv_block(path))


def list_part_files(table_dir: PathLike) -> List[Path]:
    """Completed parquet files of a table directory, sorted by part index."""
    d = Path(table_dir)
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.suffix == ".parquet" and not is_sidecar_or_tmp(p.name))


def read_scan_summary(scan_dir: PathLike) -> ScanSummary:
    """``_scan_meta.json`` of a scan directory, or a summary rebuilt from the footers.

    A rebuilt summary has ``status="incomplete"`` because the sidecar is written
    last: its absence means the sink never finished the scan.
    """
    d = Path(scan_dir)
    sidecar = d / SCAN_META_FILENAME
    if sidecar.is_file():
        with open(sidecar, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        version = doc.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ScanFormatError(f"{sidecar}: schema_version {version!r} not supported (expects {SCHEMA_VERSION})")
        return ScanSummary.model_validate(doc)

    tables: Dict[str, TableSummary] = {}
    scan_fields: Optional[Dict[str, Any]] = None
    first_batch: Optional[str] = None
    dropped = 0
    for table in TABLES:
        files = list_part_files(d / table)
        summary = TableSummary(num_files=len(files), files=[p.name for p in files])
        for p in files:
            meta = read_file_metadata(p)
            if scan_fields is None:
                scan_fields = ScanFields.model_validate(meta.model_dump()).model_dump()
            summary.num_rows += meta.num_rows
            if meta.t_first is not None:
                summary.t_first = meta.t_first if summary.t_first is None else min(summary.t_first, meta.t_first)
            if meta.t_last is not None:
                summary.t_last = meta.t_last if summary.t_last is None else max(summary.t_last, meta.t_last)
            dropped = max(dropped, meta.dropped_rows_so_far)
            first_batch = first_batch or meta.first_batch_wall_time_iso
        tables[table] = summary
    if scan_fields is None:
        raise ScanFormatError(f"{d}: no {SCAN_META_FILENAME} and no completed parquet files")
    return ScanSummary(
        **scan_fields,
        status="incomplete",
        tables=tables,
        dropped_rows=dropped,
        first_batch_wall_time_iso=first_batch,
    )


def read_scan_metadata(path: PathLike) -> Union[ParquetScanMetadata, ScanSummary]:
    """Validated metadata of a parquet file (:class:`ParquetScanMetadata`) or of a scan directory
    (:class:`ScanSummary`)."""
    p = Path(path)
    if p.is_dir():
        return read_scan_summary(p)
    if p.is_file():
        return read_file_metadata(p)
    raise FileNotFoundError(p)


class ScanTables:
    """The three tables of a scan directory as pyarrow datasets.

    ``events``, ``tdc`` and ``control`` are :class:`pyarrow.dataset.Dataset`
    objects (lazy; temporary ``.``-prefixed and sidecar ``_``-prefixed files are
    ignored). :meth:`read` materialises one as a :class:`pyarrow.Table`;
    :meth:`to_seconds` converts a ``t`` column with the scan's clock.
    """

    def __init__(self, scan_dir: PathLike):
        self.path = Path(scan_dir)
        if not self.path.is_dir():
            raise FileNotFoundError(self.path)
        self._datasets: Dict[str, ds.Dataset] = {}
        for table in TABLES:
            table_dir = self.path / table
            files = list_part_files(table_dir)
            schema = TABLE_SCHEMAS[table]
            if files:
                self._datasets[table] = ds.dataset([str(p) for p in files], format="parquet", schema=schema)
            else:
                self._datasets[table] = ds.dataset(pa.table([], schema=schema))
        self.clock_hz = CLOCK_HZ
        try:
            meta = read_scan_summary(self.path)
            self.clock_hz = meta.clock_hz
            self.metadata: Optional[ScanSummary] = meta
        except ScanFormatError:
            self.metadata = None

    @property
    def events(self) -> ds.Dataset:
        return self._datasets["events"]

    @property
    def tdc(self) -> ds.Dataset:
        return self._datasets["tdc"]

    @property
    def control(self) -> ds.Dataset:
        return self._datasets["control"]

    def read(self, table: str, columns: Optional[List[str]] = None, **kwargs: Any) -> pa.Table:
        """Materialise one table (``kwargs`` go to ``Dataset.to_table``, e.g. ``filter=``)."""
        if table not in self._datasets:
            raise KeyError(f"unknown table {table!r}; expected one of {TABLES}")
        return self._datasets[table].to_table(columns=columns, **kwargs)

    def to_seconds(self, t: Union[np.ndarray, pa.Array, pa.ChunkedArray, int]) -> Union[np.ndarray, float]:
        """Ticks → seconds using this scan's clock."""
        if isinstance(t, (pa.Array, pa.ChunkedArray)):
            t = t.to_numpy()
        return ticks_to_seconds(t, self.clock_hz)


def open_scan(scan_dir: PathLike) -> ScanTables:
    """Open a scan directory; see :class:`ScanTables`."""
    return ScanTables(scan_dir)
