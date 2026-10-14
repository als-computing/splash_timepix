"""Unit tests for :mod:`splash_timepix.parquet_meta` (models, readers, ``open_scan``)."""

from __future__ import annotations

import json

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from splash_timepix.parquet_meta import (
    CLOCK_HZ,
    KV_KEY,
    SCAN_META_FILENAME,
    SCHEMA_VERSION,
    TABLE_SCHEMAS,
    ParquetScanMetadata,
    ScanFormatError,
    ScanSummary,
    WriterConfig,
    list_part_files,
    open_scan,
    read_file_metadata,
    read_scan_metadata,
    read_scan_summary,
    ticks_to_seconds,
)
from splash_timepix.parquet_sink import ParquetSink

from .conftest import make_batch

pytestmark = pytest.mark.unit


def _write_scan(tmp_path, scan_meta, name="scan-1", n=3_000):
    sink = ParquetSink(tmp_path / "pq", roll_bytes=10**9, row_group_rows=1_000)
    scan_dir = sink.start(name, scan_meta)
    sink.offer(make_batch(n, 0, tdc_every=500, control_every=1500))
    sink.stop({"total_packets": n})
    sink.shutdown()
    return scan_dir


def test_model_round_trips_through_kv_and_json(scan_meta):
    meta = ParquetScanMetadata(
        **scan_meta,
        scan_name="s",
        start_time_iso="2026-10-14T00:00:00+00:00",
        writer_config=WriterConfig(roll_bytes=1, row_group_rows=1),
        table="events",
        file_index=3,
        num_rows=5,
        t_first=1,
        t_last=2,
    )
    kv = meta.to_kv()
    assert set(kv) == {KV_KEY}
    again = ParquetScanMetadata.model_validate_json(kv[KV_KEY])
    assert again == meta
    assert again.clock_hz == CLOCK_HZ and again.schema_version == SCHEMA_VERSION


def test_read_scan_metadata_dispatches_on_file_or_directory(tmp_path, scan_meta):
    scan_dir = _write_scan(tmp_path, scan_meta)
    f = list_part_files(scan_dir / "events")[0]
    assert isinstance(read_scan_metadata(f), ParquetScanMetadata)
    assert isinstance(read_scan_metadata(scan_dir), ScanSummary)
    with pytest.raises(FileNotFoundError):
        read_scan_metadata(tmp_path / "nope")


def test_summary_rebuilt_from_footers_when_sidecar_missing(tmp_path, scan_meta):
    scan_dir = _write_scan(tmp_path, scan_meta)
    (scan_dir / SCAN_META_FILENAME).unlink()
    summary = read_scan_summary(scan_dir)
    assert summary.status == "incomplete"
    assert summary.scan_name == "scan-1"
    assert summary.tables["events"].num_rows == 3_000
    assert summary.tables["tdc"].num_rows == 6
    assert summary.tables["control"].num_rows == 2
    assert summary.tables["events"].t_first == 0 and summary.tables["events"].t_last == 2_999 * 10


def test_unknown_schema_version_is_rejected_clearly(tmp_path, scan_meta):
    scan_dir = _write_scan(tmp_path, scan_meta)
    f = list_part_files(scan_dir / "events")[0]
    doc = json.loads(pq.read_metadata(f).metadata[KV_KEY.encode()])
    doc["schema_version"] = 99
    bad = tmp_path / "bad.parquet"
    table = pq.read_table(f).replace_schema_metadata({KV_KEY: json.dumps(doc)})
    pq.write_table(table, bad)
    with pytest.raises(ScanFormatError, match="schema_version 99"):
        read_file_metadata(bad)

    plain = tmp_path / "plain.parquet"
    pq.write_table(pa.table({"a": [1]}), plain)
    with pytest.raises(ScanFormatError, match=KV_KEY):
        read_file_metadata(plain)

    sidecar = scan_dir / SCAN_META_FILENAME
    doc = json.loads(sidecar.read_text())
    doc["schema_version"] = 99
    sidecar.write_text(json.dumps(doc))
    with pytest.raises(ScanFormatError, match="schema_version 99"):
        read_scan_summary(scan_dir)


def test_open_scan_tables_and_to_seconds(tmp_path, scan_meta):
    scan_dir = _write_scan(tmp_path, scan_meta)
    scan = open_scan(scan_dir)
    assert scan.read("events").num_rows == 3_000
    assert scan.read("tdc").num_rows == 6
    assert scan.read("control").num_rows == 2
    assert scan.read("events", columns=["t"]).column_names == ["t"]
    assert scan.events.schema.equals(TABLE_SCHEMAS["events"], check_metadata=False)
    t = scan.read("events").column("t")
    secs = scan.to_seconds(t)
    np.testing.assert_allclose(secs, t.to_numpy().astype(np.float64) / CLOCK_HZ)
    assert scan.to_seconds(CLOCK_HZ) == 1.0
    assert ticks_to_seconds(np.array([CLOCK_HZ, 2 * CLOCK_HZ], dtype=np.uint64)).tolist() == [1.0, 2.0]
    assert scan.metadata is not None and scan.metadata.status == "complete"
    with pytest.raises(KeyError):
        scan.read("photons")
    with pytest.raises(FileNotFoundError):
        open_scan(tmp_path / "missing")
