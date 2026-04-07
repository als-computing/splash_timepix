"""Tests for ``SocketDataServer`` TCP ingest and batch parsing.

These focus on the packet path that feeds ``splash_timepix.app`` (live-cli → TCP →
parser → callback). For the full UI-style pipeline (ZMQ + heartbeat + disconnect),
see ``test_main_workflow.py`` and ``test_start_stop_messages.py``.
"""

import socket
import threading
import time

import pytest

from splash_timepix.parser import BatchParseResult
from splash_timepix.simulator import PacketSimulator, SimulatorConfig
from splash_timepix.socket_server import SocketDataServer


def _batch_total_packets(r: BatchParseResult) -> int:
    return r.n_pixels + r.n_tdc + r.n_control


class TestServerLifecycle:
    """Tests for server start/stop functionality."""

    def test_server_initialization(self, server, test_port):
        """Test server initializes with correct parameters."""
        assert server.host == "localhost"
        assert server.port == test_port
        assert server.buffer_size == 100
        assert not server.running

    def test_server_start_stop(self, server):
        """Test basic server start and stop."""
        assert not server.running

        server.start()
        time.sleep(0.1)  # Give threads time to start
        assert server.running

        server.stop()
        assert not server.running

    def test_server_double_start(self, server):
        """Test that starting an already-running server is safe."""
        server.start()
        time.sleep(0.1)

        # Second start should be handled gracefully
        server.start()
        assert server.running

        server.stop()

    def test_server_stop_when_not_running(self, server):
        """Test that stopping a non-running server is safe."""
        assert not server.running
        server.stop()  # Should not raise an error
        assert not server.running


class TestPacketReception:
    """Tests for receiving and parsing packets."""

    def test_receive_single_pixel_packet(self, server, test_port, simulator):
        """Test receiving a single pixel packet."""
        received: list[BatchParseResult] = []

        def callback(result: BatchParseResult):
            received.append(result)

        server.set_data_callback(callback)
        server.callback_batch_size = 1  # Flush one packet per raw batch when possible
        server.batch_byte_size = server.callback_batch_size * server.PACKET_SIZE
        server.start()
        time.sleep(0.1)

        # Connect and send one pixel packet
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.connect(("localhost", test_port))

            packet_bytes = simulator.generate_pixel_event()
            client.sendall(packet_bytes)

            # Wait for processing
            time.sleep(0.3)

            assert sum(r.n_pixels for r in received) >= 1

        finally:
            client.close()
            server.stop()

    def test_receive_multiple_packets(self, server, test_port, simulator):
        """Test receiving multiple packets in sequence."""
        received: list[BatchParseResult] = []

        def callback(result: BatchParseResult):
            received.append(result)

        server.set_data_callback(callback)
        server.start()
        time.sleep(0.1)

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.connect(("localhost", test_port))

            # Send 20 pixel packets
            for _ in range(20):
                packet_bytes = simulator.generate_pixel_event()
                client.sendall(packet_bytes)

            # Wait for processing
            time.sleep(0.3)

            total_pixels = sum(r.n_pixels for r in received)
            assert total_pixels >= 20

        finally:
            client.close()
            server.stop()

    def test_receive_mixed_packet_types(self, server, test_port, simulator):
        """Test receiving different packet types."""
        received: list[BatchParseResult] = []

        def callback(result: BatchParseResult):
            received.append(result)

        server.set_data_callback(callback)
        server.callback_batch_size = 1
        server.batch_byte_size = server.callback_batch_size * server.PACKET_SIZE
        server.start()
        time.sleep(0.1)

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.connect(("localhost", test_port))

            # Send pixel packet
            client.sendall(simulator.generate_pixel_event())

            # Send TDC pulse (2 packets)
            for tdc_packet in simulator.generate_tdc_pulse():
                client.sendall(tdc_packet)

            # Send another pixel packet
            client.sendall(simulator.generate_pixel_event())

            # Wait for processing
            time.sleep(0.5)

            assert any(r.n_pixels > 0 for r in received)
            assert any(r.n_tdc > 0 for r in received)

        finally:
            client.close()
            server.stop()

    def test_receive_stream_from_simulator(self, server, test_port):
        """Test receiving a realistic packet stream."""
        received: list[BatchParseResult] = []

        def callback(result: BatchParseResult):
            received.append(result)

        server.set_data_callback(callback)
        server.start()
        time.sleep(0.1)

        # Send packets in a separate thread
        def send_stream():
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                client.connect(("localhost", test_port))

                config = SimulatorConfig(pixel_count_rate=500, tdc_frequency=5.0)
                sim = PacketSimulator(config)

                for packet_bytes in sim.generate_stream(0.5):
                    client.sendall(packet_bytes)

            finally:
                client.close()

        sender_thread = threading.Thread(target=send_stream, daemon=True)
        sender_thread.start()
        sender_thread.join(timeout=2.0)

        # Wait for processing
        time.sleep(0.3)

        total_packets = sum(_batch_total_packets(r) for r in received)
        assert total_packets > 100

        pixel_count = sum(r.n_pixels for r in received)
        tdc_count = sum(r.n_tdc for r in received)

        assert pixel_count > 0
        assert tdc_count > 0

        server.stop()


