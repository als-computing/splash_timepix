"""
Example usage of the SocketDataServer.

This script demonstrates how to set up and use the multi-threaded socket server
that reads (simulated) TimePix3 messages and processes them into numpy arrays.
"""

import time

import numpy as np

from splash_timepix.socket_server import SocketDataServer


def main():
    """
    Example of using the SocketDataServer.
    """
    print("Starting Socket Data Server Example")
    print("=" * 50)

    # Create the server
    server = SocketDataServer(host="localhost", port=8888, buffer_size=1000)

    # Set up a callback to handle new data
    event_count = 0
    
    def data_callback(new_data: np.ndarray) -> None:
        """Callback function called when new data is processed."""
        nonlocal event_count
        event_count += len(new_data)
        print(f"📊 Received {len(new_data)} new events (Total: {event_count})")
        #$% should this include a call to a qt plot?

    # Set the callback
    server.set_data_callback(data_callback)

    try:
        # Start the server
        print("🚀 Starting server on localhost:8888")
        server.start()

        print("🔍 Server is running and waiting for connections...")
        print("💡 To test the server, run the test source:")
        print("   python -m splash_timepix.test_source")
        print("🛑 Press Ctrl+C to stop the server")
        print()

        # Keep the main thread alive and show periodic stats
        start_time = time.time()
        last_stats_time = start_time
        stats_history = []
        stats_length = 5 # length of shift register
        stats_update_time = 1 # seconds

        while True:
            time.sleep(1)
            current_time = time.time()

            # Show/ update stats
            if current_time - last_stats_time >= stats_update_time:
                data_array = server.get_data_array()
                queue_size = server.get_queue_size()
                uptime = current_time - start_time

                stats = (
                    f"⏱️  Server uptime: {uptime:.0f}s\n"
                    f"📦 Queue size: {queue_size}\n"
                    f"📊 Total data points: {np.sum(data_array)}\n"
                    + "-" * 30
                )

                stats_history.append(stats)
                if len(stats_history) > stats_length:
                    stats_history.pop(0)

                # Clear the terminal and print the stats shift register
                import os
                os.system('cls' if os.name == 'nt' else 'clear')
                print(f"Last {stats_length} stats:\n")
                for s in stats_history:
                    print(s)
                last_stats_time = current_time

    except KeyboardInterrupt:
        print("\n🛑 Shutting down server...")
        server.stop()
        print("✅ Server stopped successfully")


if __name__ == "__main__":
    main()
