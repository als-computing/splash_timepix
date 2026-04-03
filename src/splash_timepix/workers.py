"""Worker threads for processing TimePix3 data streams.

This module provides worker thread functions that consume processed data
from the socket server and either visualize it or publish it downstream.
"""

import logging
import os
import queue
import select
import sys
import time
from typing import Optional

import cv2
import msgpack
import numpy as np
import zmq

logger = logging.getLogger(__name__)


def input_listener(server, reset_event, print_event, stop_event):
    """Background thread to listen for user commands.

    Monitors stdin for interactive commands during server operation.
    Supports commands:
    - 'r': Reset session statistics
    - 'p': Print timing configuration

    Args:
        server: SocketDataServer instance (currently unused, available for future commands)
        reset_event: Threading event to signal stats reset
        print_event: Threading event to signal config print request
        stop_event: Threading event to signal shutdown

    Note:
        Uses select.select() which is Unix-specific. Will not work on Windows.
    """
    while not stop_event.is_set():
        try:
            # Non-blocking check if input is available (Unix only)
            # Timeout of 0.5 seconds allows checking stop_event regularly
            ready, _, _ = select.select([sys.stdin], [], [], 0.5)

            if ready:
                cmd = input().strip().lower()
                if cmd == "r":
                    reset_event.set()
                    os.system("clear")
                    print("Resetting session stats...")
                elif cmd == "p":
                    print_event.set()
                    os.system("clear")
                    print("Printing timing settings...\n")
        except EOFError:
            break
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Error in input listener: {e}")
            break

    logger.info("Input listener thread finished")


def plotting_worker(xyt_queue, stop_event):
    """Worker thread that visualizes accumulated 3D arrays as heatmaps.

    Dequeues arrays of shape (x, y, t) from the processing queue and displays
    them as 2D heatmaps where:
    - X-axis: detector x-coordinate (horizontal)
    - Y-axis: time bin index (vertical, with bin 0 at top)
    - Color: number of events in each (x, t) bin

    The y-coordinate of the detector is summed over (collapsed).

    Note: Now expects tuples of (array, flush_metadata) from queue, but only
    uses the array for plotting.

    Args:
        xyt_queue: Thread-safe queue containing (array, metadata) tuples
        stop_event: Threading event to signal shutdown

    Interactive Controls:
        Press 'q' in the plot window to close and stop processing
    """
    logger.info("Plotting worker thread started")

    window_name = "Time-Resolved Heatmap (X vs Time)"
    display_height = 400
    display_width = 600

    try:
        # Create window with fixed size
        cv2.namedWindow(window_name, cv2.WINDOW_AUTOSIZE)

        while not stop_event.is_set():
            try:
                # Unpack the format (array, metadata)
                array_data, flush_metadata = xyt_queue.get(timeout=0.1)

                logger.info(
                    f"Plotting worker received array: shape={array_data.shape}, "
                    f"total={np.sum(array_data)}, flush_meta={flush_metadata}"
                )

                # Sum over y-axis: (x, y, t) -> (x, t)
                heatmap_data = np.sum(array_data, axis=1)

                # Transpose for display: (t, x)
                # Rows = time bins (top to bottom), Cols = x position (left to right)
                heatmap_data = heatmap_data.T

                # Flip vertically so time bin 0 is at bottom
                heatmap_data = np.flipud(heatmap_data)

                # Calculate stats
                total_counts = np.sum(heatmap_data)
                max_counts = np.max(heatmap_data)

                logger.info(f"Heatmap stats: total={total_counts}, max={max_counts}")

                # Normalize to 0-255 for uint8 display
                if max_counts > 0:
                    normalized = (heatmap_data / max_counts * 255).astype(np.uint8)
                else:
                    normalized = np.zeros_like(heatmap_data, dtype=np.uint8)

                # Apply VIRIDIS colormap
                colored = cv2.applyColorMap(normalized, cv2.COLORMAP_VIRIDIS)

                # Resize to fixed display size
                colored = cv2.resize(
                    colored,
                    (display_width, display_height),
                    interpolation=cv2.INTER_NEAREST,
                )

                # Add stats overlay
                font = cv2.FONT_HERSHEY_SIMPLEX
                font_scale = 0.7
                font_thickness = 2
                text_color = (255, 255, 255)  # White
                shadow_color = (0, 0, 0)  # Black shadow for readability

                # Total counts
                text = f"Total counts: {total_counts}"
                position = (10, 30)
                cv2.putText(
                    colored,
                    text,
                    (position[0] + 1, position[1] + 1),
                    font,
                    font_scale,
                    shadow_color,
                    font_thickness + 1,
                )
                cv2.putText(
                    colored,
                    text,
                    position,
                    font,
                    font_scale,
                    text_color,
                    font_thickness,
                )

                # Max counts
                text = f"Max counts/bin: {max_counts}"
                position = (10, 60)
                cv2.putText(
                    colored,
                    text,
                    (position[0] + 1, position[1] + 1),
                    font,
                    font_scale,
                    shadow_color,
                    font_thickness + 1,
                )
                cv2.putText(
                    colored,
                    text,
                    position,
                    font,
                    font_scale,
                    text_color,
                    font_thickness,
                )

                # Array shape
                text = f"Shape: {array_data.shape}"
                position = (10, 90)
                cv2.putText(
                    colored,
                    text,
                    (position[0] + 1, position[1] + 1),
                    font,
                    font_scale,
                    shadow_color,
                    font_thickness + 1,
                )
                cv2.putText(
                    colored,
                    text,
                    position,
                    font,
                    font_scale,
                    text_color,
                    font_thickness,
                )

                # Flush info if available
                if flush_metadata:
                    flush_num = flush_metadata.get("flush_number", "?")
                    cycles = flush_metadata.get("cycles_in_flush", "?")
                    text = f"Flush #{flush_num} ({cycles} cycles)"
                    position = (10, 120)
                    cv2.putText(
                        colored,
                        text,
                        (position[0] + 1, position[1] + 1),
                        font,
                        font_scale,
                        shadow_color,
                        font_thickness + 1,
                    )
                    cv2.putText(
                        colored,
                        text,
                        position,
                        font,
                        font_scale,
                        text_color,
                        font_thickness,
                    )

                # Display
                cv2.imshow(window_name, colored)

                xyt_queue.task_done()

            except queue.Empty:
                pass  # Normal timeout, continue
            except Exception as e:
                logger.error(f"Error in plotting worker: {e}", exc_info=True)

            # Always process GUI events and check for key press
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                logger.info("User pressed 'q', closing display")
                break

    except Exception as e:
        logger.error(f"Fatal error in plotting worker: {e}", exc_info=True)

    finally:
        # Cleanup for Qt
        logger.info("Starting window cleanup...")
        try:
            # Step 1: Destroy the specific window
            cv2.destroyWindow(window_name)

            # Step 2: Process events multiple times with small delays
            for i in range(20):
                cv2.waitKey(10)  # 10ms
                if i % 5 == 0:
                    time.sleep(0.01)  # Small sleep every 5 iterations

            # Step 3: Give Qt subsystem time to fully cleanup
            time.sleep(0.1)

            logger.info("Window cleanup complete")

        except Exception as e:
            logger.error(f"Error during window cleanup: {e}")

        logger.info("Plotting worker thread finished")


