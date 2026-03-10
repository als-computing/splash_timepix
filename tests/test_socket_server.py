"""Integration tests for the SocketDataServer.

Tests the multi-threaded socket server that receives and parses
TimePix3 packets using realistic simulator data.
"""

import socket
import threading
import time

import pytest

from splash_timepix.parser import PixelPacket, TDCPacket
from splash_timepix.simulator import PacketSimulator, SimulatorConfig
from splash_timepix.socket_server import SocketDataServer


def get_free_port():
    """Get a free port from the OS."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        s.listen(1)
        port = s.getsockname()[1]
    return port


@pytest.fixture
def test_port():
    """Get a free port for each test."""
    return get_free_port()


@pytest.fixture
def server(test_port):
    """Create a server instance for testing."""
    srv = SocketDataServer(
        host="localhost",
        port=test_port,
        buffer_size=100,
        debug=False,
        callback_batch_size=10,  # Small batch for faster testing
    )
    yield srv

    # Cleanup
    if srv.running:
        srv.stop()

    # Give OS time to release the port
    time.sleep(0.2)


@pytest.fixture
def simulator():
    """Create a simulator with test-friendly settings."""
    config = SimulatorConfig(
        pixel_count_rate=1000,  # 1 kHz
        tdc_frequency=10.0,  # 10 Hz
        include_control_packets=False,
    )
    return PacketSimulator(config)


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
        received_packets = []

        def callback(packets):
            received_packets.extend(packets)

        server.set_data_callback(callback)
        server.callback_batch_size = 1  # Flush immediately for this test
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

            # Should have received the packet
            assert len(received_packets) >= 1
            assert isinstance(received_packets[0], PixelPacket)

        finally:
            client.close()
            server.stop()

    def test_receive_multiple_packets(self, server, test_port, simulator):
        """Test receiving multiple packets in sequence."""
        received_packets = []

        def callback(packets):
            received_packets.extend(packets)

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

            # Should have received all packets
            assert len(received_packets) >= 20
            assert all(isinstance(p, PixelPacket) for p in received_packets)

        finally:
            client.close()
            server.stop()

    def test_receive_mixed_packet_types(self, server, test_port, simulator):
        """Test receiving different packet types."""
        received_packets = []

        def callback(packets):
            received_packets.extend(packets)

        server.set_data_callback(callback)
        server.callback_batch_size = 1  # Flush immediately
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

            # Should have all packet types
            packet_types = [type(p) for p in received_packets]
            assert PixelPacket in packet_types
            assert TDCPacket in packet_types

        finally:
            client.close()
            server.stop()

    def test_receive_stream_from_simulator(self, server, test_port):
        """Test receiving a realistic packet stream."""
        received_packets = []

        def callback(packets):
            received_packets.extend(packets)

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

        # Should have received many packets
        assert len(received_packets) > 100

        # Should have both pixel and TDC packets
        pixel_count = sum(1 for p in received_packets if isinstance(p, PixelPacket))
        tdc_count = sum(1 for p in received_packets if isinstance(p, TDCPacket))

        assert pixel_count > 0
        assert tdc_count > 0

        server.stop()


class TestCallbackBatching:
    """Tests for callback batching mechanism."""

    def test_callback_batching(self, server, test_port, simulator):
        """Test that callbacks are batched correctly."""
        callback_invocations = []

        def callback(packets):
            callback_invocations.append(len(packets))

        server.set_data_callback(callback)
        server.callback_batch_size = 5  # Batch every 5 packets
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

            # Should have at least 2 callback invocations with batch size 5
            assert len(callback_invocations) >= 2
            assert any(batch_size == 5 for batch_size in callback_invocations)

        finally:
            client.close()
            server.stop()

    def test_callback_timeout_flush(self, server, test_port, simulator):
        """Test that partial batches flush on timeout."""
        callback_invocations = []

        def callback(packets):
            callback_invocations.append(len(packets))

        server.set_data_callback(callback)
        server.callback_batch_size = 100  # Large batch that won't fill
        server.start()
        time.sleep(0.1)

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.connect(("localhost", test_port))

            # Send only 5 packets (less than batch size)
            for _ in range(5):
                packet_bytes = simulator.generate_pixel_event()
                client.sendall(packet_bytes)

            # Wait for timeout flush (1 second timeout in processor)
            time.sleep(1.5)

            # Should have flushed the partial batch
            assert len(callback_invocations) >= 1
            assert sum(callback_invocations) == 5  # Total packets received

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
        """Test that callback buffer size can be queried."""
        initial_size = server.get_callback_buffer_size()
        assert initial_size == 0

        server.start()
        time.sleep(0.1)

        size = server.get_callback_buffer_size()
        assert isinstance(size, int)
        assert size >= 0

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
        server.callback_batch_size = 1  # Ensure callback fires quickly
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
        received_packets = []

        def callback(packets):
            received_packets.extend(packets)

        server.set_data_callback(callback)
        server.callback_batch_size = 1  # Immediate flush
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

        # Should have received packets from both clients
        assert len(received_packets) >= 2

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
