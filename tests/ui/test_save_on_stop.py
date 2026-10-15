"""The ``_meta.json`` written by the Operator tab records where the scan's parquet went.

The UI learns the parquet directory and status only from the ZMQ ``TimePixStop``
message. The stop message and the save-on-stop can arrive in either order, so both
orders must leave the same ``parquet`` / ``stop_message`` blocks in ``_meta.json``.
``tpx-tiled-register`` later merges this file into the Tiled node as ``ui``.

Runs headless with the offscreen Qt platform plugin.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("PySide6", reason="PySide6 (ui extra) not installed")
pytest.importorskip("pyqtgraph", reason="pyqtgraph (ui extra) not installed")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

_qapp = QApplication.instance() or QApplication(sys.argv[:1])

from splash_timepix.ui.operator_tab import OperatorTab, _parquet_info  # noqa: E402

SCAN = "0b4f7a1c-9d2e-4c3a-8f5b-6e7d8c9a0b1f"


def _stop_message(scan_name: str = SCAN, **overrides) -> dict:
    msg = {
        "msg_type": "stop",
        "scan_name": scan_name,
        "total_packets": 1234,
        "total_cycles": 7,
        "parquet_dir": f"/data/parquet/{scan_name}",
        "parquet_status": "complete",
        "parquet_rows": 98765,
        "parquet_dropped_rows": 0,
    }
    msg.update(overrides)
    return msg


@pytest.fixture
def tab(monkeypatch, tmp_path: Path) -> OperatorTab:
    """An OperatorTab with isolated preferences and one accumulated 2-D frame."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    widget = OperatorTab()
    widget._cumulative_sum = np.arange(12, dtype=np.float64).reshape(4, 3) * 7
    widget._total_cycles = 7
    widget._flush_count = 3
    widget._last_metadata = {"scan_name": SCAN, "t_delta_ns": 2.0, "tdc_frequency_hz": 1000.0, "n_bins": 3}
    yield widget
    widget.deleteLater()


def _read_meta(json_path: Path) -> dict:
    with open(json_path) as f:
        return json.load(f)


def test_parquet_info_picks_the_four_parquet_fields():
    info = _parquet_info(_stop_message())
    assert info == {
        "dir": f"/data/parquet/{SCAN}",
        "status": "complete",
        "rows": 98765,
        "dropped_rows": 0,
    }
    # A stop from a server running without --parquet-dir leaves every field None.
    assert _parquet_info({"msg_type": "stop", "scan_name": SCAN}) == {
        "dir": None,
        "status": None,
        "rows": None,
        "dropped_rows": None,
    }


def test_stop_before_save_lands_in_meta_json(tab: OperatorTab, tmp_path: Path):
    stop = _stop_message()
    tab.on_control_received(stop)

    png, csv, energy, time, json_path, slug = tab.save_average_data(str(tmp_path / "out"), "000001")
    assert json_path is not None and json_path.exists()
    assert slug.startswith(SCAN.replace("-", "")[:8])

    meta = _read_meta(json_path)
    assert meta["scan_name"] == SCAN
    assert meta["parquet"] == _parquet_info(stop)
    assert meta["stop_message"] == stop
    assert meta["zmq_metadata"]["scan_name"] == SCAN
    assert set(meta["energy_calibration"]) == {"pixel_per_ev", "ev_at_mid"}


def test_save_before_stop_is_completed_when_the_stop_arrives(tab: OperatorTab, tmp_path: Path):
    *_, json_path, _slug = tab.save_average_data(str(tmp_path / "out"), "000002")
    assert json_path is not None
    before = _read_meta(json_path)
    assert "parquet" not in before and "stop_message" not in before

    stop = _stop_message(parquet_status="incomplete", parquet_dropped_rows=42)
    tab.on_control_received(stop)

    after = _read_meta(json_path)
    assert after["parquet"] == {
        "dir": f"/data/parquet/{SCAN}",
        "status": "incomplete",
        "rows": 98765,
        "dropped_rows": 42,
    }
    assert after["stop_message"] == stop
    # The merge keeps everything that was already there.
    for key in ("scan_name", "total_cycles", "array_shape", "energy_calibration", "zmq_metadata"):
        assert after[key] == before[key]
    assert not list(json_path.parent.glob("*.tmp"))


def test_stop_for_another_scan_does_not_touch_meta_json(tab: OperatorTab, tmp_path: Path):
    *_, json_path, _slug = tab.save_average_data(str(tmp_path / "out"), "000003")
    assert json_path is not None
    before = _read_meta(json_path)

    tab.on_control_received(_stop_message(scan_name="ffffffff-0000-0000-0000-000000000000"))
    assert _read_meta(json_path) == before

    # And a later save for *this* scan does not borrow the other scan's stop.
    *_, json_path2, _ = tab.save_average_data(str(tmp_path / "out2"), "000004")
    assert json_path2 is not None
    assert "parquet" not in _read_meta(json_path2)


def test_non_stop_control_messages_are_ignored(tab: OperatorTab, tmp_path: Path):
    *_, json_path, _slug = tab.save_average_data(str(tmp_path / "out"), "000005")
    assert json_path is not None
    before = _read_meta(json_path)
    tab.on_control_received({"msg_type": "start", "scan_name": SCAN, "parquet_dir": "/nope"})
    assert _read_meta(json_path) == before
    assert tab._last_stop is None
