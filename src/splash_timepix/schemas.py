"""
Message schemas for TimePix3 data acquisition.

This module defines schemas for start, stop, and event messages that signal the beginning,
end, and data flushes of a data acquisition run. These messages are published via ZMQ
alongside the data arrays to allow downstream consumers to track acquisition lifecycle.

Similar to ArroyoXPS, but adapted for TimePix3 detector parameters.
"""

from typing import Literal, Optional

import numpy as np
from pydantic import BaseModel, ConfigDict, Field


class TimePixStart(BaseModel):
    """
    Start message sent at the beginning of a data acquisition run.

    This message is published when the first data arrives and contains all
    the configuration parameters for the acquisition.

    Example JSON:
    {
        "msg_type": "start",
        "scan_name": "acquisition_20250115T143022Z",
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
        "collapse_y": false,
        "zmq_port": 5657,
        "tcp_port": 9090
    }
    """

    msg_type: Literal["start"] = "start"
    scan_name: str = Field(..., description="Unique identifier for this acquisition run")
    tdc_frequency_hz: float = Field(..., description="TDC trigger frequency in Hz")
    t_delta_ns: float = Field(..., description="Time bin width in nanoseconds")
    t_cycle_ns: float = Field(..., description="Full time cycle in nanoseconds")
    n_bins: int = Field(..., description="Number of time bins")
    detector_size_x: int = Field(..., description="Detector X dimension")
    detector_size_y: int = Field(..., description="Detector Y dimension")
    flush_interval_s: float = Field(..., description="Flush interval in seconds")
    cycles_per_flush: int = Field(..., description="Expected cycles per flush")
    tdc_channel: int = Field(..., description="TDC channel (0=both, 1=ch1, 2=ch2)")
    tdc_edge: str = Field(..., description="TDC edge trigger ('rising' or 'falling')")
    collapse_y: bool = Field(..., description="Whether Y dimension is collapsed")
    zmq_port: int = Field(..., description="ZMQ publishing port")
    tcp_port: int = Field(..., description="TCP socket port for live-cli connection")


class TimePixStop(BaseModel):
    """
    Stop message sent at the end of a data acquisition run.

    This message is published when acquisition stops (shutdown, disconnect, etc.)
    and contains summary statistics about the run.

    Example JSON:
    {
        "msg_type": "stop",
        "scan_name": "acquisition_20250115T143022Z",
        "total_flushes": 120,
        "total_cycles": 120000,
        "total_packets": 5000000,
        "acquisition_duration_s": 120.5,
        "pixels_discarded_before_trigger": 1000,
        "pixels_discarded_outside_window": 500
    }
    """

    msg_type: Literal["stop"] = "stop"
    scan_name: str = Field(..., description="Unique identifier for this acquisition run")
    total_flushes: int = Field(..., description="Total number of flushes published")
    total_cycles: int = Field(..., description="Total number of TDC cycles processed")
    total_packets: int = Field(..., description="Total number of packets received")
    acquisition_duration_s: float = Field(..., description="Total acquisition duration in seconds")
    pixels_discarded_before_trigger: int = Field(
        default=0, description="Total pixels discarded before first TDC trigger"
    )
    pixels_discarded_outside_window: int = Field(default=0, description="Total pixels discarded outside time window")


class TimePixEvent(BaseModel):
    """
    Event message sent for each data flush during acquisition.

    This message is published whenever a 3D array is flushed and contains
    both the array data and metadata about the flush.

    Note: The array data is stored as a numpy array, but when serialized via ZMQ,
    it's sent as raw bytes in a separate message part.
    """

    msg_type: Literal["event"] = "event"
    # Array data (numpy array) - not serialized in JSON, sent separately as bytes
    array: Optional[np.ndarray] = Field(None, description="3D array data (x, y, t or x, t)")

    # Metadata fields (from flush_metadata + static_metadata)
    timestamp: float = Field(..., description="Unix timestamp when flush occurred")
    shape: tuple = Field(..., description="Array shape tuple")
    dtype: str = Field(..., description="Numpy dtype as string")

    # Static configuration (same for all events in a run)
    tdc_frequency_hz: float = Field(..., description="TDC trigger frequency in Hz")
    t_delta_ns: float = Field(..., description="Time bin width in nanoseconds")
    t_cycle_ns: float = Field(..., description="Full time cycle in nanoseconds")
    n_bins: int = Field(..., description="Number of time bins")
    detector_size_x: int = Field(..., description="Detector X dimension")
    detector_size_y: int = Field(..., description="Detector Y dimension")
    flush_interval_s: float = Field(..., description="Flush interval in seconds")
    cycles_per_flush: int = Field(..., description="Expected cycles per flush")
    tdc_channel: int = Field(..., description="TDC channel (0=both, 1=ch1, 2=ch2)")
    tdc_edge: str = Field(..., description="TDC edge trigger ('rising' or 'falling')")
    collapse_y: bool = Field(..., description="Whether Y dimension is collapsed")

    # Per-flush fields
    flush_number: int = Field(..., description="Sequential flush number (1-indexed)")
    cycles_in_flush: int = Field(..., description="Actual cycles in this flush")
    total_cycles: int = Field(..., description="Cumulative cycle count")
    pixels_discarded_before_trigger: int = Field(
        default=0, description="Pixels discarded before first TDC in this flush"
    )
    pixels_discarded_outside_window: int = Field(
        default=0, description="Pixels discarded outside time window in this flush"
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)  # Allow numpy arrays
