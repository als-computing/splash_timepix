"""Unit tests for :class:`splash_timepix.parquet_sink.ParquetSink` (no sockets, no subprocesses)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from splash_timepix.parquet_meta import (
    KV_KEY_BYTES,
    SCAN_META_FILENAME,
    TABLES,
    list_part_files,
    read_file_metadata,
    read_scan_summary,
)
from splash_timepix.parquet_sink import ParquetSink, check_writable
from splash_timepix.parser import PacketParser

from .conftest import expected_events, make_batch

pytestmark = pytest.mark.unit


def _sink(tmp_path: Path, **kw) -> ParquetSink:
    defaults = dict(roll_bytes=50_000, row_group_rows=5_000)
    defaults.update(kw)
    return ParquetSink(tmp_path / "pq", **defaults)


def _events_df(scan_dir: Path) -> pd.DataFrame:
    return pd.read_parquet(scan_dir / "events").sort_values("t").reset_index(drop=True)


class TestSchemaAndContents:
    def test_round_trip_exact(self, tmp_path, scan_meta):
        sink = _sink(tmp_path)
        scan_dir = sink.start("scan-1", scan_meta)
        t0, n = 1000, 30_000
        for k in range(3):
            sink.offer(make_batch(10_000, t0 + k * 100_000, tdc_every=1000, control_every=5000))
        res = sink.stop()
        sink.shutdown()

        assert res["status"] == "complete"
        assert res["rows"] == n
        df = _events_df(scan_dir)
        assert df.dtypes.to_dict() == {
            "x": np.dtype("uint16"),
            "y": np.dtype("uint16"),
            "tot": np.dtype("uint16"),
            "t": np.dtype("uint64"),
        }
        exp = pd.concat([pd.DataFrame(expected_events(10_000, t0 + k * 100_000)) for k in range(3)])
        exp = exp.sort_values("t").reset_index(drop=True)
        pd.testing.assert_frame_equal(df, exp)

        tdc = pd.read_parquet(scan_dir / "tdc")
        assert len(tdc) == 3 * 10
        assert set(tdc["channel"].unique()) == {1}
        assert set(tdc["edge"].unique()) == {0}
        ctl = pd.read_parquet(scan_dir / "control")
        assert len(ctl) == 3 * 2
        assert set(ctl["subtype"].unique()) == {2}

    def test_arrow_field_metadata_carries_units(self, tmp_path, scan_meta):
        sink = _sink(tmp_path)
        scan_dir = sink.start("scan-1", scan_meta)
        sink.offer(make_batch(100, 0))
        sink.stop()
        sink.shutdown()
        schema = pq.read_schema(list_part_files(scan_dir / "events")[0])
        assert schema.field("t").metadata[b"clock_hz"] == b"3840000000"
        assert schema.field("tot").metadata[b"clock_hz"] == b"40000000"

    def test_empty_result_int64_timestamps_are_coerced(self, tmp_path, scan_meta):
        sink = _sink(tmp_path)
        sink.start("scan-1", scan_meta)
        sink.offer(PacketParser()._empty_result())  # int64 empties: must not raise or write rows
        sink.offer(make_batch(10, 5))
        res = sink.stop()
        sink.shutdown()
        assert res["status"] == "complete" and res["rows"] == 10

    def test_every_table_gets_a_file_even_when_empty(self, tmp_path, scan_meta):
        sink = _sink(tmp_path)
        scan_dir = sink.start("scan-1", scan_meta)
        sink.offer(make_batch(50, 0))  # pixels only
        sink.stop()
        sink.shutdown()
        for table in TABLES:
            files = list_part_files(scan_dir / table)
            assert len(files) == 1, table
            assert pq.read_table(files[0]).num_rows == (50 if table == "events" else 0)
        # The empty files still carry a valid footer block.
        meta = read_file_metadata(list_part_files(scan_dir / "tdc")[0])
        assert meta.table == "tdc" and meta.num_rows == 0 and meta.t_first is None


class TestRollingAndFooters:
    def test_rolls_per_table_on_own_size(self, tmp_path, scan_meta):
        sink = _sink(tmp_path, roll_bytes=20_000, row_group_rows=2_000)
        scan_dir = sink.start("scan-1", scan_meta)
        for k in range(20):
            # Random x/y/tot so dictionary encoding cannot shrink the file to nothing.
            sink.offer(_random_batch(5_000, t0=k * 10_000_000, seed=k))
        sink.stop()
        sink.shutdown()

        ev_files = list_part_files(scan_dir / "events")
        tdc_files = list_part_files(scan_dir / "tdc")
        assert len(ev_files) > 1, "events never rolled"
        assert len(tdc_files) == 1, "tiny tdc table should not roll with events"
        # Sequential indices, no gaps, no .tmp left behind.
        assert [p.name for p in ev_files] == [f"part-{i:06d}.parquet" for i in range(len(ev_files))]
        assert not list((scan_dir / "events").glob(".*.tmp"))
        # Per-file footer values match the file's contents; no empty trailing file.
        for p in ev_files:
            meta = read_file_metadata(p)
            tbl = pq.read_table(p)
            assert tbl.num_rows > 0, f"{p.name} is empty"
            assert meta.num_rows == tbl.num_rows
            t = tbl.column("t").to_numpy()
            assert meta.t_first == int(t.min()) and meta.t_last == int(t.max())
            assert meta.t_monotonic is True
        # Time spans of events and tdc cover the same scan.
        tdc_meta = read_file_metadata(tdc_files[0])
        assert tdc_meta.t_first >= read_file_metadata(ev_files[0]).t_first
        assert tdc_meta.t_last <= read_file_metadata(ev_files[-1]).t_last + 1

    def test_monotonic_flag_false_for_shuffled_batch(self, tmp_path, scan_meta):
        sink = _sink(tmp_path)
        scan_dir = sink.start("scan-1", scan_meta)
        sink.offer(make_batch(2_000, 0, shuffle=True))
        sink.stop()
        sink.shutdown()
        meta = read_file_metadata(list_part_files(scan_dir / "events")[0])
        assert meta.t_monotonic is False
        assert meta.t_first == 0 and meta.t_last == 1_999 * 10

    def test_footer_has_scan_fields_user_and_detector(self, tmp_path, scan_meta):
        meta_json = tmp_path / "meta.json"
        meta_json.write_text(json.dumps({"user": {"sample": "Cu"}, "detector": {"serval_version": "3.3.2"}}))
        sink = _sink(tmp_path, meta_json_path=meta_json)
        scan_dir = sink.start("scan-xyz", scan_meta)
        sink.offer(make_batch(100, 0))
        sink.stop()
        sink.shutdown()
        p = list_part_files(scan_dir / "events")[0]
        raw = json.loads(pq.read_metadata(p).metadata[KV_KEY_BYTES])
        assert raw["scan_name"] == "scan-xyz"
        assert raw["schema_version"] == 1
        assert raw["clock_hz"] == 3840000000 and raw["tot_clock_hz"] == 40000000
        assert raw["user"] == {"sample": "Cu"}
        assert raw["detector"] == {"serval_version": "3.3.2"}
        assert raw["source"] == {"kind": "simulator", "path": None}
        assert raw["writer_config"]["compression"] == "zstd"
        assert raw["first_batch_wall_time_iso"] is not None
        assert raw["t_first_s"] == pytest.approx(0.0) and raw["t_last_s"] == pytest.approx(990 / 3.84e9)

    def test_meta_json_written_after_start_is_still_picked_up(self, tmp_path, scan_meta):
        """acq.py writes the detector block *after* tpx-stream started; the sink reads lazily."""
        meta_json = tmp_path / "meta.json"
        meta_json.write_text(json.dumps({"user": {"sample": "A"}}))
        sink = _sink(tmp_path, meta_json_path=meta_json)
        scan_dir = sink.start("scan-1", scan_meta)
        meta_json.write_text(json.dumps({"user": {"sample": "A"}, "detector": {"bias": 100}}))
        sink.offer(make_batch(100, 0))
        sink.stop()
        sink.shutdown()
        meta = read_file_metadata(list_part_files(scan_dir / "events")[0])
        assert meta.detector == {"bias": 100}
        assert read_scan_summary(scan_dir).detector == {"bias": 100}


class TestLifecycle:
    def test_offer_before_start_is_buffered(self, tmp_path, scan_meta):
        sink = _sink(tmp_path)
        sink.offer(make_batch(100, 0))
        scan_dir = sink.start("scan-1", scan_meta)
        sink.offer(make_batch(100, 10_000))
        res = sink.stop()
        sink.shutdown()
        assert res["rows"] == 200
        assert len(_events_df(scan_dir)) == 200

    def test_stop_drains_everything_queued(self, tmp_path, scan_meta):
        sink = _sink(tmp_path, row_group_rows=1_000_000)  # nothing flushes until stop
        scan_dir = sink.start("scan-1", scan_meta)
        for k in range(50):
            sink.offer(make_batch(2_000, k * 100_000))
        res = sink.stop()
        sink.shutdown()
        assert res["rows"] == 100_000
        assert len(_events_df(scan_dir)) == 100_000

    def test_sidecar_written_last_with_counts_and_stop_message(self, tmp_path, scan_meta):
        sink = _sink(tmp_path)
        scan_dir = sink.start("scan-1", scan_meta)
        sink.offer(make_batch(1_000, 0, tdc_every=100))
        assert not (scan_dir / SCAN_META_FILENAME).exists()
        sink.stop({"total_packets": 42, "total_cycles": 7})
        sink.shutdown()
        summary = read_scan_summary(scan_dir)
        assert summary.status == "complete"
        assert summary.tables["events"].num_rows == 1_000
        assert summary.tables["tdc"].num_rows == 10
        assert summary.tables["events"].files == ["part-000000.parquet"]
        assert summary.stop_message == {"total_packets": 42, "total_cycles": 7}
        assert summary.acquisition_duration_s is not None and summary.stop_time_iso is not None

    def test_stop_without_start_is_none_and_second_scan_is_separate(self, tmp_path, scan_meta):
        sink = _sink(tmp_path)
        assert sink.stop() is None
        d1 = sink.start("scan-1", scan_meta)
        sink.offer(make_batch(10, 0))
        sink.stop()
        d2 = sink.start("scan-2", scan_meta)
        sink.offer(make_batch(20, 0))
        sink.stop()
        sink.shutdown()
        assert d1 != d2
        assert len(_events_df(d1)) == 10 and len(_events_df(d2)) == 20

    def test_stats_reflect_progress(self, tmp_path, scan_meta):
        sink = _sink(tmp_path)
        assert sink.stats()["parquet_status"] == "idle"
        sink.start("scan-1", scan_meta)
        sink.offer(make_batch(6_000, 0))
        deadline = time.time() + 5
        while sink.stats()["parquet_rows"] < 5_000 and time.time() < deadline:
            time.sleep(0.05)
        st = sink.stats()
        assert st["parquet_status"] == "writing"
        assert st["parquet_rows"] >= 5_000  # one full row group written, remainder pending
        assert st["parquet_q_max"] == sink.queue_max_rows
        sink.stop()
        assert sink.stats()["parquet_status"] == "complete"
        sink.shutdown()

    def test_completed_file_readable_while_tmp_present(self, tmp_path, scan_meta):
        """Criterion 1 at unit level: a rolled file opens while the next is still being written."""
        sink = _sink(tmp_path, roll_bytes=20_000, row_group_rows=2_000)
        scan_dir = sink.start("scan-1", scan_meta)
        for k in range(12):
            sink.offer(_random_batch(5_000, t0=k * 10_000_000, seed=k))
        deadline = time.time() + 5
        while not list_part_files(scan_dir / "events") and time.time() < deadline:
            time.sleep(0.05)
        completed = list_part_files(scan_dir / "events")
        assert completed, "no file rolled while writing"
        tmp_files = list((scan_dir / "events").glob(".part-*.parquet.tmp"))
        assert tmp_files, "expected an in-progress .tmp file alongside the completed one"
        # pandas/pyarrow dataset discovery must ignore the dot-file and read the completed ones.
        # Another file may complete between the listing and the read, hence the bracket.
        rows_before = sum(pq.read_metadata(p).num_rows for p in completed)
        df = pd.read_parquet(scan_dir / "events")
        rows_after = sum(pq.read_metadata(p).num_rows for p in list_part_files(scan_dir / "events"))
        assert rows_before <= len(df) <= rows_after
        sink.stop()
        sink.shutdown()


class TestBackpressure:
    def test_drop_policy_counts_and_marks_incomplete(self, tmp_path, scan_meta):
        sink = _sink(tmp_path, queue_max_rows=1_000, on_full="drop")
        # Stall the writer so the queue fills: swap its thread target with a blocked one.
        sink._ensure_threads()
        gate = _block_writer(sink)
        try:
            sink.start("scan-1", scan_meta)
            for k in range(10):
                sink.offer(make_batch(500, k * 10_000))
        finally:
            gate.set()
        res = sink.stop()
        sink.shutdown()
        assert res["dropped_rows"] > 0
        assert res["dropped_batches"] > 0
        assert res["status"] == "incomplete"
        assert res["rows"] + res["dropped_rows"] == 5_000
        assert read_scan_summary(res["scan_dir"]).status == "incomplete"

    def test_block_policy_loses_nothing(self, tmp_path, scan_meta):
        sink = _sink(tmp_path, queue_max_rows=1_000, on_full="block")
        scan_dir = sink.start("scan-1", scan_meta)
        for k in range(20):
            sink.offer(make_batch(500, k * 10_000))
        res = sink.stop()
        sink.shutdown()
        assert res["dropped_rows"] == 0 and res["status"] == "complete"
        assert len(_events_df(scan_dir)) == 10_000


class TestErrors:
    def test_oserror_turns_sink_off_without_raising(self, tmp_path, scan_meta, monkeypatch):
        sink = _sink(tmp_path)
        scan_dir = sink.start("scan-1", scan_meta)
        # Make the next file open fail as if the disk were gone.
        import splash_timepix.parquet_sink as ps

        def failing_open(self):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(ps._TableWriter, "_open", failing_open)
        for k in range(3):
            sink.offer(make_batch(6_000, k * 100_000))  # never raises into the caller
        res = sink.stop()
        assert res["status"] == "error"
        assert sink.enabled is False
        assert sink.stats()["parquet_status"] == "error"
        sink.offer(make_batch(10, 0))  # counted no-op after the failure
        sink.shutdown()
        # Nothing half-written is visible as a completed file.
        assert list_part_files(scan_dir / "events") == []

    def test_check_writable(self, tmp_path):
        d = check_writable(tmp_path / "new" / "dir")
        assert d.is_dir()
        ro = tmp_path / "ro"
        ro.mkdir()
        os.chmod(ro, 0o500)
        try:
            if os.access(ro, os.W_OK):  # running as root: cannot test the refusal
                pytest.skip("directory permissions not enforced for this user")
            with pytest.raises(OSError):
                check_writable(ro)
        finally:
            os.chmod(ro, 0o700)

    def test_bad_on_full_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            ParquetSink(tmp_path, on_full="explode")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _random_batch(n: int, *, t0: int, seed: int):
    """Incompressible-ish pixel batch so byte-based rolling triggers at small sizes."""
    from splash_timepix.simulator import PacketBuilder

    rng = np.random.default_rng(seed)
    xs = rng.integers(0, 1024, n)
    ys = rng.integers(0, 1024, n)
    tots = rng.integers(0, 1024, n)
    dts = np.cumsum(rng.integers(1, 5000, n))
    packets = [
        PacketBuilder.build_pixel_packet(int(t0 + dts[i]), int(tots[i]), int(xs[i]), int(ys[i])) for i in range(n)
    ]
    packets.append(PacketBuilder.build_tdc_packet(int(t0 + dts[-1] + 1), 1, 0))
    return PacketParser().parse_batch(b"".join(packets))


def _block_writer(sink: ParquetSink):
    """Park the writer thread on an event so offered batches pile up in the queue."""
    import threading

    gate = threading.Event()
    original = sink._handle_batch

    def blocked(batch):
        gate.wait(timeout=10)
        return original(batch)

    sink._handle_batch = blocked  # type: ignore[method-assign]
    return gate
