"""
Example usage of the SocketDataServer.

This script demonstrates how to set up and use the multi-threaded socket server
that reads 5-byte messages and processes them into numpy arrays.
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
    data_history = []

    def data_callback(new_data: np.ndarray) -> None:
        """Callback function called when new data is processed."""
        data_history.extend(new_data.tolist())
        print(f"📊 New data: {new_data[0]} (Total received: {len(data_history)})")

        # Show statistics every 10 data points
        if len(data_history) % 10 == 0:
            array = np.array(data_history)
            print(
                f"📈 Stats - Count: {len(array)}, Mean: {np.mean(array):.2f}, "
                f"Min: {np.min(array)}, Max: {np.max(array)}"
            )

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

        while True:
            time.sleep(1)
            current_time = time.time()

            # Show stats every 30 seconds
            if current_time - last_stats_time >= 30:
                data_array = server.get_data_array()
                queue_size = server.get_queue_size()
                uptime = current_time - start_time

                print(f"⏱️  Server uptime: {uptime:.0f}s")
                print(f"📦 Queue size: {queue_size}")
                print(f"📊 Total data points: {len(data_array)}")

                if len(data_array) > 0:
                    recent_values = (
                        data_array[-5:] if len(data_array) >= 5 else data_array
                    )
                    print(f"🔢 Recent values: {recent_values}")

                print("-" * 30)
                last_stats_time = current_time

    except KeyboardInterrupt:
        print("\n🛑 Shutting down server...")
        server.stop()

        # Show final statistics
        final_data = server.get_data_array()
        print("📊 Final statistics:")
        print(f"   Total data points processed: {len(final_data)}")

        if len(final_data) > 0:
            print(f"   Mean value: {np.mean(final_data):.2f}")
            print(f"   Min value: {np.min(final_data)}")
            print(f"   Max value: {np.max(final_data)}")
            print(f"   Standard deviation: {np.std(final_data):.2f}")

        print("✅ Server stopped successfully")


if __name__ == "__main__":
    main()
