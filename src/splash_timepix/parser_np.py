#!/usr/bin/env python3
"""
NumPy-accelerated parser for 96-bit live data processing packets.

This is a drop-in replacement for parser.py that uses vectorized NumPy
operations for batch parsing. Provides 10-100x speedup at high count rates.

Supports Pixel, TDC, and Control packet types.

Usage:
    # Single packet (compatible with original API)
    from splash_timepix.parser_np import PacketParser, PixelPacket, TDCPacket
    parser = PacketParser()
    packet = parser.parse(data_12_bytes)
    
    # Batch parsing (NEW - much faster for high rates)
    result = parser.parse_batch(data_bytes)
    # result['pixel_x'], result['pixel_y'], etc. are NumPy arrays
"""

from dataclasses import dataclass
from enum import IntEnum
from typing import Union, Optional, Dict
import numpy as np

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


# =============================================================================
# DATACLASSES (unchanged from original - for API compatibility)
# =============================================================================

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
    x: int    # X coordinate
    y: int    # Y coordinate
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
        return TDCChannel(self.channel).name if self.channel in [1, 2] else f"Unknown({self.channel})"

    @property
    def edge_name(self) -> str:
        return TDCEdge(self.edge).name if self.edge in [0, 1] else f"Unknown({self.edge})"


@dataclass
class ControlPacket(BasePacket):
    """Control packet data structure"""
    subtype: int
    reserved: int

    @property
    def subtype_name(self) -> str:
        return ControlSubtype(self.subtype).name if self.subtype <= 3 else f"Unknown({self.subtype})"


# =============================================================================
# BATCH RESULT CONTAINER
# =============================================================================

@dataclass
class BatchParseResult:
    """Container for vectorized batch parse results.
    
    All arrays are aligned by index - pixel_x[i] corresponds to pixel_y[i], etc.
    
    Attributes:
        n_pixels: Number of pixel packets parsed
        n_tdc: Number of TDC packets parsed  
        n_control: Number of control packets parsed
        n_unknown: Number of unknown/invalid packets
        
        # Pixel arrays (length = n_pixels)
        pixel_x: X coordinates
        pixel_y: Y coordinates
        pixel_timestamp: Timestamps (3840 MHz ticks)
        pixel_tot: Time-over-threshold values
        
        # TDC arrays (length = n_tdc)
        tdc_timestamp: Timestamps (3840 MHz ticks)
        tdc_channel: Channel numbers (1 or 2)
        tdc_edge: Edge type (0=rise, 1=fall)
        
        # Control arrays (length = n_control)
        control_timestamp: Timestamps
        control_subtype: Subtype codes
        
        # Original indices for ordering reconstruction
        pixel_indices: Original packet indices for pixels
        tdc_indices: Original packet indices for TDCs
        control_indices: Original packet indices for controls
    """
    n_pixels: int
    n_tdc: int
    n_control: int
    n_unknown: int
    
    # Pixel data
    pixel_x: np.ndarray
    pixel_y: np.ndarray
    pixel_timestamp: np.ndarray
    pixel_tot: np.ndarray
    pixel_indices: np.ndarray
    
    # TDC data
    tdc_timestamp: np.ndarray
    tdc_channel: np.ndarray
    tdc_edge: np.ndarray
    tdc_indices: np.ndarray
    
    # Control data
    control_timestamp: np.ndarray
    control_subtype: np.ndarray
    control_indices: np.ndarray


# =============================================================================
# NUMPY PACKET PARSER
# =============================================================================

