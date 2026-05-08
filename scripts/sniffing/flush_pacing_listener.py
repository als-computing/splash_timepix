#!/usr/bin/env python3
"""Flush-pacing diagnostic listener.

Standalone subscriber for the streaming server's data + heartbeat PUB
sockets.  Designed to run *in parallel* with the operator UI so we can
tell the three failure modes apart when "slow UI updates" happen on a
real detector:

1. **Wire-level burst**: many event messages arrive within milliseconds of
   each other, then a long silence.  Visible here as <50 ms inter-event
   deltas mixed with multi-second gaps.

2. **Server-side back-pressure / drops**: the server's ``xyt_queue`` (in
   front of the ZMQ worker) or its ingest queue (in front of the data
   processor) fills up.  Visible here as climbing ``q_xyt_sz`` /
   ``q_ingest_sz`` numbers from the heartbeat, and missing
   ``flush_number``s in the event sequence.

3. **UI-only stutter**: this listener sees a clean ~``flush_interval``
   cadence and a contiguous ``flush_number`` sequence even though the UI
   looks slow.  The PUB socket drops messages per-subscriber when that
   subscriber's pipe hits SNDHWM, so a slow UI can lag without affecting
   this listener.  If the diagnostic is regular but the UI is bursty,
   the bottleneck is on the UI thread.

Usage::

    # In one terminal (server already running, UI optionally running):
    python scripts/sniffing/flush_pacing_listener.py

    # When the acquisition is over, Ctrl+C to print the final summary
    # and write a JSON artifact to /tmp/flush_pacing_<unix-ts>.json.

Connect to a non-default port::

    python scripts/sniffing/flush_pacing_listener.py --zmq-port 5657 --hb-port 5658

The script is intentionally dependency-light: stdlib + ``pyzmq`` +
``msgpack``, the same set already required by the package.
"""

from __future__ import annotations

import argparse
import json
import signal
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import msgpack
import zmq


# =============================================================================
# Data structures
# =============================================================================


@dataclass
class EventRecord:
    """One ``event`` ZMQ message as observed by this listener."""

    arrived_monotonic: float  # time.monotonic() at recv() of metadata frame
    arrived_wall: float  # time.time() at recv()
    metadata_timestamp: float  # 'timestamp' field set by zmq_worker (server side)
    flush_number: Optional[int]
    cycles_in_flush: Optional[int]
    total_cycles: Optional[int]
    array_bytes: int  # length of the 2nd frame, not parsed as ndarray


@dataclass
class HeartbeatSnapshot:
    """Latest heartbeat fields we care about for back-pressure diagnosis."""

    received_monotonic: float
    state: str = ""
    q_ingest_sz: Optional[int] = None
    q_ingest_max: Optional[int] = None
    q_xyt_sz: Optional[int] = None
    q_xyt_max: Optional[int] = None
    q_ctrl_sz: Optional[int] = None
    q_ctrl_max: Optional[int] = None


@dataclass
class State:
    """Aggregator for everything we observe over the run."""

    events: List[EventRecord] = field(default_factory=list)
    starts_seen: int = 0
    stops_seen: int = 0
    # Full heartbeat time-series (every snapshot, in arrival order).  Lets us
    # plot q_ingest_sz vs t and tell "permanent backlog" apart from
    # "transient bolus drained promptly".
    heartbeats: List[HeartbeatSnapshot] = field(default_factory=list)
    # Last value of each q_* field seen, plus its peak across the run.
    last_hb: Optional[HeartbeatSnapshot] = None
    peak_q_ingest_sz: int = 0
    peak_q_xyt_sz: int = 0
    peak_q_ctrl_sz: int = 0
    # Set when we see start/stop so we can derive flush_interval / tdc_freq.
    # ``start_received_monotonic`` is the listener's clock at recv() of the
    # ``start`` frame; lets us measure start→first-event latency.
    start_metadata: Optional[Dict] = None
    start_received_monotonic: Optional[float] = None
    stop_metadata: Optional[Dict] = None
    stop_received_monotonic: Optional[float] = None
    started_monotonic: float = field(default_factory=time.monotonic)
    # Wall-clock counterpart of ``started_monotonic`` taken at the same
    # instant.  Lets external tools (e.g. tcpdump pcaps using kernel epoch
    # timestamps) re-base their data into the same t=0 the listener used
    # for events_t_rel_s and heartbeat_t_rel_s.
    started_at_epoch_s: float = field(default_factory=time.time)


