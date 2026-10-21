"""Criteria 2 and 3: Tiled reads the files, and the metadata in Tiled's database comes from the files."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest
from tiled.queries import Key

from splash_timepix.parquet_meta import (
    KV_KEY_BYTES,
    SCAN_META_FILENAME,
    TILED_MARKER_FILENAME,
    list_part_files,
    read_scan_summary,
)
from splash_timepix.tiled_register import (
    IncompleteScanError,
    RegistrationError,
    collect_scan,
    ensure_path,
    find_unregistered,
    register_scan,
    register_unregistered,
)

pytestmark = pytest.mark.integration


def _register(rig, scan_dir, **kw):
    container = ensure_path(rig.client, "tpx3")
    return register_scan(container, scan_dir, path="tpx3", **kw)


class TestCriterion2TiledReadsTheFiles:
    def test_read_equals_pyarrow_with_dtypes_and_partitions(self, tiled_rig, write_scan, tmp_path):
        scan_dir = write_scan("scan-1", n_batches=10)
        rig = tiled_rig(readable=[tmp_path / "pq"])
        marker = _register(rig, scan_dir)
        node = rig.client["tpx3"][marker["key"]]

        for table in ("events", "tdc", "control"):
            files = list_part_files(scan_dir / table)
            local = pq.read_table(scan_dir / table).to_pandas()
            remote = node[table].read()
            assert node[table].structure().npartitions == len(files)
            pd.testing.assert_frame_equal(remote.reset_index(drop=True), local.reset_index(drop=True), check_dtype=True)
            for i, f in enumerate(files):
                assert len(node[table].read_partition(i)) == pq.read_metadata(f).num_rows
        ev = node["events"].read()
        assert ev.dtypes.to_dict() == {
            "x": np.dtype("uint16"),
            "y": np.dtype("uint16"),
            "tot": np.dtype("uint16"),
            "t": np.dtype("uint64"),
        }
        assert len(list_part_files(scan_dir / "events")) > 1, "test should exercise several partitions"

    def test_scan_root_outside_readable_storage_fails_loudly(self, tiled_rig, write_scan, tmp_path):
        scan_dir = write_scan("scan-1")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        rig = tiled_rig(readable=[elsewhere])
        with pytest.raises(RegistrationError, match="readable_storage|--read"):
            _register(rig, scan_dir)
        assert not (scan_dir / TILED_MARKER_FILENAME).exists()


class TestCriterion3MetadataFromFilesIntoDatabase:
    def test_metadata_comes_from_footers_not_sidecar(self, tiled_rig, write_scan, tmp_path):
        scan_dir = write_scan("scan-1", n_batches=8, incomplete=True)
        assert not (scan_dir / SCAN_META_FILENAME).exists()
        rig = tiled_rig(readable=[tmp_path / "pq"])
        marker = _register(rig, scan_dir, allow_incomplete=True)
        md = rig.client["tpx3"][marker["key"]].metadata

        footer = json.loads(pq.read_metadata(list_part_files(scan_dir / "events")[0]).metadata[KV_KEY_BYTES])
        for k in ("scan_name", "tdc_frequency_hz", "tdc_channel", "clock_hz", "mode", "user", "detector", "source"):
            assert md[k] == footer[k], k
        assert md["status"] == "incomplete"
        # Values only the files know: t range from row-group statistics, row counts, sizes.
        events = pq.read_table(scan_dir / "events")
        t = events.column("t").to_numpy()
        assert md["t_first"] == int(t.min()) and md["t_last"] == int(t.max())
        assert md["tables"]["events"]["num_rows"] == events.num_rows
        assert md["tables"]["events"]["bytes"] == sum(p.stat().st_size for p in list_part_files(scan_dir / "events"))
        assert md["tables"]["events"]["num_row_groups"] == sum(
            pq.read_metadata(p).num_row_groups for p in list_part_files(scan_dir / "events")
        )

        # Searchable on a key that exists only in the footers / statistics, plus operator and detector fields.
        c = rig.client["tpx3"]
        assert list(c.search(Key("scan_name") == "scan-1")) == [marker["key"]]
        assert list(c.search(Key("tdc_frequency_hz") > 0)) == [marker["key"]]
        assert list(c.search(Key("user.sample") == "Cu foil")) == [marker["key"]]
        assert list(c.search(Key("detector.serval_version") == "3.3.2")) == [marker["key"]]
        mid = int((int(t.min()) + int(t.max())) // 2)
        assert list(c.search(Key("t_first") <= mid).search(Key("t_last") >= mid)) == [marker["key"]]
        assert list(c.search(Key("t_first") > int(t.max()))) == []

    def test_metadata_survives_server_restart_same_sqlite(self, tiled_rig, write_scan, tmp_path):
        scan_dir = write_scan("scan-1")
        rig = tiled_rig(readable=[tmp_path / "pq"])
        marker = _register(rig, scan_dir)
        before = dict(rig.client["tpx3"][marker["key"]].metadata)
        rig.restart()
        c = rig.client["tpx3"]
        assert list(c.search(Key("scan_name") == "scan-1")) == [marker["key"]]
        assert list(c.search(Key("user.esaf") == "P-1")) == [marker["key"]]
        after = dict(c[marker["key"]].metadata)
        assert after == before
        assert len(c[marker["key"]]["events"].read()) == pq.read_table(scan_dir / "events").num_rows

    def test_sidecar_is_cross_checked_and_completion_fields_added(self, tiled_rig, write_scan, tmp_path):
        scan_dir = write_scan("scan-1")
        rig = tiled_rig(readable=[tmp_path / "pq"])
        marker = _register(rig, scan_dir)
        md = rig.client["tpx3"][marker["key"]].metadata
        summary = read_scan_summary(scan_dir)
        assert md["status"] == "complete"
        assert md["stop_message"] == summary.stop_message
        assert md["acquisition_duration_s"] == summary.acquisition_duration_s
        assert md["registration"]["tool"] == "tpx-tiled-register"
        assert md["registration"]["files_signature"]

    def test_ui_meta_json_is_merged_with_raw_tpx3_link_and_slug_key(self, tiled_rig, write_scan, tmp_path):
        scan_dir = write_scan("scan-1")
        root = scan_dir.parent
        slug = "000042_scan1abc_20261014T120000"
        (root / f"{slug}_meta.json").write_text(
            json.dumps({"scan_name": "scan-1", "total_cycles": 7, "energy_calibration": {"pixel_per_ev": 1.5}})
        )
        (root / f"{slug}.tpx3").write_bytes(b"\0" * 12)
        rig = tiled_rig(readable=[root])
        marker = _register(rig, scan_dir)
        assert marker["key"] == slug
        md = rig.client["tpx3"][slug].metadata
        assert md["ui"]["energy_calibration"] == {"pixel_per_ev": 1.5}
        assert md["ui"]["raw_tpx3_path"] == str(root / f"{slug}.tpx3")
        assert list(rig.client["tpx3"].search(Key("ui.energy_calibration.pixel_per_ev") == 1.5)) == [slug]


class TestIdempotencyAndCatchUp:
    def test_reregister_unchanged_keeps_node_and_foreign_metadata(self, tiled_rig, write_scan, tmp_path):
        scan_dir = write_scan("scan-1")
        rig = tiled_rig(readable=[tmp_path / "pq"])
        m1 = _register(rig, scan_dir)
        node = rig.client["tpx3"][m1["key"]]
        node.update_metadata({"analysis": {"peak_ev": 930.5}})
        m2 = _register(rig, scan_dir)
        assert m2["action"] == "updated"
        assert list(rig.client["tpx3"]) == [m1["key"]]
        md = rig.client["tpx3"][m1["key"]].metadata
        assert md["analysis"] == {"peak_ev": 930.5}, "metadata added in Tiled must survive"
        assert md["registration"]["register_time_iso"] >= m1["register_time_iso"]

    def test_incomplete_then_complete_recreates_with_more_partitions(self, tiled_rig, tmp_path, scan_meta):
        from splash_timepix.parquet_sink import ParquetSink
        from tests.parquet.conftest import make_random_batch

        sink = ParquetSink(tmp_path / "pq", roll_bytes=20_000, row_group_rows=2_000)
        scan_dir = sink.start("scan-1", scan_meta)
        for k in range(6):
            sink.offer(make_random_batch(5_000, t0=k * 10_000_000, seed=k))
        # Wait until at least one file has rolled, then register mid-scan.
        import time

        deadline = time.time() + 10
        while not list_part_files(scan_dir / "events") and time.time() < deadline:
            time.sleep(0.05)
        rig = tiled_rig(readable=[tmp_path / "pq"])
        m1 = _register(rig, scan_dir, allow_incomplete=True)
        n1 = rig.client["tpx3"][m1["key"]]["events"].structure().npartitions
        assert rig.client["tpx3"][m1["key"]].metadata["status"] == "incomplete"

        for k in range(6, 12):
            sink.offer(make_random_batch(5_000, t0=k * 10_000_000, seed=k))
        sink.stop()
        sink.shutdown()
        m2 = _register(rig, scan_dir)
        assert m2["action"] == "recreated"
        node = rig.client["tpx3"][m2["key"]]
        assert node["events"].structure().npartitions > n1
        assert node.metadata["status"] == "complete"
        assert len(node["events"].read()) == pq.read_table(scan_dir / "events").num_rows

    def test_force_recreates(self, tiled_rig, write_scan, tmp_path):
        scan_dir = write_scan("scan-1")
        rig = tiled_rig(readable=[tmp_path / "pq"])
        _register(rig, scan_dir)
        assert _register(rig, scan_dir, force=True)["action"] == "recreated"

    def test_refuses_incomplete_without_flag(self, tiled_rig, write_scan, tmp_path):
        scan_dir = write_scan("scan-1", incomplete=True)
        rig = tiled_rig(readable=[tmp_path / "pq"])
        with pytest.raises(IncompleteScanError):
            _register(rig, scan_dir)
        assert "tpx3" not in rig.client or list(rig.client["tpx3"]) == []

    def test_key_collision_with_foreign_node_is_refused(self, tiled_rig, write_scan, tmp_path):
        scan_dir = write_scan("scan-1")
        rig = tiled_rig(readable=[tmp_path / "pq"])
        ensure_path(rig.client, "tpx3").create_container(key="scan-1", metadata={"mine": True})
        with pytest.raises(RegistrationError, match="not a tpx3_scan"):
            _register(rig, scan_dir)

    def test_catch_up_registers_everything_without_a_marker(self, tiled_rig, write_scan, tmp_path):
        a = write_scan("scan-a")
        b = write_scan("scan-b")
        c = write_scan("scan-c", incomplete=True)
        (tmp_path / "pq" / "not_a_scan").mkdir()
        root = tmp_path / "pq"
        assert find_unregistered(root) == [a, b, c]
        rig = tiled_rig(readable=[root])
        _register(rig, a)
        assert find_unregistered(root) == [b, c]
        result = register_unregistered(ensure_path(rig.client, "tpx3"), root, path="tpx3")
        assert result["registered"] == [str(b)]
        assert result["skipped"] == [str(c)]
        assert result["failed"] == []
        assert find_unregistered(root) == [c]
        assert sorted(rig.client["tpx3"]) == ["scan-a", "scan-b"]
        # The marker records where the scan went.
        marker = json.loads((b / TILED_MARKER_FILENAME).read_text())
        assert marker["path"] == "tpx3" and marker["key"] == "scan-b" and marker["status"] == "complete"

    def test_collect_scan_without_tiled(self, write_scan):
        """The metadata builder has no Tiled dependency and can be used on its own."""
        scan_dir = write_scan("scan-1")
        md, files = collect_scan(scan_dir)
        assert set(files) == {"events", "tdc", "control"}
        assert md["num_events"] == md["tables"]["events"]["num_rows"] > 0
        assert md["t_monotonic"] is True
        assert json.dumps(md)  # JSON-serialisable for Tiled
        assert collect_scan(Path(scan_dir))[0]["scan_name"] == "scan-1"