class TestCallbackBatching:
    """Tests for callback batching mechanism."""

    def test_callback_batching(self, server, test_port, simulator):
        """Test that callbacks are batched correctly."""
        callback_invocations: list[int] = []

        def callback(result: BatchParseResult):
            callback_invocations.append(_batch_total_packets(result))

        server.set_data_callback(callback)
        server.callback_batch_size = 5  # Raw batches of 5 packets (60 bytes)
        server.batch_byte_size = server.callback_batch_size * server.PACKET_SIZE
        server.start()
        time.sleep(0.1)

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.connect(("localhost", test_port))

            # Send exactly 10 packets (should trigger 2 batches of 5)
            for _ in range(10):
                packet_bytes = simulator.generate_pixel_event()
                client.sendall(packet_bytes)

            # Wait for processing
            time.sleep(0.3)

            assert len(callback_invocations) >= 2
            assert any(batch_size == 5 for batch_size in callback_invocations)

        finally:
            client.close()
            server.stop()

    def test_callback_timeout_flush(self, server, test_port, simulator):
        """Test that partial batches flush on read timeout."""
        callback_invocations: list[int] = []

        def callback(result: BatchParseResult):
            callback_invocations.append(_batch_total_packets(result))

        server.set_data_callback(callback)
        server.callback_batch_size = 100  # Large batch that won't fill from 5 packets
        server.batch_byte_size = server.callback_batch_size * server.PACKET_SIZE
        server.start()
        time.sleep(0.1)

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.connect(("localhost", test_port))

            # Send only 5 packets (less than batch byte threshold)
            for _ in range(5):
                packet_bytes = simulator.generate_pixel_event()
                client.sendall(packet_bytes)

            # Partial batches flush after ~0.1s idle in the reader
            time.sleep(0.5)

            assert len(callback_invocations) >= 1
            assert sum(callback_invocations) == 5

        finally:
            client.close()
            server.stop()


class TestQueueManagement:
    """Tests for queue size and buffer management."""

    def test_queue_size_reporting(self, server):
        """Test that queue size can be queried."""
        initial_size = server.get_queue_size()
        assert initial_size == 0

        # After starting, size should still be queryable
        server.start()
        time.sleep(0.1)

        size = server.get_queue_size()
        assert isinstance(size, int)
        assert size >= 0

        server.stop()

    def test_callback_buffer_size_reporting(self, server):
        """Test that callback buffer size can be queried (np server always reports 0)."""
        initial_size = server.get_callback_buffer_size()
        assert initial_size == 0

        server.start()
        time.sleep(0.1)

        size = server.get_callback_buffer_size()
        assert isinstance(size, int)
        assert size == 0

        server.stop()

    def test_unknown_packet_counting(self, server, test_port):
        """Test that unknown packets are counted."""
        server.start()
        time.sleep(0.1)

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.connect(("localhost", test_port))

            # Create a packet with invalid type (type=15)
            packet_type = 15  # Invalid
            timestamp = 1000
            data = 0
            full_value = data | (timestamp << 36) | (packet_type << 92)
            invalid_packet = full_value.to_bytes(12, byteorder="big")

            client.sendall(invalid_packet)

            # Wait for processing
            time.sleep(0.2)

            # Should have counted the unknown packet
            unknown_count = server.get_unknown_packet_count()
            assert unknown_count > 0

        finally:
            client.close()
            server.stop()