# =============================================================================
# Live ticker
# =============================================================================


def _format_live_ticker(state: State, last_n: int = 5) -> str:
    """Build a one-line live-status string for stdout."""
    elapsed = time.monotonic() - state.started_monotonic
    n_evt = len(state.events)

    if n_evt >= 2:
        deltas = [
            state.events[i].arrived_monotonic - state.events[i - 1].arrived_monotonic
            for i in range(1, n_evt)
        ]
        last_deltas = deltas[-last_n:]
        last_deltas_str = "[" + ", ".join(f"{d:.2f}" for d in last_deltas) + "]"
        avg_dt = statistics.fmean(deltas)
    else:
        last_deltas_str = "[-]"
        avg_dt = 0.0

    hb = state.last_hb
    if hb is not None:
        hb_age = time.monotonic() - hb.received_monotonic
        hb_str = (
            f"hb={hb.state} (age {hb_age:.1f}s)"
            f"  q_ingest={hb.q_ingest_sz}/{hb.q_ingest_max}"
            f"  q_xyt={hb.q_xyt_sz}/{hb.q_xyt_max}"
        )
    else:
        hb_str = "hb=<none>"

    return (
        f"[t={elapsed:6.1f}s] N={n_evt:4d} ev "
        f"start/stop={state.starts_seen}/{state.stops_seen}  "
        f"avg_dt={avg_dt:.2f}s  last{last_n}_dt={last_deltas_str}  "
        f"{hb_str}"
    )


# =============================================================================
# Receive loop
# =============================================================================


def _consume_data_message(sock: zmq.Socket, state: State, arrived_monotonic: float) -> None:
    """Pull one full ZMQ message off the data socket and update ``state``.

    Single-frame messages (``start``, ``stop``) are decoded fully.
    Two-frame ``event`` messages have their array-bytes frame consumed
    but *not* parsed as ndarray — we only need its length.
    """
    try:
        meta_bytes = sock.recv()
    except zmq.Again:
        return
    arrived_wall = time.time()

    try:
        meta = msgpack.unpackb(meta_bytes)
    except Exception:
        return

    msg_type = meta.get("msg_type")

    if msg_type == "start":
        state.starts_seen += 1
        state.start_metadata = meta
        state.start_received_monotonic = arrived_monotonic
        return

    if msg_type == "stop":
        state.stops_seen += 1
        state.stop_metadata = meta
        state.stop_received_monotonic = arrived_monotonic
        return

    # ``event`` (or anything not start/stop): expect a 2nd frame.
    array_bytes_len = 0
    if sock.getsockopt(zmq.RCVMORE):
        try:
            array_bytes = sock.recv()
            array_bytes_len = len(array_bytes)
        except zmq.Again:
            pass

    state.events.append(
        EventRecord(
            arrived_monotonic=arrived_monotonic,
            arrived_wall=arrived_wall,
            metadata_timestamp=float(meta.get("timestamp", 0.0)),
            flush_number=meta.get("flush_number"),
            cycles_in_flush=meta.get("cycles_in_flush"),
            total_cycles=meta.get("total_cycles"),
            array_bytes=array_bytes_len,
        )
    )


def _consume_heartbeat(sock: zmq.Socket, state: State) -> None:
    """Pull one heartbeat message and update the latest snapshot."""
    try:
        hb_bytes = sock.recv()
    except zmq.Again:
        return

    try:
        hb = msgpack.unpackb(hb_bytes)
    except Exception:
        return

    snap = HeartbeatSnapshot(
        received_monotonic=time.monotonic(),
        state=hb.get("state", ""),
        q_ingest_sz=hb.get("q_ingest_sz"),
        q_ingest_max=hb.get("q_ingest_max"),
        q_xyt_sz=hb.get("q_xyt_sz"),
        q_xyt_max=hb.get("q_xyt_max"),
        q_ctrl_sz=hb.get("q_ctrl_sz"),
        q_ctrl_max=hb.get("q_ctrl_max"),
    )
    state.last_hb = snap
    state.heartbeats.append(snap)

    if snap.q_ingest_sz is not None:
        state.peak_q_ingest_sz = max(state.peak_q_ingest_sz, snap.q_ingest_sz)
    if snap.q_xyt_sz is not None:
        state.peak_q_xyt_sz = max(state.peak_q_xyt_sz, snap.q_xyt_sz)
    if snap.q_ctrl_sz is not None:
        state.peak_q_ctrl_sz = max(state.peak_q_ctrl_sz, snap.q_ctrl_sz)


