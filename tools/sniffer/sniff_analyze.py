#!/usr/bin/env python3
"""Sniffer-pcap analyzer for the Serval → live-cli → splash_timepix.app pipeline.

Pairs with ``tools/sniffer/flush_pacing_listener.py``.  Given:

  * a pcap from ``tcpdump -i lo 'tcp port 7070 or tcp port 9090'``
  * (optionally) a JSON artifact from ``flush_pacing_listener.py``

it produces an aligned timeline of:

  * Bytes/s on hop A (Serval → live-cli, dst port 7070)
  * Bytes/s on hop B (live-cli → splash_timepix.app, dst port 9090)
  * Flush events emitted by ``splash_timepix.app`` to its ZMQ subscribers

The three series share a wall-clock origin (the listener's
``started_at_epoch_s``, falling back to the first pcap packet) so 45 s
"bolus" patterns appear as flat regions on whichever port is the offender.

Usage::

    python tools/sniffer/sniff_analyze.py /tmp/sniff_NNN.pcap
    python tools/sniffer/sniff_analyze.py /tmp/sniff_NNN.pcap --listener /tmp/flush_pacing_NNN.json
    python tools/sniffer/sniff_analyze.py /tmp/sniff_NNN.pcap --bin 0.1   # 100 ms bins

Diagnostic interpretation
-------------------------

After parsing, the script classifies the bolus pattern across hops:

  * Hop A (port 7070) bursts AND hop B (port 9090) bursts in lock-step
    → Serval is the source.  live-cli is faithfully forwarding bursty
    input.
  * Hop A is smooth at sub-second cadence, hop B bursts
    → live-cli is buffering its output.  Input fine, output bolused.
  * Hop A bursts, hop B smooth
    → Unexpected.  Suggests live-cli is *de-bursting* upstream input,
    which would be backwards of the symptom we're chasing.
  * Both smooth, but flush events bursty
    → Bug is inside splash_timepix.app after all (we already disproved
    this via the heartbeat queues, so this would invalidate the prior
    diagnosis).

"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import dpkt


HOP_A_DST_PORT = 7070  # Serval → live-cli
HOP_B_DST_PORT = 9090  # live-cli → splash_timepix.app


@dataclass
class PacketEvent:
    ts_epoch: float
    dst_port: int
    src_port: int
    payload_len: int
    flags: int


@dataclass
class HopStats:
    label: str
    dst_port: int
    packets: List[PacketEvent] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(p.payload_len for p in self.packets)

    @property
    def n_data_packets(self) -> int:
        return sum(1 for p in self.packets if p.payload_len > 0)

    @property
    def first_data_ts(self) -> Optional[float]:
        for p in self.packets:
            if p.payload_len > 0:
                return p.ts_epoch
        return None

    @property
    def last_data_ts(self) -> Optional[float]:
        for p in reversed(self.packets):
            if p.payload_len > 0:
                return p.ts_epoch
        return None


def _parse_pcap(path: Path) -> Tuple[HopStats, HopStats, float]:
    hop_a = HopStats(label="Serval→live-cli", dst_port=HOP_A_DST_PORT)
    hop_b = HopStats(label="live-cli→app", dst_port=HOP_B_DST_PORT)

    capture_start_ts: Optional[float] = None

    with path.open("rb") as f:
        reader = dpkt.pcap.Reader(f)
        linktype = reader.datalink()
        for ts, buf in reader:
            if capture_start_ts is None:
                capture_start_ts = ts

            ip = _decode_link_layer(buf, linktype)
            if ip is None or not isinstance(ip, dpkt.ip.IP):
                continue
            if not isinstance(ip.data, dpkt.tcp.TCP):
                continue
            tcp = ip.data
            payload_len = len(tcp.data)

            ev = PacketEvent(
                ts_epoch=float(ts),
                dst_port=int(tcp.dport),
                src_port=int(tcp.sport),
                payload_len=payload_len,
                flags=int(tcp.flags),
            )

            if tcp.dport == HOP_A_DST_PORT:
                hop_a.packets.append(ev)
            elif tcp.dport == HOP_B_DST_PORT:
                hop_b.packets.append(ev)

    if capture_start_ts is None:
        capture_start_ts = 0.0
    return hop_a, hop_b, capture_start_ts


def _decode_link_layer(buf: bytes, linktype: int):
    """Return the IP layer regardless of capture link type.

    Loopback captures typically use one of:
      * DLT_EN10MB (1) → Ethernet header (14 bytes)
      * DLT_NULL (0) → 4-byte BSD loopback header
      * DLT_LINUX_SLL (113) → 16-byte Linux cooked
      * DLT_LINUX_SLL2 (276) → 20-byte Linux cooked v2
    """
    if linktype == dpkt.pcap.DLT_EN10MB:
        try:
            eth = dpkt.ethernet.Ethernet(buf)
            return eth.data
        except dpkt.dpkt.UnpackError:
            return None
    if linktype == dpkt.pcap.DLT_NULL:
        # 4-byte BSD loopback header, then IP.
        if len(buf) < 4:
            return None
        try:
            return dpkt.ip.IP(buf[4:])
        except dpkt.dpkt.UnpackError:
            return None
    if linktype == 113:
        # Linux cooked v1.
        if len(buf) < 16:
            return None
        try:
            return dpkt.ip.IP(buf[16:])
        except dpkt.dpkt.UnpackError:
            return None
    if linktype == 276:
        if len(buf) < 20:
            return None
        try:
            return dpkt.ip.IP(buf[20:])
        except dpkt.dpkt.UnpackError:
            return None
    return None


def _bucketize(packets: List[PacketEvent], origin: float, bin_s: float, n_bins: int) -> List[int]:
    bytes_per_bin = [0] * n_bins
    for p in packets:
        if p.payload_len <= 0:
            continue
        t_rel = p.ts_epoch - origin
        idx = int(t_rel / bin_s)
        if 0 <= idx < n_bins:
            bytes_per_bin[idx] += p.payload_len
    return bytes_per_bin


def _detect_gaps(packets: List[PacketEvent], min_gap_s: float) -> List[Tuple[float, float, float]]:
    """Return list of (gap_start_ts, gap_end_ts, duration_s) for silences ≥ min_gap_s.

    Only counts packets that carry payload (data-direction).
    """
    data_ts = [p.ts_epoch for p in packets if p.payload_len > 0]
    if len(data_ts) < 2:
        return []
    gaps = []
    for i in range(1, len(data_ts)):
        d = data_ts[i] - data_ts[i - 1]
        if d >= min_gap_s:
            gaps.append((data_ts[i - 1], data_ts[i], d))
    return gaps


def _ascii_bar(value: int, max_value: int, width: int = 40) -> str:
    if max_value <= 0:
        return ""
    n = int(round(width * value / max_value))
    return "█" * n


def _print_timeline(
    hop_a: HopStats,
    hop_b: HopStats,
    origin: float,
    bin_s: float,
    duration_s: float,
    flush_t_rel: List[float],
    flush_cycles: List[Optional[int]],
) -> None:
    n_bins = max(1, int(duration_s / bin_s) + 1)
    a_bins = _bucketize(hop_a.packets, origin, bin_s, n_bins)
    b_bins = _bucketize(hop_b.packets, origin, bin_s, n_bins)

    # Bucket flushes per bin.
    flushes_per_bin = [0] * n_bins
    for t in flush_t_rel:
        idx = int(t / bin_s)
        if 0 <= idx < n_bins:
            flushes_per_bin[idx] += 1

    max_a = max(a_bins) if a_bins else 0
    max_b = max(b_bins) if b_bins else 0

    print("\n" + "=" * 100)
    print(f"ALIGNED TIMELINE  (bin={bin_s:.3f}s, duration={duration_s:.1f}s, "
          f"origin = listener t=0 = {origin:.3f} epoch)")
    print("=" * 100)
    print(f"{'t_rel(s)':>9} | {'A:Serval→live-cli (bytes)':<55} {'B:live-cli→app (bytes)':<55} | flushes")
    print("-" * 130)
    for i in range(n_bins):
        t = i * bin_s
        a_val = a_bins[i]
        b_val = b_bins[i]
        f_val = flushes_per_bin[i]
        a_bar = _ascii_bar(a_val, max_a, 30)
        b_bar = _ascii_bar(b_val, max_b, 30)
        # Suppress spammy "all zero" rows in long quiet stretches but keep
        # a marker every 10 bins so the time axis is always visible.
        if a_val == 0 and b_val == 0 and f_val == 0 and i % 10 != 0:
            continue
        print(f"{t:9.2f} | {a_val:>10d} {a_bar:<30}  | "
              f"{b_val:>10d} {b_bar:<30}  | {f_val}")


def _print_gap_summary(label: str, gaps: List[Tuple[float, float, float]], origin: float) -> None:
    print(f"\n{label}: {len(gaps)} gaps ≥ threshold")
    if not gaps:
        return
    for start, end, dur in gaps[:20]:
        ts_rel_start = start - origin
        ts_rel_end = end - origin
        print(f"  [{ts_rel_start:7.2f}s → {ts_rel_end:7.2f}s] silent for {dur:5.2f}s")
    if len(gaps) > 20:
        print(f"  ... and {len(gaps) - 20} more")


def _interpret(
    hop_a_gaps: List[Tuple[float, float, float]],
    hop_b_gaps: List[Tuple[float, float, float]],
    listener_metrics: Optional[Dict],
) -> None:
    print("\n" + "=" * 100)
    print("DIAGNOSIS")
    print("=" * 100)

    a_count = len(hop_a_gaps)
    b_count = len(hop_b_gaps)

    if listener_metrics:
        flush_intvl = listener_metrics.get("flush_interval_s_from_start_msg") or 1.0
    else:
        flush_intvl = 1.0

    big_a = [g for g in hop_a_gaps if g[2] >= 5 * flush_intvl]
    big_b = [g for g in hop_b_gaps if g[2] >= 5 * flush_intvl]

    print(f"hop A (Serval → live-cli) ≥ {5*flush_intvl:.1f}s gaps : {len(big_a)}")
    print(f"hop B (live-cli → app   ) ≥ {5*flush_intvl:.1f}s gaps : {len(big_b)}")

    # Pair-up overlapping bolus boundaries: a hop-A gap that ends within
    # 2 s of a hop-B gap end is consistent with "live-cli faithfully
    # forwards an upstream bolus".  Hop-A smooth + hop-B big = live-cli
    # buffering its own output.
    paired = 0
    for a_start, a_end, a_dur in big_a:
        for b_start, b_end, b_dur in big_b:
            if abs(a_end - b_end) < 2.0 and abs(a_dur - b_dur) < 5.0:
                paired += 1
                break

    if len(big_a) >= 2 and paired >= max(1, len(big_a) // 2):
        verdict = (
            "SERVAL is the source.  Hop A and hop B both show the same\n"
            "  multi-second silences, so live-cli is just forwarding bursty\n"
            "  input.  Recommend escalating to Henrique (live-cli dev) with\n"
            "  this pcap + the Serval version, OR investigating Serval's\n"
            "  flush/queue settings on the JSON destination definition."
        )
    elif len(big_a) == 0 and len(big_b) >= 2:
        verdict = (
            "LIVE-CLI is the source.  Hop A is smooth (sub-second cadence)\n"
            "  but hop B accumulates and dumps in multi-second boluses.\n"
            "  live-cli is buffering its TCP output despite getting input\n"
            "  in real time.  This is reproducible evidence we can send\n"
            "  to Henrique with a request for an output-flush knob."
        )
    elif len(big_a) == 0 and len(big_b) == 0:
        verdict = (
            "BOTH HOPS are smooth on the wire.  The 45s symptom is therefore\n"
            "  NOT in serval/live-cli.  Re-check splash_timepix.app — possibly\n"
            "  the SUB-side of the listener was the bottleneck."
        )
    else:
        verdict = (
            "MIXED / UNCLEAR pattern.  Inspect the timeline above and the\n"
            "  paired-gap counts to distinguish.  Some boluses on hop A may\n"
            "  align with hop B and others not."
        )
    print("\nVerdict:")
    for line in verdict.splitlines():
        print(f"  {line}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze tcpdump pcap of Serval/live-cli/app pipeline")
    parser.add_argument("pcap", type=Path, help="Path to pcap file")
    parser.add_argument("--listener", type=Path, default=None,
                        help="JSON artifact from flush_pacing_listener.py (optional)")
    parser.add_argument("--bin", type=float, default=1.0,
                        help="Bin size in seconds for timeline (default: 1.0)")
    parser.add_argument("--gap-threshold", type=float, default=2.0,
                        help="Minimum silence in seconds to flag as gap (default: 2.0)")
    parser.add_argument("--no-timeline", action="store_true",
                        help="Skip the per-bin ASCII timeline (just summary + gaps)")
    args = parser.parse_args()

    if not args.pcap.exists():
        print(f"pcap not found: {args.pcap}", file=sys.stderr)
        return 1

    print(f"Reading {args.pcap}...")
    hop_a, hop_b, capture_start = _parse_pcap(args.pcap)

    listener_metrics = None
    listener_origin: Optional[float] = None
    if args.listener and args.listener.exists():
        with args.listener.open() as f:
            listener_metrics = json.load(f)
        listener_origin = listener_metrics.get("started_at_epoch_s")

    origin = listener_origin if listener_origin else capture_start

    a_first = hop_a.first_data_ts
    b_first = hop_b.first_data_ts
    a_last = hop_a.last_data_ts
    b_last = hop_b.last_data_ts

    last_seen = max(filter(None, [a_last, b_last, capture_start])) if (a_last or b_last) else capture_start
    duration_s = max(0.0, last_seen - origin)

    print("\n" + "=" * 100)
    print("SUMMARY")
    print("=" * 100)
    print(f"Capture started     : epoch {capture_start:.3f}")
    if listener_origin:
        print(f"Listener started at : epoch {listener_origin:.3f}  (Δ vs capture: {capture_start - listener_origin:+.2f}s)")
    print(f"Time origin (t=0)   : epoch {origin:.3f}")
    print(f"Total wall duration : {duration_s:.2f}s")

    print(f"\nhop A  Serval → live-cli  (dst port {HOP_A_DST_PORT})")
    print(f"  data packets        : {hop_a.n_data_packets}")
    print(f"  total bytes         : {hop_a.total_bytes:,}")
    if a_first is not None:
        print(f"  first data byte at  : t={a_first - origin:+.2f}s")
        print(f"  last data byte at   : t={a_last - origin:+.2f}s")
        if a_last and a_first and a_last > a_first:
            print(f"  avg byte rate       : {hop_a.total_bytes / (a_last - a_first) / 1024:.1f} KB/s")

    print(f"\nhop B  live-cli → app     (dst port {HOP_B_DST_PORT})")
    print(f"  data packets        : {hop_b.n_data_packets}")
    print(f"  total bytes         : {hop_b.total_bytes:,}")
    if b_first is not None:
        print(f"  first data byte at  : t={b_first - origin:+.2f}s")
        print(f"  last data byte at   : t={b_last - origin:+.2f}s")
        if b_last and b_first and b_last > b_first:
            print(f"  avg byte rate       : {hop_b.total_bytes / (b_last - b_first) / 1024:.1f} KB/s")

    a_gaps = _detect_gaps(hop_a.packets, args.gap_threshold)
    b_gaps = _detect_gaps(hop_b.packets, args.gap_threshold)
    _print_gap_summary(f"hop A gaps ≥ {args.gap_threshold}s", a_gaps, origin)
    _print_gap_summary(f"hop B gaps ≥ {args.gap_threshold}s", b_gaps, origin)

    flush_t_rel: List[float] = []
    flush_cycles: List[Optional[int]] = []
    if listener_metrics:
        flush_t_rel = listener_metrics.get("events_t_rel_s") or []
        flush_cycles = listener_metrics.get("events_cycles_in_flush") or []
        n_flush = len(flush_t_rel)
        print(f"\nlistener: {n_flush} flush events, "
              f"first at t={flush_t_rel[0]:.2f}s, last at t={flush_t_rel[-1]:.2f}s"
              if n_flush > 0 else "\nlistener: 0 flush events")

    if not args.no_timeline:
        _print_timeline(hop_a, hop_b, origin, args.bin, duration_s, flush_t_rel, flush_cycles)

    _interpret(a_gaps, b_gaps, listener_metrics)
    return 0


if __name__ == "__main__":
    sys.exit(main())
