#!/usr/bin/env python3
"""
Simulator for generating 96-bit live data packets.
Supports Pixel, TDC, and Control packet generation for experimental setups.
"""

import random
import time
from dataclasses import dataclass
from typing import Generator, List, Optional

import numpy as np

# Constants from the parser
TIMESTAMP_CLOCK_MHZ = 3840
TIMESTAMP_PS_PER_TICK = 260.41666
TOT_CLOCK_MHZ = 40
TOT_NS_PER_TICK = 25.0


class PacketType:
    PIXEL = 0
    TDC = 1
    CONTROL = 2


class ControlSubtype:
    SHUTTER_OPEN = 0
    SHUTTER_CLOSE = 1
    HEARTBEAT = 2
    TIMESTAMP = 3


class PacketBuilder:
    """Builder for creating 96-bit packets"""

    @staticmethod
    def build_pixel_packet(
        timestamp: int, tot: int, x: int, y: int, reserved: int = 0
    ) -> bytes:
        """
        Build a pixel packet.

        Args:
            timestamp: 56-bit timestamp value
            tot: Time over threshold (10 bits, 0-1023)
            x: X coordinate (10 bits, 0-1023)
            y: Y coordinate (10 bits, 0-1023)
            reserved: Reserved bits (6 bits)

        Returns:
            12 bytes representing the packet
        """
        # Validate inputs
        assert 0 <= timestamp < (1 << 56), f"Timestamp out of range: {timestamp}"
        assert 0 <= tot <= 1023, f"ToT out of range: {tot}"
        assert 0 <= x <= 1023, f"X out of range: {x}"
        assert 0 <= y <= 1023, f"Y out of range: {y}"
        assert 0 <= reserved <= 63, f"Reserved out of range: {reserved}"

        packet_type = PacketType.PIXEL
        pixel_data = reserved | (y << 6) | (x << 16) | (tot << 26)
        full_value = pixel_data | (timestamp << 36) | (packet_type << 92)

        return full_value.to_bytes(12, byteorder="little")

    @staticmethod
    def build_tdc_packet(
        timestamp: int, channel: int, edge: int, reserved: int = 0
    ) -> bytes:
        """
        Build a TDC packet.

        Args:
            timestamp: 56-bit timestamp value
            channel: TDC channel (6 bits, typically 1 or 2)
            edge: Edge type (1 bit, 0=rise, 1=fall)
            reserved: Reserved bits (29 bits)

        Returns:
            12 bytes representing the packet
        """
        assert 0 <= timestamp < (1 << 56), f"Timestamp out of range: {timestamp}"
        assert 0 <= channel <= 63, f"Channel out of range: {channel}"
        assert edge in [0, 1], f"Edge must be 0 or 1: {edge}"
        assert 0 <= reserved < (1 << 29), f"Reserved out of range: {reserved}"

        packet_type = PacketType.TDC
        tdc_data = reserved | (edge << 29) | (channel << 30)
        full_value = tdc_data | (timestamp << 36) | (packet_type << 92)

        return full_value.to_bytes(12, byteorder="little")

    @staticmethod
    def build_control_packet(timestamp: int, subtype: int, reserved: int = 0) -> bytes:
        """
        Build a control packet.

        Args:
            timestamp: 56-bit timestamp value
            subtype: Control subtype (4 bits)
            reserved: Reserved bits (32 bits)

        Returns:
            12 bytes representing the packet
        """
        assert 0 <= timestamp < (1 << 56), f"Timestamp out of range: {timestamp}"
        assert 0 <= subtype <= 15, f"Subtype out of range: {subtype}"
        assert 0 <= reserved < (1 << 32), f"Reserved out of range: {reserved}"

        packet_type = PacketType.CONTROL
        control_data = reserved | (subtype << 32)
        full_value = control_data | (timestamp << 36) | (packet_type << 92)

        return full_value.to_bytes(12, byteorder="little")


@dataclass
class SimulatorConfig:
    """Configuration for the packet simulator"""

    pixel_count_rate: float = 1000.0  # Average pixels per second
    tdc_frequency: float = 0.1  # TDC pulses per second (e.g., 0.1 Hz = every 10s)
    tdc_channel: int = 1  # Which TDC channel to use
    tdc_pulse_width_ns: float = 100.0  # Width of TDC pulse in nanoseconds
    tot_mean: float = 100.0  # Mean time-over-threshold value
    tot_sigma: float = 20.0  # Standard deviation for ToT
    detector_size_x: int = 1024  # Detector width
    detector_size_y: int = 1024  # Detector height
    include_control_packets: bool = True  # Whether to generate control packets
    control_packet_interval: float = 1.0  # Seconds between control sequences