# =============================================================================
# Final summary + interpretation
# =============================================================================


def _summarise(state: State) -> Dict:
    """Compute final metrics in a JSON-serialisable dict."""
    n = len(state.events)
    deltas = (
        [
            state.events[i].arrived_monotonic - state.events[i - 1].arrived_monotonic
            for i in range(1, n)
        ]
        if n >= 2
        else []
    )

    if deltas:
        mean = statistics.fmean(deltas)
        median = statistics.median(deltas)
        std = statistics.pstdev(deltas) if len(deltas) > 1 else 0.0
        cv = std / mean if mean > 0 else 0.0
        dmin = min(deltas)
        dmax = max(deltas)
        frac_burst = sum(1 for d in deltas if d < 0.050) / len(deltas)
    else:
        mean = median = std = cv = dmin = dmax = frac_burst = 0.0

    flush_interval = None
    tdc_freq = None
    if state.start_metadata:
        flush_interval = state.start_metadata.get("flush_interval_s")
        tdc_freq = state.start_metadata.get("tdc_frequency_hz")

    if flush_interval and flush_interval > 0:
        frac_silence = sum(1 for d in deltas if d > 2.0 * flush_interval) / len(deltas) if deltas else 0.0
    else:
        frac_silence = 0.0

    flush_numbers = [e.flush_number for e in state.events if e.flush_number is not None]
    expected_seq = list(range(1, len(flush_numbers) + 1))
    missing_or_reordered = flush_numbers != expected_seq
    gaps_in_seq: List[int] = []
    if flush_numbers:
        for i in range(1, len(flush_numbers)):
            d = flush_numbers[i] - flush_numbers[i - 1]
            if d != 1:
                gaps_in_seq.append(d)

    # Time origin for the artifact: the listener's start time.  Subtracting
    # ``state.started_monotonic`` from every recorded monotonic timestamp
    # makes the JSON post-processable without leaking the host's monotonic
    # clock offset (which is meaningless across machines).
    t0 = state.started_monotonic

    events_t_rel = [e.arrived_monotonic - t0 for e in state.events]
    hb_t_rel = [hb.received_monotonic - t0 for hb in state.heartbeats]
    hb_q_ingest = [hb.q_ingest_sz for hb in state.heartbeats]
    hb_q_xyt = [hb.q_xyt_sz for hb in state.heartbeats]
    hb_q_ctrl = [hb.q_ctrl_sz for hb in state.heartbeats]
    hb_states = [hb.state for hb in state.heartbeats]

    # Latency from start message to first event message — the dominant
    # diagnostic for "first 45 s of an acquisition show no flushes".  None
    # when we missed start (subscriber attached late).
    if state.start_received_monotonic is not None and state.events:
        start_to_first_event_s = state.events[0].arrived_monotonic - state.start_received_monotonic
    else:
        start_to_first_event_s = None

    # Latency from stop message to listener's last event arrival.  Sanity
    # check that the server emitted its tail before publishing stop.
    if state.stop_received_monotonic is not None and state.events:
        last_event_to_stop_s = state.stop_received_monotonic - state.events[-1].arrived_monotonic
    else:
        last_event_to_stop_s = None

    return {
        "started_at_epoch_s": state.started_at_epoch_s,
        "n_events": n,
        "n_starts": state.starts_seen,
        "n_stops": state.stops_seen,
        "duration_s": (
            state.events[-1].arrived_monotonic - state.events[0].arrived_monotonic if n >= 2 else 0.0
        ),
        "flush_interval_s_from_start_msg": flush_interval,
        "tdc_frequency_hz_from_start_msg": tdc_freq,
        "delta_mean_s": mean,
        "delta_median_s": median,
        "delta_std_s": std,
        "delta_min_s": dmin,
        "delta_max_s": dmax,
        "cv": cv,
        "fraction_sub_50ms": frac_burst,
        "fraction_over_2x_flush_interval": frac_silence,
        "flush_numbers_contiguous": not missing_or_reordered,
        "flush_number_gaps_seen": gaps_in_seq[:20],
        "peak_q_ingest_sz": state.peak_q_ingest_sz,
        "peak_q_xyt_sz": state.peak_q_xyt_sz,
        "peak_q_ctrl_sz": state.peak_q_ctrl_sz,
        "heartbeat_q_ingest_max_observed": (
            max((hb.q_ingest_max for hb in state.heartbeats if hb.q_ingest_max is not None), default=0)
        ),
        "heartbeat_q_xyt_max_observed": (
            max((hb.q_xyt_max for hb in state.heartbeats if hb.q_xyt_max is not None), default=0)
        ),
        "stop_total_cycles": (
            state.stop_metadata.get("total_cycles") if state.stop_metadata else None
        ),
        "stop_total_flushes": (
            state.stop_metadata.get("total_flushes") if state.stop_metadata else None
        ),
        "start_to_first_event_s": start_to_first_event_s,
        "last_event_to_stop_s": last_event_to_stop_s,
        # All times below are seconds since the listener started, so they
        # share an origin and are directly correlatable.
        "events_t_rel_s": events_t_rel,
        "events_flush_number": flush_numbers,
        "events_cycles_in_flush": [e.cycles_in_flush for e in state.events],
        "events_total_cycles": [e.total_cycles for e in state.events],
        "events_array_bytes": [e.array_bytes for e in state.events],
        "heartbeat_t_rel_s": hb_t_rel,
        "heartbeat_state": hb_states,
        "heartbeat_q_ingest_sz": hb_q_ingest,
        "heartbeat_q_xyt_sz": hb_q_xyt,
        "heartbeat_q_ctrl_sz": hb_q_ctrl,
    }


