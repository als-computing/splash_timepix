#!/usr/bin/env python3
"""
Simple ZMQ subscriber to receive and display published flushes and control messages.
Demonstrates rich metadata usage for averaging and analysis, and handling of
start/stop control messages similar to ArroyoXPS.

Modify this example to build applications receiving pre-processed TimePix3 data.
"""

import msgpack
import numpy as np
import zmq


def main():
    # Create ZMQ context and SUB socket
    context = zmq.Context()
    socket = context.socket(zmq.SUB)

    # Connect to publisher
    socket.connect("tcp://localhost:5657")

    # Subscribe to all messages (empty filter = receive everything)
    socket.setsockopt(zmq.SUBSCRIBE, b"")

    print("Subscriber connected to tcp://localhost:5657")
    print("Waiting for messages (start/stop or data flushes)...\n")

    # For running average calculation
    cumulative_sum = None
    total_cycles = 0
    current_scan_name = None

    try:
        while True:
            # Receive first part (always present)
            metadata_bytes = socket.recv()

            # Unpack metadata to check message type
            metadata = msgpack.unpackb(metadata_bytes)
            msg_type = metadata.get("msg_type")

            # Control messages (start/stop) are single-part, data messages are multi-part
            is_data_message = msg_type != "start" and msg_type != "stop"

            # Only receive second part for data messages
            array_bytes = None
            if is_data_message:
                # Set a short timeout to avoid blocking forever if message is malformed
                socket.setsockopt(zmq.RCVTIMEO, 1000)  # 1 second timeout
                try:
                    array_bytes = socket.recv()
                except zmq.Again:
                    print("WARNING: Expected second part for data message " "but none received")
                    continue
                finally:
                    # Reset timeout to infinite for next message
                    socket.setsockopt(zmq.RCVTIMEO, -1)

            if msg_type == "start":
                # Handle start message
                current_scan_name = metadata.get("scan_name", "unknown")
                print("=" * 60)
                print("START MESSAGE RECEIVED")
                print("=" * 60)
                print(f"   Scan name: {current_scan_name}")
                print(f"   TDC frequency: {metadata.get('tdc_frequency_hz', 'N/A')} Hz")
                print(f"   Time bin width: {metadata.get('t_delta_ns', 'N/A')} ns")
                print(f"   Time cycle: {metadata.get('t_cycle_ns', 'N/A')} ns")
                print(f"   Number of bins: {metadata.get('n_bins', 'N/A')}")
                detector_x = metadata.get("detector_size_x", "N/A")
                detector_y = metadata.get("detector_size_y", "N/A")
                print(f"   Detector size: {detector_x} × {detector_y}")
                print(f"   TDC channel: {metadata.get('tdc_channel', 'N/A')} (edge: {metadata.get('tdc_edge', 'N/A')})")
                print(f"   Flush interval: {metadata.get('flush_interval_s', 'N/A')} s")
                print(f"   Cycles per flush: {metadata.get('cycles_per_flush', 'N/A')}")
                print(f"   Collapse Y: {metadata.get('collapse_y', 'N/A')}")
                print("=" * 60)
                print()
                # Reset running average for new scan
                cumulative_sum = None
                total_cycles = 0
                continue

            elif msg_type == "stop":
                # Handle stop message
                print("=" * 60)
                print("STOP MESSAGE RECEIVED")
                print("=" * 60)
                print(f"   Scan name: {metadata.get('scan_name', 'unknown')}")
                print(f"   Total flushes: {metadata.get('total_flushes', 'N/A')}")
                print(f"   Total cycles: {metadata.get('total_cycles', 'N/A')}")
                print(f"   Total packets: {metadata.get('total_packets', 'N/A')}")
                print(f"   Acquisition duration: {metadata.get('acquisition_duration_s', 'N/A'):.2f} s")
                print(f"   Pixels discarded (before trigger): {metadata.get('pixels_discarded_before_trigger', 'N/A')}")
                print(f"   Pixels discarded (outside window): {metadata.get('pixels_discarded_outside_window', 'N/A')}")
                print("=" * 60)
                print()
                continue

            # Process data message (flush)
            if not is_data_message:
                # This should not happen - we already handled start/stop above
                continue

            # Reconstruct array
            shape = tuple(metadata["shape"])
            dtype = metadata["dtype"]
            array = np.frombuffer(array_bytes, dtype=dtype).reshape(shape)

            # Calculate stats
            total_counts = np.sum(array)
            max_counts = np.max(array)

            # Per-flush metadata
            flush_number = metadata.get("flush_number", 0)
            cycles_in_flush = metadata.get("cycles_in_flush", 0)
            flush_total_cycles = metadata.get("total_cycles", 0)

            print(f"Received flush #{flush_number} (scan: {current_scan_name or 'unknown'})")
            print(f"   Timestamp: {metadata['timestamp']:.3f}")
            print(f"   Size: {len(array_bytes)/1024/1024:.2f} MB")
            print()

            # Static configuration (same for all flushes in a run)
            print("   Configuration:")
            print(f"      Shape: {shape}")
            print(f"      TDC frequency: {metadata.get('tdc_frequency_hz', 'N/A')} Hz")
            print(f"      Time bin width: {metadata.get('t_delta_ns', 'N/A')} ns")
            print(f"      Time cycle: {metadata.get('t_cycle_ns', 'N/A')} ns")
            print(f"      Bins: {metadata.get('n_bins', 'N/A')}")
            print(f"      TDC channel: {metadata.get('tdc_channel', 'N/A')} (edge: {metadata.get('tdc_edge', 'N/A')})")
            print()

            print("   Flush info:")
            print(f"      Cycles in this flush: {cycles_in_flush}")
            print(f"      Total cycles so far: {flush_total_cycles}")
            print(f"      Discarded (before trigger): {metadata.get('pixels_discarded_before_trigger', 'N/A')}")
            print(f"      Discarded (outside window): {metadata.get('pixels_discarded_outside_window', 'N/A')}")
            print()

            print("   Flush stats:")
            print(f"      Total counts: {total_counts}")
            print(f"      Max counts/bin: {max_counts}")
            print()

            # Running average calculation
            if cycles_in_flush > 0:
                if cumulative_sum is None:
                    cumulative_sum = array.astype(np.float64)
                    total_cycles = cycles_in_flush
                else:
                    cumulative_sum += array.astype(np.float64)
                    total_cycles += cycles_in_flush

                # Calculate current average (per cycle)
                average = cumulative_sum / total_cycles
                avg_total = np.sum(average)
                avg_max = np.max(average)

                print(f"   Running average (over {total_cycles} cycles):")
                print(f"      Avg counts/cycle: {avg_total:.2f}")
                print(f"      Avg max/bin/cycle: {avg_max:.4f}")

            print()
            print("-" * 60)
            print()

    except KeyboardInterrupt:
        flush_count = metadata.get("flush_number", 0) if "metadata" in dir() else 0
        print(f"\nReceived {flush_count} flushes total")
        if total_cycles > 0:
            print(f"   Total cycles averaged: {total_cycles}")
        socket.close()
        context.term()


if __name__ == "__main__":
    main()
