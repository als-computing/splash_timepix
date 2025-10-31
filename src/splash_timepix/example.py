"""
Example usage of the SocketDataServer.

This script demonstrates how to set up and use the multi-threaded socket server
that reads (simulated) TimePix3 messages and processes them into numpy arrays.
"""

import time
#import datetime
import os
import logging
import numpy as np

from splash_timepix.socket_server import SocketDataServer, RingBufferHandler

import typer
app = typer.Typer()



@app.command()
def main(port: int = 9090, 
         buffer_size: int = 1000,  
         stats_update_time: int = 1,
         verbose: bool = False):
    """
    Example of using the SocketDataServer.

    Args:
        port: Port number for the server
        buffer_size: Size of the internal data buffer
        stats_update_time: Time interval (seconds) to update and display stats
        verbose: Print info for every incoming event
    """
    print("Starting Socket Data Server Example")
    print("=" * 50)

    # Create and attach the ring buffer handler
    ring_handler = RingBufferHandler(capacity=10)
    #ring_handler.setLevel(logging.ERROR)  # Only capture ERROR and above
    ring_handler.setLevel(logging.WARNING)  # Capture WARNING and above (WARNING, ERROR, CRITICAL)
    ring_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    
    # Get the socket_server logger and add our handler to it
    socket_logger = logging.getLogger('splash_timepix.socket_server')
    socket_logger.addHandler(ring_handler)

    # Create the server
    server = SocketDataServer(host="localhost", port=port, buffer_size=buffer_size)

    # Set up a callback to handle new data
    event_count = 0
    
    def data_callback(new_data: np.ndarray) -> None:
        """Callback function called when new data is processed."""
        nonlocal event_count, connection_time
        if connection_time is None:  # First data received = connection made
            connection_time = time.time()
        event_count += len(new_data)
        if verbose:
            print(f"📊 Received {len(new_data)} new events (Total: {event_count})")

    # Set the callback
    server.set_data_callback(data_callback)

    try:
        # Start the server
        print(f"🚀 Starting server on localhost:{port}")
        server.start()

        print("🔍 Server is running and waiting for connections...")
        print("💡 To test the server, run the test source:")
        print("   python -m splash_timepix.test_source")
        print("🛑 Press Ctrl+C to stop the server")
        print()

        # Keep the main thread alive and show periodic stats

        # Keep the main thread alive and show periodic stats
        start_time = time.time()
        connection_time = None  # Track when client connects
        last_stats_time = start_time
        last_stats_change = "No packets received yet"
        last_total_data_points = 0
        unknown_count = 0

        while True:
            time.sleep(1)
            current_time = time.time()

            # Show/ update stats
            if current_time - last_stats_time >= stats_update_time:
                data_array = server.get_data_array()
                queue_size = server.get_queue_size()
                uptime = current_time - start_time
                current_total_data_points = np.sum(data_array)
                unknown_count = server.get_unknown_packet_count() 
                
                # Calculate time since connection and packets since connection
                if connection_time is not None:
                    time_since_connection = current_time - connection_time
                    packets_since_connection = current_total_data_points
                    rate = packets_since_connection / time_since_connection
                    rate_str = f"{rate:.1f}"
                else:
                    time_since_connection = 0
                    packets_since_connection = 0
                    rate_str = "N/A"
                    
                stats = (
                    f"⏱️  Server uptime: {uptime:.3f}s\n"
                    f"🔌 Time since connection: {time_since_connection:.3f}s\n"
                    f"📦 Queue size: {queue_size}\n"
                    f"📊 Packets since connection: {packets_since_connection}\n"
                    f"📊 Total data points: {current_total_data_points}\n"
                    f"📈 Packets/s (since connection): {rate_str}\n"
                    f"⚠️  Unknown packets: {unknown_count}\n"
                    + "-" * 30
                )

                # Update last stats 
                if current_total_data_points != last_total_data_points:
                    last_stats_change = stats

                # Clear the terminal and print the stats shift register
                os.system('cls' if os.name == 'nt' else 'clear')
                print(f"Current stats:\n")
                print(stats)
                print()
                print(f"Stats after last received packet:\n")
                print(last_stats_change)
                # update last event time
                last_stats_time = current_time
                # update number of total data points
                last_total_data_points = np.sum(data_array)

                # Show recent errors from ring buffer
                recent_errors = ring_handler.get_logs()
                if recent_errors:
                    print()
                    print(f"⚠️  Recent errors (last {len(recent_errors)}):\n")
                    for err in recent_errors:
                        print(f"  {err}")

                # Show valid packet samples
                valid_samples = server.get_valid_packet_samples()
                if valid_samples:
                    print()
                    print(f"✅ Recent valid packets (last {len(valid_samples)}):\n")
                    for sample in valid_samples:
                        print(f"  {sample}")


    except KeyboardInterrupt:
        print("\n🛑 Shutting down server...")
        server.stop()
        print("✅ Server stopped successfully")


if __name__ == "__main__":
    main()
