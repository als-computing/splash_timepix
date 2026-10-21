"""Integration tests: ``tpx-stream --parquet-dir`` fed by the simulator over TCP.

These are the tests behind criterion 1 ("data streaming of Timepix3 into parquet
files"): the files must be *exactly* what went over the wire, completed files must
be readable while the scan is still running, and the stop message must point at
the directory.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List

import numpy as np
import pandas as pd
import psutil
import pyarrow.parquet as pq
import pytest

from splash_timepix.parquet_meta import SCAN_META_FILENAME, list_part_files, read_file_metadata, read_scan_summary
from splash_timepix.parser import PacketParser
from tests.conftest import collect_messages_until_stop

pytestmark = [pytest.mark.integration, pytest.mark.slow]


class _RecordingSocket:
    """Wraps the simulator's TCP socket and keeps a copy of every byte sent."""

    def __init__(self, inner, sink: List[bytes]):
        self._inner = inner
        self._sink = sink

    def sendall(self, data, *args):
        self._sink.append(bytes(data))
        return self._inner.sendall(data, *args)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _run_simulator(rig, duration: float, record: bool = True):
    """Connect the in-process simulator, stream for ``duration`` s, disconnect. Returns sent bytes."""
    sent: List[bytes] = []
    source = rig.make_simulator()
    assert source.connect(), "simulator failed to connect"
    if record:
        source.socket = _RecordingSocket(source.socket, sent)
    source.start_auto_sending(duration)
    if source.send_thread:
        source.send_thread.join(timeout=duration + 20)
    source.stop_auto_sending()
    return b"".join(sent)


def _sorted_events(df: pd.DataFrame) -> pd.DataFrame:
    return df.sort_values(["t", "x", "y", "tot"], kind="stable").reset_index(drop=True)


def _split(messages):
    starts = [m for m in messages if m.get("msg_type") == "start"]
    stops = [m for m in messages if m.get("msg_type") == "stop"]
    events = [m for m in messages if m.get("msg_type") not in ("start", "stop")]
    return starts, stops, events


def test_parquet_equals_wire_exactly_and_stop_points_at_it(streaming_rig, tmp_path):
    meta_json = tmp_path / "meta.json"
    meta_json.write_text(json.dumps({"user": {"sample": "Cu foil", "esaf": "P-1"}, "detector": {"bias": 100}}))
    rig = streaming_rig(
        tdc_frequency=1000.0,
        cps=50_000.0,
        flush_interval=0.5,
        exit_on_disconnect=True,
        collapse_y=True,
        parquet_dir=tmp_path / "pq",
        parquet_meta_json=meta_json,
    )
    wire = _run_simulator(rig, duration=3.0)
    messages = collect_messages_until_stop(rig.sub_sock, timeout_s=20.0)
    starts, stops, events = _split(messages)
    assert len(stops) == 1 and starts
    stop = stops[0]

    # The stop message names the directory; it is <parquet_dir>/<scan_name>.
    assert stop["parquet_status"] == "complete", stop
    scan_dir = Path(stop["parquet_dir"])
    assert scan_dir == tmp_path / "pq" / stop["scan_name"]
    assert scan_dir.is_dir()
    assert stop["parquet_dropped_rows"] == 0

    # What went over the wire, decoded with the same parser the server uses.
    sent = PacketParser().parse_batch(wire)
    assert sent.n_pixels > 10_000, "simulator sent too little to be meaningful"
    exp_events = _sorted_events(
        pd.DataFrame(
            {
                "x": sent.pixel_x.astype(np.uint16),
                "y": sent.pixel_y.astype(np.uint16),
                "tot": sent.pixel_tot.astype(np.uint16),
                "t": sent.pixel_timestamp.astype(np.uint64),
            }
        )
    )
    got_events = _sorted_events(pd.read_parquet(scan_dir / "events"))
    pd.testing.assert_frame_equal(got_events, exp_events)
    assert stop["parquet_rows"] == len(exp_events)
    # Not the ZMQ stop's total_packets: that one counts *binned* pixels.

    exp_tdc = pd.DataFrame(
        {
            "t": sent.tdc_timestamp.astype(np.uint64),
            "channel": sent.tdc_channel.astype(np.uint8),
            "edge": sent.tdc_edge.astype(np.uint8),
        }
    ).sort_values(["t", "channel", "edge"], kind="stable")
    got_tdc = pd.read_parquet(scan_dir / "tdc").sort_values(["t", "channel", "edge"], kind="stable")
    pd.testing.assert_frame_equal(got_tdc.reset_index(drop=True), exp_tdc.reset_index(drop=True))
    assert len(got_tdc) > 0, "no TDC edges recorded"

    # Footer metadata: scan identity, settings, operator and detector blocks.
    meta = read_file_metadata(list_part_files(scan_dir / "events")[0])
    assert meta.scan_name == stop["scan_name"]
    assert meta.tdc_frequency_hz == 1000.0 and meta.collapse_y is True and meta.mode == "timing"
    assert meta.source.kind == "simulator"
    assert meta.user == {"sample": "Cu foil", "esaf": "P-1"}
    assert meta.detector == {"bias": 100}

    # Sidecar: complete, counts agree with the files, carries the stop message.
    summary = read_scan_summary(scan_dir)
    assert summary.status == "complete"
    assert summary.tables["events"].num_rows == len(exp_events)
    assert summary.stop_message["scan_name"] == stop["scan_name"]
    assert summary.stop_message["total_packets"] == stop["total_packets"]

    # The streaming server exits on its own after the disconnect.
    assert rig.server_proc.wait(timeout=15) == 0


