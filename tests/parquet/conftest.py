"""Fixtures shared by the parquet tests: real ``BatchParseResult`` builders and a scan-metadata dict."""

from __future__ import annotations

from typing import Dict, List

import numpy as np
import pytest

from splash_timepix.parser import BatchParseResult, PacketParser
from splash_timepix.simulator import PacketBuilder


def make_batch(
    n_pixels: int,
    t0: int,
    *,
    dt: int = 10,
    tdc_every: int = 0,
    control_every: int = 0,
    shuffle: bool = False,
    seed: int = 0,
) -> BatchParseResult:
    """Parse a synthetic packet stream the way ``SocketDataServer`` would.

    Pixels are time-ordered at ``t0 + i*dt`` with ``x = i % 256``, ``y = (i // 256) % 256``,
    ``tot = 100 + i % 50``. Every ``tdc_every``-th pixel is followed by a TDC rising edge on
    channel 1, every ``control_every``-th by a heartbeat control packet. ``shuffle`` randomises
    the packet order (non-monotonic ``t``).
    """
    packets: List[bytes] = []
    for i in range(n_pixels):
        t = t0 + i * dt
        packets.append(PacketBuilder.build_pixel_packet(t, 100 + (i % 50), i % 256, (i // 256) % 256))
        if tdc_every and i % tdc_every == 0:
            packets.append(PacketBuilder.build_tdc_packet(t + 1, 1, 0))
        if control_every and i % control_every == 0:
            packets.append(PacketBuilder.build_control_packet(t + 2, 2))
    if shuffle:
        rng = np.random.default_rng(seed)
        rng.shuffle(packets)
    return PacketParser().parse_batch(b"".join(packets))


def make_random_batch(n: int, *, t0: int, seed: int, tdc: bool = True) -> BatchParseResult:
    """Incompressible-ish pixel batch (random x/y/tot, jittered t) so byte-based rolling triggers at small sizes.

    ``make_batch`` compresses to well under a byte per row, which never rolls a 20 kB file.
    """
    rng = np.random.default_rng(seed)
    xs = rng.integers(0, 1024, n)
    ys = rng.integers(0, 1024, n)
    tots = rng.integers(0, 1024, n)
    dts = np.cumsum(rng.integers(1, 5000, n))
    packets = [
        PacketBuilder.build_pixel_packet(int(t0 + dts[i]), int(tots[i]), int(xs[i]), int(ys[i])) for i in range(n)
    ]
    if tdc:
        packets.append(PacketBuilder.build_tdc_packet(int(t0 + dts[-1] + 1), 1, 0))
    return PacketParser().parse_batch(b"".join(packets))


def expected_events(n_pixels: int, t0: int, dt: int = 10) -> Dict[str, np.ndarray]:
    i = np.arange(n_pixels)
    return {
        "x": (i % 256).astype(np.uint16),
        "y": ((i // 256) % 256).astype(np.uint16),
        "tot": (100 + i % 50).astype(np.uint16),
        "t": (t0 + i * dt).astype(np.uint64),
    }


@pytest.fixture
def scan_meta() -> dict:
    return dict(
        mode="timing",
        tdc_frequency_hz=1000.0,
        tdc_channel=1,
        tdc_edge="rising",
        t_delta_ns=100.0,
        t_cycle_ns=1e6,
        n_bins=10_000,
        collapse_y=True,
        detector_size_x=256,
        detector_size_y=256,
        source={"kind": "simulator"},
    )
