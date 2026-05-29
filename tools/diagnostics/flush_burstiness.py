#!/usr/bin/env python3
"""Flush-burstiness diagnostic: 5 × 5 (cps, tdc_frequency) grid experiment.

Spawns the streaming server + simulator CLI for each combination in a
logarithmic grid and measures the wall-clock distribution of ZMQ ``event``
messages to characterise flush cadence regularity.

Primary metric
--------------
CV (coefficient of variation = std(Δt) / mean(Δt)) of inter-flush gaps.

  CV ≈ 0  — perfectly regular, one flush per flush_interval
  CV ≥ 1  — flushes arriving in bursts with long silences between them

Background
----------
In production the server used to emit flushes in bursts when the upstream
(Serval + luna-iterator) batched packets in multi-second boluses: the
original TDC-count-based flush gate fired repeatedly inside a single
``data_callback`` invocation.  Server commit ``754c857`` switched to a
wall-clock gate so flush cadence is decoupled from TDC arrival cadence.
Run this script before/after any change that touches the flush path to check
for regressions in pacing smoothness.

Usage
-----
::

    # Default 5×5 grid, 15 s per combo (~8 min total)
    .venv/bin/python3 tools/diagnostics/flush_burstiness.py

    # Quick 2×2 smoke run, 5 s per combo
    .venv/bin/python3 tools/diagnostics/flush_burstiness.py \\
        --cps 100,10000 --tdc 10,1000 --daq 5

    # Override via environment variables (same effect)
    BURSTINESS_CPS=100,10000 BURSTINESS_TDC=10,1000 BURSTINESS_DAQ=5 \\
        .venv/bin/python3 tools/diagnostics/flush_burstiness.py

    # Save JSON artifact for before/after diffing
    .venv/bin/python3 tools/diagnostics/flush_burstiness.py \\
        --artifact /tmp/burstiness_before.json
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

import msgpack
import zmq

# ---------------------------------------------------------------------------
# Repo root (tools/diagnostics/ → tools/ → repo root)
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_free_port() -> int:
    """Return an available ephemeral TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        s.listen(1)
        return s.getsockname()[1]


def _wait_for_ready(port: int, timeout: float = 10.0) -> bool:
    """Poll the server's heartbeat PUB until it emits READY or timeout."""
    from splash_timepix.heartbeat import wait_for_ready

    return wait_for_ready(port=port, timeout=timeout)


# ---------------------------------------------------------------------------
# Per-combo result
# ---------------------------------------------------------------------------


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

    notes: str = ""

    @property
    def deltas_s(self) -> List[float]:
        ts = self.event_arrival_monotonic
        if len(ts) < 2:
            return []
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


# ---------------------------------------------------------------------------
# Per-combo server + simulator lifecycle
# ---------------------------------------------------------------------------


def _spawn_server(
    *,
    tcp_port: int,
    zmq_port: int,
    hb_port: int,
    tdc_frequency: float,
    flush_interval: float,
) -> subprocess.Popen:
    cmd = [
        sys.executable,
        "-m",
        "splash_timepix.app",
        "--host", "localhost",
        "--port", str(tcp_port),
        "--zmq-port", str(zmq_port),
        "--heartbeat-port", str(hb_port),
        "--tdc-frequency", str(tdc_frequency),
        "--flush-interval", str(flush_interval),
        "--collapse-y",
        "--exit-on-disconnect",
    ]
    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=REPO_ROOT,
    )


def _spawn_simulator(
    *,
    tcp_port: int,
    duration: int,
    cps: float,
    tdc_frequency: float,
) -> subprocess.Popen:
    cmd = [
        sys.executable,
        "-m",
        "splash_timepix.simulator_cli",
        "--auto-start",
        "--port", str(tcp_port),
        "--tdc-frequency", str(tdc_frequency),
        "--cps", str(cps),
        "--duration", str(max(1, int(round(duration)))),
        "--no-count",
    ]
    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        cwd=REPO_ROOT,
    )


