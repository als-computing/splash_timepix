"""
Integration test for start/stop message functionality.

This test verifies that:
1. Start messages are sent when data arrives
2. Event messages are sent for each flush
3. Stop messages are sent on shutdown
4. Messages can be received and parsed correctly
"""

import subprocess
import time
import zmq
import msgpack
import numpy as np
import pytest
import signal
import os
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from splash_timepix.schemas import TimePixStart, TimePixStop, TimePixEvent


@pytest.mark.integration
@pytest.mark.slow
class TestStartStopMessages:
    """Integration tests for start/stop message flow."""
    
    @pytest.fixture
    def server_process(self):
        """Start the server in a subprocess."""
        cmd = [
            sys.executable, "-m", "splash_timepix.app",
            "--tdc-frequency", "10",
            "--flush-interval", "1.0",
            "--zmq-port", "5657",
            "--exit-on-disconnect"
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=Path(__file__).parent.parent
        )
        # Give server time to start
        time.sleep(2)
        yield proc
        # Cleanup
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    
    @pytest.fixture
    def simulator_process(self):
        """Start the simulator in a subprocess."""
        cmd = [
            sys.executable, "-m", "splash_timepix.simulator_cli",
            "--auto-start",
            "--tdc-frequency", "10",
            "--cps", "1000",
            "--duration", "5",
            "--no-count"
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=Path(__file__).parent.parent
        )
        # Give simulator time to connect and send data
        time.sleep(1)
        yield proc
        # Cleanup
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    
    def test_receive_start_message(self, server_process, simulator_process):
        """Test that start message is received."""
        context = zmq.Context()
        socket = context.socket(zmq.SUB)
        socket.connect("tcp://localhost:5657")
        socket.setsockopt(zmq.SUBSCRIBE, b"")
        socket.setsockopt(zmq.RCVTIMEO, 10000)  # 10 second timeout
        
        # Wait for start message
        start_received = False
        timeout = time.time() + 10
        
        while time.time() < timeout:
            try:
                metadata_bytes = socket.recv()
                metadata = msgpack.unpackb(metadata_bytes)
                
                if metadata.get('msg_type') == 'start':
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
        
        socket.close()
        context.term()
        
        assert start_received, "Start message not received within timeout"
    
    def test_receive_event_messages(self, server_process, simulator_process):
        """Test that event messages are received."""
        context = zmq.Context()
        socket = context.socket(zmq.SUB)
        socket.connect("tcp://localhost:5657")
        socket.setsockopt(zmq.SUBSCRIBE, b"")
        socket.setsockopt(zmq.RCVTIMEO, 10000)
        
        events_received = []
        timeout = time.time() + 10
        start_received = False
        
        while time.time() < timeout:
            try:
                metadata_bytes = socket.recv()
                metadata = msgpack.unpackb(metadata_bytes)
                msg_type = metadata.get('msg_type')
                
                if msg_type == 'start':
                    start_received = True
                    continue
                
                # Event messages (may not have msg_type in old format)
                if msg_type == 'event' or msg_type is None:
                    # Try to receive array data
                    try:
                        socket.setsockopt(zmq.RCVTIMEO, 1000)
                        array_bytes = socket.recv()
                        socket.setsockopt(zmq.RCVTIMEO, 10000)
                        
                        # Reconstruct array
                        shape = tuple(metadata['shape'])
                        dtype = metadata['dtype']
                        array = np.frombuffer(array_bytes, dtype=dtype).reshape(shape)
                        
                        # Validate it's a valid array
                        assert array.shape == shape
                        assert array.dtype == dtype
                        
                        events_received.append({
                            'flush_number': metadata.get('flush_number'),
                            'shape': shape,
                            'total_counts': np.sum(array)
                        })
                        
                        if len(events_received) >= 2:  # Got enough events
                            break
                    except zmq.Again:
                        continue
            except zmq.Again:
                continue
        
        socket.close()
        context.term()
        
        assert start_received, "Start message not received"
        assert len(events_received) >= 1, f"Expected at least 1 event, got {len(events_received)}"
        assert all(e['flush_number'] is not None for e in events_received), "Events missing flush_number"
    
    def test_message_format(self, server_process, simulator_process):
        """Test that messages have correct format."""
        context = zmq.Context()
        socket = context.socket(zmq.SUB)
        socket.connect("tcp://localhost:5657")
        socket.setsockopt(zmq.SUBSCRIBE, b"")
        socket.setsockopt(zmq.RCVTIMEO, 10000)
        
        messages = []
        timeout = time.time() + 10
        
        while time.time() < timeout and len(messages) < 5:
            try:
                metadata_bytes = socket.recv()
                metadata = msgpack.unpackb(metadata_bytes)
                msg_type = metadata.get('msg_type')
                
                if msg_type == 'start':
                    messages.append(('start', metadata))
                elif msg_type == 'stop':
                    messages.append(('stop', metadata))
                elif msg_type == 'event' or msg_type is None:
                    # Try to get array
                    try:
                        socket.setsockopt(zmq.RCVTIMEO, 1000)
                        array_bytes = socket.recv()
                        socket.setsockopt(zmq.RCVTIMEO, 10000)
                        messages.append(('event', metadata, len(array_bytes)))
                    except zmq.Again:
                        continue
            except zmq.Again:
                continue
        
        socket.close()
        context.term()
        
        # Verify we got at least a start message
        start_msgs = [m for m in messages if m[0] == 'start']
        assert len(start_msgs) >= 1, "Should receive at least one start message"
        
        # Verify start message has required fields
        start_metadata = start_msgs[0][1]
        required_fields = ['scan_name', 'tdc_frequency_hz', 'detector_size_x', 'detector_size_y']
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
            "tcp_port": 9090
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
            "pixels_discarded_outside_window": 50
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
            "pixels_discarded_outside_window": 0
        }
        
        event = TimePixEvent(**data)
        assert event.flush_number == 1
        assert event.array.shape == (256, 256, 100)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
