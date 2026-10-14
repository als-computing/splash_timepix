"""Bursty-wire regression test for flush pacing (server commit 754c857).

Asserts the post-fix invariants under a 2 s-bolus wire pattern that
reproduces the production Serval + luna-iterator regime:

- CV < 0.5  — flush pacing must be near-uniform
- fraction_sub_50ms < 0.1  — no microsecond-clustered emits
- flush_numbers contiguous 1..N, total_cycles monotonic
- sum(event.cycles_in_flush) == stop.total_cycles  (conservation)

For exploratory grid diagnostics (5×5 cps/tdc sweep) see:
    tools/diagnostics/flush_burstiness.py

Running
-------

::

    pytest -v -s tests/test_flush_burstiness.py -m slow
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import time
from dataclasses import dataclass, field
from typing import List, Tuple

import msgpack
import pytest
import zmq

# Wallclock budget for the simulator's wire stream to drain on the receiver.
# Must exceed the nominal DAQ window by enough headroom to cover TCP back-
# pressure at the highest-load combo (cps=100k, tdc=10k Hz, tcp_batch=2 s):
# the server's parse + per-flush ZMQ publish saturates the recv loop, which
# stalls the simulator's sendall and stretches a 15 s synthetic stream to
# 75 s+ of wallclock on slower hosts (observed locally; ~40 s on the GitHub
# Actions ubuntu-latest runner). This budget is only a *ceiling* — the
# collect loop breaks the instant the stop message arrives — so generous
# headroom is essentially free on passing runs and only bounds a genuine
# hang. 150 s ≈ 10x DAQ keeps comfortable margin across host speeds.
SIM_WAIT_BUDGET_S: float = 150.0

# After the sim closes its TCP socket the server still needs to flush its
# ingest queue, take the final cycle-count snapshot, and publish the stop
# message on the ZMQ PUB socket. Added on top of SIM_WAIT_BUDGET_S as the
# post-drain grace window for that final stop to land.
STOP_WAIT_BUDGET_S: float = 20.0


# =============================================================================
# Per-combo result container
# =============================================================================


@dataclass
class ComboResult:
    cps: float
    tdc: float
    flush_interval_s: float
    expected_flushes: float

    n_events: int = 0
    n_starts: int = 0
    n_stops: int = 0
    event_arrival_monotonic: List[float] = field(default_factory=list)
    cycles_in_flush_values: List[int] = field(default_factory=list)
    total_cycles_last: int = 0
    # Populated from the stop control message (TimePixStop.total_cycles).
    # Used by the bursty-wire regression test to verify
    # sum(event.cycles_in_flush) == stop.total_cycles.
    stop_total_cycles: int = 0
    flush_numbers: List[int] = field(default_factory=list)
    total_cycles_history: List[int] = field(default_factory=list)

    notes: str = ""

    @property
    def deltas_s(self) -> List[float]:
        if len(self.event_arrival_monotonic) < 2:
            return []
        ts = self.event_arrival_monotonic
        return [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]

    @property
    def stream_duration_s(self) -> float:
        if len(self.event_arrival_monotonic) < 2:
            return 0.0
        return self.event_arrival_monotonic[-1] - self.event_arrival_monotonic[0]

    def summary_row(self) -> str:
        d = self.deltas_s
        if d:
            mean = statistics.fmean(d)
            median = statistics.median(d)
            std = statistics.pstdev(d) if len(d) > 1 else 0.0
            dmin = min(d)
            dmax = max(d)
            cv = std / mean if mean > 0 else 0.0
            frac_fast = sum(1 for x in d if x < 0.050) / len(d)
        else:
            mean = median = std = dmin = dmax = cv = frac_fast = 0.0

        return (
            f"cps={self.cps:>8g}  tdc={self.tdc:>7g} Hz  | "
            f"flushes={self.n_events:>4d} (exp~{self.expected_flushes:>4.1f})  "
            f"stream={self.stream_duration_s:>5.2f}s  | "
            f"Δt mean={mean*1000:>7.1f}ms  med={median*1000:>7.1f}ms  "
            f"std={std*1000:>7.1f}ms  min={dmin*1000:>7.1f}ms  max={dmax*1000:>7.1f}ms  | "
            f"CV={cv:>5.2f}  <50ms={frac_fast*100:>5.1f}%  "
            f"{self.notes}"
        )


# =============================================================================
# Collector: record monotonic arrival time for every SUB frame
# =============================================================================


def _teardown_rig_eagerly(rig) -> None:
    """Wait for a rig's subprocesses to exit, close its ZMQ sockets.

    The ``streaming_rig`` fixture only tears down after the test function
    returns; since we reuse the fixture many times in a single test we have
    to unwind each combo ourselves, otherwise we keep N server subprocesses
    alive simultaneously.

    We run with ``exit_on_disconnect=True`` so the server is expected to exit
    by itself when the simulator closes its TCP socket at the end of its
    duration.  This module is also executed under a sandbox that denies
    ``kill()`` on subprocesses, so we deliberately do NOT send SIGTERM/SIGKILL
    — we just wait.  If a process refuses to exit we leak it and move on (the
    fixture's final teardown will try to terminate it, which will also fail
    under the sandbox, but it is best-effort on its side too).
    """
    for sim_proc in rig._sim_procs:
        try:
            sim_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass  # accept the leak
    try:
        rig.server_proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        pass  # accept the leak

    # Close sockets with LINGER=0 so closure is immediate even if buffered
    # frames are pending (the server is done at this point, so any in-flight
    # frames are irrelevant to the next combo).
    for s in (rig.sub_sock, rig.hb_sock):
        try:
            s.setsockopt(zmq.LINGER, 0)
            s.close()
        except Exception:
            pass
    # Do NOT term() the context — the fixture's final teardown does that
    # once, and multiple term() calls would deadlock.


def _collect_with_timestamps(
    sock: zmq.Socket,
    *,
    stop_deadline_monotonic: float,
) -> ComboResult:
    """Drain ``sock`` until a stop arrives or the deadline elapses.

    Records ``time.monotonic()`` at every ``recv()`` of an event-message
    metadata frame.  Two-part event messages have their array-bytes frame
    consumed but not parsed.
    """
    result = ComboResult(
        cps=0.0,  # filled in by caller
        tdc=0.0,
        flush_interval_s=0.0,
        expected_flushes=0.0,
    )

    sock.setsockopt(zmq.RCVTIMEO, 500)
    while time.monotonic() < stop_deadline_monotonic:
        try:
            meta_bytes = sock.recv()
            arrived = time.monotonic()
        except zmq.Again:
            continue

        try:
            meta = msgpack.unpackb(meta_bytes)
        except Exception:
            continue

        msg_type = meta.get("msg_type")
        if msg_type == "start":
            result.n_starts += 1
        elif msg_type == "stop":
            result.n_stops += 1
            stc = meta.get("total_cycles")
            if isinstance(stc, int):
                result.stop_total_cycles = stc
            break
        else:
            # Event: drain the array-bytes second frame (we don't need it).
            try:
                sock.recv()
            except zmq.Again:
                pass
            result.n_events += 1
            result.event_arrival_monotonic.append(arrived)
            cif = meta.get("cycles_in_flush")
            if isinstance(cif, int):
                result.cycles_in_flush_values.append(cif)
            tc = meta.get("total_cycles")
            if isinstance(tc, int):
                result.total_cycles_last = tc
                result.total_cycles_history.append(tc)
            fn = meta.get("flush_number")
            if isinstance(fn, int):
                result.flush_numbers.append(fn)

    return result


# =============================================================================
# Bursty-wire regression test (post-fix: asserts the green-path baseline)
# =============================================================================
#
# Reproduces the production Serval+luna-iterator wire pattern in CI by
# driving the simulator with ``tcp_batch_interval_s > 0``: bytes are
# accumulated in-memory for that many seconds, then emitted in one
# ``sendall`` per bolus.  The server then chews through the whole bolus
# back-to-back on its TCP receive thread.
#
# Under the *original* cycle-count flush gate this produced multiple
# flushes within a few milliseconds of each other (one per cycle-count
# crossing inside a single ``data_callback`` invocation), then multi-
# second silence until the next bolus — i.e. bursty ZMQ output instead of
# the one-per-flush-interval cadence the UI needs.
#
# Server commit ``754c857`` switched the flush gate to wall-clock
# (``emit_flush_if_due`` checks ``time.monotonic() - last_flush_time >=
# flush_interval``), which decouples flush cadence from TDC arrival
# cadence.  This test now asserts the post-fix invariants — CV < 0.5,
# no microsecond-clustered emits, monotonic 1..N flush numbers, and
# conservation of cycles — and so a failure here means a real
# regression in the gate semantics.
# =============================================================================

# Parameters chosen to reproduce the production "bolus" regime on the
# wire.  The simulator emits 2 s-wide boluses; with flush_interval=1.0,
# each bolus carries ~2 flush-intervals worth of data.  Under the
# wall-clock gate we expect the server to space those ~2 flushes evenly
# at ~1 s intervals regardless of when the bolus arrives.  Intentionally
# deviates from the smooth-wire grid above (tcp_batch_interval_s=0) to
# isolate any wire-level burst leaking through to the ZMQ side.
_BATCHED_TCP_INTERVAL_S: float = float(os.environ.get("BATCHED_TCP_INTERVAL_S", 2.0))
_BATCHED_FLUSH_INTERVAL_S: float = float(os.environ.get("BATCHED_FLUSH", 1.0))
_BATCHED_DAQ_SECONDS: int = int(os.environ.get("BATCHED_DAQ", 15))

# Three combos covering low/mid/high rate.  At the high-rate combo
# (cps=100k, tdc=10k Hz) TCP backpressure stretches the wire stream to
# several times the nominal DAQ window — the test's count tolerance
# scales off observed stream_duration so this is expected and benign.
_BATCHED_COMBOS: Tuple[Tuple[int, int], ...] = (
    # (cps, tdc_frequency_hz)
    (1_000, 100),
    (10_000, 1_000),
    (100_000, 10_000),
)


def _compute_metrics(combo: ComboResult) -> dict:
    """Derive burstiness / conservation metrics from a ComboResult.

    Keeps the artifact schema in one place so before/after JSON diffs
    against /tmp/burstiness_*.json are trivial to compute.
    """
    deltas = combo.deltas_s
    if deltas:
        cv = (statistics.pstdev(deltas) / statistics.fmean(deltas)) if len(deltas) > 1 else 0.0
        frac_sub_50ms = sum(1 for d in deltas if d < 0.050) / len(deltas)
        max_gap = max(deltas)
    else:
        cv = 0.0
        frac_sub_50ms = 0.0
        max_gap = 0.0

    return {
        "cps": combo.cps,
        "tdc": combo.tdc,
        "flush_interval_s": combo.flush_interval_s,
        "tcp_batch_interval_s": _BATCHED_TCP_INTERVAL_S,
        "n_events": combo.n_events,
        "n_starts": combo.n_starts,
        "n_stops": combo.n_stops,
        "expected_flushes": combo.expected_flushes,
        "cv": cv,
        "fraction_sub_50ms": frac_sub_50ms,
        "max_gap_s": max_gap,
        "stream_duration_s": combo.stream_duration_s,
        "total_cycles_reported": combo.total_cycles_last,
        "stop_total_cycles": combo.stop_total_cycles,
        "sum_cycles_in_flush": sum(combo.cycles_in_flush_values),
        "flush_numbers": list(combo.flush_numbers),
        "total_cycles_history": list(combo.total_cycles_history),
        "notes": combo.notes,
    }


def _write_artifact(metrics: List[dict], path: str) -> None:
    """Dump per-combo metrics to a JSON file for before/after diffing."""
    with open(path, "w") as f:
        json.dump(
            {
                "tcp_batch_interval_s": _BATCHED_TCP_INTERVAL_S,
                "flush_interval_s": _BATCHED_FLUSH_INTERVAL_S,
                "daq_seconds": _BATCHED_DAQ_SECONDS,
                "combos": metrics,
            },
            f,
            indent=2,
            sort_keys=True,
        )


@pytest.mark.slow
@pytest.mark.integration
def test_batched_wire_regression(streaming_rig):
    """Pass/fail regression test that exercises the bursty-wire regime.

    Asserts the post-fix (server commit 754c857, wall-clock flush gate)
    invariants under a 2 s-bolus wire pattern:

        - CV < 0.5  — flush pacing must be near-uniform; a regression to
          the cycle-count gate would push CV to 0.84-0.96 against this
          fixture.
        - fraction_sub_50ms < 0.1  — no microsecond-clustered emits;
          the cycle-count gate produced 7-43% sub-50ms gaps here.
        - n_flushes within 0.7-1.3 of (stream_duration / flush_interval)
          — expected scales with observed wire-drain duration so heavy-
          load combos under TCP backpressure are not flagged for the
          backpressure itself, only for genuine pacing bugs.
        - flush_numbers contiguous 1..N, total_cycles monotonic, and
          sum(event.cycles_in_flush) == stop.total_cycles — conservation
          invariants the UI's running averages depend on.

    The test writes a JSON artifact to ``/tmp/burstiness_latest.json``
    on every run.  Historical workflow: rename to
    ``/tmp/burstiness_before.json`` / ``burstiness_after.json`` to diff
    pacing metrics across server changes that touch the flush path.
    """
    results: List[ComboResult] = []

    print("\n\n")
    print("=" * 100)
    print(
        f"BURSTY-WIRE REGRESSION — {len(_BATCHED_COMBOS)} combos, "
        f"{_BATCHED_DAQ_SECONDS}s each, "
        f"flush_interval={_BATCHED_FLUSH_INTERVAL_S}s, "
        f"tcp_batch_interval_s={_BATCHED_TCP_INTERVAL_S}s"
    )
    print("=" * 100)

    for combo_idx, (cps, tdc) in enumerate(_BATCHED_COMBOS, start=1):
        print(
            f"\n[{combo_idx}/{len(_BATCHED_COMBOS)}] "
            f"cps={cps}  tdc={tdc} Hz  batch={_BATCHED_TCP_INTERVAL_S}s  ...",
            flush=True,
        )

        rig = streaming_rig(
            tdc_frequency=float(tdc),
            cps=float(cps),
            flush_interval=_BATCHED_FLUSH_INTERVAL_S,
            exit_on_disconnect=True,
            collapse_y=True,
        )

        sim_proc: subprocess.Popen = rig.spawn_simulator_cli(
            duration=_BATCHED_DAQ_SECONDS,
            cps=cps,
            tdc_frequency=tdc,
            counting=False,
            tcp_batch_interval_s=_BATCHED_TCP_INTERVAL_S,
        )

        deadline = time.monotonic() + SIM_WAIT_BUDGET_S + STOP_WAIT_BUDGET_S
        combo = _collect_with_timestamps(
            rig.sub_sock,
            stop_deadline_monotonic=deadline,
        )
        combo.cps = cps
        combo.tdc = tdc
        combo.flush_interval_s = _BATCHED_FLUSH_INTERVAL_S

        # Wall-clock flush gate (server commit 754c857): the streaming server
        # publishes one flush per flush_interval of wallclock during the
        # stream, so expected count tracks the *observed* stream duration —
        # not the nominal DAQ window. For low-cps combos stream ≈ DAQ; for
        # the high-cps combo, TCP backpressure stretches stream to several
        # times the DAQ duration, and expected scales accordingly.
        combo.expected_flushes = max(0.0, combo.stream_duration_s / _BATCHED_FLUSH_INTERVAL_S)

        try:
            sim_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass

        if combo.n_stops == 0:
            combo.notes = "(NO stop received)"
        elif combo.n_events == 0:
            combo.notes = "(NO events)"

        results.append(combo)
        print("   " + combo.summary_row())
        _teardown_rig_eagerly(rig)

    # -------------------------------------------------------------------------
    # Write artifact before asserting so the JSON is available even when
    # the test fails (which is the Phase-1 expectation).
    # -------------------------------------------------------------------------
    all_metrics = [_compute_metrics(c) for c in results]
    artifact_path = os.environ.get("BURSTINESS_ARTIFACT", "/tmp/burstiness_latest.json")
    _write_artifact(all_metrics, artifact_path)
    print(f"\nArtifact written to {artifact_path}")

    # -------------------------------------------------------------------------
    # Assertions.  Collect all failures first so the output shows every
    # combo's verdict in one shot rather than aborting at the first
    # broken combo — this gives the operator a complete picture of the
    # current state.
    # -------------------------------------------------------------------------
    failures: List[str] = []

    for combo, m in zip(results, all_metrics):
        label = f"cps={combo.cps:g}/tdc={combo.tdc:g}"

        # --- Stop sanity ---------------------------------------------------
        if combo.n_stops != 1:
            failures.append(f"{label}: expected exactly 1 stop, got {combo.n_stops}")
            continue  # the rest depend on a well-formed acquisition

        if combo.n_events == 0:
            failures.append(f"{label}: no event messages produced")
            continue

        # --- Count tolerance ----------------------------------------------
        low = 0.7 * combo.expected_flushes
        high = 1.3 * combo.expected_flushes
        if not (low <= combo.n_events <= high):
            failures.append(
                f"{label}: n_flushes={combo.n_events} outside "
                f"[{low:.1f}, {high:.1f}] (expected~{combo.expected_flushes:.1f})"
            )

        # --- CV (burstiness) ----------------------------------------------
        if m["cv"] >= 0.5:
            failures.append(f"{label}: CV={m['cv']:.3f} exceeds 0.5 threshold (bursty pacing)")

        # --- Sub-50ms fraction --------------------------------------------
        if m["fraction_sub_50ms"] >= 0.1:
            failures.append(
                f"{label}: fraction_sub_50ms={m['fraction_sub_50ms']:.3f} >= 0.1 (microsecond-clustered emits)"
            )

        # --- Max gap ------------------------------------------------------
        if m["max_gap_s"] >= 2.0 * _BATCHED_FLUSH_INTERVAL_S:
            failures.append(
                f"{label}: max_gap={m['max_gap_s']:.3f}s >= 2 * flush_interval ({2*_BATCHED_FLUSH_INTERVAL_S:.3f}s)"
            )

        # --- Flush number sequence ----------------------------------------
        expected_seq = list(range(1, len(combo.flush_numbers) + 1))
        if combo.flush_numbers != expected_seq:
            failures.append(
                f"{label}: flush_numbers not 1..N: {combo.flush_numbers[:8]}... (expected {expected_seq[:8]}...)"
            )

        # --- Monotonic total_cycles ---------------------------------------
        tch = combo.total_cycles_history
        non_monotonic = [(i, tch[i], tch[i + 1]) for i in range(len(tch) - 1) if tch[i + 1] < tch[i]]
        if non_monotonic:
            failures.append(f"{label}: total_cycles not monotonic at indices {non_monotonic[:3]}")

        # --- Conservation of cycles (the killer invariant) ----------------
        # Catches: off-by-one in cycles_in_flush, double-emit from TOCTOU,
        # lost-emit from any source.  Running-average correctness in the
        # UI depends on this exactly.
        sum_cif = sum(combo.cycles_in_flush_values)
        if sum_cif != combo.stop_total_cycles:
            failures.append(
                f"{label}: sum(cycles_in_flush)={sum_cif} != "
                f"stop.total_cycles={combo.stop_total_cycles} "
                f"(conservation violated)"
            )

    if failures:
        pytest.fail(
            "\n".join(
                [f"Bursty-wire regression: {len(failures)} assertion(s) failed:"]
                + [f"  - {f}" for f in failures]
                + [f"Artifact: {artifact_path}"]
            )
        )
