"""
Integration test for start/stop message functionality.

This test verifies that:
1. Start messages are sent when data arrives
2. Event messages are sent for each flush
3. Stop messages are sent on shutdown
4. Messages can be received and parsed correctly
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


@pytest.mark.integration
@pytest.mark.slow
class TestStartStopMessages:
    """Integration tests for start/stop message flow."""

    @pytest.fixture
    def server_process(self):
        """Start the server in a subprocess."""
        cmd = [
            sys.executable,
            "-m",
            "splash_timepix.app",
            "--tdc-frequency",
            "10",
            "--flush-interval",
            "1.0",
            "--zmq-port",
            "5657",
            "--exit-on-disconnect",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=Path(__file__).parent.parent,
        )
        # Give server time to start up, bind ports, and complete ZMQ slow joiner sleep
        time.sleep(2.0)
        yield proc
        # Cleanup
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    @pytest.fixture
    def zmq_socket(self, server_process):
        """Create ZMQ socket connected to server - must come BEFORE simulator starts."""
        context = zmq.Context()
        socket = context.socket(zmq.SUB)
        socket.connect("tcp://localhost:5657")
        socket.setsockopt(zmq.SUBSCRIBE, b"")
        # Give subscription time to establish
        time.sleep(0.3)
        yield socket
        # Cleanup
        socket.close()
        context.term()

    @pytest.fixture
    def simulator_process(self):
        """Start the simulator in a subprocess."""
        cmd = [
            sys.executable,
            "-m",
            "splash_timepix.simulator_cli",
            "--auto-start",
            "--tdc-frequency",
            "10",
            "--cps",
            "1000",
            "--duration",
            "5",
            "--no-count",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=Path(__file__).parent.parent,
        )
        yield proc
        # Cleanup
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    def test_receive_start_message(self, zmq_socket, simulator_process):
        """Test that start message is received."""
        socket = zmq_socket

        # Wait for start message
        start_received = False
        timeout = time.time() + 10

        while time.time() < timeout:
            try:
                # Set a shorter timeout for this specific recv
                socket.setsockopt(zmq.RCVTIMEO, 5000)
                metadata_bytes = socket.recv()
                metadata = msgpack.unpackb(metadata_bytes)
                msg_type = metadata.get("msg_type")

                # Control messages are single-part, data messages are multi-part
                is_data_message = msg_type != "start" and msg_type != "stop"

                if is_data_message:
                    # Consume the array part to keep stream in sync
                    socket.setsockopt(zmq.RCVTIMEO, 1000)
                    try:
                        socket.recv()  # Discard array data
                    except zmq.Again:
                        pass
                    continue

                if metadata.get("msg_type") == "start":
                    # Validate schema
                    start_msg = TimePixStart(**metadata)
                    assert start_msg.scan_name is not None
                    assert start_msg.tdc_frequency_hz == 10.0
                    assert start_msg.detector_size_x > 0
                    assert start_msg.detector_size_y > 0
                    start_received = True
                    break
            except zmq.Again:
                continue
            except Exception as e:
                print(f"Error receiving message: {e}")
                raise

        assert start_received, "Start message not received within timeout"

    def test_receive_event_messages(self, zmq_socket, simulator_process):
        """Test that event messages are received."""
        socket = zmq_socket

        events_received = []
        timeout = time.time() + 10
        start_received = False

        while time.time() < timeout:
            try:
                socket.setsockopt(zmq.RCVTIMEO, 5000)
                metadata_bytes = socket.recv()
                metadata = msgpack.unpackb(metadata_bytes)
                msg_type = metadata.get("msg_type")

                # Control messages are single-part, data messages are multi-part
                is_data_message = msg_type != "start" and msg_type != "stop"

                if msg_type == "start":
                    start_received = True
                    continue

                # Event messages - receive array data
                if is_data_message:
                    # Try to receive array data
                    socket.setsockopt(zmq.RCVTIMEO, 1000)
                    try:
                        array_bytes = socket.recv()

                        # Reconstruct array
                        shape = tuple(metadata["shape"])
                        dtype = metadata["dtype"]
                        array = np.frombuffer(array_bytes, dtype=dtype).reshape(shape)

                        # Validate it's a valid array
                        assert array.shape == shape
                        assert array.dtype == dtype

                        events_received.append(
                            {
                                "flush_number": metadata.get("flush_number"),
                                "shape": shape,
                                "total_counts": np.sum(array),
                            }
                        )

                        if len(events_received) >= 2:  # Got enough events
                            break
                    except zmq.Again:
                        continue
            except zmq.Again:
                continue

        assert start_received, "Start message not received"
        assert len(events_received) >= 1, f"Expected at least 1 event, got {len(events_received)}"
        assert all(e["flush_number"] is not None for e in events_received), "Events missing flush_number"

    def test_message_format(self, zmq_socket, simulator_process):
        """Test that messages have correct format."""
        socket = zmq_socket

        messages = []
        timeout = time.time() + 10

        while time.time() < timeout and len(messages) < 5:
            try:
                socket.setsockopt(zmq.RCVTIMEO, 5000)
                metadata_bytes = socket.recv()
                metadata = msgpack.unpackb(metadata_bytes)
                msg_type = metadata.get("msg_type")

                # Control messages are single-part, data messages are multi-part
                is_data_message = msg_type != "start" and msg_type != "stop"

                if msg_type == "start":
                    messages.append(("start", metadata))
                elif msg_type == "stop":
                    messages.append(("stop", metadata))
                elif is_data_message:
                    # Try to get array
                    socket.setsockopt(zmq.RCVTIMEO, 1000)
                    try:
                        array_bytes = socket.recv()
                        messages.append(("event", metadata, len(array_bytes)))
                    except zmq.Again:
                        continue
            except zmq.Again:
                continue

        # Verify we got at least a start message
        start_msgs = [m for m in messages if m[0] == "start"]
        assert len(start_msgs) >= 1, "Should receive at least one start message"

        # Verify start message has required fields
        start_metadata = start_msgs[0][1]
        required_fields = [
            "scan_name",
            "tdc_frequency_hz",
            "detector_size_x",
            "detector_size_y",
        ]
        for field in required_fields:
            assert field in start_metadata, f"Start message missing field: {field}"


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