def _print_interpretation(metrics: Dict) -> None:
    """Translate the metrics into a human-readable diagnosis."""
    print("\n" + "=" * 78)
    print("INTERPRETATION")
    print("=" * 78)

    if metrics["n_events"] < 2:
        print("Too few events to draw any conclusion.")
        return

    fi = metrics["flush_interval_s_from_start_msg"]
    if fi is None:
        print("- No 'start' message captured: cannot derive expected cadence from the wire.")
        print("  Run the listener earlier (before the acquisition starts) to capture it.")
        return

    print(f"- Configured flush_interval = {fi:.3f}s, "
          f"tdc_frequency = {metrics['tdc_frequency_hz_from_start_msg']} Hz")
    print(f"- Observed inter-event delta: "
          f"mean={metrics['delta_mean_s']*1000:.1f} ms, "
          f"median={metrics['delta_median_s']*1000:.1f} ms, "
          f"max={metrics['delta_max_s']*1000:.1f} ms,  CV={metrics['cv']:.2f}")

    # Startup-latency check: the dominant diagnostic when the symptom is
    # "first 45 s show no flushes".  If start→first-event >> flush_interval
    # *and* the first event carries a huge cycles_in_flush, the upstream
    # (live-cli sort buffer or Serval ramp) is holding data back; our
    # server only emits once handle_tdc has actually been called.
    s2fe = metrics.get("start_to_first_event_s")
    if s2fe is not None:
        first_cif = metrics["events_cycles_in_flush"][0] if metrics["events_cycles_in_flush"] else None
        tdc_hz = metrics.get("tdc_frequency_hz_from_start_msg") or 0
        first_cif_seconds = (first_cif / tdc_hz) if (first_cif and tdc_hz) else None

        line = f"- start→first-event latency: {s2fe:.2f}s"
        if first_cif_seconds is not None:
            line += f"  (first event carried {first_cif} cycles ≈ {first_cif_seconds:.2f}s of data)"
        print(line)

        if s2fe >= 3 * fi:
            print(f"  → Startup latency is {s2fe/fi:.1f}× flush_interval.  Our server cannot")
            print("    emit before handle_tdc fires, so this latency is upstream of")
            print("    splash_timepix.app — most likely live-cli's sort buffer ")
            print("    (--max-delay-bins) or Serval's startup ramp.")
            if first_cif_seconds is not None and first_cif_seconds >= 0.8 * s2fe:
                print(f"    The first event then carries ~{first_cif_seconds:.1f}s of data, which")
                print("    confirms a single upstream bolus released after the warmup window,")
                print("    not a sustained back-pressure inside our server.")

    burst = metrics["fraction_sub_50ms"]
    silence = metrics["fraction_over_2x_flush_interval"]

    if burst >= 0.05 and silence >= 0.05:
        print(f"- {burst*100:.1f}% of deltas <50ms AND {silence*100:.1f}% > 2*flush_interval")
        print("  → Wire-level BURST pattern: the server is still emitting clumps of flushes")
        print("    followed by silence.  The wall-clock gate in app.py is either bypassed or")
        print("    the upstream is delivering bigger boluses than the gate can pace.")
    elif burst >= 0.05:
        print(f"- {burst*100:.1f}% of deltas <50ms (sub-50 ms)")
        print("  → Some bursting, but no long silences.  Likely the gate is mostly working")
        print("    but occasional clumps slip through, possibly when the watchdog and a")
        print("    fresh bolus race.")
    elif silence >= 0.05:
        print(f"- {silence*100:.1f}% of deltas > 2*flush_interval")
        print("  → Long silences without bursting.  Either the upstream paused, the")
        print("    server's xyt_queue dropped flushes, or a flush_number gap exists.")
    else:
        print("- Cadence on the wire looks REGULAR by these metrics.")
        print("  If the UI still appears stuttery, the bottleneck is most likely UI-side")
        print("  (paint coalescing, on_flush_received doing heavy work on the GUI thread,")
        print("  or the SUB socket on the UI dropping due to its own SNDHWM peer pipe).")

    if not metrics["flush_numbers_contiguous"]:
        print(f"- flush_number sequence has gaps: {metrics['flush_number_gaps_seen']}")
        print("  → The server's xyt_queue dropped flushes (queue full → put_nowait failed),")
        print("    OR the PUB socket dropped them on send (SNDHWM hit, DONTWAIT raised).")
        print("    Cross-reference with the heartbeat queue peaks below.")
    else:
        print("- flush_number sequence is contiguous: no flushes were dropped between")
        print("  the server and this listener.")

    print(f"- Heartbeat queue peaks: q_ingest={metrics['peak_q_ingest_sz']}, "
          f"q_xyt={metrics['peak_q_xyt_sz']}, q_ctrl={metrics['peak_q_ctrl_sz']}")
    if metrics["peak_q_xyt_sz"] >= 8:
        print("  → q_xyt peak ≥ 8/10: the ZMQ worker (or its subscribers) is the bottleneck;")
        print("    server-side back-pressure is the dominant effect.")

    q_in_series = [v for v in metrics.get("heartbeat_q_ingest_sz", []) if v is not None]
    q_in_max = metrics.get("heartbeat_q_ingest_max_observed", 0)
    if q_in_series and q_in_max:
        # Compare a "permanent backlog" model (median high) vs a "transient
        # spike" model (median low, max high) using the heartbeat series.
        q_med = statistics.median(q_in_series)
        q_max = max(q_in_series)
        frac_busy = sum(1 for v in q_in_series if v > 0.5 * q_in_max) / len(q_in_series)
        print(f"- q_ingest series:  median={q_med:.0f}/{q_in_max}  "
              f"max={q_max}/{q_in_max}  frac>50%={frac_busy*100:.0f}%")
        if frac_busy >= 0.5:
            print("  → q_ingest is busy more than half the time → data processor is")
            print("    permanently behind, not just briefly spiking.  Consider raising")
            print("    callback_batch_size, profiling parse_batch+bin_pixels, or moving")
            print("    binning to a worker pool.")
        elif q_max >= 0.8 * q_in_max and frac_busy < 0.2:
            print("  → q_ingest spikes briefly then drains → upstream bolus pattern,")
            print("    not a sustained CPU shortage on the data processor.")


