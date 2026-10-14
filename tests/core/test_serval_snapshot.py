"""``acq.py --meta-json``: the Serval detector snapshot that ends up in every parquet footer.

``ServalClient`` is mocked; no Serval is needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from splash_timepix.serval_client import acq
from splash_timepix.serval_client.lib import ServalError

pytestmark = pytest.mark.unit

DASHBOARD = {
    "Server": {"SoftwareVersion": "3.3.2", "DiskSpace": []},
    "Detector": {
        "DetectorInfo": {
            "IfaceName": "SPIDR",
            "DetectorType": "TPX3",
            "ChipboardId": "W0028_H07",
            "NumberOfChips": 1,
            "ChipType": "Timepix3",
            "PixCount": 65536,
        },
        "ChipTemperatures": [41.2],
    },
    "Measurement": {"FrameCount": 3},
}
CONFIG = {
    "BiasVoltage": 100,
    "BiasEnabled": True,
    "Thresholds": [450, 0, 0, 0],
    "Polarity": "Positive",
    "TriggerMode": "CONTINUOUS",
    "ExposureTime": 1.0,
    "TriggerPeriod": 1.0,
    "nTriggers": 60,
    "GlobalTimestampInterval": 0.0,
}
CHIPS = [{"Id": "W0028_H07", "Adjust": [0] * 4}]


def _client(dashboard=DASHBOARD, config=CONFIG, chips=CHIPS, fail=()):
    c = mock.Mock()
    c.get_dashboard.side_effect = _effect("dashboard", dashboard, fail)
    c.get_detector_config.side_effect = _effect("config", config, fail)
    c.get_detector_chips.side_effect = _effect("chips", chips, fail)
    return c


def _effect(name, value, fail):
    def fn():
        if name in fail:
            raise ServalError(f"{name} unavailable")
        return value

    return fn


def test_snapshot_lifts_searchable_fields_and_keeps_raw_documents():
    snap = acq.detector_snapshot(_client())
    assert snap["serval_version"] == "3.3.2"
    assert snap["detector_type"] == "TPX3"
    assert snap["chip_ids"] == ["W0028_H07"]
    assert snap["chip_count"] == 1
    assert snap["bias_voltage"] == 100
    assert snap["thresholds"] == [450, 0, 0, 0]
    assert snap["trigger_mode"] == "CONTINUOUS"
    assert snap["chip_temperatures"] == [41.2]
    assert snap["config"] == CONFIG
    assert snap["dashboard"] == DASHBOARD
    assert snap["chips"] == CHIPS
    assert "errors" not in snap
    assert snap["snapshot_time_iso"].endswith("+00:00")
    json.dumps(snap)  # must be JSON-serialisable as is


def test_snapshot_merges_into_file_that_already_holds_user_block(tmp_path: Path):
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"user": {"sample": "Cu foil", "notes": "n"}}))
    acq.write_detector_snapshot(meta, _client())
    doc = json.loads(meta.read_text())
    assert doc["user"] == {"sample": "Cu foil", "notes": "n"}, "the UI's block must survive the merge"
    assert doc["detector"]["serval_version"] == "3.3.2"
    assert not list(tmp_path.glob("*.tmp"))


def test_snapshot_creates_file_when_missing(tmp_path: Path):
    meta = tmp_path / "sub" / "meta.json"
    acq.write_detector_snapshot(meta, _client())
    assert json.loads(meta.read_text())["detector"]["detector_type"] == "TPX3"


def test_partial_failure_keeps_what_worked_and_records_errors(tmp_path: Path):
    meta = tmp_path / "meta.json"
    snap = acq.write_detector_snapshot(meta, _client(fail=("chips", "config")))
    assert snap["serval_version"] == "3.3.2"
    assert snap["config"] == {} and snap["chips"] is None
    assert snap["bias_voltage"] is None
    assert len(snap["errors"]) == 2
    doc = json.loads(meta.read_text())
    assert doc["detector"]["errors"] == snap["errors"]


def test_total_failure_yields_empty_detector_without_raising(tmp_path: Path):
    meta = tmp_path / "meta.json"
    snap = acq.write_detector_snapshot(meta, _client(fail=("dashboard", "config", "chips")))
    assert snap["serval_version"] is None and snap["chip_ids"] is None
    assert json.loads(meta.read_text())["detector"]["errors"]


def test_main_snapshots_after_config_and_before_start(tmp_path: Path, monkeypatch):
    """The snapshot must see the configuration this scan runs with: after update_detector_config,
    before start_acquisition."""
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"user": {"sample": "S"}}))
    order = []
    client = _client()
    client.update_detector_config.side_effect = lambda cfg: order.append("update_config")
    client.start_acquisition.side_effect = lambda: order.append("start")
    client.get_detector_config.side_effect = lambda: (order.append("get_config"), dict(CONFIG))[1]
    monkeypatch.setattr(acq, "ServalClient", lambda *a, **k: client)
    monkeypatch.setattr(acq.time, "sleep", lambda s: None)

    real_write = acq.write_detector_snapshot

    def spy(path, c):
        order.append("snapshot")
        return real_write(path, c)

    monkeypatch.setattr(acq, "write_detector_snapshot", spy)
    acq.main(["-time", "5", "--preview", "--meta-json", str(meta)])

    assert order.index("update_config") < order.index("snapshot") < order.index("start")
    doc = json.loads(meta.read_text())
    assert doc["user"] == {"sample": "S"} and doc["detector"]["serval_version"] == "3.3.2"
    # Preview mode: streaming-only destination, no file writing.
    dest = client.set_destination.call_args[0][0]
    assert len(dest["Raw"]) == 1


def test_main_without_meta_json_takes_no_snapshot(monkeypatch):
    client = _client()
    monkeypatch.setattr(acq, "ServalClient", lambda *a, **k: client)
    monkeypatch.setattr(acq.time, "sleep", lambda s: None)
    called = []
    monkeypatch.setattr(acq, "write_detector_snapshot", lambda *a: called.append(a))
    acq.main(["-time", "5", "--preview"])
    assert called == []
