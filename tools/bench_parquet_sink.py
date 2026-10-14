#!/usr/bin/env python3
"""Benchmark :class:`splash_timepix.parquet_sink.ParquetSink` without sockets or ``tpx-stream``.

Drives pre-built ``BatchParseResult`` objects into a sink at a chosen pixel rate
and reports what the disk and the writer thread can sustain::

    python tools/bench_parquet_sink.py --dir /data/bench --rate 40e6 --seconds 20
    python tools/bench_parquet_sink.py --dir /data/bench --rate 40e6 --seconds 20 --no-sink   # cost baseline

Run on the acquisition PC's data disk this answers the questions left open in
``info/plans/parquet_streaming_tiled.md`` §13: compressed bytes per hit, roll
interval at the configured size, whether the disk keeps up at the peak rate, and
how much CPU ``offer()`` steals from the thread that also feeds the live view.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

from splash_timepix.parquet_meta import list_part_files
from splash_timepix.parquet_sink import ParquetSink
from splash_timepix.parser import BatchParseResult


def make_batches(n_batches: int, batch_rows: int, tdc_per_batch: int, seed: int = 0) -> list[BatchParseResult]:
    """Realistic-looking batches: time-sorted, 256×256 pixels, 10-bit ToT, TDC edges interleaved."""
    rng = np.random.default_rng(seed)
    out = []
    t = 0
    for _ in range(n_batches):
        dt = rng.integers(1, 200, batch_rows, dtype=np.int64)  # ~100 ticks ≈ 26 ns apart on average
        ts = (t + np.cumsum(dt)).astype(np.uint64)
        t = int(ts[-1])
        tdc_idx = np.sort(rng.choice(batch_rows, tdc_per_batch, replace=False)) if tdc_per_batch else np.array([], int)
        out.append(
            BatchParseResult(
                n_pixels=batch_rows,
                n_tdc=int(tdc_per_batch),
                n_control=0,
                n_unknown=0,
                pixel_x=rng.integers(0, 256, batch_rows).astype(np.int32),
                pixel_y=rng.integers(0, 256, batch_rows).astype(np.int32),
                pixel_timestamp=ts,
                pixel_tot=np.clip(rng.normal(100, 20, batch_rows), 0, 1023).astype(np.int32),
                pixel_indices=np.arange(batch_rows, dtype=np.int64),
                tdc_timestamp=ts[tdc_idx] if tdc_per_batch else np.array([], dtype=np.uint64),
                tdc_channel=np.ones(tdc_per_batch, dtype=np.uint8),
                tdc_edge=np.zeros(tdc_per_batch, dtype=np.uint8),
                tdc_indices=tdc_idx.astype(np.int64),
                control_timestamp=np.array([], dtype=np.uint64),
                control_subtype=np.array([], dtype=np.uint8),
                control_indices=np.array([], dtype=np.int64),
            )
        )
    return out


def shift_batch(b: BatchParseResult, offset: int) -> BatchParseResult:
    """Reuse a pre-built batch with its timestamps moved forward (cheap: one add per array)."""
    off = np.uint64(offset)
    return BatchParseResult(
        n_pixels=b.n_pixels,
        n_tdc=b.n_tdc,
        n_control=0,
        n_unknown=0,
        pixel_x=b.pixel_x,
        pixel_y=b.pixel_y,
        pixel_timestamp=b.pixel_timestamp + off,
        pixel_tot=b.pixel_tot,
        pixel_indices=b.pixel_indices,
        tdc_timestamp=b.tdc_timestamp + off,
        tdc_channel=b.tdc_channel,
        tdc_edge=b.tdc_edge,
        tdc_indices=b.tdc_indices,
        control_timestamp=b.control_timestamp,
        control_subtype=b.control_subtype,
        control_indices=b.control_indices,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", type=Path, default=None, help="directory on the disk to test (default: temp dir)")
    p.add_argument("--rate", type=float, default=40e6, help="pixel rate to drive, hits/s (default 40e6)")
    p.add_argument("--seconds", type=float, default=10.0, help="duration (default 10)")
    p.add_argument("--batch-rows", type=int, default=10_000, help="pixels per BatchParseResult (default 10000)")
    p.add_argument("--tdc-per-batch", type=int, default=5)
    p.add_argument("--roll-mb", type=int, default=512)
    p.add_argument("--row-group-rows", type=int, default=1_000_000)
    p.add_argument("--queue-max-rows", type=int, default=20_000_000)
    p.add_argument("--on-full", choices=["drop", "block"], default="drop")
    p.add_argument("--no-sink", action="store_true", help="generate batches but do not offer them (baseline)")
    p.add_argument("--keep", action="store_true", help="keep the written files")
    args = p.parse_args(argv)

    root = args.dir or Path(tempfile.mkdtemp(prefix="bench_parquet_"))
    root.mkdir(parents=True, exist_ok=True)
    print(f"disk: {root}  free: {shutil.disk_usage(root).free / 1e9:.1f} GB")

    n_proto = 64
    protos = make_batches(n_proto, args.batch_rows, args.tdc_per_batch)
    span = int(protos[-1].pixel_timestamp[-1]) + 1

    sink = None
    if not args.no_sink:
        sink = ParquetSink(
            root,
            roll_bytes=args.roll_mb * 2**20,
            row_group_rows=args.row_group_rows,
            queue_max_rows=args.queue_max_rows,
            on_full=args.on_full,
        )
        sink.start(
            "bench",
            dict(
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
                source={"kind": "simulator", "path": "bench_parquet_sink.py"},
            ),
        )

    batches_per_s = args.rate / args.batch_rows
    interval = 1.0 / batches_per_s
    offer_lat = []
    q_hwm = 0
    offered = 0
    cpu0 = time.process_time()
    t_start = time.perf_counter()
    next_t = t_start
    i = 0
    stop_flag = threading.Event()

    def sampler():
        nonlocal q_hwm
        while not stop_flag.is_set():
            if sink is not None:
                q_hwm = max(q_hwm, sink.stats()["parquet_q_rows"])
            time.sleep(0.05)

    th = threading.Thread(target=sampler, daemon=True)
    th.start()

    while True:
        now = time.perf_counter()
        if now - t_start >= args.seconds:
            break
        if now < next_t:
            time.sleep(min(0.001, next_t - now))
            continue
        next_t += interval
        b = shift_batch(protos[i % n_proto], (i // n_proto) * span)
        i += 1
        if sink is not None:
            t0 = time.perf_counter()
            sink.offer(b)
            offer_lat.append(time.perf_counter() - t0)
        offered += b.n_pixels
    elapsed = time.perf_counter() - t_start
    stop_flag.set()

    achieved = offered / elapsed
    print(f"offered: {offered:,} hits in {elapsed:.2f}s → {achieved/1e6:.2f} Mhit/s (target {args.rate/1e6:.1f})")
    if achieved < 0.9 * args.rate:
        print("  NOTE: the driver loop itself could not reach the target rate on this machine")
    if sink is None:
        print(f"driver CPU: {time.process_time() - cpu0:.2f}s")
        return 0

    t_stop0 = time.perf_counter()
    res = sink.stop(timeout=600)
    t_stop = time.perf_counter() - t_stop0
    sink.shutdown()
    cpu = time.process_time() - cpu0

    scan_dir = Path(res["scan_dir"])
    files = list_part_files(scan_dir / "events")
    nbytes = sum(f.stat().st_size for f in files)
    rows = res["rows"]
    print(f"status: {res['status']}  rows written: {rows:,}  dropped: {res['dropped_rows']:,}")
    print(f"files: {len(files)} × ~{nbytes / max(1, len(files)) / 2**20:.0f} MB = {nbytes/2**20:.0f} MB")
    if rows:
        bph = nbytes / rows
        print(
            f"compressed: {bph:.2f} bytes/hit  (raw on the queue: 14 B/hit)  → {achieved*bph/1e6:.0f} MB/s at this rate"
        )
        if len(files) > 1:
            print(f"roll interval at {args.roll_mb} MB: ~{elapsed/len(files):.1f}s")
    print(f"writer throughput: {rows/(elapsed + t_stop)/1e6:.2f} Mhit/s incl. {t_stop:.1f}s stop/drain")
    print(f"queue high-water: {q_hwm:,} rows of {args.queue_max_rows:,}")
    if offer_lat:
        lat = np.array(offer_lat) * 1e6
        print(
            f"offer(): mean {lat.mean():.1f} µs  p50 {np.percentile(lat, 50):.1f}  "
            f"p99 {np.percentile(lat, 99):.1f}  max {lat.max():.0f} µs  (n={len(lat)})"
        )
    print(f"process CPU: {cpu:.2f}s over {elapsed + t_stop:.2f}s wall → {cpu/(elapsed + t_stop):.2f} cores")
    if not args.keep and args.dir is None:
        shutil.rmtree(root, ignore_errors=True)
    elif not args.keep:
        shutil.rmtree(scan_dir, ignore_errors=True)
    return 0 if res["status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
