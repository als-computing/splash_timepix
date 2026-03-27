"""Unit and integration tests for the TimePix3 packet simulator.

Tests packet generation, configuration handling, and stream production.
"""

import time

import pytest

from splash_timepix.parser import ControlPacket, PacketParser, PixelPacket, TDCPacket
from splash_timepix.simulator import ControlSubtype, PacketBuilder, PacketSimulator, SimulatorConfig


@pytest.fixture
def parser():
    """Create a parser for validating generated packets."""
    return PacketParser()


@pytest.fixture
def default_config():
    """Create a default simulator configuration."""
    return SimulatorConfig()


class TestPacketBuilder:
    """Tests for the PacketBuilder class."""

    def test_build_pixel_packet_structure(self, parser):
        """Test that built pixel packets have correct structure."""
        packet_bytes = PacketBuilder.build_pixel_packet(timestamp=1000, tot=100, x=128, y=64, reserved=0)

        # Verify it's 12 bytes
        assert len(packet_bytes) == 12

        # Parse and verify fields
        packet = parser.parse(packet_bytes)
        assert isinstance(packet, PixelPacket)
        assert packet.timestamp == 1000
        assert packet.tot == 100
        assert packet.x == 128
        assert packet.y == 64

    def test_build_pixel_packet_boundary_values(self, parser):
        """Test pixel packets with min and max values."""
        # Minimum values
        packet_bytes = PacketBuilder.build_pixel_packet(0, 0, 0, 0, 0)
        packet = parser.parse(packet_bytes)
        assert packet.timestamp == 0
        assert packet.tot == 0
        assert packet.x == 0
        assert packet.y == 0

        # Maximum values (10-bit fields = 1023)
        packet_bytes = PacketBuilder.build_pixel_packet(timestamp=(1 << 56) - 1, tot=1023, x=1023, y=1023, reserved=63)
        packet = parser.parse(packet_bytes)
        assert packet.timestamp == (1 << 56) - 1
        assert packet.tot == 1023
        assert packet.x == 1023
        assert packet.y == 1023

    def test_build_tdc_packet_structure(self, parser):
        """Test that built TDC packets have correct structure."""
        packet_bytes = PacketBuilder.build_tdc_packet(timestamp=2000, channel=1, edge=0, reserved=0)  # rising

        assert len(packet_bytes) == 12

        packet = parser.parse(packet_bytes)
        assert isinstance(packet, TDCPacket)
        assert packet.timestamp == 2000
        assert packet.channel == 1
        assert packet.edge == 0

    def test_build_tdc_packet_both_edges(self, parser):
        """Test TDC packets with rising and falling edges."""
        # Rising edge
        packet_bytes = PacketBuilder.build_tdc_packet(1000, 1, 0, 0)
        packet = parser.parse(packet_bytes)
        assert packet.edge == 0

        # Falling edge
        packet_bytes = PacketBuilder.build_tdc_packet(2000, 1, 1, 0)
        packet = parser.parse(packet_bytes)
        assert packet.edge == 1

    def test_build_tdc_packet_both_channels(self, parser):
        """Test TDC packets on both channels."""
        # Channel 1
        packet_bytes = PacketBuilder.build_tdc_packet(1000, 1, 0, 0)
        packet = parser.parse(packet_bytes)
        assert packet.channel == 1

        # Channel 2
        packet_bytes = PacketBuilder.build_tdc_packet(2000, 2, 0, 0)
        packet = parser.parse(packet_bytes)
        assert packet.channel == 2

    def test_build_control_packet_structure(self, parser):
        """Test that built control packets have correct structure."""
        packet_bytes = PacketBuilder.build_control_packet(timestamp=3000, subtype=ControlSubtype.HEARTBEAT, reserved=0)

        assert len(packet_bytes) == 12

        packet = parser.parse(packet_bytes)
        assert isinstance(packet, ControlPacket)
        assert packet.timestamp == 3000
        assert packet.subtype == ControlSubtype.HEARTBEAT

    def test_build_control_packet_all_subtypes(self, parser):
        """Test control packets with all valid subtypes."""
        subtypes = [
            ControlSubtype.SHUTTER_OPEN,
            ControlSubtype.SHUTTER_CLOSE,
            ControlSubtype.HEARTBEAT,
            ControlSubtype.TIMESTAMP,
        ]

        for subtype in subtypes:
            packet_bytes = PacketBuilder.build_control_packet(1000, subtype, 0)
            packet = parser.parse(packet_bytes)
            assert packet.subtype == subtype