def zmq_worker(
    xyt_queue,
    stop_event,
    zmq_port: int = 5657,
    static_metadata: Optional[dict] = None,
    message_queue: Optional[queue.Queue] = None,
):
    """Worker thread that publishes accumulated 3D arrays and control messages via ZMQ PUB socket.

    Dequeues arrays of shape (x, y, t) from the processing queue and publishes
    them using a ZMQ PUB socket with msgpack serialization. Also publishes
    start/stop control messages from the message_queue.

    Data message format (multi-part):
        Part 1: Metadata (msgpack encoded dict)
            Static fields (from app.py configuration):
            - 'tdc_frequency_hz': TDC trigger frequency
            - 't_delta_ns': Time bin width in nanoseconds
            - 't_cycle_ns': Full time cycle in nanoseconds
            - 'n_bins': Number of time bins
            - 'flush_interval_s': Configured flush interval
            - 'cycles_per_flush': Expected cycles per flush
            - 'tdc_channel': TDC channel (0=both, 1, 2)
            - 'tdc_edge': TDC edge ("rising" or "falling")

            Per-flush fields:
            - 'shape': tuple of array dimensions
            - 'dtype': numpy dtype as string
            - 'timestamp': Unix timestamp (float)
            - 'array_count': Sequential counter (0-indexed)
            - 'cycles_in_flush': Actual cycles in this flush
            - 'total_cycles': Cumulative cycle count
            - 'flush_number': Sequential flush number (1-indexed)
            - 'pixels_discarded_before_trigger': Pixels before first TDC
            - 'pixels_discarded_outside_window': Pixels outside time window

        Part 2: Array data (raw bytes)

    Control message format (single-part):
        Part 1: Message dict (msgpack encoded)
            - 'msg_type': "start" or "stop"
            - Other fields depend on message type (see schemas.py)

    Subscribers can receive and reconstruct arrays using:
        metadata = msgpack.unpackb(msg[0])
        array_bytes = msg[1]
        array = np.frombuffer(array_bytes, dtype=metadata['dtype']).reshape(metadata['shape'])

    Subscribers can identify control messages by checking if msg_type is "start" or "stop"
    in the metadata dict.

    Args:
        xyt_queue: Thread-safe queue containing (array, flush_metadata) tuples
        stop_event: Threading event to signal shutdown
        zmq_port: Port number for ZMQ PUB socket (default: 5657)
        static_metadata: Dict of static configuration parameters to include
        message_queue: Optional queue for start/stop control messages (dict objects)

    Note:
        Uses non-blocking sends (DONTWAIT) to avoid blocking on slow subscribers.
        Arrays are dropped if subscribers can't keep up.
    """
    logger.info(f"ZMQ worker thread started (publishing on tcp://*:{zmq_port})")

    if static_metadata:
        logger.info(f"Static metadata: {static_metadata}")

    # Create ZMQ context and socket
    context = zmq.Context()
    socket = context.socket(zmq.PUB)

    try:
        socket.bind(f"tcp://*:{zmq_port}")

        # Set high-water mark to prevent unbounded memory growth
        socket.setsockopt(zmq.SNDHWM, 10)

        # Give subscribers time to connect (ZMQ slow joiner problem)
        # Note: Start messages sent before this sleep might be missed by late-connecting subscribers
        time.sleep(1.0)  # Increased to 1 second to help with slow joiner problem

        flush_count = 0

        while not stop_event.is_set():
            # First, check for control messages (start/stop) - higher priority
            if message_queue is not None:
                try:
                    control_message = message_queue.get_nowait()
                    # Control messages are single-part (just metadata)
                    message_bytes = msgpack.packb(control_message)
                    try:
                        socket.send(message_bytes, zmq.DONTWAIT)
                        msg_type = control_message.get("msg_type", "unknown")
                        logger.info(f"Published {msg_type} message: {control_message.get('scan_name', 'N/A')}")
                        print(
                            f"Published {msg_type} message: {control_message.get('scan_name', 'N/A')}"
                        )  # Also print to console
                    except zmq.Again:
                        msg_type = control_message.get("msg_type", "control")
                        logger.warning(f"ZMQ send would block, dropping {msg_type} message")
                        print(f"WARNING: ZMQ send would block, dropping {msg_type} message")
                    message_queue.task_done()
                    continue  # Process control message, then continue loop
                except queue.Empty:
                    pass  # No control messages, continue to data processing

            try:
                # Unpack array and per-flush metadata
                array_data, flush_metadata = xyt_queue.get(timeout=1.0)

                # Build combined metadata
                metadata = {}

                # Add message type for event messages
                metadata["msg_type"] = "event"

                # Add static metadata first
                if static_metadata:
                    metadata.update(static_metadata)

                # Add per-flush fields
                metadata["timestamp"] = time.time()
                metadata.update(flush_metadata)

                # Serialize metadata with msgpack
                metadata_bytes = msgpack.packb(metadata)

                # Get array as bytes
                array_bytes = array_data.tobytes()

                # Send multi-part message (non-blocking)
                try:
                    socket.send(metadata_bytes, zmq.SNDMORE | zmq.DONTWAIT)
                    socket.send(array_bytes, zmq.DONTWAIT)

                    flush_num = flush_metadata.get("flush_number", "?")
                    logger.info(
                        f"Published flush #{flush_num}: shape={array_data.shape}, "
                        f"total_counts={np.sum(array_data)}, "
                        f"cycles={flush_metadata.get('cycles_in_flush', '?')}, "
                        f"size={len(array_bytes)/1024/1024:.2f} MB"
                    )

                except zmq.Again:
                    logger.warning("ZMQ send would block (no subscribers or slow subscribers), dropping flush")

                flush_count += 1
                xyt_queue.task_done()

            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Error in ZMQ worker: {e}", exc_info=True)

        # stop_event can flip true while we are blocked on xyt_queue.get(); then we leave
        # the loop without running the control-message branch. Drain the queue so stop
        # reaches subscribers before the PUB socket closes.
        if message_queue is not None:
            while True:
                try:
                    control_message = message_queue.get_nowait()
                    message_bytes = msgpack.packb(control_message)
                    try:
                        socket.send(message_bytes, zmq.DONTWAIT)
                        msg_type = control_message.get("msg_type", "unknown")
                        logger.info(f"Published {msg_type} message: {control_message.get('scan_name', 'N/A')}")
                        print(f"Published {msg_type} message: {control_message.get('scan_name', 'N/A')}")
                    except zmq.Again:
                        msg_type = control_message.get("msg_type", "control")
                        logger.warning(f"ZMQ send would block, dropping {msg_type} message")
                        print(f"WARNING: ZMQ send would block, dropping {msg_type} message")
                    message_queue.task_done()
                except queue.Empty:
                    break

        logger.info(f"ZMQ worker published {flush_count} flushes total")

    except Exception as e:
        logger.error(f"Fatal error in ZMQ worker: {e}", exc_info=True)

    finally:
        # Clean shutdown
        socket.close()
        context.term()
        logger.info("ZMQ worker thread finished")