# =============================================================================
# Main loop
# =============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(description="Flush-pacing diagnostic listener")
    parser.add_argument("--host", default="localhost", help="Host of the streaming server")
    parser.add_argument("--zmq-port", type=int, default=5657, help="Data PUB port")
    parser.add_argument("--hb-port", type=int, default=5658, help="Heartbeat PUB port")
    parser.add_argument("--ticker-interval", type=float, default=2.0,
                        help="Seconds between live status lines (default: 2.0)")
    parser.add_argument("--artifact", default=None,
                        help="JSON output path (default: /tmp/flush_pacing_<ts>.json)")
    args = parser.parse_args()

    if args.artifact is None:
        args.artifact = f"/tmp/flush_pacing_{int(time.time())}.json"

    state = State()

    ctx = zmq.Context()
    data_sock = ctx.socket(zmq.SUB)
    data_sock.connect(f"tcp://{args.host}:{args.zmq_port}")
    data_sock.setsockopt(zmq.SUBSCRIBE, b"")

    hb_sock = ctx.socket(zmq.SUB)
    hb_sock.connect(f"tcp://{args.host}:{args.hb_port}")
    hb_sock.setsockopt(zmq.SUBSCRIBE, b"")

    poller = zmq.Poller()
    poller.register(data_sock, zmq.POLLIN)
    poller.register(hb_sock, zmq.POLLIN)

    # Graceful Ctrl+C handler so the final summary always runs.
    stop_requested = {"flag": False}

    def _handle_sigint(signum, frame):
        stop_requested["flag"] = True

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    print("=" * 78)
    print("FLUSH PACING LISTENER")
    print("=" * 78)
    print(f"data    : tcp://{args.host}:{args.zmq_port}")
    print(f"heartbeat: tcp://{args.host}:{args.hb_port}")
    print(f"artifact: {args.artifact}")
    print("Ctrl+C to stop and print summary.\n")

    last_ticker_monotonic = time.monotonic()

    try:
        while not stop_requested["flag"]:
            socks = dict(poller.poll(timeout=200))  # ms
            now = time.monotonic()

            if data_sock in socks:
                _consume_data_message(data_sock, state, arrived_monotonic=now)

            if hb_sock in socks:
                _consume_heartbeat(hb_sock, state)

            if now - last_ticker_monotonic >= args.ticker_interval:
                print(_format_live_ticker(state), flush=True)
                last_ticker_monotonic = now

    finally:
        try:
            data_sock.setsockopt(zmq.LINGER, 0)
            data_sock.close()
            hb_sock.setsockopt(zmq.LINGER, 0)
            hb_sock.close()
            ctx.term()
        except Exception:
            pass

    metrics = _summarise(state)

    print("\n" + "=" * 78)
    print("FINAL SUMMARY")
    print("=" * 78)
    print(f"events received : {metrics['n_events']}")
    print(f"start/stop seen : {metrics['n_starts']}/{metrics['n_stops']}")
    print(f"stream duration : {metrics['duration_s']:.2f}s")
    if metrics["delta_mean_s"]:
        print(f"delta mean      : {metrics['delta_mean_s']*1000:.1f} ms")
        print(f"delta median    : {metrics['delta_median_s']*1000:.1f} ms")
        print(f"delta std       : {metrics['delta_std_s']*1000:.1f} ms")
        print(f"delta min/max   : {metrics['delta_min_s']*1000:.1f} / "
              f"{metrics['delta_max_s']*1000:.1f} ms")
        print(f"CV              : {metrics['cv']:.3f}")
        print(f"fraction <50 ms : {metrics['fraction_sub_50ms']*100:.2f} %")
        print(f"fraction >2*FI  : {metrics['fraction_over_2x_flush_interval']*100:.2f} %")
    print(f"flush_numbers   : {'contiguous' if metrics['flush_numbers_contiguous'] else 'GAPS!'}")
    if metrics["flush_number_gaps_seen"]:
        print(f"  gaps         : {metrics['flush_number_gaps_seen']}")
    print(f"queue peaks     : ingest={metrics['peak_q_ingest_sz']}, "
          f"xyt={metrics['peak_q_xyt_sz']}, ctrl={metrics['peak_q_ctrl_sz']}")
    if metrics["stop_total_cycles"] is not None:
        print(f"stop reports    : total_cycles={metrics['stop_total_cycles']}, "
              f"total_flushes={metrics['stop_total_flushes']}")

    _print_interpretation(metrics)

    try:
        with open(args.artifact, "w") as f:
            json.dump(metrics, f, indent=2, sort_keys=True)
        print(f"\nArtifact written to {args.artifact}")
    except OSError as exc:
        print(f"\nFailed to write artifact: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
