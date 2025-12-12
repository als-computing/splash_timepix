#!/usr/bin/env python3
"""
Simple ZMQ subscriber to receive and display published flushes.
Demonstrates rich metadata usage for averaging and analysis.
Modify this example to build applications receiving pre-processed TimePix3 data.
"""

import zmq
import msgpack
import numpy as np

def main():
    # Create ZMQ context and SUB socket
    context = zmq.Context()
    socket = context.socket(zmq.SUB)
    
    # Connect to publisher
    socket.connect("tcp://localhost:5657")
    
    # Subscribe to all messages (empty filter = receive everything)
    socket.setsockopt(zmq.SUBSCRIBE, b"")
    
    print("📡 Subscriber connected to tcp://localhost:5657")
    print("Waiting for flushes...\n")
    
    # For running average calculation
    cumulative_sum = None
    total_cycles = 0
    
    try:
        while True:
            # Receive multi-part message
            metadata_bytes = socket.recv()
            array_bytes = socket.recv()
            
            # Unpack metadata
            metadata = msgpack.unpackb(metadata_bytes)
            
            # Reconstruct array
            shape = tuple(metadata['shape'])
            dtype = metadata['dtype']
            array = np.frombuffer(array_bytes, dtype=dtype).reshape(shape)
            
            # Calculate stats
            total_counts = np.sum(array)
            max_counts = np.max(array)
            
            # Per-flush metadata
            flush_number = metadata.get('flush_number', 0)
            cycles_in_flush = metadata.get('cycles_in_flush', 0)
            flush_total_cycles = metadata.get('total_cycles', 0)
            
            print(f"📦 Received flush #{flush_number}")
            print(f"   Timestamp: {metadata['timestamp']:.3f}")
            print(f"   Size: {len(array_bytes)/1024/1024:.2f} MB")
            print()
            
            # Static configuration (same for all flushes in a run)
            print(f"   📊 Configuration:")
            print(f"      Shape: {shape}")
            print(f"      TDC frequency: {metadata.get('tdc_frequency_hz', 'N/A')} Hz")
            print(f"      Time bin width: {metadata.get('t_delta_ns', 'N/A')} ns")
            print(f"      Time cycle: {metadata.get('t_cycle_ns', 'N/A')} ns")
            print(f"      Bins: {metadata.get('n_bins', 'N/A')}")
            print(f"      TDC channel: {metadata.get('tdc_channel', 'N/A')} (edge: {metadata.get('tdc_edge', 'N/A')})")
            print()
            
            print(f"   🔄 Flush info:")
            print(f"      Cycles in this flush: {cycles_in_flush}")
            print(f"      Total cycles so far: {flush_total_cycles}")
            print(f"      Discarded (before trigger): {metadata.get('pixels_discarded_before_trigger', 'N/A')}")
            print(f"      Discarded (outside window): {metadata.get('pixels_discarded_outside_window', 'N/A')}")
            print()
            
            print(f"   📈 Flush stats:")
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
                
                print(f"   📊 Running average (over {total_cycles} cycles):")
                print(f"      Avg counts/cycle: {avg_total:.2f}")
                print(f"      Avg max/bin/cycle: {avg_max:.4f}")
            
            print()
            print("-" * 60)
            print()
            
    except KeyboardInterrupt:
        flush_count = metadata.get('flush_number', 0) if 'metadata' in dir() else 0
        print(f"\n✅ Received {flush_count} flushes total")
        if total_cycles > 0:
            print(f"   Total cycles averaged: {total_cycles}")
        socket.close()
        context.term()

if __name__ == "__main__":
    main()
    