def _collect(
    sock: zmq.Socket,
    *,
    deadline: float,
) -> ComboResult:
    result = ComboResult(cps=0, tdc=0, flush_interval_s=0, expected_flushes=0)
    sock.setsockopt(zmq.RCVTIMEO, 500)
    while time.monotonic() < deadline:
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
            break
        else:
            try:
                sock.recv()  # consume array-bytes frame
            except zmq.Again:
                pass
            result.n_events += 1
            result.event_arrival_monotonic.append(arrived)
    return result


def _teardown(
    *,
    server_proc: subprocess.Popen,
    sim_proc: subprocess.Popen,
    sub_sock: zmq.Socket,
    hb_sock: zmq.Socket,
) -> None:
    for proc, t in [(sim_proc, 5), (server_proc, 8)]:
        try:
            proc.wait(timeout=t)
        except subprocess.TimeoutExpired:
            pass
    for s in (sub_sock, hb_sock):
        try:
            s.setsockopt(zmq.LINGER, 0)
            s.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Grid loop
# ---------------------------------------------------------------------------


def _cv(r: ComboResult) -> float:
    d = r.deltas_s
    if not d:
        return -1.0
    m = statistics.fmean(d)
    if m == 0:
        return -1.0
    return (statistics.pstdev(d) if len(d) > 1 else 0.0) / m