def test_completed_file_readable_while_scan_is_running(streaming_rig, tmp_path):
    """Criterion 1: a rolled file opens with pyarrow *while* the simulator is still sending,
    and dataset discovery ignores the in-progress ``.tmp`` file."""
    # The in-process simulator manages ~35-50k packets/s whatever cps is asked for, and
    # synthetic events compress to ~4 B/row, so a quarter-megabyte roll fires every few seconds.
    rig = streaming_rig(
        tdc_frequency=1000.0,
        cps=100_000.0,
        flush_interval=1.0,
        exit_on_disconnect=True,
        collapse_y=True,
        parquet_dir=tmp_path / "pq",
        parquet_roll_mb=0.25,
    )
    source = rig.make_simulator()
    assert source.connect()
    source.start_auto_sending(25.0)

    scan_dirs: List[Path] = []
    completed: List[Path] = []
    deadline = time.time() + 20.0
    while time.time() < deadline:
        scan_dirs = [p for p in (tmp_path / "pq").glob("*") if p.is_dir()]
        if scan_dirs:
            completed = list_part_files(scan_dirs[0] / "events")
            if completed:
                break
        time.sleep(0.2)
    still_sending = source.send_thread is not None and source.send_thread.is_alive()
    try:
        assert completed, "no parquet file rolled while the scan was running"
        assert still_sending, "the simulator finished before a file rolled; raise cps or duration"
        events_dir = scan_dirs[0] / "events"
        tmp_files = list(events_dir.glob(".part-*.parquet.tmp"))
        assert tmp_files, "expected an in-progress .tmp file next to the completed one"
        assert not (scan_dirs[0] / SCAN_META_FILENAME).exists(), "sidecar must only appear at stop"
        n_completed = sum(pq.read_metadata(p).num_rows for p in completed)
        assert n_completed > 0
        df = pd.read_parquet(events_dir)  # discovery must skip the dot-file
        assert len(df) == n_completed
        for p in completed:
            m = read_file_metadata(p)
            assert m.num_rows == pq.read_metadata(p).num_rows
    finally:
        source.stop_auto_sending()
    messages = collect_messages_until_stop(rig.sub_sock, timeout_s=20.0)
    _, stops, _ = _split(messages)
    assert len(stops) == 1 and stops[0]["parquet_status"] == "complete"
    assert not list((scan_dirs[0] / "events").glob(".*.tmp")), "no .tmp may remain after stop"


def test_drop_policy_with_tiny_queue_reports_incomplete(streaming_rig, tmp_path):
    rig = streaming_rig(
        tdc_frequency=1000.0,
        cps=50_000.0,
        flush_interval=0.5,
        exit_on_disconnect=True,
        collapse_y=True,
        parquet_dir=tmp_path / "pq",
        parquet_on_full="drop",
        parquet_queue_max_rows=50,  # every batch is larger than this
    )
    _run_simulator(rig, duration=2.0, record=False)
    messages = collect_messages_until_stop(rig.sub_sock, timeout_s=20.0)
    _, stops, events = _split(messages)
    assert len(stops) == 1 and events, "ZMQ path must be unaffected by parquet drops"
    stop = stops[0]
    assert stop["parquet_status"] == "incomplete"
    assert stop["parquet_dropped_rows"] > 0
    summary = read_scan_summary(Path(stop["parquet_dir"]))
    assert summary.status == "incomplete" and summary.dropped_rows == stop["parquet_dropped_rows"]


