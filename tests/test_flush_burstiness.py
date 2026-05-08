"""Burstiness / flush-pacing experiment.

Runs the streaming server under the `streaming_rig` fixture with a grid of
``(cps, tdc_frequency)`` values and measures the wall-clock distribution of
ZMQ ``event`` messages as they arrive at a subscriber.

Why this exists
---------------

In production (behind luna-iterator) the server emits flushes in bursts: the
sorter upstream batches packets in multi-second chunks, so the server sees
many TDCs back-to-back and the current TDC-count-based flush gate fires
repeatedly inside a single ``data_callback`` invocation.  See ``solution.md``.

In this test the simulator sends packets one-at-a-time over TCP (so no
upstream batching), but the ``SocketDataServer`` still batches callbacks
(``callback_batch_size=10`` by default) and the server's flush gate is still
cycle-count-based.  When the TDC rate is much higher than the configured
flush rate, we still expect to see short inter-flush deltas inside a batch.

Design
------

- 5x5 grid of (cps, tdc) — 25 combos, logarithmic in both axes.
- DAQ = 15 s per combo, ``flush_interval = 1 s`` (matches the symptom case
  in ``solution.md``).
- Fresh server subprocess per combo via the ``streaming_rig`` fixture.
- Simulator spawned via ``simulator_cli --auto-start --duration 15``.
- Per combo we record ``time.monotonic()`` at every ``recv()`` of an event
  message, then derive: count, expected count, mean/median/std/min/max of
  inter-flush deltas, the coefficient of variation (std/mean, our primary
  burstiness metric), the fraction of deltas under 50 ms (microsecond-cluster
  concern), and the longest silence between flushes.

Running
-------

::

    pytest -v -s tests/test_flush_burstiness.py
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

# =============================================================================
# Grid (overridable via env for quick smoke runs)
#   BURSTINESS_CPS="100,10000" BURSTINESS_TDC="10,1000" BURSTINESS_DAQ=5 ...
# =============================================================================


def _env_int_tuple(name: str, default: Tuple[int, ...]) -> Tuple[int, ...]:
    raw = os.environ.get(name)
    if not raw:
        return default
    return tuple(int(x.strip()) for x in raw.split(",") if x.strip())


CPS_VALUES: Tuple[int, ...] = _env_int_tuple("BURSTINESS_CPS", (10, 100, 1_000, 10_000, 100_000))
TDC_VALUES: Tuple[int, ...] = _env_int_tuple("BURSTINESS_TDC", (1, 10, 100, 1_000, 10_000))

DAQ_SECONDS: int = int(os.environ.get("BURSTINESS_DAQ", 15))
FLUSH_INTERVAL_S: float = float(os.environ.get("BURSTINESS_FLUSH", 1.0))

# Give the simulator subprocess generous grace over the DAQ window so we do
# not truncate the tail on slow machines.  15 s DAQ + ~5 s buffer.
SIM_WAIT_BUDGET_S: float = 25.0

# After sim exits we still need to see the stop message on the SUB socket.
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
# Experiment
# =============================================================================


@pytest.mark.slow
@pytest.mark.integration
def test_flush_burstiness_grid(streaming_rig):
    """Sweep (cps, tdc) and print a burstiness summary.

    This is an experiment, not a pass/fail test: it asserts only that the
    rig came up and that *something* was received (to catch catastrophic
    misconfiguration).  The interesting output is the printed table at the
    end.
    """
    results: List[ComboResult] = []

    total_combos = len(CPS_VALUES) * len(TDC_VALUES)
    print("\n\n")
    print("=" * 100)
    print(
        f"FLUSH BURSTINESS EXPERIMENT — {total_combos} combos, {DAQ_SECONDS}s each, "
        f"flush_interval={FLUSH_INTERVAL_S}s"
    )
    print("=" * 100)

    combo_idx = 0
    for tdc in TDC_VALUES:
        for cps in CPS_VALUES:
            combo_idx += 1
            print(f"\n[{combo_idx}/{total_combos}] cps={cps}  tdc={tdc} Hz  ...", flush=True)

            rig = streaming_rig(
                tdc_frequency=float(tdc),
                cps=float(cps),
                flush_interval=FLUSH_INTERVAL_S,
                # --exit-on-disconnect: the server self-exits cleanly when
                # the simulator closes its TCP socket at the end of the DAQ.
                # This matters because we run under a sandbox that denies
                # kill() on subprocesses, so relying on the server's own
                # shutdown is the only way to release its port.
                exit_on_disconnect=True,
                # Detector shape defaults; we don't stress memory here.
                collapse_y=True,
            )

            # Fire the simulator.  It opens the TCP socket on
            # start_auto_sending(), which is what triggers the server's
            # "client connected" branch and the start ZMQ message.
            sim_proc: subprocess.Popen = rig.spawn_simulator_cli(
                duration=DAQ_SECONDS,
                cps=cps,
                tdc_frequency=tdc,
                counting=False,  # avoid per-packet parse overhead
            )

            # Compute expected flush count:
            #   flush_every_n_cycles = max(1, int(flush_interval * tdc))
            #   one flush fires every Nth TDC, DAQ=15s → tdc*15 TDCs total
            f_every = max(1, int(FLUSH_INTERVAL_S * tdc))
            expected = max(0.0, (tdc * DAQ_SECONDS) / f_every - 1)  # first flush at cycle N (not 0)

            # Collect until stop or budget exhausted.
            deadline = time.monotonic() + SIM_WAIT_BUDGET_S + STOP_WAIT_BUDGET_S
            combo = _collect_with_timestamps(
                rig.sub_sock,
                stop_deadline_monotonic=deadline,
            )
            combo.cps = cps
            combo.tdc = tdc
            combo.flush_interval_s = FLUSH_INTERVAL_S
            combo.expected_flushes = expected

            # Reap the sim (we ran it with --duration so it exits by itself;
            # under the sandbox we cannot kill() it, so we just wait).
            try:
                sim_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass  # will be handled by eager teardown below

            # Lightweight annotations
            if combo.n_stops == 0:
                combo.notes = "(NO stop received)"
            elif combo.n_events == 0:
                combo.notes = "(NO events)"

            # Annotate the known low-TDC race.
            if tdc < 50:
                if combo.notes:
                    combo.notes += " [low-TDC race zone]"
                else:
                    combo.notes = "[low-TDC race zone]"

            results.append(combo)
            print("   " + combo.summary_row())

            # Tear this combo's rig down NOW rather than leaking 25 server
            # subprocesses and 50 ZMQ sockets to the fixture's final teardown.
            _teardown_rig_eagerly(rig)

    # =========================================================================
    # Final summary
    # =========================================================================

    print("\n\n")
    print("=" * 110)
    print("SUMMARY — sorted by burstiness (CV of inter-flush Δt, descending)")
    print("=" * 110)
    print("CV = std(Δt) / mean(Δt).  CV ≈ 0 is perfectly regular; CV ≥ 1 means bursts dominate.")
    print("'<50ms' = fraction of inter-flush gaps below 50 ms, i.e. flushes arriving almost on top of each other.")
    print("-" * 110)

    def _cv(r: ComboResult) -> float:
        d = r.deltas_s
        if not d:
            return -1.0
        m = statistics.fmean(d)
        if m == 0:
            return -1.0
        s = statistics.pstdev(d) if len(d) > 1 else 0.0
        return s / m

    for r in sorted(results, key=_cv, reverse=True):
        print(r.summary_row())

    print("\n")
    print("=" * 110)
    print("GRID VIEW — coefficient of variation of inter-flush Δt")
    print("=" * 110)
    header = "               " + "".join(f"cps={c:>7g}  " for c in CPS_VALUES)
    print(header)
    for tdc in TDC_VALUES:
        row = [f"tdc={tdc:>6g} Hz | "]
        for cps in CPS_VALUES:
            r = next((x for x in results if x.tdc == tdc and x.cps == cps), None)
            if r is None:
                row.append("    -     ")
                continue
            cv = _cv(r)
            if cv < 0:
                row.append("   n/a    ")
            else:
                row.append(f"  {cv:>5.2f}    ")
        print("".join(row))

    print("\n")
    print("=" * 110)
    print("GRID VIEW — flushes received vs expected  (recv / ~expected)")
    print("=" * 110)
    print(header)
    for tdc in TDC_VALUES:
        row = [f"tdc={tdc:>6g} Hz | "]
        for cps in CPS_VALUES:
            r = next((x for x in results if x.tdc == tdc and x.cps == cps), None)
            if r is None:
                row.append("    -     ")
                continue
            row.append(f"  {r.n_events:>3d}/{r.expected_flushes:>4.0f}  ")
        print("".join(row))

    # Sanity gate: we want at least one combo to have produced flushes,
    # otherwise the whole rig is broken and the numbers above are noise.
    produced_any = any(r.n_events > 0 for r in results)
    assert produced_any, "no combo produced any ZMQ event messages — rig is broken"


# =============================================================================
# Bursty-wire regression test (expected to FAIL against the unfixed server)
# =============================================================================
#
# Reproduces the production Serval+luna-iterator symptom in CI by driving
# the simulator with ``tcp_batch_interval_s > 0``: bytes are accumulated
# in-memory for that many seconds, then emitted in one ``sendall`` per
# bolus.  The server then chews through the whole bolus back-to-back on
# its TCP receive thread, and its current cycle-count flush gate fires
# all the flushes for the bolus within a few milliseconds of each other.
# Wall-clock silence between boluses produces multi-second gaps.  The
# result: bursty flushes instead of the one-per-flush-interval cadence
# the UI needs.
#
# Once the wall-clock flush gate from solution.md lands in app.py, this
# test is expected to PASS with the same parameters.  That red-then-
# green transition is the acceptance criterion for the fix.
# =============================================================================

# Parameters chosen to reproduce the production "bolus" regime.  The
# simulator emits 2s-wide boluses; with flush_interval=1.0, each bolus
# carries ~2 flush-intervals of data, so each bolus triggers ~2 flushes
# back-to-back on the server's TCP thread before the next bolus arrives
# ~2s later.  That mirrors the luna-iterator pattern described in
# solution.md.  Intentionally deviates from the smooth-wire grid above
# (tcp_batch_interval_s=0) to isolate the wire-level burst.
_BATCHED_TCP_INTERVAL_S: float = float(os.environ.get("BATCHED_TCP_INTERVAL_S", 2.0))
_BATCHED_FLUSH_INTERVAL_S: float = float(os.environ.get("BATCHED_FLUSH", 1.0))
_BATCHED_DAQ_SECONDS: int = int(os.environ.get("BATCHED_DAQ", 15))

# Three combos covering low/mid/high rate.  At all three, the bolus
# interval is >= flush_interval so 2+ flushes sit inside each callback
# invocation — that is the burstiness trigger.  All use the same
# flush_interval so the expected-flush arithmetic is identical across
# combos.
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

    Against the UNFIXED server (current state):
        - The cycle-count flush gate fires every time cycle_count crosses
          a multiple of flush_every_n_cycles.  Multiple crossings inside
          a single callback invocation → multiple flushes at near-zero
          wall-clock separation, then N seconds of silence until the
          next bolus.  Expect failures on CV, fraction_sub_50ms, and
          n_flushes tolerance; possibly on sum(cycles_in_flush) ==
          stop.total_cycles if the off-by-one bites.

    Against the FIXED server (post-Phase 2):
        - The wall-clock gate decouples flush timing from TDC arrival.
          Expect CV < 0.5, sub-50ms ~ 0, counts within tolerance, and
          conservation of cycles holding exactly.

    The test writes a JSON artifact to ``/tmp/burstiness_latest.json``
    on every run; the outer workflow renames this to
    ``/tmp/burstiness_before.json`` after the red run and
    ``/tmp/burstiness_after.json`` after the green run so the operator
    can quantify the improvement.
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

        f_every = max(1, int(_BATCHED_FLUSH_INTERVAL_S * tdc))
        expected = max(0.0, (tdc * _BATCHED_DAQ_SECONDS) / f_every - 1)

        deadline = time.monotonic() + SIM_WAIT_BUDGET_S + STOP_WAIT_BUDGET_S
        combo = _collect_with_timestamps(
            rig.sub_sock,
            stop_deadline_monotonic=deadline,
        )
        combo.cps = cps
        combo.tdc = tdc
        combo.flush_interval_s = _BATCHED_FLUSH_INTERVAL_S
        combo.expected_flushes = expected

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
