#!/usr/bin/env python3
"""
Parser for 96-bit live data processing packets.
Supports Pixel, TDC, and Control packet types.
"""

from dataclasses import dataclass
from enum import IntEnum
from typing import Union

# Constants for time conversions
TIMESTAMP_CLOCK_MHZ = 3840
TIMESTAMP_PS_PER_TICK = 260.41666  # 1 / 3840 MHz in picoseconds
TOT_CLOCK_MHZ = 40
TOT_NS_PER_TICK = 25.0  # 1 / 40 MHz in nanoseconds


class PacketType(IntEnum):
    PIXEL = 0
    TDC = 1
    CONTROL = 2


class TDCChannel(IntEnum):
    TDC_1 = 1
    TDC_2 = 2


class TDCEdge(IntEnum):
    RISE = 0
    FALL = 1


class ControlSubtype(IntEnum):
    SHUTTER_OPEN = 0
    SHUTTER_CLOSE = 1
    HEARTBEAT = 2
    TIMESTAMP = 3


@dataclass
class BasePacket:
    """Base packet class with common functionality"""

    packet_type: int
    timestamp: int  # 56-bit timestamp in 3840 MHz clock ticks

    @property
    def timestamp_ps(self) -> float:
        """Convert timestamp to picoseconds"""
        return self.timestamp * TIMESTAMP_PS_PER_TICK


@dataclass
class PixelPacket(BasePacket):
    """Pixel packet data structure"""

    tot: int  # Time over Threshold in 40 MHz clock ticks
    x: int  # X coordinate
    y: int  # Y coordinate
    reserved: int

    @property
    def tot_ns(self) -> float:
        """Convert ToT to nanoseconds"""
        return self.tot * TOT_NS_PER_TICK


@dataclass
class TDCPacket(BasePacket):
    """TDC packet data structure"""

    channel: int
    edge: int
    reserved: int

    @property
    def channel_name(self) -> str:
        """Get channel name"""
        return (
            TDCChannel(self.channel).name
            if self.channel in [1, 2]
            else f"Unknown({self.channel})"
        )

    @property
    def edge_name(self) -> str:
        """Get edge type name"""
        return (
            TDCEdge(self.edge).name if self.edge in [0, 1] else f"Unknown({self.edge})"
        )


@dataclass
class ControlPacket(BasePacket):
    """Control packet data structure"""

    subtype: int
    reserved: int

    @property
    def subtype_name(self) -> str:
        """Get subtype name"""
        return (
            ControlSubtype(self.subtype).name
            if self.subtype <= 3
            else f"Unknown({self.subtype})"
        )