def test_block_policy_loses_nothing_with_tiny_queue(streaming_rig, tmp_path):
    rig = streaming_rig(
        tdc_frequency=1000.0,
        cps=50_000.0,
        flush_interval=0.5,
        exit_on_disconnect=True,
        collapse_y=True,
        parquet_dir=tmp_path / "pq",
        parquet_on_full="block",
        parquet_queue_max_rows=20_000,
    )
    wire = _run_simulator(rig, duration=2.0)
    messages = collect_messages_until_stop(rig.sub_sock, timeout_s=30.0)
    _, stops, _ = _split(messages)
    assert len(stops) == 1
    stop = stops[0]
    sent = PacketParser().parse_batch(wire)
    assert stop["parquet_status"] == "complete" and stop["parquet_dropped_rows"] == 0
    assert stop["parquet_rows"] == sent.n_pixels


def test_kill_minus_nine_leaves_only_footer_complete_files(streaming_rig, tmp_path):
    rig = streaming_rig(
        tdc_frequency=1000.0,
        cps=100_000.0,
        flush_interval=1.0,
        exit_on_disconnect=True,
        collapse_y=True,
        parquet_dir=tmp_path / "pq",
        parquet_roll_mb=0.25,
    )
    source = rig.make_simulator()
    assert source.connect()
    source.start_auto_sending(30.0)
    deadline = time.time() + 20.0
    scan_dir = None
    while time.time() < deadline:
        dirs = [p for p in (tmp_path / "pq").glob("*") if p.is_dir()]
        if dirs and list_part_files(dirs[0] / "events"):
            scan_dir = dirs[0]
            break
        time.sleep(0.2)
    rig.server_proc.kill()
    rig.server_proc.wait(timeout=10)
    source.stop_auto_sending()
    assert scan_dir is not None, "no completed file before the kill"

    completed = list_part_files(scan_dir / "events")
    assert completed
    for p in completed:
        assert pq.read_metadata(p).num_rows > 0  # footer intact
        read_file_metadata(p)
    # A leftover temporary file is expected and must be invisible to dataset discovery.
    leftover = list((scan_dir / "events").glob(".part-*.parquet.tmp"))
    assert leftover, "the file being written at kill time should remain as .tmp"
    assert len(pd.read_parquet(scan_dir / "events")) == sum(pq.read_metadata(p).num_rows for p in completed)
    # No sidecar: the scan never finished, and the rebuilt summary says so.
    assert not (scan_dir / SCAN_META_FILENAME).exists()
    assert read_scan_summary(scan_dir).status == "incomplete"


def test_server_cpu_per_packet_within_tolerance_with_parquet(streaming_rig, tmp_path):
    """The parquet sink must not make the live view materially more expensive.

    Compares the streaming server's CPU seconds per wire packet with and without
    ``--parquet-dir``. Packet *counts* cannot be compared: the simulator paces itself
    by wall clock and the number it manages to send in a fixed window varies by a
    factor of two between runs, independent of the server. CPU per packet is stable
    to ~20 %; the bound here is deliberately loose (a tolerance, not an equality),
    and the precise cost is measured by ``tools/bench_parquet_sink.py``.
    """

    def run(parquet_dir):
        rig = streaming_rig(
            tdc_frequency=10_000.0,
            cps=200_000.0,
            flush_interval=0.5,
            exit_on_disconnect=True,
            collapse_y=True,
            parquet_dir=parquet_dir,
        )
        proc = psutil.Process(rig.server_proc.pid)
        cpu0 = proc.cpu_times()
        wire = _run_simulator(rig, duration=4.0)
        try:
            cpu1 = proc.cpu_times()
        except psutil.NoSuchProcess:  # exited already
            cpu1 = cpu0
        messages = collect_messages_until_stop(rig.sub_sock, timeout_s=30.0)
        _, stops, _ = _split(messages)
        assert len(stops) == 1
        n_packets = len(wire) // 12
        cpu = (cpu1.user + cpu1.system) - (cpu0.user + cpu0.system)
        return cpu / max(1, n_packets), n_packets, stops[0]

    per_packet_plain, n_plain, _ = run(None)
    per_packet_pq, n_pq, stop_pq = run(tmp_path / "pq")
    assert n_plain > 50_000 and n_pq > 50_000, (n_plain, n_pq)
    assert stop_pq["parquet_status"] == "complete"
    assert (
        per_packet_pq <= 1.5 * per_packet_plain
    ), f"CPU per packet rose from {per_packet_plain*1e6:.1f} µs to {per_packet_pq*1e6:.1f} µs with parquet on"