class PacketSimulator:
    """Simulate realistic packet streams for detector experiments"""

    def __init__(
        self,
        config: Optional[SimulatorConfig] = None,
        start_timestamp: Optional[int] = None,
    ):
        """
        Initialize simulator.

        Args:
            config: Simulator configuration
            start_timestamp: Starting timestamp (defaults to current time)
        """
        self.config = config or SimulatorConfig()
        self.builder = PacketBuilder()

        # Initialize timestamp
        if start_timestamp is None:
            # Convert current time to timestamp ticks
            self.start_real_time = time.time()
            self.start_timestamp = int(
                self.start_real_time * TIMESTAMP_CLOCK_MHZ * 1_000_000
            )
        else:
            self.start_real_time = time.time()
            self.start_timestamp = start_timestamp

        # Calculate TDC pulse width in timestamp ticks
        self.tdc_pulse_width_ticks = int(
            self.config.tdc_pulse_width_ns / (TIMESTAMP_PS_PER_TICK / 1000)
        )

        # State
        self.shutter_open = False

    def get_current_timestamp(self) -> int:
        """Get current timestamp based on elapsed real time"""
        elapsed_time = time.time() - self.start_real_time
        elapsed_ticks = int(elapsed_time * TIMESTAMP_CLOCK_MHZ * 1_000_000)
        return self.start_timestamp + elapsed_ticks

    def generate_pixel_event(self) -> bytes:
        """Generate a single pixel event with random position and ToT"""
        timestamp = self.get_current_timestamp()

        # Random position uniformly distributed across detector
        x = random.randint(0, self.config.detector_size_x - 1)
        y = random.randint(0, self.config.detector_size_y - 1)

        # ToT with normal distribution, clipped to valid range
        tot = int(
            np.clip(
                np.random.normal(self.config.tot_mean, self.config.tot_sigma), 0, 1023
            )
        )

        return self.builder.build_pixel_packet(timestamp, tot, x, y)

    def generate_tdc_pulse(self, channel: Optional[int] = None) -> List[bytes]:
        """Generate a TDC pulse (rising and falling edge)"""
        timestamp = self.get_current_timestamp()
        channel = channel or self.config.tdc_channel

        # Rising edge
        rise_packet = self.builder.build_tdc_packet(timestamp, channel, edge=0)

        # Falling edge after pulse width
        fall_timestamp = timestamp + self.tdc_pulse_width_ticks
        fall_packet = self.builder.build_tdc_packet(fall_timestamp, channel, edge=1)

        return [rise_packet, fall_packet]

    def generate_control_sequence(self) -> List[bytes]:
        """Generate a control sequence"""
        packets = []
        timestamp = self.get_current_timestamp()

        # Heartbeat
        packets.append(
            self.builder.build_control_packet(timestamp, ControlSubtype.HEARTBEAT)
        )

        # Timestamp packet
        packets.append(
            self.builder.build_control_packet(timestamp + 100, ControlSubtype.TIMESTAMP)
        )

        return packets

    def generate_stream(self, duration_seconds: float) -> Generator[bytes, None, None]:
        """
        Generate a mixed stream of packets over time.

        Pixels follow Poisson distribution based on count rate.
        TDC pulses occur at fixed frequency.

        Args:
            duration_seconds: How long to generate packets

        Yields:
            Packet bytes in chronological order
        """
        start_time = time.time()

        # Calculate intervals
        tdc_interval = (
            1.0 / self.config.tdc_frequency
            if self.config.tdc_frequency > 0
            else float("inf")
        )
        control_interval = (
            self.config.control_packet_interval
            if self.config.include_control_packets
            else float("inf")
        )

        # Next event times
        next_tdc_time = start_time + tdc_interval
        next_control_time = start_time + control_interval

        # For pixel events, we use exponential distribution for inter-arrival times
        # This gives us Poisson-distributed events
        if self.config.pixel_count_rate > 0:
            next_pixel_time = start_time + random.expovariate(
                self.config.pixel_count_rate
            )
        else:
            next_pixel_time = float("inf")

        # Event queue to ensure chronological order
        events = []

        while time.time() - start_time < duration_seconds:
            current_time = time.time()

            # Check if we need to generate new events
            while next_pixel_time <= current_time and self.config.pixel_count_rate > 0:
                events.append((next_pixel_time, "pixel", None))
                next_pixel_time += random.expovariate(self.config.pixel_count_rate)

            while next_tdc_time <= current_time:
                events.append((next_tdc_time, "tdc", None))
                next_tdc_time += tdc_interval

            while (
                next_control_time <= current_time
                and self.config.include_control_packets
            ):
                events.append((next_control_time, "control", None))
                next_control_time += control_interval

            # Sort events by time
            events.sort(key=lambda x: x[0])

            # Process events that should have occurred by now
            while events and events[0][0] <= current_time:
                event_time, event_type, _ = events.pop(0)

                if event_type == "pixel":
                    yield self.generate_pixel_event()
                elif event_type == "tdc":
                    for packet in self.generate_tdc_pulse():
                        yield packet
                elif event_type == "control":
                    for packet in self.generate_control_sequence():
                        yield packet

            # Small sleep to prevent busy waiting
            time.sleep(0.0001)