class PacketParser:
    """Parser for 96-bit live data packets with NumPy acceleration.
    
    Provides both single-packet parsing (API compatible with original)
    and batch parsing for high-throughput applications.
    """

    def __init__(self):
        self.packet_size = 12  # 96 bits = 12 bytes

    # -------------------------------------------------------------------------
    # SINGLE PACKET PARSING (original API)
    # -------------------------------------------------------------------------
    
    def parse(self, data: bytes) -> Union[PixelPacket, TDCPacket, ControlPacket, None]:
        """Parse a single 96-bit packet from bytes.
        
        API compatible with original parser.py
        
        Args:
            data: 12 bytes representing the packet
            
        Returns:
            Parsed packet object or None if invalid
        """
        if len(data) != self.packet_size:
            raise ValueError(f"Expected {self.packet_size} bytes, got {len(data)}")

        full_value = int.from_bytes(data, byteorder="big")
        
        packet_type = (full_value >> 92) & 0xF
        timestamp = (full_value >> 36) & 0xFFFFFFFFFFFFFF
        packet_specific = full_value & 0xFFFFFFFFF

        if packet_type == PacketType.PIXEL:
            return self._parse_pixel(packet_type, timestamp, packet_specific)
        elif packet_type == PacketType.TDC:
            return self._parse_tdc(packet_type, timestamp, packet_specific)
        elif packet_type == PacketType.CONTROL:
            return self._parse_control(packet_type, timestamp, packet_specific)
        else:
            return None

    def _parse_pixel(self, packet_type: int, timestamp: int, specific_data: int) -> PixelPacket:
        reserved = specific_data & 0x3F
        x = (specific_data >> 6) & 0x3FF
        y = (specific_data >> 16) & 0x3FF
        tot = (specific_data >> 26) & 0x3FF
        return PixelPacket(packet_type=packet_type, timestamp=timestamp,
                          tot=tot, x=x, y=y, reserved=reserved)

    def _parse_tdc(self, packet_type: int, timestamp: int, specific_data: int) -> TDCPacket:
        reserved = specific_data & 0x1FFFFFFF
        edge = (specific_data >> 29) & 0x1
        channel = (specific_data >> 30) & 0x3F
        return TDCPacket(packet_type=packet_type, timestamp=timestamp,
                        channel=channel, edge=edge, reserved=reserved)

    def _parse_control(self, packet_type: int, timestamp: int, specific_data: int) -> ControlPacket:
        reserved = specific_data & 0xFFFFFFFF
        subtype = (specific_data >> 32) & 0xF
        return ControlPacket(packet_type=packet_type, timestamp=timestamp,
                            subtype=subtype, reserved=reserved)

    def parse_stream(self, data: bytes):
        """Parse multiple packets from a byte stream (generator).
        
        API compatible with original parser.py
        """
        offset = 0
        while offset + self.packet_size <= len(data):
            packet_data = data[offset:offset + self.packet_size]
            packet = self.parse(packet_data)
            if packet:
                yield packet
            offset += self.packet_size

    # -------------------------------------------------------------------------
    # BATCH PARSING (NumPy accelerated - NEW)
    # -------------------------------------------------------------------------
    
    def parse_batch(self, data: bytes) -> BatchParseResult:
        """Parse multiple 96-bit packets using vectorized NumPy operations.
        
        This is the high-performance method for processing large batches.
        Returns arrays instead of individual packet objects.
        
        Args:
            data: Byte buffer containing N * 12 bytes
            
        Returns:
            BatchParseResult with separate arrays for each packet type
        """
        n_packets = len(data) // self.packet_size
        if n_packets == 0:
            return self._empty_result()
        
        # Trim to exact multiple of packet size
        data = data[:n_packets * self.packet_size]
        
        # Convert to numpy array and reshape to (n_packets, 12)
        raw = np.frombuffer(data, dtype=np.uint8).reshape(n_packets, 12)
        
        # ---- VECTORIZED 96-BIT PARSING ----
        # We need to extract a 96-bit value from 12 bytes (big-endian)
        # Strategy: combine bytes into larger integers, then shift/mask
        
        # Bytes 0-3 -> upper 32 bits (contains packet_type and upper timestamp)
        # Bytes 4-7 -> middle 32 bits (lower timestamp and upper specific)
        # Bytes 8-11 -> lower 32 bits (specific data)
        
        upper = (raw[:, 0].astype(np.uint64) << 24 |
                 raw[:, 1].astype(np.uint64) << 16 |
                 raw[:, 2].astype(np.uint64) << 8 |
                 raw[:, 3].astype(np.uint64))
        
        middle = (raw[:, 4].astype(np.uint64) << 24 |
                  raw[:, 5].astype(np.uint64) << 16 |
                  raw[:, 6].astype(np.uint64) << 8 |
                  raw[:, 7].astype(np.uint64))
        
        lower = (raw[:, 8].astype(np.uint64) << 24 |
                 raw[:, 9].astype(np.uint64) << 16 |
                 raw[:, 10].astype(np.uint64) << 8 |
                 raw[:, 11].astype(np.uint64))
        
        # Reconstruct 96-bit value components
        # full_value = upper << 64 | middle << 32 | lower
        # But we extract fields directly to avoid 128-bit issues
        
        # Packet type: bits 92-95 (top 4 bits of upper word)
        packet_types = ((upper >> 28) & 0xF).astype(np.uint8)
        
        # Timestamp: bits 36-91 (56 bits)
        # = (upper & 0x0FFFFFFF) << 28 | (middle >> 4)
        timestamp_high = (upper & 0x0FFFFFFF).astype(np.uint64) << 28
        timestamp_low = (middle >> 4).astype(np.uint64)
        timestamps = timestamp_high | timestamp_low
        
        # Packet-specific: bits 0-35 (36 bits)
        # = (middle & 0xF) << 32 | lower
        specific_high = (middle & 0xF).astype(np.uint64) << 32
        specific = specific_high | lower
        
        # ---- SEPARATE BY PACKET TYPE ----
        pixel_mask = packet_types == PacketType.PIXEL
        tdc_mask = packet_types == PacketType.TDC
        control_mask = packet_types == PacketType.CONTROL
        unknown_mask = ~(pixel_mask | tdc_mask | control_mask)
        
        # ---- EXTRACT PIXEL FIELDS ----
        pixel_indices = np.where(pixel_mask)[0]
        pixel_specific = specific[pixel_mask]
        pixel_timestamps = timestamps[pixel_mask]
        
        pixel_x = ((pixel_specific >> 6) & 0x3FF).astype(np.int32)
        pixel_y = ((pixel_specific >> 16) & 0x3FF).astype(np.int32)
        pixel_tot = ((pixel_specific >> 26) & 0x3FF).astype(np.int32)
        
        # ---- EXTRACT TDC FIELDS ----
        tdc_indices = np.where(tdc_mask)[0]
        tdc_specific = specific[tdc_mask]
        tdc_timestamps = timestamps[tdc_mask]
        
        tdc_edge = ((tdc_specific >> 29) & 0x1).astype(np.uint8)
        tdc_channel = ((tdc_specific >> 30) & 0x3F).astype(np.uint8)
        
        # ---- EXTRACT CONTROL FIELDS ----
        control_indices = np.where(control_mask)[0]
        control_specific = specific[control_mask]
        control_timestamps = timestamps[control_mask]
        
        control_subtype = ((control_specific >> 32) & 0xF).astype(np.uint8)
        
        return BatchParseResult(
            n_pixels=len(pixel_indices),
            n_tdc=len(tdc_indices),
            n_control=len(control_indices),
            n_unknown=int(np.sum(unknown_mask)),
            
            pixel_x=pixel_x,
            pixel_y=pixel_y,
            pixel_timestamp=pixel_timestamps,
            pixel_tot=pixel_tot,
            pixel_indices=pixel_indices,
            
            tdc_timestamp=tdc_timestamps,
            tdc_channel=tdc_channel,
            tdc_edge=tdc_edge,
            tdc_indices=tdc_indices,
            
            control_timestamp=control_timestamps,
            control_subtype=control_subtype,
            control_indices=control_indices,
        )
    
    def _empty_result(self) -> BatchParseResult:
        """Return empty result for zero-length input."""
        empty_i32 = np.array([], dtype=np.int32)
        empty_i64 = np.array([], dtype=np.int64)
        empty_u8 = np.array([], dtype=np.uint8)
        empty_idx = np.array([], dtype=np.int64)
        
        return BatchParseResult(
            n_pixels=0, n_tdc=0, n_control=0, n_unknown=0,
            pixel_x=empty_i32, pixel_y=empty_i32,
            pixel_timestamp=empty_i64, pixel_tot=empty_i32,
            pixel_indices=empty_idx,
            tdc_timestamp=empty_i64, tdc_channel=empty_u8,
            tdc_edge=empty_u8, tdc_indices=empty_idx,
            control_timestamp=empty_i64, control_subtype=empty_u8,
            control_indices=empty_idx,
        )
    
    def parse_batch_to_objects(self, data: bytes) -> list:
        """Parse batch but return list of packet objects (hybrid approach).
        
        Useful when you need objects but want faster parsing than parse_stream.
        ~3-5x faster than parse_stream due to vectorized parsing.
        """
        result = self.parse_batch(data)
        packets = []
        
        # Create pixel packets
        for i in range(result.n_pixels):
            packets.append((
                result.pixel_indices[i],
                PixelPacket(
                    packet_type=PacketType.PIXEL,
                    timestamp=int(result.pixel_timestamp[i]),
                    tot=int(result.pixel_tot[i]),
                    x=int(result.pixel_x[i]),
                    y=int(result.pixel_y[i]),
                    reserved=0
                )
            ))
        
        # Create TDC packets
        for i in range(result.n_tdc):
            packets.append((
                result.tdc_indices[i],
                TDCPacket(
                    packet_type=PacketType.TDC,
                    timestamp=int(result.tdc_timestamp[i]),
                    channel=int(result.tdc_channel[i]),
                    edge=int(result.tdc_edge[i]),
                    reserved=0
                )
            ))
        
        # Create control packets
        for i in range(result.n_control):
            packets.append((
                result.control_indices[i],
                ControlPacket(
                    packet_type=PacketType.CONTROL,
                    timestamp=int(result.control_timestamp[i]),
                    subtype=int(result.control_subtype[i]),
                    reserved=0
                )
            ))
        
        # Sort by original index to maintain order
        packets.sort(key=lambda x: x[0])
        return [p[1] for p in packets]


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def format_packet(packet: Union[PixelPacket, TDCPacket, ControlPacket]) -> str:
    """Format a packet for display (compatible with original)."""
    if isinstance(packet, PixelPacket):
        return (f"Pixel: time={packet.timestamp} ({packet.timestamp_ps:.2f} ps), "
                f"ToT={packet.tot} ({packet.tot_ns:.2f} ns), x={packet.x}, y={packet.y}")
    elif isinstance(packet, TDCPacket):
        return (f"TDC: time={packet.timestamp} ({packet.timestamp_ps:.2f} ps), "
                f"channel={packet.channel_name}, edge={packet.edge_name}")
    elif isinstance(packet, ControlPacket):
        return (f"Control: time={packet.timestamp} ({packet.timestamp_ps:.2f} ps), "
                f"subtype={packet.subtype_name}")
    else:
        return "Unknown packet"


