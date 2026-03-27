#!/usr/bin/env python3
"""
Quick test script for start/stop messages.

This script subscribes to ZMQ messages and verifies that start/stop/event
messages are being sent correctly.

Usage:
    1. Start the server in another terminal:
       python -m splash_timepix.app --tdc-frequency 10 --flush-interval 1.0

    2. Run this script (start subscriber BEFORE simulator):
       # From project root:
       cd /home/gabrielgazolla/Downloads/task/splash_timepix
       python tests/test_start_stop_quick.py
       # OR from anywhere:
       python -m splash_timepix.tests.test_start_stop_quick
       # Wait for it to say "Waiting for messages..."

    3. Start the simulator in another terminal:
       python -m splash_timepix.simulator_cli
       # Then: cps 1000, tdc 10, start 10

    IMPORTANT: Start the subscriber (step 2) BEFORE the simulator (step 3)
    to ensure you receive the start message (ZMQ slow joiner problem).

    NOTE: This is a manual integration test script, not a pytest test.
    Do NOT run it with pytest - run it directly with Python.
"""

import time

import msgpack
import numpy as np
import zmq


def main():
    print("=" * 70)
    print("Testing Start/Stop Messages")
    print("=" * 70)
    print()
    print("Make sure the server and simulator are running!")
    print("Press Ctrl+C to stop")
    print()

    # Create ZMQ context and SUB socket
    context = zmq.Context()
    socket = context.socket(zmq.SUB)

    # Connect to publisher
    zmq_address = "tcp://localhost:5657"
    socket.connect(zmq_address)

    # Subscribe to all messages
    socket.setsockopt(zmq.SUBSCRIBE, b"")
    socket.setsockopt(zmq.RCVTIMEO, 5000)  # 5 second timeout

    print(f"Connected to {zmq_address}")
    print("Waiting 2 seconds for connection to stabilize (ZMQ slow joiner)...")
    time.sleep(2)  # Give ZMQ time to establish connection
    print("Waiting for messages...\n")

    # Statistics
    start_count = 0
    event_count = 0
    stop_count = 0
    current_scan_name = None
    start_time = time.time()

    try:
        while True:
            try:
                # Receive first part (metadata)
                metadata_bytes = socket.recv()
                metadata = msgpack.unpackb(metadata_bytes)
                msg_type = metadata.get("msg_type")

                # Handle different message types
                if msg_type == "start":
                    start_count += 1
                    current_scan_name = metadata.get("scan_name", "unknown")

                    print("START MESSAGE RECEIVED")
                    print(f"   Scan: {current_scan_name}")
                    print(f"   TDC frequency: {metadata.get('tdc_frequency_hz')} Hz")
                    print(f"   Detector: {metadata.get('detector_size_x')} × {metadata.get('detector_size_y')}")
                    print(f"   Time bins: {metadata.get('n_bins')} × {metadata.get('t_delta_ns')} ns")
                    print()

                elif msg_type == "stop":
                    stop_count += 1

                    print("STOP MESSAGE RECEIVED")
                    print(f"   Scan: {metadata.get('scan_name', 'unknown')}")
                    print(f"   Total flushes: {metadata.get('total_flushes')}")
                    print(f"   Total cycles: {metadata.get('total_cycles')}")
                    print(f"   Duration: {metadata.get('acquisition_duration_s', 0):.2f} s")
                    print()

                elif msg_type == "event" or msg_type is None:
                    # Event message - try to receive array
                    try:
                        socket.setsockopt(zmq.RCVTIMEO, 1000)
                        array_bytes = socket.recv()
                        socket.setsockopt(zmq.RCVTIMEO, 5000)

                        # Reconstruct array
                        shape = tuple(metadata["shape"])
                        dtype = metadata["dtype"]
                        array = np.frombuffer(array_bytes, dtype=dtype).reshape(shape)

                        event_count += 1
                        flush_num = metadata.get("flush_number", "?")
                        total_counts = np.sum(array)

                        # Print all events (can be changed to print every Nth if too verbose)
                        print(
                            f"Event #{event_count} (Flush #{flush_num}): "
                            f"shape={shape}, counts={total_counts}, "
                            f"cycles={metadata.get('cycles_in_flush', '?')}"
                        )
                    except zmq.Again:
                        print("WARNING: Expected array data but none received")
                        continue
                else:
                    print(f"WARNING: Unknown message type: {msg_type}")

                # Print summary every 10 events
                if event_count > 0 and event_count % 10 == 0:
                    elapsed = time.time() - start_time
                    print(
                        f"\nSummary: {start_count} start, {event_count} events, "
                        f"{stop_count} stop (in {elapsed:.1f}s)\n"
                    )

            except zmq.Again:
                # Timeout - check if we should continue
                elapsed = time.time() - start_time
                # If we got start message but no stop, wait longer (server might still be running)
                if start_count > 0 and stop_count == 0:
                    # Wait up to 60 seconds total if we're waiting for stop message
                    if elapsed < 60:
                        continue
                    else:
                        print("\nWaiting for stop message (server may still be running)...")
                        print("   Tip: Stop the server (Ctrl+C) to receive stop message")
                        break
                elif elapsed > 30:
                    print("\nNo messages received in 30 seconds, stopping...")
                    break
                continue

    except KeyboardInterrupt:
        print("\n\nInterrupted by user")

    finally:
        # Final summary
        elapsed = time.time() - start_time
        print("\n" + "=" * 70)
        print("FINAL SUMMARY")
        print("=" * 70)
        print(f"Start messages:  {start_count}")
        print(f"Event messages:  {event_count}")
        print(f"Stop messages:   {stop_count}")
        print(f"Total time:      {elapsed:.1f} seconds")
        print()

        if start_count > 0 and event_count > 0:
            if stop_count > 0:
                print("TEST PASSED: All messages received (start, events, stop)!")
            else:
                print("PARTIAL: Start and events received, but no stop message")
                print("   (Stop message is only sent when server shuts down)")
                print("   To get stop message: Stop the server with Ctrl+C")
        else:
            print("TEST FAILED: Missing messages")

        # Cleanup
        socket.close()
        context.term()
        print("Cleaned up")


if __name__ == "__main__":
    main()
