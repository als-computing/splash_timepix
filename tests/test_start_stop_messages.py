"""
Integration test for the ZMQ control plane used by the Operator UI.

``splash_timepix.ui.main`` relies on:

- ``start`` / ``event`` / ``stop`` messages on the data PUB socket (port 5657 by default).
- The subscriber connecting before the data source starts (ZMQ slow joiner).

A single end-to-end test exercises that path with **dynamic ports** so tests can run
in parallel and avoid collisions with a developer's local UI session.
"""

import subprocess
import sys
import time
from pathlib import Path
from typing import List

import msgpack
import numpy as np
import pytest
import zmq

from splash_timepix.schemas import TimePixEvent, TimePixStart, TimePixStop
from splash_timepix.simulator_cli import SimulatorSource
from tests.port_utils import get_free_port


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


def _collect_zmq_until_stop(sock: zmq.Socket, timeout_s: float = 12.0) -> List[dict]:
    """Drain *sock* and return a list of message dicts until a stop arrives or timeout.

    Two-part event messages have ``array`` replaced with the byte-length so the
    dicts stay picklable/printable.  Control messages (start/stop) are single-part.
    """
    messages: List[dict] = []
    sock.setsockopt(zmq.RCVTIMEO, 800)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            meta_bytes = sock.recv()
        except zmq.Again:
            # Keep waiting unless we already saw a stop
            if any(m.get("msg_type") == "stop" for m in messages):
                break
            continue
        meta = msgpack.unpackb(meta_bytes)
        msg_type = meta.get("msg_type")
        if msg_type in ("start", "stop"):
            messages.append(meta)
            if msg_type == "stop":
                break
        else:
            # event: drain the second part (array bytes)
            try:
                array_bytes = sock.recv()
                meta["_array_bytes"] = len(array_bytes)
            except zmq.Again:
                pass
            messages.append(meta)
    return messages


@pytest.mark.integration
@pytest.mark.slow
def test_simulator_pipeline_delivers_start_and_flush_events():
    """Server + simulator subprocesses: start message, event payloads, schema fields."""
    repo_root = Path(__file__).resolve().parent.parent
    tcp_port = get_free_port()
    zmq_port = get_free_port()
    hb_port = get_free_port()

    server_cmd = [
        sys.executable,
        "-m",
        "splash_timepix.app",
        "--host",
        "localhost",
        "--port",
        str(tcp_port),
        "--zmq-port",
        str(zmq_port),
        "--heartbeat-port",
        str(hb_port),
        "--tdc-frequency",
        "10",
        "--flush-interval",
        "1.0",
        "--exit-on-disconnect",
    ]
    server_proc = subprocess.Popen(
        server_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        cwd=repo_root,
    )

    ctx = zmq.Context()
    socket_sub = ctx.socket(zmq.SUB)
    try:
        time.sleep(2.2)
        if server_proc.poll() is not None:
            err = server_proc.stderr.read().decode(errors="replace") if server_proc.stderr else ""
            pytest.fail(f"server exited early: {err}")

        socket_sub.connect(f"tcp://127.0.0.1:{zmq_port}")
        socket_sub.setsockopt(zmq.SUBSCRIBE, b"")
        time.sleep(0.35)

        sim_cmd = [
            sys.executable,
            "-m",
            "splash_timepix.simulator_cli",
            "--auto-start",
            "--port",
            str(tcp_port),
            "--tdc-frequency",
            "10",
            "--cps",
            "1000",
            "--duration",
            "5",
            "--no-count",
        ]
        sim_proc = subprocess.Popen(
            sim_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            cwd=repo_root,
        )

        start_received = False
        events_received = []
        deadline = time.time() + 25.0

        while time.time() < deadline and len(events_received) < 2:
            socket_sub.setsockopt(zmq.RCVTIMEO, 4000)
            try:
                metadata_bytes = socket_sub.recv()
            except zmq.Again:
                continue
            metadata = msgpack.unpackb(metadata_bytes)
            msg_type = metadata.get("msg_type")
            is_data_message = msg_type not in ("start", "stop")

            if msg_type == "start":
                start_msg = TimePixStart(**metadata)
                assert start_msg.scan_name
                assert start_msg.tdc_frequency_hz == 10.0
                assert start_msg.detector_size_x > 0
                assert start_msg.detector_size_y > 0
                for field in (
                    "scan_name",
                    "tdc_frequency_hz",
                    "detector_size_x",
                    "detector_size_y",
                ):
                    assert field in metadata
                start_received = True
                continue

            if msg_type == "stop":
                continue

            if is_data_message:
                socket_sub.setsockopt(zmq.RCVTIMEO, 2000)
                try:
                    array_bytes = socket_sub.recv()
                except zmq.Again:
                    continue
                shape = tuple(metadata["shape"])
                dtype = metadata["dtype"]
                array = np.frombuffer(array_bytes, dtype=dtype).reshape(shape)
                assert array.shape == shape
                assert array.dtype == np.dtype(dtype)
                events_received.append(
                    {
                        "flush_number": metadata.get("flush_number"),
                        "shape": shape,
                        "total_counts": float(np.sum(array)),
                    }
                )

        sim_proc.terminate()
        try:
            sim_proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            sim_proc.kill()

        assert start_received, "Start message not received within timeout"
        assert len(events_received) >= 1, f"Expected at least one flush event, got {events_received!r}"
        assert all(e["flush_number"] is not None for e in events_received)
    finally:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            server_proc.kill()
        socket_sub.close()
        ctx.term()


