"""Exit codes and messages of ``tpx-tiled-register``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from splash_timepix.parquet_meta import TILED_MARKER_FILENAME
from splash_timepix.tiled_register import cli

pytestmark = pytest.mark.integration


@pytest.fixture
def patched_connect(monkeypatch, tiled_rig, tmp_path):
    """Point the CLI's connect() at the in-process server instead of a URI."""
    import splash_timepix.tiled_register as tr

    rig = tiled_rig(readable=[tmp_path / "pq"])

    def fake_connect(uri, api_key=None, path="tpx3"):
        if uri.startswith("http://unreachable"):
            raise ConnectionError("connection refused")
        return tr.ensure_path(rig.client, path)

    monkeypatch.setattr(tr, "connect", fake_connect)
    return rig


def test_register_ok(patched_connect, write_scan, capsys):
    scan_dir = write_scan("scan-1")
    assert cli([str(scan_dir), "--uri", "http://x", "--path", "tpx3"]) == 0
    out = capsys.readouterr().out
    assert "created: tpx3/scan-1" in out
    assert (scan_dir / TILED_MARKER_FILENAME).exists()
    assert cli([str(scan_dir), "--uri", "http://x"]) == 0
    assert "updated: tpx3/scan-1" in capsys.readouterr().out


def test_incomplete_refused_exit_2_then_allowed(patched_connect, write_scan, capsys):
    scan_dir = write_scan("scan-1", incomplete=True)
    assert cli([str(scan_dir), "--uri", "http://x"]) == 2
    assert "--allow-incomplete" in capsys.readouterr().err
    assert not (scan_dir / TILED_MARKER_FILENAME).exists()
    assert cli([str(scan_dir), "--uri", "http://x", "--allow-incomplete"]) == 0
    assert json.loads((scan_dir / TILED_MARKER_FILENAME).read_text())["status"] == "incomplete"


def test_unreachable_server_exit_1_no_marker(patched_connect, write_scan, capsys):
    scan_dir = write_scan("scan-1")
    assert cli([str(scan_dir), "--uri", "http://unreachable:1"]) == 1
    assert "Cannot connect" in capsys.readouterr().err
    assert not (scan_dir / TILED_MARKER_FILENAME).exists()
    # Catch-up afterwards finds it.
    assert cli(["--unregistered", "--scan-root", str(scan_dir.parent), "--uri", "http://x"]) == 0
    assert "registered 1" in capsys.readouterr().out
    assert (scan_dir / TILED_MARKER_FILENAME).exists()


def test_not_a_scan_dir_exit_1(patched_connect, tmp_path, capsys):
    empty = tmp_path / "pq" / "empty"
    empty.mkdir(parents=True)
    assert cli([str(empty), "--uri", "http://x", "--allow-incomplete"]) == 1
    assert "no completed parquet files" in capsys.readouterr().err


def test_usage_errors_exit_2(capsys):
    assert cli(["--unregistered"]) == 2
    assert cli([]) == 2


def test_explicit_key(patched_connect, write_scan, capsys):
    scan_dir = write_scan("scan-1")
    assert cli([str(scan_dir), "--uri", "http://x", "--key", "my-run"]) == 0
    assert "tpx3/my-run" in capsys.readouterr().out
    assert "my-run" in patched_connect.client["tpx3"]
    Path(scan_dir / TILED_MARKER_FILENAME).unlink()
