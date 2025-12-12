"""
Production implementation of the SocketDataServer.

This script demonstrates how to set up and use the multi-threaded socket server
that reads (simulated) TimePix3 messages and processes them into numpy arrays.
"""

import time
import os
import logging
import psutil
import math
import numpy as np
import threading
import queue

from splash_timepix.socket_server import SocketDataServer, RingBufferHandler
from splash_timepix.parser import PixelPacket, TDCPacket, TDCEdge
from splash_timepix.simulator import SimulatorConfig
from splash_timepix.workers import input_listener, plotting_worker, zmq_worker

import typer
app = typer.Typer()

# Constants from parser for timestamp conversion
TIMESTAMP_CLOCK_MHZ = 3840
TIMESTAMP_PS_PER_TICK = 260.41666  # 1 / 3840 MHz in picoseconds

# Module-level logger
logger = logging.getLogger(__name__)


@app.command()
def main(host: str = "localhost",
         port: int = 9090,
         buffer_size: int = 1000,
         callback_batch_size: int = 10000,
         stats_update_time: int = 2,
         plot: bool = False,
         verbose: bool = False,
         zmq_port: int = 5657,
         tdc_ch: int = 0,
         tdc_edge: str = "rising",
         tdc_frequency: float = 1E0,
         t_delta_ns: float = -1,
         flush_interval: float = 5.0):
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
        flush_interval: Time between array flushes in seconds (default: 1)
    """
    os.system('cls' if os.name == 'nt' else 'clear')
    print("Starting TimPix3 Streaming Application")
    print("=" * 50)
    print()

    # Calculate binning and display parameters from user inputs
    t_cycle = (1.0 / tdc_frequency) * 1e12  # seconds → picoseconds
    t_cycle_ticks = t_cycle / TIMESTAMP_PS_PER_TICK
    if t_delta_ns > 0: # user passed a value
        t_delta = t_delta_ns * 1e3  # nanoseconds → picoseconds
        t_delta_ticks = t_delta / TIMESTAMP_PS_PER_TICK
        n_bins = math.ceil(t_cycle_ticks / t_delta_ticks)
    else: # default case
        n_bins = 2000 # using this default number of bins calculate pars
        t_delta = t_cycle / n_bins # time bin width in picoseconds
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
    ring_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    
    # Get the socket_server logger and add our handler to it
    socket_logger = logging.getLogger('splash_timepix.socket_server')
    socket_logger.addHandler(ring_handler)

    # Create the server
    server = SocketDataServer(
        host=host, 
        port=port, 
        buffer_size=buffer_size, 
        debug=verbose,  # verbose enables packet buffer
        callback_batch_size=callback_batch_size
    )

    # Create stop event for input_listener
    stop_event = threading.Event()

    # Create processing queue
    xyt_queue = queue.Queue(maxsize=10)
    # Choose worker based on plot flag
    if plot:
        worker_function = plotting_worker
        worker_args = (xyt_queue, stop_event)
        logger.info("🎨 Starting plotting worker")
    else:
        worker_function = zmq_worker
        worker_args = (xyt_queue, stop_event, zmq_port)
        logger.info("📡 Starting ZMQ worker")
    
    worker_thread = threading.Thread(
        target=worker_function, 
        args=worker_args, 
        daemon=True
    )
    worker_thread.start()

    # Define x, y, t accumulator array
    config = SimulatorConfig()
    detector_size_x = config.detector_size_x
    detector_size_y = config.detector_size_y
    xyt_array = np.zeros((detector_size_x, detector_size_y, n_bins), dtype=np.uint32)
    xyt_lock = threading.Lock()
    
    # Calculate flush cycles from interval and frequency
    flush_every_n_cycles = max(1, int(flush_interval * tdc_frequency))
    
    # Errors and warnings
    if tdc_frequency < 0.1:
        logger.error(f"❌ Low TDC frequency ({tdc_frequency} Hz) → detector may miss TDC events")
        return
    if flush_interval < (1.0 / tdc_frequency):
        logger.warning(f"⚠️  Flush interval ({flush_interval}s) < TDC period ({1.0/tdc_frequency:.2f}s)")
        logger.warning(f"   Will flush every TDC cycle (flush_every_n_cycles = 1)")

    # Memory check
    array_size_gb = xyt_array.nbytes / (1024**3)

    if array_size_gb > 1.0:
        suggested_bins = int(n_bins * 1.0 / array_size_gb)
        suggested_t_delta_ns = t_delta_ns * (n_bins / suggested_bins)
        logger.warning(f"⚠️  Large array: {array_size_gb:.2f} GB")
        logger.warning(f"💡 Suggestion: use --t-delta-ns {suggested_t_delta_ns:.1f} to reduce to ~1 GB")
    elif array_size_gb > 5.0:
        logger.error(f"❌ Array too large ({array_size_gb:.2f} GB)! Increase t_delta_ns or decrease TDC frequency")
        return
    
    # Log final configuration
    logger.info(f"📊 TDC: {tdc_frequency} Hz → t_cycle = {t_cycle:.3e} ps ({t_cycle/1e12:.3f} s)")
    logger.info(f"⏱️  Time bins: {n_bins} bins × {t_delta_ns} ns = {t_cycle/1e12:.3f} s total")
    logger.info(f"💾 Array size: {array_size_gb:.3f} GB per flush")
    logger.info(f"🔄 Flush: every {flush_every_n_cycles} cycles ({flush_interval} s)")
    
    # Monitor memory usage of the server
    process = psutil.Process(os.getpid())
    print(f"Monitoring Python server process (PID: {os.getpid()})")

    # Set up a callback to handle new data
    event_count = 0
    
    # Time-resolved binning callback
    # State variables
    t_zero = None
    cycle_count = 0
    pixels_before_trigger = 0
    pixels_outside_window = 0
    last_tdc_warning_time = None
    first_pixel_time = None
    
    # Convert edge string to enum value
    target_edge = 0 if tdc_edge.lower() == "rising" else 1
    
    def data_callback(new_data) -> None:
        """Time-resolved binning with TDC triggers."""
        nonlocal event_count, t_zero, cycle_count
        nonlocal pixels_before_trigger, pixels_outside_window
        nonlocal last_tdc_warning_time, first_pixel_time
        
        current_time = time.time()
        
        for packet in new_data:
            # Handle TDC packets
            if isinstance(packet, TDCPacket):
                # Check if this TDC matches our criteria
                matches_channel = (tdc_ch == 0) or (packet.channel == tdc_ch)
                matches_edge = (packet.edge == target_edge)
                
                if matches_channel and matches_edge:
                    # Flush x, y, t accumulator array if needed
                    if cycle_count > 0 and cycle_count % flush_every_n_cycles == 0:
                        with xyt_lock:
                            array_copy = xyt_array.copy()
                            xyt_array.fill(0)
                        
                        try:
                            xyt_queue.put_nowait(array_copy)
                            logger.info(f"🔄 Flushed x, y, t array after {cycle_count} cycles "
                                      f"(discarded before trigger: {pixels_before_trigger}, "
                                      f"outside window: {pixels_outside_window})")
                            pixels_before_trigger = 0
                            pixels_outside_window = 0
                        except queue.Full:
                            logger.warning("Processing queue full, dropping array")
                    
                    # Update t_zero and increment cycle count
                    t_zero = packet.timestamp
                    cycle_count += 1
                    last_tdc_warning_time = current_time
            
            # Handle Pixel packets
            elif isinstance(packet, PixelPacket):                   
                if first_pixel_time is None:
                    first_pixel_time = current_time
                
                event_count += 1
                
                # Check for TDC timeout warning (10s after first pixel)
                if (first_pixel_time is not None and 
                    last_tdc_warning_time is None and 
                    current_time - first_pixel_time > 10.0):
                    logger.warning("⚠️  No matching TDC triggers received in 10s after first pixel")
                    last_tdc_warning_time = current_time  # Prevent repeated warnings
                
                # Discard pixels before first TDC
                if t_zero is None:
                    pixels_before_trigger += 1
                    continue
                
                # Calculate relative time in ticks
                t_relative_ticks = packet.timestamp - t_zero
                
                # Check if within time window
                if t_relative_ticks < 0 or t_relative_ticks >= t_cycle_ticks:
                    pixels_outside_window += 1
                    if verbose:
                        t_relative_ps = t_relative_ticks * TIMESTAMP_PS_PER_TICK
                        logger.warning(f"Pixel outside time window: t_relative={t_relative_ps:.1f} ps "
                                     f"(window: 0 to {t_cycle} ps)")
                    continue
                
                # Calculate time bin and update x, y, t accumulator
                time_bin = int(t_relative_ticks / t_delta_ticks)
                
                # Bounds check (shouldn't happen but be safe)
                if 0 <= time_bin < n_bins:
                    with xyt_lock:
                        xyt_array[packet.x, packet.y, time_bin] += 1

    # Set the callback
    server.set_data_callback(data_callback)

    try:
        # Start the server
        print(f"🚀 Starting server on localhost:{port}")
        server.start()

        # Wait so the console can be read
        wait_after_start = 1
        print(f"\n⏱️  Waiting for {wait_after_start} seconds...")
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
        
        # Start input listener thread
        input_thread = threading.Thread(
            target=input_listener, 
            args=(server, reset_event, print_event, stop_event), 
            daemon=True
        )
        input_thread.start()

        while True:
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
                    print(f"📻 TDC channel: {tdc_ch} ({'both' if tdc_ch == 0 else f'ch{tdc_ch}'}) (edge: {tdc_edge})")
                    print(f"〰 TDC frequency: {tdc_frequency:.3e} Hz (time cycle: {t_cycle/1e12:.3e} s)")
                    print(f"↔️  Time bin width: {t_delta/1E12:.3e} s (# of bins: {n_bins:.3e})")
                    print(f"📼 3D array (x,y,t): {array_size_gb:.3f} GB [{xyt_array.shape}]")
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
                
                info = (
                    f"🛑 Press Ctrl+C to stop the server\n"
                    f"🔄 Type 'r' to reset session stats\n"
                    f"⚙️  Type 'p' to print timing settings\n"
                )
                
                # Get stats
                xyt_queue_size = xyt_queue.qsize()
                xyt_queue_max = xyt_queue.maxsize
                xyt_queue_str = f"{xyt_queue_size} / {xyt_queue_max}"
                
                stats = (
                    f"⏱️  Server uptime: {uptime:.0f}s\n"
                    f"💾 Server memory: {current_mem:.3f} GB\n"
                    f"📊 Total packets: {current_total_data_points:.3e}\n"
                    f"📈 Overall rate: {rate_str} packets/s\n"
                    f"⚠️  Unknown packets: {unknown_count}\n"
                    f"📦 [queue] messages: {queue_size} / {server.buffer_size}\n"
                    f"📝 [buffer] callback: {callback_buffer_size} / {callback_batch_size}\n"
                    f"📊 [queue] x,y,t 3D: {xyt_queue_str}\n"
                )
                
                session_stats = (
                    f"⏱️  Session duration: {session_duration:.3f}s\n"
                    f"📊 Session packets: {session_packet_count:.3e}\n"
                    f"📈 Session rate: {session_rate_str} packets/s\n"
                )

                # Update last event time
                last_stats_time = current_time
                # Update number of total data points
                last_total_data_points = event_count

                # Clear the terminal and print the stats
                os.system('cls' if os.name == 'nt' else 'clear')
                print(info)
                print()
                print(f"Overall Stats")
                print("-" * 30)
                print(stats)
                print()
                print(f"Session Stats")
                print("-" * 30)
                print(session_stats)
                
                # Show recent errors from ring buffer (only if verbose)
                if verbose:
                    recent_errors = ring_handler.get_logs()
                    if recent_errors:
                        print()
                        print(f"⚠️  Recent errors (last {len(recent_errors)}):\n")
                        for err in recent_errors:
                            print(f"  {err}")

                    # Show valid packet samples (only if verbose)
                    valid_samples = server.get_valid_packet_samples()
                    if valid_samples:
                        print()
                        print(f"✅ Recent valid packets (last {len(valid_samples)}):\n")
                        for sample in valid_samples:
                            print(f"  {sample}")


    except KeyboardInterrupt:
        print("\n🛑 Shutting down server...")
        
        # Signal all threads to stop
        stop_event.set()
        
        # Stop the server (closes sockets, stops threads)
        server.stop()
        
        # Wait for worker thread
        if worker_thread.is_alive():
            logger.info("Waiting for worker thread to finish...")
            worker_thread.join(timeout=10)
            if worker_thread.is_alive():
                logger.warning("Worker thread did not finish in time")
        
        # Wait for input listener thread
        if input_thread.is_alive():
            logger.info("Waiting for input listener to finish...")
            input_thread.join(timeout=2)
            if input_thread.is_alive():
                logger.warning("Input listener did not finish in time")
        
        print("✅ Server stopped successfully")


if __name__ == "__main__":
    app()