@pytest.mark.unit
class TestSchemas:
    """Unit tests for message schemas."""

    def test_timepix_start_schema(self):
        """Test TimePixStart schema validation."""
        data = {
            "msg_type": "start",
            "scan_name": "test_scan",
            "tdc_frequency_hz": 1000.0,
            "t_delta_ns": 10.0,
            "t_cycle_ns": 1000000.0,
            "n_bins": 100,
            "detector_size_x": 256,
            "detector_size_y": 256,
            "flush_interval_s": 1.0,
            "cycles_per_flush": 1000,
            "tdc_channel": 1,
            "tdc_edge": "rising",
            "collapse_y": False,
            "zmq_port": 5657,
            "tcp_port": 9090,
        }

        start = TimePixStart(**data)
        assert start.scan_name == "test_scan"
        assert start.tdc_frequency_hz == 1000.0

    def test_timepix_stop_schema(self):
        """Test TimePixStop schema validation."""
        data = {
            "msg_type": "stop",
            "scan_name": "test_scan",
            "total_flushes": 10,
            "total_cycles": 10000,
            "total_packets": 50000,
            "acquisition_duration_s": 10.5,
            "pixels_discarded_before_trigger": 100,
            "pixels_discarded_outside_window": 50,
        }

        stop = TimePixStop(**data)
        assert stop.scan_name == "test_scan"
        assert stop.total_flushes == 10

    def test_timepix_event_schema(self):
        """Test TimePixEvent schema validation."""
        array = np.zeros((256, 256, 100), dtype=np.uint32)

        data = {
            "msg_type": "event",
            "array": array,
            "timestamp": 1234567890.0,
            "shape": (256, 256, 100),
            "dtype": "uint32",
            "tdc_frequency_hz": 1000.0,
            "t_delta_ns": 10.0,
            "t_cycle_ns": 1000000.0,
            "n_bins": 100,
            "detector_size_x": 256,
            "detector_size_y": 256,
            "flush_interval_s": 1.0,
            "cycles_per_flush": 1000,
            "tdc_channel": 1,
            "tdc_edge": "rising",
            "collapse_y": False,
            "flush_number": 1,
            "cycles_in_flush": 1000,
            "total_cycles": 1000,
            "pixels_discarded_before_trigger": 0,
            "pixels_discarded_outside_window": 0,
        }

        event = TimePixEvent(**data)
        assert event.flush_number == 1
        assert event.array.shape == (256, 256, 100)