# =============================================================================
# BENCHMARK / TEST
# =============================================================================

if __name__ == "__main__":
    import time
    
    parser = PacketParser()
    
    # Create test packets
    def make_pixel_bytes(x, y, tot, timestamp):
        packet_type = 0
        reserved = 0
        pixel_data = reserved | (x << 6) | (y << 16) | (tot << 26)
        full_value = pixel_data | (timestamp << 36) | (packet_type << 92)
        return full_value.to_bytes(12, byteorder="big")
    
    def make_tdc_bytes(channel, edge, timestamp):
        packet_type = 1
        reserved = 0
        tdc_data = reserved | (edge << 29) | (channel << 30)
        full_value = tdc_data | (timestamp << 36) | (packet_type << 92)
        return full_value.to_bytes(12, byteorder="big")
    
    # Test single packet parsing (compatibility)
    print("=== Single Packet Parsing (API Compatibility) ===")
    pixel_bytes = make_pixel_bytes(x=256, y=128, tot=100, timestamp=1000000)
    packet = parser.parse(pixel_bytes)
    print(format_packet(packet))
    
    tdc_bytes = make_tdc_bytes(channel=1, edge=0, timestamp=2000000)
    packet = parser.parse(tdc_bytes)
    print(format_packet(packet))
    
    # Benchmark batch parsing
    print("\n=== Batch Parsing Benchmark ===")
    
    # Generate test data: 100k packets (90% pixel, 10% TDC)
    n_test = 100000
    test_data = b""
    for i in range(n_test):
        if i % 10 == 0:
            test_data += make_tdc_bytes(1, 0, i * 1000)
        else:
            test_data += make_pixel_bytes(i % 256, i % 256, 50, i * 1000)
    
    # Benchmark original parse_stream
    t0 = time.perf_counter()
    count_stream = sum(1 for _ in parser.parse_stream(test_data))
    t_stream = time.perf_counter() - t0
    print(f"parse_stream: {count_stream} packets in {t_stream*1000:.1f} ms "
          f"({count_stream/t_stream/1e6:.2f} M packets/sec)")
    
    # Benchmark batch parsing
    t0 = time.perf_counter()
    result = parser.parse_batch(test_data)
    t_batch = time.perf_counter() - t0
    count_batch = result.n_pixels + result.n_tdc + result.n_control
    print(f"parse_batch:  {count_batch} packets in {t_batch*1000:.1f} ms "
          f"({count_batch/t_batch/1e6:.2f} M packets/sec)")
    
    print(f"\nSpeedup: {t_stream/t_batch:.1f}x")
    print(f"  Pixels: {result.n_pixels}, TDCs: {result.n_tdc}, Unknown: {result.n_unknown}")
    