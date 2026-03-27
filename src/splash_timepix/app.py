"""
Production implementation of the SocketDataServer.

This script demonstrates how to set up and use the multi-threaded socket server
that reads (simulated) TimePix3 messages and processes them into numpy arrays.
"""

import logging
import math
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timezone

import numpy as np
import psutil
import typer

from splash_timepix.heartbeat import HeartbeatPublisher, ServerState
from splash_timepix.schemas import TimePixStart, TimePixStop

# from splash_timepix.socket_server import SocketDataServer, RingBufferHandler
# from splash_timepix.parser import PixelPacket, TDCPacket, TDCEdge
from splash_timepix.simulator import SimulatorConfig
from splash_timepix.socket_server_np import RingBufferHandler, SocketDataServer
from splash_timepix.workers import input_listener, plotting_worker, zmq_worker

app = typer.Typer()

# Constants from parser for timestamp conversion
TIMESTAMP_CLOCK_MHZ = 3840
TIMESTAMP_PS_PER_TICK = 260.41666  # 1 / 3840 MHz in picoseconds

# Module-level logger
logger = logging.getLogger(__name__)


@app.command()
def main(
    host: str = "localhost",
    port: int = 9090,
    buffer_size: int = 1000,
    callback_batch_size: int = 10000,
    stats_update_time: int = 1,
    plot: bool = False,
    verbose: bool = False,
    zmq_port: int = 5657,
    tdc_ch: int = 0,
    tdc_edge: str = "rising",
    tdc_frequency: float = 1e2,
    t_delta_ns: float = -1,
    n_bins: int = 350,
    flush_interval: float = 1.0,
    exit_on_disconnect: bool = False,
    collapse_y: bool = False,
    heartbeat_port: int = 5658,
):
    """
    Time-resolved TimePix3 data streaming server.

    Args:
        host: Host address for the server to bind to (default: "localhost")
        port: Port number for the server to bind to (default: 9090)
        buffer_size: Size of the internal data buffer (default: 1000)
        callback_batch_size: Number of packets to send per callback (default: 10000)
        stats_update_time: Time between stats updates in seconds (default: 2)
        plot: Use plotting worker (vs ZMQ publishing [default]) (default: False)
        verbose: Show detailed logs, packet samples, and error history (default: False)
        zmq_port: Port number for ZMQ PUB socket (default: 5657)
        tdc_ch: TDC channel to use (0=both, 1=channel 1, 2=channel 2)
        tdc_edge: TDC edge to trigger on ("rising"[default] or "falling")
        tdc_frequency: Expected TDC trigger frequency in Hz
        t_delta_ns: Time bin width in nanoseconds (defaults to auto-binning)
        n_bins: Number of bins (used if no t_delta_ns value is passed)
        flush_interval: Time between array flushes in seconds (default: 1)
        exit_on_disconnect: Exit when client disconnects (for orchestrated runs)
        collapse_y: Send x,y,t (False) or x,t data (True)
        heartbeat_port: Port for ZMQ heartbeat messages (default: 5658)
    """
    os.system("cls" if os.name == "nt" else "clear")
    print("Starting TimPix3 Streaming Application")
    print("=" * 50)
    if exit_on_disconnect:
        print("Mode: Exit on client disconnect (orchestrated)")
    else:
        print("Mode: Persistent (Ctrl+C to stop)")
    print()

    # Calculate binning and display parameters from user inputs
    t_cycle = (1.0 / tdc_frequency) * 1e12  # seconds → picoseconds
    t_cycle_ticks = t_cycle / TIMESTAMP_PS_PER_TICK
    if t_delta_ns > 0:  # user passed a value for width of one bin
        t_delta = t_delta_ns * 1e3  # nanoseconds → picoseconds
        t_delta_ticks = t_delta / TIMESTAMP_PS_PER_TICK
        n_bins = math.ceil(t_cycle_ticks / t_delta_ticks)
    else:  # use default or user-defined value for number of bins
        t_delta = t_cycle / n_bins  # time bin width in picoseconds
        t_delta_ticks = t_delta / TIMESTAMP_PS_PER_TICK
        t_delta_ns = t_delta / 1e3  # picoseconds → nanoseconds

    # Set logging level based on verbose flag
    if verbose:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING, format="%(asctime)s - %(levelname)s - %(message)s")

    # Create and attach the ring buffer handler
    ring_handler = RingBufferHandler(capacity=10)
    if verbose:
        ring_handler.setLevel(logging.WARNING)
    else:
        ring_handler.setLevel(logging.ERROR)
    ring_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))

    # Get the socket_server logger and add our handler to it
    socket_logger = logging.getLogger("splash_timepix.socket_server")
    socket_logger.addHandler(ring_handler)

    # Start heartbeat publisher
    heartbeat = HeartbeatPublisher(port=heartbeat_port, data_port=zmq_port, tcp_port=port, interval=1.0)
    heartbeat.start()

    # Create the server with exit_on_disconnect flag
    server = SocketDataServer(
        host=host,
        port=port,
        buffer_size=buffer_size,
        debug=verbose,  # verbose enables packet buffer
        callback_batch_size=callback_batch_size,
        exit_on_disconnect=exit_on_disconnect,
    )

    # Create stop event for input_listener
    stop_event = threading.Event()

    # Create processing queue
    xyt_queue = queue.Queue(maxsize=10)

    # Create message queue for start/stop control messages (only used with ZMQ worker)
    message_queue = queue.Queue(maxsize=10) if not plot else None

    # Generate unique scan name
    # Generate initial scan_name (will be regenerated for each new client connection)
    # UTC, ISO 8601 format (YYYYMMDDTHHMMSSZ) for unambiguous, sortable identifiers
    def generate_scan_name():
        return f"acquisition_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"

    scan_name = generate_scan_name()

    # Define x, y, t accumulator array
    config = SimulatorConfig()

    detector_size_x = config.detector_size_x
    detector_size_y = config.detector_size_y

    if collapse_y:
        xyt_array = np.zeros((detector_size_x, n_bins), dtype=np.uint32)
    else:
        xyt_array = np.zeros((detector_size_x, detector_size_y, n_bins), dtype=np.uint32)

    xyt_lock = threading.Lock()

    # Build static metadata dict (parameters that don't change during run)
    # (depends on whether x,y,t or x,t is sent `collapse_y`)
    static_metadata = {
        "tdc_frequency_hz": tdc_frequency,
        "t_delta_ns": t_delta_ns,
        "t_cycle_ns": t_cycle / 1e3,
        "n_bins": n_bins,
        "shape": ((detector_size_x, n_bins) if collapse_y else (detector_size_x, detector_size_y, n_bins)),
        "dtype": "uint32",
        "flush_interval_s": flush_interval,
        "cycles_per_flush": max(1, int(flush_interval * tdc_frequency)),
        "tdc_channel": tdc_ch,
        "tdc_edge": tdc_edge,
        "collapse_y": collapse_y,
    }

    # Choose worker based on plot flag
    if plot:
        worker_function = plotting_worker
        worker_args = (xyt_queue, stop_event)
        logger.info("Starting plotting worker")
    else:
        worker_function = zmq_worker
        worker_args = (xyt_queue, stop_event, zmq_port, static_metadata, message_queue)
        logger.info("Starting ZMQ worker")

    worker_thread = threading.Thread(target=worker_function, args=worker_args, daemon=True)
    worker_thread.start()

    # Calculate flush cycles from interval and frequency
    flush_every_n_cycles = max(1, int(flush_interval * tdc_frequency))

    # Errors and warnings
    if tdc_frequency < 0.1:
        logger.error(f"Low TDC frequency ({tdc_frequency} Hz) → detector may miss TDC events")
        return
    if flush_interval < (1.0 / tdc_frequency):
        logger.warning(f"Flush interval ({flush_interval}s) < TDC period ({1.0/tdc_frequency:.2f}s)")
        logger.warning("   Will flush every TDC cycle (flush_every_n_cycles = 1)")

    # Memory check
    array_size_gb = xyt_array.nbytes / (1024**3)

    if array_size_gb > 1.0:
        suggested_bins = int(n_bins * 1.0 / array_size_gb)
        suggested_t_delta_ns = t_delta_ns * (n_bins / suggested_bins)
        logger.warning(f"Large array: {array_size_gb:.2f} GB")
        logger.warning(f"Suggestion: use --t-delta-ns {suggested_t_delta_ns:.1f} to reduce to ~1 GB")
    elif array_size_gb > 5.0:
        logger.error(f"Array too large ({array_size_gb:.2f} GB)! Increase t_delta_ns or decrease TDC frequency")
        return

    # Log final configuration
    logger.info(f"TDC: {tdc_frequency} Hz → t_cycle = {t_cycle:.3e} ps ({t_cycle/1e12:.3f} s)")
    logger.info(f"Time bins: {n_bins} bins × {t_delta_ns} ns = {t_cycle/1e12:.3f} s total")
    logger.info(f"Array size: {array_size_gb:.3f} GB per flush")
    logger.info(f"Flush: every {flush_every_n_cycles} cycles ({flush_interval} s)")

    # Monitor memory usage of the server
    process = psutil.Process(os.getpid())
    print(f"Monitoring Python server process (PID: {os.getpid()})")

    # Set up a callback to handle new data
    event_count = 0

    # Time-resolved binning callback
    # State variables
    t_zero = None
    cycle_count = 0
    flush_count = 0
    pixels_before_trigger = 0
    pixels_outside_window = 0
    last_tdc_warning_time = None
    first_pixel_time = None
    start_message_sent = False  # Track if start message has been sent
    acquisition_start_time = None  # Track when acquisition actually started

    # Convert edge string to enum value
    target_edge = 0 if tdc_edge.lower() == "rising" else 1

    # Pre-allocate local accumulator to avoid repeated allocation
    # (no lock needed during accumulation)
    if collapse_y:
        local_accumulator = np.zeros((detector_size_x, n_bins), dtype=np.uint32)
    else:
        local_accumulator = np.zeros((detector_size_x, detector_size_y, n_bins), dtype=np.uint32)

    def data_callback_np(result) -> None:
        """Time-resolved binning - processes pixels and TDCs in temporal order.

        CRITICAL FIX: The previous version processed all TDCs first, then all pixels.
        This caused pixels to be binned against the wrong t_zero (the last TDC in batch).

        This version:
        1. Filters valid TDCs and gets their timestamps + original indices
        2. For each TDC (in order), bins all pixels between previous TDC and this one
        3. Then bins remaining pixels after the last TDC
        """
        nonlocal last_tdc_warning_time, first_pixel_time
        nonlocal start_message_sent, acquisition_start_time

        current_time = time.time()

        # Send start message when first data arrives
        if not start_message_sent and message_queue is not None:
            acquisition_start_time = current_time
            start_msg = TimePixStart(
                scan_name=scan_name,
                tdc_frequency_hz=tdc_frequency,
                t_delta_ns=t_delta_ns,
                t_cycle_ns=t_cycle / 1e3,
                n_bins=n_bins,
                detector_size_x=detector_size_x,
                detector_size_y=detector_size_y,
                flush_interval_s=flush_interval,
                cycles_per_flush=max(1, int(flush_interval * tdc_frequency)),
                tdc_channel=tdc_ch,
                tdc_edge=tdc_edge,
                collapse_y=collapse_y,
                zmq_port=zmq_port,
                tcp_port=port,
            )
            try:
                message_queue.put_nowait(start_msg.model_dump())
                start_message_sent = True
                logger.info(f"Queued start message for scan: {scan_name}")
                print(f"Queued start message for scan: {scan_name}")  # Also print to console
            except queue.Full:
                logger.warning("Message queue full, dropping start message")
                print("WARNING: Message queue full, dropping start message")

        # Early exit if nothing to process
        if result.n_pixels == 0 and result.n_tdc == 0:
            return

        # =====================================================================
        # STEP 1: Get valid TDCs sorted by original index (temporal order)
        # =====================================================================
        valid_tdc_indices = None
        valid_tdc_timestamps = None

        if result.n_tdc > 0:
            # Filter by channel and edge
            if tdc_ch == 0:
                channel_mask = np.ones(result.n_tdc, dtype=bool)
            else:
                channel_mask = result.tdc_channel == tdc_ch

            edge_mask = result.tdc_edge == target_edge
            valid_tdc_mask = channel_mask & edge_mask

            if np.any(valid_tdc_mask):
                valid_tdc_indices = result.tdc_indices[valid_tdc_mask]
                valid_tdc_timestamps = result.tdc_timestamp[valid_tdc_mask]

                # Sort by original index to maintain temporal order
                sort_order = np.argsort(valid_tdc_indices)
                valid_tdc_indices = valid_tdc_indices[sort_order]
                valid_tdc_timestamps = valid_tdc_timestamps[sort_order]

        # =====================================================================
        # STEP 2: Prepare pixel data
        # =====================================================================
        if result.n_pixels > 0:
            if first_pixel_time is None:
                first_pixel_time = current_time

            # TDC timeout warning
            if last_tdc_warning_time is None and current_time - first_pixel_time > 10.0:
                logger.warning("No matching TDC triggers received in 10s")
                last_tdc_warning_time = current_time

            pixel_indices = result.pixel_indices
            pixel_x = result.pixel_x
            pixel_y = result.pixel_y
            pixel_ts = result.pixel_timestamp
        else:
            pixel_indices = np.array([], dtype=np.int64)

        # =====================================================================
        # STEP 3: Process in temporal order using index boundaries
        # =====================================================================

        # Helper function to bin a subset of pixels
        def bin_pixels(mask):
            """Bin pixels selected by mask against current t_zero."""
            nonlocal pixels_before_trigger, pixels_outside_window, event_count

            n_selected = np.sum(mask)
            if n_selected == 0:
                return

            if t_zero is None:
                pixels_before_trigger += int(n_selected)
                return

            event_count += int(n_selected)

            # Get selected pixel data
            sel_x = pixel_x[mask]
            sel_y = pixel_y[mask]
            sel_ts = pixel_ts[mask]

            # Calculate relative time
            t_relative = sel_ts - t_zero

            # Bounds check
            valid = (t_relative >= 0) & (t_relative < t_cycle_ticks)
            n_outside = int(n_selected - np.sum(valid))

            if n_outside > 0:
                pixels_outside_window += n_outside

            if not np.any(valid):
                return

            # Bin valid pixels
            x_valid = sel_x[valid]
            t_valid = t_relative[valid]
            time_bins = (t_valid / t_delta_ticks).astype(np.int32)
            np.clip(time_bins, 0, n_bins - 1, out=time_bins)

            if collapse_y:
                np.add.at(local_accumulator, (x_valid, time_bins), 1)
            else:
                y_valid = sel_y[valid]
                np.add.at(local_accumulator, (x_valid, y_valid, time_bins), 1)

        # Helper function to handle TDC trigger (flush check + update t_zero)
        def handle_tdc(tdc_ts):
            """Process a TDC trigger: check for flush, update t_zero."""
            nonlocal t_zero, cycle_count, flush_count
            nonlocal pixels_before_trigger, pixels_outside_window
            nonlocal last_tdc_warning_time
            nonlocal xyt_array

            # Check if we need to flush
            if cycle_count > 0 and cycle_count % flush_every_n_cycles == 0:
                with xyt_lock:
                    xyt_array += local_accumulator
                    array_copy = xyt_array.copy()
                    xyt_array.fill(0)

                local_accumulator.fill(0)
                flush_count += 1

                flush_metadata = {
                    "cycles_in_flush": flush_every_n_cycles,
                    "total_cycles": cycle_count,
                    "flush_number": flush_count,
                    "pixels_discarded_before_trigger": int(pixels_before_trigger),
                    "pixels_discarded_outside_window": int(pixels_outside_window),
                }

                try:
                    xyt_queue.put_nowait((array_copy, flush_metadata))
                    logger.info(f"Flushed: #{flush_count}, cycles={flush_every_n_cycles}")
                    pixels_before_trigger = 0
                    pixels_outside_window = 0
                except queue.Full:
                    logger.warning("Processing queue full, dropping array")

            t_zero = int(tdc_ts)
            cycle_count += 1
            last_tdc_warning_time = current_time

        # =====================================================================
        # STEP 4: Main processing loop - interleave TDCs and pixels by index
        # =====================================================================

        if valid_tdc_indices is None or len(valid_tdc_indices) == 0:
            # No valid TDCs - just bin all pixels against current t_zero
            if result.n_pixels > 0:
                bin_pixels(np.ones(result.n_pixels, dtype=bool))
        else:
            # Process pixels and TDCs in temporal order
            last_boundary = -1  # Start before any packet

            for i, (tdc_idx, tdc_ts) in enumerate(zip(valid_tdc_indices, valid_tdc_timestamps)):
                # Bin pixels between last boundary and this TDC
                if result.n_pixels > 0:
                    mask = (pixel_indices > last_boundary) & (pixel_indices < tdc_idx)
                    bin_pixels(mask)

                # Process this TDC
                handle_tdc(tdc_ts)
                last_boundary = tdc_idx

            # Bin pixels after the last TDC
            if result.n_pixels > 0:
                mask = pixel_indices > last_boundary
                bin_pixels(mask)

    # def data_callback_vectorized(new_data) -> None:
    #     """Time-resolved binning with TDC triggers - VECTORIZED VERSION.

    #     Key optimizations:
    #     1. Separates packets by type once, then processes in batches
    #     2. Uses NumPy vectorized operations for pixel binning
    #     3. Accumulates into local array, only locks during flush
    #     4. Uses np.add.at for efficient histogram accumulation
    #     """
    #     nonlocal event_count, t_zero, cycle_count, flush_count
    #     nonlocal pixels_before_trigger, pixels_outside_window
    #     nonlocal last_tdc_warning_time, first_pixel_time
    #     nonlocal local_accumulator

    #     current_time = time.time()

    #     # ---- STEP 1: Separate packet types (single pass) ----
    #     pixels = []
    #     tdcs = []

    #     for packet in new_data:
    #         if isinstance(packet, PixelPacket):
    #             pixels.append(packet)
    #         elif isinstance(packet, TDCPacket):
    #             tdcs.append(packet)
    #         # Control packets ignored for binning

    #     # ---- STEP 2: Process TDC packets (order matters for t_zero) ----
    #     for packet in tdcs:
    #         matches_channel = (tdc_ch == 0) or (packet.channel == tdc_ch)
    #         matches_edge = (packet.edge == target_edge)

    #         if matches_channel and matches_edge:
    #             # Check if we need to flush
    #             if cycle_count > 0 and cycle_count % flush_every_n_cycles == 0:
    #                 with xyt_lock:
    #                     # Merge local accumulator into main array
    #                     xyt_array += local_accumulator
    #                     array_copy = xyt_array.copy()
    #                     xyt_array.fill(0)

    #                 # Clear local accumulator
    #                 local_accumulator.fill(0)

    #                 cycles_in_flush = flush_every_n_cycles
    #                 flush_count += 1

    #                 flush_metadata = {
    #                     'cycles_in_flush': cycles_in_flush,
    #                     'total_cycles': cycle_count,
    #                     'flush_number': flush_count,
    #                     'pixels_discarded_before_trigger': pixels_before_trigger,
    #                     'pixels_discarded_outside_window': pixels_outside_window,
    #                 }

    #                 try:
    #                     xyt_queue.put_nowait((array_copy, flush_metadata))
    #                     logger.info(f"Flushed: #{flush_count}, cycles={cycles_in_flush}")
    #                     pixels_before_trigger = 0
    #                     pixels_outside_window = 0
    #                 except queue.Full:
    #                     logger.warning("Processing queue full, dropping array")

    #             t_zero = packet.timestamp
    #             cycle_count += 1
    #             last_tdc_warning_time = current_time

    #     # ---- STEP 3: Process Pixel packets (vectorized) ----
    #     if not pixels:
    #         return

    #     if first_pixel_time is None:
    #         first_pixel_time = current_time

    #     # TDC timeout warning
    #     if (first_pixel_time is not None and
    #         last_tdc_warning_time is None and
    #         current_time - first_pixel_time > 10.0):
    #         logger.warning("No matching TDC triggers received in 10s")
    #         last_tdc_warning_time = current_time

    #     # No t_zero yet - count and discard all pixels
    #     if t_zero is None:
    #         pixels_before_trigger += len(pixels)
    #         return

    #     event_count += len(pixels)

    #     # ---- VECTORIZED PIXEL PROCESSING ----
    #     n_pixels = len(pixels)

    #     # Extract arrays from packet objects
    #     x_arr = np.empty(n_pixels, dtype=np.int32)
    #     y_arr = np.empty(n_pixels, dtype=np.int32)
    #     ts_arr = np.empty(n_pixels, dtype=np.int64)

    #     for i, p in enumerate(pixels):
    #         x_arr[i] = p.x
    #         y_arr[i] = p.y
    #         ts_arr[i] = p.timestamp

    #     # Vectorized time calculation
    #     t_relative = ts_arr - t_zero

    #     # Vectorized bounds check
    #     valid_mask = (t_relative >= 0) & (t_relative < t_cycle_ticks)
    #     n_outside = n_pixels - np.sum(valid_mask)

    #     if n_outside > 0:
    #         pixels_outside_window += n_outside
    #         if verbose and n_outside > 100:
    #             logger.warning(f"Batch: {n_outside}/{n_pixels} pixels outside window")

    #     # Filter to valid pixels only
    #     x_valid = x_arr[valid_mask]
    #     y_valid = y_arr[valid_mask]
    #     t_valid = t_relative[valid_mask]

    #     if len(x_valid) == 0:
    #         return

    #     # Vectorized bin calculation
    #     time_bins = (t_valid / t_delta_ticks).astype(np.int32)

    #     # Clip bins to valid range (safety check)
    #     np.clip(time_bins, 0, n_bins - 1, out=time_bins)

    #     # ---- ACCUMULATE WITHOUT LOCK (into local array) ----
    #     if collapse_y:
    #         np.add.at(local_accumulator, (x_valid, time_bins), 1)
    #     else:
    #         np.add.at(local_accumulator, (x_valid, y_valid, time_bins), 1)

    # def data_callback(new_data) -> None:
    #     """Time-resolved binning with TDC triggers."""
    #     nonlocal event_count, t_zero, cycle_count, flush_count
    #     nonlocal pixels_before_trigger, pixels_outside_window
    #     nonlocal last_tdc_warning_time, first_pixel_time

    #     current_time = time.time()

    #     for packet in new_data:
    #         # Handle TDC packets
    #         if isinstance(packet, TDCPacket):
    #             # Check if this TDC matches our criteria
    #             matches_channel = (tdc_ch == 0) or (packet.channel == tdc_ch)
    #             matches_edge = (packet.edge == target_edge)

    #             if matches_channel and matches_edge:
    #                 # Flush x, y, t accumulator array if needed
    #                 if cycle_count > 0 and cycle_count % flush_every_n_cycles == 0:
    #                     with xyt_lock:
    #                         array_copy = xyt_array.copy()
    #                         xyt_array.fill(0)

    #                     # Calculate cycles in this flush
    #                     cycles_in_flush = flush_every_n_cycles
    #                     flush_count += 1

    #                     # Create per-flush metadata
    #                     flush_metadata = {
    #                         'cycles_in_flush': cycles_in_flush,
    #                         'total_cycles': cycle_count,
    #                         'flush_number': flush_count,
    #                         'pixels_discarded_before_trigger': pixels_before_trigger,
    #                         'pixels_discarded_outside_window': pixels_outside_window,
    #                     }

    #                     try:
    #                         xyt_queue.put_nowait((array_copy, flush_metadata))
    #                         logger.info(f"Flushed x, y, t array: flush #{flush_count}, "
    #                                   f"cycles={cycles_in_flush}, total_cycles={cycle_count}, "
    #                                   f"(discarded: {pixels_before_trigger} before trigger, "
    #                                   f"{pixels_outside_window} outside window)")
    #                         pixels_before_trigger = 0
    #                         pixels_outside_window = 0
    #                     except queue.Full:
    #                         logger.warning("Processing queue full, dropping array")

    #                 # Update t_zero and increment cycle count
    #                 t_zero = packet.timestamp
    #                 cycle_count += 1
    #                 last_tdc_warning_time = current_time

    #         # Handle Pixel packets
    #         elif isinstance(packet, PixelPacket):
    #             if first_pixel_time is None:
    #                 first_pixel_time = current_time

    #             event_count += 1

    #             # Check for TDC timeout warning (10s after first pixel)
    #             if (first_pixel_time is not None and
    #                 last_tdc_warning_time is None and
    #                 current_time - first_pixel_time > 10.0):
    #                 logger.warning("No matching TDC triggers received in 10s after first pixel")
    #                 last_tdc_warning_time = current_time  # Prevent repeated warnings

    #             # Discard pixels before first TDC
    #             if t_zero is None:
    #                 pixels_before_trigger += 1
    #                 continue

    #             # Calculate relative time in ticks
    #             t_relative_ticks = packet.timestamp - t_zero

    #             # Check if within time window
    #             if t_relative_ticks < 0 or t_relative_ticks >= t_cycle_ticks:
    #                 pixels_outside_window += 1
    #                 if verbose:
    #                     t_relative_ps = t_relative_ticks * TIMESTAMP_PS_PER_TICK
    #                     logger.warning(f"Pixel outside time window: t_relative={t_relative_ps:.1f} ps "
    #                                  f"(window: 0 to {t_cycle} ps)")
    #                 continue

    #             # Calculate time bin and update x, y, t accumulator
    #             time_bin = int(t_relative_ticks / t_delta_ticks)

    #             # Bounds check (shouldn't happen but be safe)
    #             if 0 <= time_bin < n_bins:
    #                 with xyt_lock:
    #                     if collapse_y:
    #                         xyt_array[packet.x, time_bin] += 1
    #                     else:
    #                         xyt_array[packet.x, packet.y, time_bin] += 1

    # Set the callback
    server.set_data_callback(data_callback_np)
    # server.set_data_callback(data_callback_vectorized)
    # server.set_data_callback(data_callback)

    try:
        # Start the server
        print(f"Starting server on localhost:{port}")
        server.start()

        # Server is now ready for connections
        heartbeat.set_state(ServerState.READY)

        # Wait so the console can be read and give ZMQ subscribers time to connect
        # This helps with the "slow joiner" problem where start messages might be missed
        wait_after_start = 2
        print(f"\nWaiting for {wait_after_start} seconds for subscribers to connect...")
        time.sleep(wait_after_start)

        # Keep the main thread alive and show overall stats
        start_time = time.time()
        last_stats_time = start_time
        last_total_data_points = 0
        unknown_count = 0

        # Session tracking (can be reset)
        session_start_time = time.time()
        session_start_count = 0
        session_end_time = time.time()
        reset_event = threading.Event()
        print_event = threading.Event()

        # Start input listener thread (only if not in exit_on_disconnect mode)
        input_thread = None
        if not exit_on_disconnect:
            input_thread = threading.Thread(
                target=input_listener,
                args=(server, reset_event, print_event, stop_event),
                daemon=True,
            )
            input_thread.start()

        # Track client connection state for heartbeat updates
        was_client_connected = False
        stop_message_sent_on_disconnect = False  # Track if we already sent stop on disconnect

        while server.running:
            # Check if we should exit due to client disconnect
            if exit_on_disconnect and server.client_disconnected_event.is_set():
                logger.info("Client disconnected, initiating shutdown...")
                # Send stop message before shutdown
                if message_queue is not None and start_message_sent and not stop_message_sent_on_disconnect:
                    acquisition_duration = (time.time() - acquisition_start_time) if acquisition_start_time else 0.0
                    stop_msg = TimePixStop(
                        scan_name=scan_name,
                        total_flushes=flush_count,
                        total_cycles=cycle_count,
                        total_packets=event_count,
                        acquisition_duration_s=acquisition_duration,
                        pixels_discarded_before_trigger=int(pixels_before_trigger),
                        pixels_discarded_outside_window=int(pixels_outside_window),
                    )
                    try:
                        message_queue.put_nowait(stop_msg.model_dump())
                        logger.info(f"Queued stop message (client disconnect) for scan: {scan_name}")
                        print(f"Queued stop message (client disconnect) for scan: {scan_name}")
                        stop_message_sent_on_disconnect = True
                        time.sleep(0.5)  # Give worker time to send
                    except queue.Full:
                        logger.warning("Message queue full, dropping stop message")
                break

            # Update heartbeat state based on client connection
            if server.client_connected and not was_client_connected:
                heartbeat.set_state(ServerState.STREAMING)
                was_client_connected = True
                # Reset stop message flag when new client connects
                stop_message_sent_on_disconnect = False
                # Reset acquisition state for new client connection (new acquisition)
                start_message_sent = False
                acquisition_start_time = None
                scan_name = generate_scan_name()
                # Reset counters for new acquisition
                cycle_count = 0
                flush_count = 0
                event_count = 0
                pixels_before_trigger = 0
                pixels_outside_window = 0
                t_zero = None
                first_pixel_time = None
                # Clear arrays for fresh start
                with xyt_lock:
                    xyt_array.fill(0)
                local_accumulator.fill(0)
                logger.info(f"New client connected - resetting acquisition state. New scan: {scan_name}")
                print(f"New client connected - resetting acquisition state. New scan: {scan_name}")
            elif not server.client_connected and was_client_connected:
                # Client just disconnected - send stop message
                heartbeat.set_state(ServerState.READY)
                was_client_connected = False

                # Send stop message when client disconnects (even without --exit-on-disconnect)
                if message_queue is not None and start_message_sent and not stop_message_sent_on_disconnect:
                    acquisition_duration = (time.time() - acquisition_start_time) if acquisition_start_time else 0.0
                    stop_msg = TimePixStop(
                        scan_name=scan_name,
                        total_flushes=flush_count,
                        total_cycles=cycle_count,
                        total_packets=event_count,
                        acquisition_duration_s=acquisition_duration,
                        pixels_discarded_before_trigger=int(pixels_before_trigger),
                        pixels_discarded_outside_window=int(pixels_outside_window),
                    )
                    try:
                        message_queue.put_nowait(stop_msg.model_dump())
                        logger.info(f"Queued stop message (client disconnected) for scan: {scan_name}")
                        print(f"Queued stop message (client disconnected) for scan: {scan_name}")
                        stop_message_sent_on_disconnect = True
                        time.sleep(0.5)  # Give worker time to send
                    except queue.Full:
                        logger.warning("Message queue full, dropping stop message")

            time.sleep(1)
            current_time = time.time()

            # Show/ update overall stats
            if current_time - last_stats_time >= stats_update_time:
                queue_size = server.get_queue_size()
                callback_buffer_size = server.get_callback_buffer_size()
                current_mem = process.memory_info().rss / 1024**3
                uptime = current_time - start_time
                current_total_data_points = event_count
                unknown_count = server.get_unknown_packet_count()
                rate = current_total_data_points / uptime
                rate_str = f"{rate:.3e}"

                # Check for reset command
                if reset_event.is_set():
                    session_start_time = None  # Will be set on next packet
                    session_start_count = current_total_data_points
                    session_end_time = None
                    reset_event.clear()

                # Check for timing print command
                if print_event.is_set():
                    print(f"TDC channel: {tdc_ch} ({'both' if tdc_ch == 0 else f'ch{tdc_ch}'}) (edge: {tdc_edge})")
                    print(f"TDC frequency: {tdc_frequency:.3e} Hz (time cycle: {t_cycle/1e12:.3e} s)")
                    print(f"Time bin width: {t_delta/1E12:.3e} s (# of bins: {n_bins:.3e})")
                    print(f"3D array (x,y,t): {array_size_gb:.3f} GB [{xyt_array.shape}]")
                    print(f"                     (flushed every {flush_interval} s ({flush_every_n_cycles:.3e} cycles)")
                    print()
                    input("Press ENTER to continue...")
                    print_event.clear()

                # Set session start time when first packet arrives after reset
                if session_start_time is None and current_total_data_points > session_start_count:
                    session_start_time = current_time

                # Update session end time whenever new packets arrive
                if current_total_data_points > last_total_data_points:
                    session_end_time = current_time

                # Calculate session stats (since last reset)
                session_packet_count = current_total_data_points - session_start_count
                if session_start_time is not None and session_packet_count > 0:
                    session_duration = session_end_time - session_start_time
                    session_rate = session_packet_count / session_duration if session_duration > 0 else 0
                    session_rate_str = f"{session_rate:.3e}"
                else:
                    session_duration = 0
                    session_rate_str = "N/A"

                # Build info string based on mode
                if exit_on_disconnect:
                    info = "Running in orchestrated mode (will exit on client disconnect)\n"
                else:
                    info = (
                        "Press Ctrl+C to stop the server\n"
                        "Type 'r' to reset session stats\n"
                        "Type 'p' to print timing settings\n"
                    )

                # Get stats
                xyt_queue_size = xyt_queue.qsize()
                xyt_queue_max = xyt_queue.maxsize
                xyt_queue_str = f"{xyt_queue_size} / {xyt_queue_max}"

                stats = (
                    f"Server uptime: {uptime:.0f}s\n"
                    f"Server memory: {current_mem:.3f} GB\n"
                    f"Total packets: {current_total_data_points:.3e}\n"
                    f"Overall rate: {rate_str} packets/s\n"
                    f"Unknown packets: {unknown_count}\n"
                    f"[queue] messages: {queue_size} / {server.buffer_size}\n"
                    f"[buffer] callback: {callback_buffer_size} / {callback_batch_size}\n"
                    f"[queue] x,y,t 3D: {xyt_queue_str}\n"
                    f"Flushes: {flush_count} (cycles: {cycle_count})\n"
                )

                session_stats = (
                    f"Session duration: {session_duration:.3f}s\n"
                    f"Session packets: {session_packet_count:.3e}\n"
                    f"Session rate: {session_rate_str} packets/s\n"
                )

                # Update last event time
                last_stats_time = current_time
                # Update number of total data points
                last_total_data_points = event_count

                # Clear the terminal and print the stats
                os.system("cls" if os.name == "nt" else "clear")
                print(info)
                print()
                print("Overall Stats")
                print("-" * 30)
                print(stats)
                print()
                print("Session Stats")
                print("-" * 30)
                print(session_stats)

                # Show recent errors from ring buffer (only if verbose)
                if verbose:
                    recent_errors = ring_handler.get_logs()
                    if recent_errors:
                        print()
                        print(f"Recent errors (last {len(recent_errors)}):\n")
                        for err in recent_errors:
                            print(f"  {err}")

                    # Show valid packet samples (only if verbose)
                    valid_samples = server.get_valid_packet_samples()
                    if valid_samples:
                        print()
                        print(f"Recent valid packets (last {len(valid_samples)}):\n")
                        for sample in valid_samples:
                            print(f"  {sample}")

    except KeyboardInterrupt:
        print("\nShutting down server...")

    finally:
        # Send stop message (only if we haven't already sent it on client disconnect)
        if message_queue is not None and not stop_message_sent_on_disconnect:
            acquisition_duration = (time.time() - acquisition_start_time) if acquisition_start_time else 0.0
            stop_msg = TimePixStop(
                scan_name=scan_name,
                total_flushes=flush_count,
                total_cycles=cycle_count,
                total_packets=event_count,
                acquisition_duration_s=acquisition_duration,
                pixels_discarded_before_trigger=int(pixels_before_trigger),
                pixels_discarded_outside_window=int(pixels_outside_window),
            )
            try:
                message_queue.put_nowait(stop_msg.model_dump())
                logger.info(f"Queued stop message for scan: {scan_name}")
                print(f"Queued stop message for scan: {scan_name}")  # Also print to console
                # Give worker time to send the stop message
                time.sleep(0.5)  # Increased wait time
            except queue.Full:
                logger.warning("Message queue full, dropping stop message")
                print("WARNING: Message queue full, dropping stop message")

        # Signal all threads to stop
        stop_event.set()

        # Stop heartbeat
        heartbeat.stop()

        # Stop the server (closes sockets, stops threads)
        server.stop()

        # Wait for worker thread
        if worker_thread.is_alive():
            logger.info("Waiting for worker thread to finish...")
            worker_thread.join(timeout=10)
            if worker_thread.is_alive():
                logger.warning("Worker thread did not finish in time")

        # Wait for input listener thread (if it was started)
        if input_thread and input_thread.is_alive():
            logger.info("Waiting for input listener to finish...")
            input_thread.join(timeout=2)
            if input_thread.is_alive():
                logger.warning("Input listener did not finish in time")

        print("Server stopped successfully")


if __name__ == "__main__":
    app()