class PacketParser:
    """Parser for 96-bit live data packets"""

    def __init__(self):
        self.packet_size = 12  # 96 bits = 12 bytes

    def parse(self, data: bytes) -> Union[PixelPacket, TDCPacket, ControlPacket, None]:
        """
        Parse a 96-bit packet from bytes.

        Args:
            data: 12 bytes representing the packet

        Returns:
            Parsed packet object or None if invalid
        """
        if len(data) != self.packet_size:
            raise ValueError(f"Expected {self.packet_size} bytes, got {len(data)}")

        # Convert 12 bytes directly to a 96-bit integer
        full_value = int.from_bytes(data, byteorder="big")

        # Extract common fields
        packet_type = (full_value >> 92) & 0xF  # bits 92-95
        timestamp = (full_value >> 36) & 0xFFFFFFFFFFFFFF  # bits 36-91 (56 bits)
        packet_specific = full_value & 0xFFFFFFFFF  # bits 0-35 (36 bits)

        if packet_type == PacketType.PIXEL:
            return self._parse_pixel(packet_type, timestamp, packet_specific)
        elif packet_type == PacketType.TDC:
            return self._parse_tdc(packet_type, timestamp, packet_specific)
        elif packet_type == PacketType.CONTROL:
            return self._parse_control(packet_type, timestamp, packet_specific)
        else:
            return None  # Unknown packet type, log or raise error here?

    def _parse_pixel(
        self, packet_type: int, timestamp: int, specific_data: int
    ) -> PixelPacket:
        """Parse pixel-specific data"""
        reserved = specific_data & 0x3F  # bits 0-5
        # x and y swapped compared to "Draft-format-for-live-data-processing-v2.pdf"
        x = (specific_data >> 6) & 0x3FF  # bits 6-15
        y = (specific_data >> 16) & 0x3FF  # bits 16-25
        tot = (specific_data >> 26) & 0x3FF  # bits 26-35

        return PixelPacket(
            packet_type=packet_type,
            timestamp=timestamp,
            tot=tot,
            x=x,
            y=y,
            reserved=reserved,
        )

    def _parse_tdc(
        self, packet_type: int, timestamp: int, specific_data: int
    ) -> TDCPacket:
        """Parse TDC-specific data"""
        reserved = specific_data & 0x1FFFFFFF  # bits 0-28
        edge = (specific_data >> 29) & 0x1  # bit 29
        channel = (specific_data >> 30) & 0x3F  # bits 30-35

        return TDCPacket(
            packet_type=packet_type,
            timestamp=timestamp,
            channel=channel,
            edge=edge,
            reserved=reserved,
        )

    def _parse_control(
        self, packet_type: int, timestamp: int, specific_data: int
    ) -> ControlPacket:
        """Parse control-specific data"""
        reserved = specific_data & 0xFFFFFFFF  # bits 0-31
        subtype = (specific_data >> 32) & 0xF  # bits 32-35

        return ControlPacket(
            packet_type=packet_type,
            timestamp=timestamp,
            subtype=subtype,
            reserved=reserved,
        )

    def parse_stream(self, data: bytes):
        """
        Parse multiple packets from a byte stream.

        Yields:
            Parsed packet objects
        """
        offset = 0
        while offset + self.packet_size <= len(data):
            packet_data = data[offset : offset + self.packet_size]
            packet = self.parse(packet_data)
            if packet:
                yield packet
            offset += self.packet_size


def format_packet(packet: Union[PixelPacket, TDCPacket, ControlPacket]) -> str:
    """Format a packet for display"""
    if isinstance(packet, PixelPacket):
        return (
            f"Pixel: time={packet.timestamp} ({packet.timestamp_ps:.2f} ps), "
            f"ToT={packet.tot} ({packet.tot_ns:.2f} ns), x={packet.x}, y={packet.y}"
        )
    elif isinstance(packet, TDCPacket):
        return (
            f"TDC: time={packet.timestamp} ({packet.timestamp_ps:.2f} ps), "
            f"channel={packet.channel_name}, edge={packet.edge_name}"
        )
    elif isinstance(packet, ControlPacket):
        return (
            f"Control: time={packet.timestamp} ({packet.timestamp_ps:.2f} ps), "
            f"subtype={packet.subtype_name}"
        )
    else:
        return "Unknown packet"


# Example usage
if __name__ == "__main__":
    # Create parser
    parser = PacketParser()

    # Example: Create a pixel packet manually for testing
    # This would normally come from your TCP stream
    # Packet type=0 (Pixel), timestamp=1000000, ToT=100, X=256, Y=128

    # Construct the 96-bit value
    packet_type = 0
    timestamp = 1000000
    tot = 100
    x = 256
    y = 128
    reserved = 0

    # Build pixel-specific data (36 bits)
    pixel_data = reserved | (y << 6) | (x << 16) | (tot << 26)

    # Build full 96-bit value
    full_value = pixel_data | (timestamp << 36) | (packet_type << 92)

    # Convert to bytes (big-endian)
    packet_bytes = full_value.to_bytes(12, byteorder="big")

    # Parse the packet
    packet = parser.parse(packet_bytes)
    print(format_packet(packet))

    # Example: Parse multiple packets from a stream
    print("\nParsing a stream with multiple packets:")
    # Simulate a stream with 3 packets
    stream = packet_bytes * 3
    for i, packet in enumerate(parser.parse_stream(stream)):
        print(f"Packet {i}: {format_packet(packet)}")
