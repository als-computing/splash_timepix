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

import msgpack
import numpy as np
import pytest
import zmq

from splash_timepix.schemas import TimePixEvent, TimePixStart, TimePixStop
from tests.port_utils import get_free_port


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


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