def run_grid(
    *,
    cps_values: Tuple[int, ...],
    tdc_values: Tuple[int, ...],
    daq_seconds: int,
    flush_interval_s: float,
    sim_wait_budget_s: float,
    stop_wait_budget_s: float,
    artifact_path: str,
) -> None:
    ctx = zmq.Context()
    results: List[ComboResult] = []

    total = len(cps_values) * len(tdc_values)
    print("\n")
    print("=" * 100)
    print(
        f"FLUSH BURSTINESS — {total} combos, {daq_seconds}s each, "
        f"flush_interval={flush_interval_s}s"
    )
    print("=" * 100)

    combo_idx = 0
    for tdc in tdc_values:
        for cps in cps_values:
            combo_idx += 1
            print(f"\n[{combo_idx}/{total}] cps={cps}  tdc={tdc} Hz  ...", flush=True)

            tcp_port = _get_free_port()
            zmq_port = _get_free_port()
            hb_port = _get_free_port()

            server_proc = _spawn_server(
                tcp_port=tcp_port,
                zmq_port=zmq_port,
                hb_port=hb_port,
                tdc_frequency=float(tdc),
                flush_interval=flush_interval_s,
            )

            if not _wait_for_ready(hb_port, timeout=10.0):
                print("  [WARN] server did not reach READY — skipping combo")
                try:
                    server_proc.terminate()
                    server_proc.wait(timeout=5)
                except Exception:
                    pass
                continue

            sub_sock = ctx.socket(zmq.SUB)
            sub_sock.connect(f"tcp://127.0.0.1:{zmq_port}")
            sub_sock.setsockopt(zmq.SUBSCRIBE, b"")

            hb_sock = ctx.socket(zmq.SUB)
            hb_sock.connect(f"tcp://127.0.0.1:{hb_port}")
            hb_sock.setsockopt(zmq.SUBSCRIBE, b"")

            # Cover the server's internal slow-joiner grace period.
            time.sleep(2.2)

            sim_proc = _spawn_simulator(
                tcp_port=tcp_port,
                duration=daq_seconds,
                cps=float(cps),
                tdc_frequency=float(tdc),
            )

            deadline = time.monotonic() + sim_wait_budget_s + stop_wait_budget_s
            combo = _collect(sub_sock, deadline=deadline)
            combo.cps = cps
            combo.tdc = tdc
            combo.flush_interval_s = flush_interval_s
            combo.expected_flushes = max(
                0.0, combo.stream_duration_s / flush_interval_s
            )

            try:
                sim_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass

            if combo.n_stops == 0:
                combo.notes = "(NO stop received)"
            elif combo.n_events == 0:
                combo.notes = "(NO events)"
            if tdc < 50:
                combo.notes = (combo.notes + " [low-TDC race zone]").strip()

            results.append(combo)
            print("   " + combo.summary_row())

            _teardown(
                server_proc=server_proc,
                sim_proc=sim_proc,
                sub_sock=sub_sock,
                hb_sock=hb_sock,
            )

    ctx.term()

    # -------------------------------------------------------------------------
    # Summary tables
    # -------------------------------------------------------------------------
    print("\n")
    print("=" * 110)
    print("SUMMARY — sorted by burstiness (CV of inter-flush Δt, descending)")
    print("=" * 110)
    print("CV = std(Δt) / mean(Δt).  CV ≈ 0 → regular;  CV ≥ 1 → bursts dominate.")
    print("'<50ms' = fraction of inter-flush gaps below 50 ms.")
    print("-" * 110)
    for r in sorted(results, key=_cv, reverse=True):
        print(r.summary_row())

    print("\n")
    print("=" * 110)
    print("GRID — CV of inter-flush Δt")
    print("=" * 110)
    header = "               " + "".join(f"cps={c:>7g}  " for c in cps_values)
    print(header)
    for tdc in tdc_values:
        row = [f"tdc={tdc:>6g} Hz | "]
        for cps in cps_values:
            r = next((x for x in results if x.tdc == tdc and x.cps == cps), None)
            row.append("    -     " if r is None else (
                "   n/a    " if _cv(r) < 0 else f"  {_cv(r):>5.2f}    "
            ))
        print("".join(row))

    print("\n")
    print("=" * 110)
    print("GRID — flushes received vs expected  (recv / ~expected)")
    print("=" * 110)
    print(header)
    for tdc in tdc_values:
        row = [f"tdc={tdc:>6g} Hz | "]
        for cps in cps_values:
            r = next((x for x in results if x.tdc == tdc and x.cps == cps), None)
            row.append("    -     " if r is None else f"  {r.n_events:>3d}/{r.expected_flushes:>4.0f}  ")
        print("".join(row))

    # -------------------------------------------------------------------------
    # Optional JSON artifact
    # -------------------------------------------------------------------------
    if artifact_path:
        payload = [
            {
                "cps": r.cps,
                "tdc": r.tdc,
                "flush_interval_s": r.flush_interval_s,
                "n_events": r.n_events,
                "n_starts": r.n_starts,
                "n_stops": r.n_stops,
                "expected_flushes": r.expected_flushes,
                "cv": _cv(r),
                "stream_duration_s": r.stream_duration_s,
                "notes": r.notes,
            }
            for r in results
        ]
        with open(artifact_path, "w") as fh:
            json.dump(
                {
                    "daq_seconds": daq_seconds,
                    "flush_interval_s": flush_interval_s,
                    "combos": payload,
                },
                fh,
                indent=2,
                sort_keys=True,
            )
        print(f"\nArtifact written → {artifact_path}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_int_list(s: str) -> Tuple[int, ...]:
    return tuple(int(x.strip()) for x in s.split(",") if x.strip())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Flush-burstiness diagnostic: sweep (cps, tdc) grid and print CV table.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--cps",
        default=os.environ.get("BURSTINESS_CPS", "10,100,1000,10000,100000"),
        help="Comma-separated counts-per-second values (default: 5-value log grid)",
    )
    parser.add_argument(
        "--tdc",
        default=os.environ.get("BURSTINESS_TDC", "1,10,100,1000,10000"),
        help="Comma-separated TDC frequencies in Hz (default: 5-value log grid)",
    )
    parser.add_argument(
        "--daq",
        type=int,
        default=int(os.environ.get("BURSTINESS_DAQ", 15)),
        help="DAQ duration per combo in seconds (default: 15)",
    )
    parser.add_argument(
        "--flush-interval",
        type=float,
        default=float(os.environ.get("BURSTINESS_FLUSH", 1.0)),
        help="Server flush interval in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--artifact",
        default="",
        help="Write JSON artifact to this path (optional, for before/after diffs)",
    )
    args = parser.parse_args()

    run_grid(
        cps_values=_parse_int_list(args.cps),
        tdc_values=_parse_int_list(args.tdc),
        daq_seconds=args.daq,
        flush_interval_s=args.flush_interval,
        sim_wait_budget_s=60.0,
        stop_wait_budget_s=20.0,
        artifact_path=args.artifact,
    )


if __name__ == "__main__":
    main()