class TestSimulatorConfig:
    """Tests for SimulatorConfig dataclass."""

    def test_default_config_values(self):
        """Test that default configuration has expected values."""
        config = SimulatorConfig()

        assert config.pixel_count_rate == 1000
        assert config.tdc_frequency == 0.1
        assert config.tdc_channel == 1
        assert config.tdc_pulse_width_ns == 100.0
        assert config.tot_mean == 100.0
        assert config.tot_sigma == 20.0
        assert config.detector_size_x == 256
        assert config.detector_size_y == 256
        assert config.include_control_packets is False

    def test_custom_config_values(self):
        """Test creating configuration with custom values."""
        config = SimulatorConfig(
            pixel_count_rate=5000,
            tdc_frequency=8.0,
            tdc_channel=2,
            detector_size_x=512,
            detector_size_y=512,
        )

        assert config.pixel_count_rate == 5000
        assert config.tdc_frequency == 8.0
        assert config.tdc_channel == 2
        assert config.detector_size_x == 512
        assert config.detector_size_y == 512


class TestPacketSimulator:
    """Tests for the PacketSimulator class."""

    def test_simulator_initialization(self):
        """Test simulator initializes with default config."""
        sim = PacketSimulator()
        assert sim.config is not None
        assert sim.builder is not None

    def test_simulator_custom_config(self):
        """Test simulator accepts custom configuration."""
        config = SimulatorConfig(pixel_count_rate=2000)
        sim = PacketSimulator(config)
        assert sim.config.pixel_count_rate == 2000

    def test_get_current_timestamp_increases(self):
        """Test that current timestamp increases over time."""
        sim = PacketSimulator()

        ts1 = sim.get_current_timestamp()
        time.sleep(0.01)  # 10ms
        ts2 = sim.get_current_timestamp()

        assert ts2 > ts1

    def test_generate_pixel_event_valid(self, parser):
        """Test that generated pixel events are valid."""
        sim = PacketSimulator()
        packet_bytes = sim.generate_pixel_event()

        # Should be parseable
        packet = parser.parse(packet_bytes)
        assert isinstance(packet, PixelPacket)

        # Coordinates should be within detector bounds
        assert 0 <= packet.x < sim.config.detector_size_x
        assert 0 <= packet.y < sim.config.detector_size_y

        # ToT should be non-negative
        assert packet.tot >= 0
        assert packet.tot <= 1023  # 10-bit max

    def test_generate_tdc_pulse_pair(self, parser):
        """Test that TDC pulse generates rise and fall pair."""
        sim = PacketSimulator()
        pulse_packets = sim.generate_tdc_pulse()

        # Should return exactly 2 packets
        assert len(pulse_packets) == 2

        # Parse both
        rise_packet = parser.parse(pulse_packets[0])
        fall_packet = parser.parse(pulse_packets[1])

        # Both should be TDC packets
        assert isinstance(rise_packet, TDCPacket)
        assert isinstance(fall_packet, TDCPacket)

        # First should be rising edge
        assert rise_packet.edge == 0

        # Second should be falling edge
        assert fall_packet.edge == 1

        # Should be on same channel
        assert rise_packet.channel == fall_packet.channel

        # Fall timestamp should be after rise
        assert fall_packet.timestamp > rise_packet.timestamp

    def test_generate_tdc_pulse_custom_channel(self, parser):
        """Test TDC pulse on custom channel."""
        config = SimulatorConfig(tdc_channel=2)
        sim = PacketSimulator(config)
        pulse_packets = sim.generate_tdc_pulse()

        rise_packet = parser.parse(pulse_packets[0])
        assert rise_packet.channel == 2

    def test_generate_control_sequence(self, parser):
        """Test control sequence generation."""
        sim = PacketSimulator()
        control_packets = sim.generate_control_sequence()

        # Should generate multiple control packets
        assert len(control_packets) >= 2

        # All should be parseable as control packets
        for packet_bytes in control_packets:
            packet = parser.parse(packet_bytes)
            assert isinstance(packet, ControlPacket)