class TestErrorHandling:
    """Tests for error handling and edge cases."""

    @pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_callback_exception_handling(self, server, test_port, simulator):
        """Test that server handles callback exceptions gracefully."""
        exception_raised = False

        def bad_callback(packets):
            nonlocal exception_raised
            exception_raised = True
            raise ValueError("Test exception in callback")

        server.set_data_callback(bad_callback)
        server.callback_batch_size = 1
        server.batch_byte_size = server.callback_batch_size * server.PACKET_SIZE
        server.start()
        time.sleep(0.2)

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.connect(("localhost", test_port))

            # Send some packets
            for _ in range(5):
                packet_bytes = simulator.generate_pixel_event()
                client.sendall(packet_bytes)

            # Wait for processing
            time.sleep(0.5)

            # Server should still be running despite callback errors
            assert server.running

            # Callback should have been invoked (even if it raised)
            assert exception_raised

        finally:
            client.close()
            server.stop()

    def test_client_disconnect(self, server, test_port, simulator):
        """Test that server handles client disconnection gracefully."""
        server.start()
        time.sleep(0.1)

        # Connect, send data, then abruptly disconnect
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.connect(("localhost", test_port))
            client.sendall(simulator.generate_pixel_event())
            # Abrupt close without proper shutdown
            client.close()

            time.sleep(0.2)

            # Server should still be running
            assert server.running

        finally:
            server.stop()

    def test_multiple_clients_sequential(self, server, test_port, simulator):
        """Test that server can handle multiple clients connecting sequentially."""
        received: list[BatchParseResult] = []

        def callback(result: BatchParseResult):
            received.append(result)

        server.set_data_callback(callback)
        server.callback_batch_size = 1
        server.batch_byte_size = server.callback_batch_size * server.PACKET_SIZE
        server.start()
        time.sleep(0.2)

        # First client
        client1 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client1.connect(("localhost", test_port))
            client1.sendall(simulator.generate_pixel_event())
            time.sleep(0.3)
        finally:
            client1.close()

        time.sleep(0.3)

        # Second client
        client2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client2.connect(("localhost", test_port))
            client2.sendall(simulator.generate_pixel_event())
            time.sleep(0.3)
        finally:
            client2.close()

        time.sleep(0.3)

        assert sum(r.n_pixels for r in received) >= 2

        server.stop()


class TestDebugMode:
    """Tests for debug mode functionality."""

    def test_debug_mode_packet_samples(self, test_port):
        """Test that debug mode captures packet samples."""
        server = SocketDataServer(
            host="localhost",
            port=test_port,
            buffer_size=100,
            debug=True,  # Enable debug mode
        )

        try:
            server.start()
            time.sleep(0.1)

            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                client.connect(("localhost", test_port))

                sim = PacketSimulator()
                for _ in range(5):
                    client.sendall(sim.generate_pixel_event())

                time.sleep(0.3)

                # Should have captured packet samples
                samples = server.get_valid_packet_samples()
                assert len(samples) > 0

            finally:
                client.close()
        finally:
            server.stop()

    def test_non_debug_mode_no_samples(self, server, test_port):
        """Test that non-debug mode doesn't capture samples."""
        server.start()
        time.sleep(0.1)

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.connect(("localhost", test_port))

            sim = PacketSimulator()
            client.sendall(sim.generate_pixel_event())

            time.sleep(0.2)

            # Should not have samples in non-debug mode
            samples = server.get_valid_packet_samples()
            assert len(samples) == 0

        finally:
            client.close()
            server.stop()


# Mark all tests as integration tests
pytestmark = pytest.mark.integration


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