@pytest.mark.integration
@pytest.mark.slow
def test_simulator_stop_sends_zmq_stop_and_second_run_resets_counters():
    """Bug #11 / issues 1 & 2: stop_auto_sending() publishes a ZMQ stop; second run
    reconnects with a fresh scan_name and flush_number starting at 1.
    """
    repo_root = Path(__file__).resolve().parent.parent
    tcp_port = get_free_port()
    zmq_port = get_free_port()
    hb_port = get_free_port()

    server_cmd = [
        sys.executable, "-m", "splash_timepix.app",
        "--host", "localhost",
        "--port", str(tcp_port),
        "--zmq-port", str(zmq_port),
        "--heartbeat-port", str(hb_port),
        "--tdc-frequency", "20",
        "--flush-interval", "0.5",
    ]
    server_proc = subprocess.Popen(
        server_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        cwd=repo_root,
    )

    ctx = zmq.Context()
    sock = ctx.socket(zmq.SUB)
    try:
        time.sleep(2.5)
        if server_proc.poll() is not None:
            err = server_proc.stderr.read().decode(errors="replace") if server_proc.stderr else ""
            pytest.fail(f"server exited early: {err}")

        sock.connect(f"tcp://127.0.0.1:{zmq_port}")
        sock.setsockopt(zmq.SUBSCRIBE, b"")
        time.sleep(0.3)

        source = SimulatorSource(host="localhost", port=tcp_port)
        assert source.connect(), "failed initial connect"

        # ── Run 1: stream for 2 s, let it finish naturally, then call stop ──
        source.start_auto_sending(2.0)
        # Wait for the natural end of the stream (+ small buffer)
        if source.send_thread:
            source.send_thread.join(timeout=6.0)
        # stop_auto_sending must still disconnect even though self.running is False
        source.stop_auto_sending()

        run1 = _collect_zmq_until_stop(sock, timeout_s=10.0)
        types1 = [m.get("msg_type") for m in run1]

        assert "start" in types1, "Bug #11/1: no start in first run"
        assert "stop" in types1, (
            "Bug #11/1: no stop received after stop_auto_sending() — "
            "disconnect() was not called when stream finished naturally"
        )
        scan1 = next(m["scan_name"] for m in run1 if m.get("msg_type") == "start")
        stop1 = next(m for m in run1 if m.get("msg_type") == "stop")
        assert stop1["scan_name"] == scan1, "stop scan_name must match start scan_name"

        # ── Run 2: start_auto_sending must auto-reconnect ──
        source.start_auto_sending(2.0)
        if source.send_thread:
            source.send_thread.join(timeout=6.0)
        source.stop_auto_sending()

        run2 = _collect_zmq_until_stop(sock, timeout_s=10.0)
        types2 = [m.get("msg_type") for m in run2]

        assert "start" in types2, "no start in second run"
        assert "stop" in types2, "no stop in second run"

        scan2 = next(m["scan_name"] for m in run2 if m.get("msg_type") == "start")
        assert scan2 != scan1, (
            "Bug #11/2: scan_name not reset between runs — "
            "server did not see a new client connection"
        )

        events2 = [m for m in run2 if m.get("msg_type") not in ("start", "stop")]
        if events2:
            assert events2[0].get("flush_number") == 1, (
                f"Bug #11/2: flush_number should restart at 1, got {events2[0].get('flush_number')}"
            )

    finally:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            server_proc.kill()
        sock.close()
        ctx.term()


@pytest.mark.integration
@pytest.mark.slow
def test_final_flush_sent_before_stop():
    """Bug #11 / issue 3: accumulated data from the last partial TDC cycles is
    published as one final event message before the stop.

    Setup:  tdc=20 Hz, flush_interval=1.0 s  →  flush_every_n_cycles=20.
    Stream: 0.7 s  →  ~14 TDC pulses  →  no regular flush fires.
    Without the fix : 0 event messages received.
    With    the fix : at least 1 event message (the final flush) received.
    """
    repo_root = Path(__file__).resolve().parent.parent
    tcp_port = get_free_port()
    zmq_port = get_free_port()
    hb_port = get_free_port()

    server_cmd = [
        sys.executable, "-m", "splash_timepix.app",
        "--host", "localhost",
        "--port", str(tcp_port),
        "--zmq-port", str(zmq_port),
        "--heartbeat-port", str(hb_port),
        "--tdc-frequency", "20",
        "--flush-interval", "1.0",
        "--exit-on-disconnect",
    ]
    server_proc = subprocess.Popen(
        server_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        cwd=repo_root,
    )

    ctx = zmq.Context()
    sock = ctx.socket(zmq.SUB)
    try:
        time.sleep(2.5)
        if server_proc.poll() is not None:
            err = server_proc.stderr.read().decode(errors="replace") if server_proc.stderr else ""
            pytest.fail(f"server exited early: {err}")

        sock.connect(f"tcp://127.0.0.1:{zmq_port}")
        sock.setsockopt(zmq.SUBSCRIBE, b"")
        time.sleep(0.3)

        source = SimulatorSource(host="localhost", port=tcp_port)
        source.set_counts_per_second(3000)
        source.set_tdc_frequency(20.0)
        assert source.connect(), "failed to connect"

        # Stream for less than one flush cycle (0.7 s < 1.0 s)
        source.start_auto_sending(0.7)
        if source.send_thread:
            source.send_thread.join(timeout=5.0)
        source.stop_auto_sending()

        messages = _collect_zmq_until_stop(sock, timeout_s=12.0)
        types = [m.get("msg_type") for m in messages]

        assert "start" in types, "no start message received"
        assert "stop" in types, "no stop message received"

        events = [m for m in messages if m.get("msg_type") not in ("start", "stop")]
        assert len(events) >= 1, (
            "Bug #11/3: no event messages received — final flush was not sent. "
            f"All message types: {types}"
        )

        # total_flushes in the stop must account for the final flush
        stop_meta = next(m for m in messages if m.get("msg_type") == "stop")
        assert stop_meta["total_flushes"] == len(events), (
            f"stop.total_flushes={stop_meta['total_flushes']} != "
            f"events received={len(events)}"
        )

    finally:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            server_proc.kill()
        sock.close()
        ctx.term()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