class TestStreamGeneration:
    """Integration tests for packet stream generation."""

    def test_generate_stream_duration(self, parser):
        """Test that stream generates for approximately correct duration."""
        config = SimulatorConfig(pixel_count_rate=100, tdc_frequency=1.0)
        sim = PacketSimulator(config)

        duration = 0.5  # 500ms
        start_time = time.time()

        packet_count = 0
        for packet_bytes in sim.generate_stream(duration):
            packet_count += 1

        elapsed = time.time() - start_time

        # Should take approximately the requested duration
        # Allow 10% tolerance for timing variability
        assert abs(elapsed - duration) < duration * 0.1

        # Should have generated some packets
        assert packet_count > 0

    def test_generate_stream_all_parseable(self, parser):
        """Test that all generated packets are valid."""
        config = SimulatorConfig(pixel_count_rate=1000, tdc_frequency=20.0, include_control_packets=True)
        sim = PacketSimulator(config)

        packet_count = 0
        for packet_bytes in sim.generate_stream(0.2):  # 200ms
            packet = parser.parse(packet_bytes)
            assert packet is not None  # Should parse successfully
            packet_count += 1

        # Should have generated multiple packets
        assert packet_count > 10

    def test_generate_stream_contains_mixed_types(self, parser):
        """Test that stream contains multiple packet types."""
        config = SimulatorConfig(
            pixel_count_rate=5000,
            tdc_frequency=100.0,
            include_control_packets=True,
            control_packet_interval=0.1,
        )
        sim = PacketSimulator(config)

        pixel_count = 0
        tdc_count = 0
        control_count = 0

        for packet_bytes in sim.generate_stream(0.5):  # 500ms
            packet = parser.parse(packet_bytes)

            if isinstance(packet, PixelPacket):
                pixel_count += 1
            elif isinstance(packet, TDCPacket):
                tdc_count += 1
            elif isinstance(packet, ControlPacket):
                control_count += 1

        # Should have all three types
        assert pixel_count > 0, "No pixel packets generated"
        assert tdc_count > 0, "No TDC packets generated"
        assert control_count > 0, "No control packets generated"

    def test_generate_stream_chronological_order(self, parser):
        """Test that packets are generated in chronological order."""
        config = SimulatorConfig(pixel_count_rate=500, tdc_frequency=10.0)
        sim = PacketSimulator(config)

        last_timestamp = 0
        for packet_bytes in sim.generate_stream(0.3):
            packet = parser.parse(packet_bytes)

            # Timestamps should be monotonically increasing
            # (allowing for timestamp wrap-around at 56 bits)
            if packet.timestamp < last_timestamp:
                # Check if this is a wrap-around
                assert last_timestamp > (1 << 55)  # Near max value

            last_timestamp = packet.timestamp

    def test_generate_stream_tdc_pairs_ordered(self, parser):
        """Test that TDC rise/fall pairs are properly ordered."""
        config = SimulatorConfig(pixel_count_rate=100, tdc_frequency=50.0)
        sim = PacketSimulator(config)

        tdc_packets = []
        for packet_bytes in sim.generate_stream(0.5):
            packet = parser.parse(packet_bytes)
            if isinstance(packet, TDCPacket):
                tdc_packets.append(packet)

        # Should have TDC packets
        assert len(tdc_packets) > 0

        # Check that rise/fall pairs are in order
        for i in range(0, len(tdc_packets) - 1, 2):
            if i + 1 < len(tdc_packets):
                rise = tdc_packets[i]
                fall = tdc_packets[i + 1]

                # Rise should come before fall
                assert rise.edge == 0, f"Expected rising edge at index {i}"
                assert fall.edge == 1, f"Expected falling edge at index {i+1}"
                assert fall.timestamp > rise.timestamp

    def test_generate_stream_no_control_packets_by_default(self, parser):
        """Test that control packets are not generated by default."""
        config = SimulatorConfig(
            pixel_count_rate=500,
            tdc_frequency=100.0,
            include_control_packets=False,  # Explicit default
        )
        sim = PacketSimulator(config)

        control_count = 0
        for packet_bytes in sim.generate_stream(0.3):
            packet = parser.parse(packet_bytes)
            if isinstance(packet, ControlPacket):
                control_count += 1

        assert control_count == 0, "Control packets generated when disabled"

    @pytest.mark.slow
    def test_generate_stream_long_duration(self, parser):
        """Test stream generation over longer duration."""
        config = SimulatorConfig(pixel_count_rate=1000, tdc_frequency=10.0)
        sim = PacketSimulator(config)

        packet_count = 0
        for packet_bytes in sim.generate_stream(2.0):  # 2 seconds
            packet = parser.parse(packet_bytes)
            assert packet is not None
            packet_count += 1

        # Should generate many packets
        # Expect roughly: 1000 pixels/s * 2s + 1 Hz TDC * 2s * 2 edges = ~2004
        assert packet_count > 1500, f"Only generated {packet_count} packets"


class TestPacketValidation:
    """Tests to ensure generated packets match expected formats."""

    def test_pixel_coordinates_within_detector(self, parser):
        """Test that pixel coordinates stay within detector bounds."""
        config = SimulatorConfig(detector_size_x=256, detector_size_y=256)
        sim = PacketSimulator(config)

        for _ in range(100):  # Generate 100 pixel events
            packet_bytes = sim.generate_pixel_event()
            packet = parser.parse(packet_bytes)

            assert 0 <= packet.x < 256
            assert 0 <= packet.y < 256

    def test_tot_values_reasonable(self, parser):
        """Test that ToT values are within expected range."""
        config = SimulatorConfig(tot_mean=100, tot_sigma=20)
        sim = PacketSimulator(config)

        tot_values = []
        for _ in range(100):
            packet_bytes = sim.generate_pixel_event()
            packet = parser.parse(packet_bytes)
            tot_values.append(packet.tot)

        # All should be non-negative and within 10-bit range
        assert all(0 <= tot <= 1023 for tot in tot_values)

        # Most should be reasonably close to mean (within 3 sigma)
        in_range = sum(1 for tot in tot_values if 40 <= tot <= 160)
        assert in_range > 90, "ToT values not following expected distribution"


# Mark integration tests
pytestmark = pytest.mark.unit


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
