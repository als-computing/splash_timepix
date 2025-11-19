#!/usr/bin/env python3
"""
Simple ZMQ subscriber to receive and display published arrays.
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
    print("Waiting for arrays...\n")
    
    array_count = 0
    
    try:
        while True:
            # Receive multi-part message
            metadata_bytes = socket.recv()
            array_bytes = socket.recv()
            
            # Unpack metadata
            metadata = msgpack.unpackb(metadata_bytes)
            
            # Reconstruct array
            array = np.frombuffer(array_bytes, dtype=metadata['dtype']).reshape(metadata['shape'])
            
            # Display info
            array_count += 1
            total_counts = np.sum(array)
            max_counts = np.max(array)
            
            print(f"📦 Received array #{array_count}")
            print(f"   Shape: {metadata['shape']}")
            print(f"   Dtype: {metadata['dtype']}")
            print(f"   Timestamp: {metadata['timestamp']:.3f}")
            print(f"   Total counts: {total_counts}")
            print(f"   Max counts: {max_counts}")
            print(f"   Size: {len(array_bytes)/1024/1024:.2f} MB")
            print()
            
    except KeyboardInterrupt:
        print(f"\n✅ Received {array_count} arrays total")
        socket.close()
        context.term()

if __name__ == "__main__":
    main()
    