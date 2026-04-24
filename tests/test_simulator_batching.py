"""Unit test for the simulator's ``tcp_batch_interval_s`` knob.

False-green guard for the bursty-wire regression test
------------------------------------------------------
The server-level regression test in ``test_flush_burstiness.py`` relies on
the simulator actually emitting *multi-packet boluses* on the wire to
reproduce the Serval+luna-iterator symptom.  If the batching code is
subtly broken and every ``sendall`` still carries only one packet, the
server-level test could silently go green for the wrong reason (or red
for a different reason than we expect).  This unit test closes that loop
by asserting the batching knob really does batch, before any server
subprocess ever runs.

Scope
-----
- Runs fully in-process (no sockets, no subprocesses).
- Replaces ``self.socket`` with a fake whose ``sendall`` records
  ``(monotonic_ts, len(data))`` per call.
- Asserts sendall invocation rate, payload size distribution, and
  multi-packet payloads.
"""

from __future__ import annotations

import statistics
import time
from typing import List, Tuple

import pytest

from splash_timepix.simulator_cli import SimulatorSource


class _FakeSocket:
    """Capture sendall calls with their arrival time + size.

    Not a real socket, just a duck-typed stand-in.  ``SimulatorSource``
    calls ``.sendall(bytes)`` in the worker and ``.close()`` on
    disconnect — we only implement those two surfaces.
    """

    def __init__(self) -> None:
        self.sendalls: List[Tuple[float, int]] = []
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.sendalls.append((time.monotonic(), len(data)))

    def close(self) -> None:
        self.closed = True


def _run_worker_with_fake_socket(
    *,
    cps: float,
    tdc_frequency: float,
    tcp_batch_interval_s: float,
    duration: float,
) -> _FakeSocket:
    """Drive ``_auto_send_worker`` directly with a fake socket.

    Avoids ``start_auto_sending`` so no real TCP is touched.  Sets
    ``self.running = True`` first because the worker's loop exits as
    soon as it sees ``running == False``.
    """
    src = SimulatorSource(host="localhost", port=9)  # port unused with fake sock
    src.pixel_count_rate = cps
    src.tdc_frequency = tdc_frequency
    src.counting = False
    src.tcp_batch_interval_s = tcp_batch_interval_s
    fake = _FakeSocket()
    src.socket = fake  # type: ignore[assignment]
    src.running = True
    # _auto_send_worker flips running=False on exit and then calls
    # self.disconnect() which invokes self.socket.close().
    src._auto_send_worker(duration)
    return fake


@pytest.mark.unit
def test_tcp_batch_interval_zero_is_per_packet():
    """Default mode: each generated packet is its own sendall.

    Anchors the default behaviour so enabling batching is demonstrably
    the thing that changes the wire profile.
    """
    fake = _run_worker_with_fake_socket(
        cps=2000.0,
        tdc_frequency=100.0,
        tcp_batch_interval_s=0.0,
        duration=1.0,
    )
    assert fake.sendalls, "worker produced no packets at all"
    # Every packet is 12 bytes; in per-packet mode every sendall carries
    # exactly one packet.
    sizes = [n for _, n in fake.sendalls]
    assert all(s == 12 for s in sizes), (
        f"tcp_batch_interval_s=0 should send 12 bytes per sendall, "
        f"got sizes {sorted(set(sizes))}"
    )
    assert fake.closed, "socket should be closed at end of worker"


@pytest.mark.unit
def test_tcp_batch_interval_produces_multi_packet_boluses():
    """Batched mode: sendall fires ~every interval, each carrying many packets."""
    cps = 10_000.0
    tdc_freq = 1_000.0
    interval = 0.5
    duration = 3.0

    fake = _run_worker_with_fake_socket(
        cps=cps,
        tdc_frequency=tdc_freq,
        tcp_batch_interval_s=interval,
        duration=duration,
    )

    # --- Invocation count -------------------------------------------------
    # duration/interval = 6 boluses; allow 4..10 to cover startup grace
    # and the final tail flush at worker exit.
    n_calls = len(fake.sendalls)
    assert 4 <= n_calls <= 10, (
        f"expected 4..10 sendall calls over {duration}s at interval={interval}s, "
        f"got {n_calls}"
    )

    # --- Payload alignment -----------------------------------------------
    # Every packet is 12 bytes; every bolus is a whole number of packets.
    sizes = [n for _, n in fake.sendalls]
    bad_align = [s for s in sizes if s % 12 != 0]
    assert not bad_align, f"payloads not a multiple of 12 bytes: {bad_align}"

    # --- Boluses must actually be multi-packet ---------------------------
    # Expected packets per bolus = cps*interval + 2*tdc_freq*interval
    # = 5000 (pixel) + 1000 (tdc rise+fall) = ~6000 packets/bolus, i.e.
    # ~72000 bytes.  Be generous (>= 100 packets) to survive CI jitter.
    mean_packets_per_bolus = statistics.fmean(sizes) / 12
    assert mean_packets_per_bolus >= 100, (
        f"mean bolus size is only {mean_packets_per_bolus:.1f} packets — "
        f"batching is not actually accumulating (batcher is broken)"
    )

    # --- Inter-send pacing --------------------------------------------
    # Inter-sendall median should be close to the configured interval
    # (exclude the startup sendall which can arrive early, and the tail
    # flush which can arrive anywhere relative to the last boundary).
    ts = [t for t, _ in fake.sendalls]
    deltas = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
    if len(deltas) >= 3:
        # drop min and max to suppress start/tail artefacts
        trimmed = sorted(deltas)[1:-1]
        median_dt = statistics.median(trimmed)
        assert 0.3 <= median_dt <= 0.8, (
            f"median inter-sendall interval {median_dt:.3f}s is far from "
            f"configured {interval}s (expected within [0.3, 0.8])"
        )

    assert fake.closed, "socket should be closed at end of worker"