def simulate_to_file(filename: str, duration: float = 10.0, **kwargs):
    """
    Save simulated packets to a file.

    Args:
        filename: Output filename
        duration: Simulation duration in seconds
        **kwargs: Configuration parameters for SimulatorConfig
    """
    config = SimulatorConfig(**kwargs)
    simulator = PacketSimulator(config)

    print(f"Simulating {duration}s of data:")
    print(f"  Pixel count rate: {config.pixel_count_rate} Hz")
    print(f"  TDC frequency: {config.tdc_frequency} Hz")
    print(f"  TDC pulse width: {config.tdc_pulse_width_ns} ns")

    packet_count = 0
    with open(filename, "wb") as f:
        for packet in simulator.generate_stream(duration):
            f.write(packet)
            packet_count += 1

    print(f"Generated {packet_count} packets")
    print(f"Average rate: {packet_count/duration:.1f} packets/s")


def simulate_to_socket(sock, duration: float = None, **kwargs):
    """
    Send simulated packets to a socket.

    Args:
        sock: Socket to send data to
        duration: Simulation duration (None for infinite)
        **kwargs: Configuration parameters for SimulatorConfig
    """
    config = SimulatorConfig(**kwargs)
    simulator = PacketSimulator(config)

    duration = duration or float("inf")
    for packet in simulator.generate_stream(duration):
        sock.sendall(packet)


# Example usage
if __name__ == "__main__":
    # Example 1: Simulate typical experimental conditions
    print("Example 1: Typical experiment")
    simulate_to_file(
        "experiment_data.bin",
        duration=10.0,
        pixel_count_rate=10000,  # 10 kHz average pixel rate
        tdc_frequency=1.0,  # 1 Hz TDC pulses
        tdc_pulse_width_ns=150,  # 150 ns pulse width
        tot_mean=120,  # Average ToT
        tot_sigma=30,  # ToT variation
    )

    # Example 2: Low-rate calibration run
    print("\nExample 2: Calibration run")
    simulate_to_file(
        "calibration_data.bin",
        duration=60.0,
        pixel_count_rate=100,  # 100 Hz pixel rate
        tdc_frequency=0.1,  # TDC pulse every 10 seconds
        tdc_pulse_width_ns=1000,  # 1 microsecond pulse
        include_control_packets=True,
    )

    # Example 3: Verify the generated data
    print("\nVerifying generated data from experiment_data.bin:")
    from parser import PacketParser, format_packet

    parser = PacketParser()
    pixel_count = 0
    tdc_count = 0
    control_count = 0

    with open("experiment_data.bin", "rb") as f:
        data = f.read()
        for packet in parser.parse_stream(data):
            if hasattr(packet, "x"):  # Pixel packet
                pixel_count += 1
            elif hasattr(packet, "channel"):  # TDC packet
                tdc_count += 1
                if tdc_count <= 4:  # Show first few TDC packets
                    print(f"  {format_packet(packet)}")
            else:  # Control packet
                control_count += 1

    print("\nPacket counts:")
    print(f"  Pixels: {pixel_count} ({pixel_count/10:.1f} Hz)")
    print(
        f"  TDC: {tdc_count} ({tdc_count/2/10:.1f} Hz)"
    )  # Divide by 2 for rise/fall pairs
    print(f"  Control: {control_count}")
