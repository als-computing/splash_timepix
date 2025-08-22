"""
Test source for the SocketDataServer.

This script creates a source that connects to the server and sends 5-byte messages
to test the socket server functionality.
"""

import random
import socket
import struct
import threading
import time
from typing import Optional


class TestSource:
    """A test source that sends 5-byte messages to the socket server."""

    def __init__(self, host: str = "localhost", port: int = 8888):
        """
        Initialize the test source.

        Args:
            host: Server host address
            port: Server port
        """
        self.host = host
        self.port = port
        self.socket: Optional[socket.socket] = None
        self.running = False
        self.send_thread: Optional[threading.Thread] = None

    def connect(self) -> bool:
        """
        Connect to the server.

        Returns:
            True if connection successful, False otherwise
        """
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.connect((self.host, self.port))
            print(f"Connected to server at {self.host}:{self.port}")
            return True
        except Exception as e:
            print(f"Failed to connect to server: {e}")
            return False

    def disconnect(self) -> None:
        """Disconnect from the server."""
        if self.socket:
            self.socket.close()
            self.socket = None
            print("Disconnected from server")

    def send_message(self, value: int, extra_byte: int = 0) -> bool:
        """
        Send a 5-byte message to the server.

        Args:
            value: 4-byte integer value
            extra_byte: Additional byte (0-255)

        Returns:
            True if sent successfully, False otherwise
        """
        if not self.socket:
            print("Not connected to server")
            return False

        try:
            # Pack the data: 4 bytes for int + 1 byte
            message = struct.pack("<I", value) + bytes([extra_byte])
            self.socket.sendall(message)
            return True
        except Exception as e:
            print(f"Failed to send message: {e}")
            return False

    def start_auto_sending(
        self, interval: float = 1.0, count: Optional[int] = None
    ) -> None:
        """
        Start automatically sending random messages.

        Args:
            interval: Time between messages in seconds
            count: Number of messages to send (None for infinite)
        """
        if self.running:
            print("Auto-sending is already running")
            return

        self.running = True
        self.send_thread = threading.Thread(
            target=self._auto_send_worker, args=(interval, count), daemon=True
        )
        self.send_thread.start()
        print(f"Started auto-sending messages every {interval} seconds")

    def stop_auto_sending(self) -> None:
        """Stop automatic message sending."""
        if not self.running:
            print("Auto-sending is not running")
            return

        self.running = False
        if self.send_thread and self.send_thread.is_alive():
            self.send_thread.join(timeout=5)
        print("Stopped auto-sending messages")

    def _auto_send_worker(self, interval: float, count: Optional[int]) -> None:
        """Worker thread for automatic message sending."""
        sent_count = 0

        while self.running:
            if count is not None and sent_count >= count:
                break

            # Generate random data
            value = random.randint(0, 1000000)
            extra_byte = random.randint(0, 255)

            if self.send_message(value, extra_byte):
                sent_count += 1
                print(f"Sent message #{sent_count}: value={value}, extra={extra_byte}")

            time.sleep(interval)

        self.running = False
        print(f"Auto-sending finished. Sent {sent_count} messages.")


def send_test_data():
    """Send some predefined test data."""
    source = TestSource()

    if not source.connect():
        return

    try:
        # Send some test messages
        test_values = [100, 200, 300, 400, 500]

        for i, value in enumerate(test_values):
            extra_byte = i + 1
            if source.send_message(value, extra_byte):
                print(f"Sent: value={value}, extra_byte={extra_byte}")
            time.sleep(0.5)

        print("Test data sent successfully")

    except KeyboardInterrupt:
        print("Interrupted by user")
    finally:
        source.disconnect()


def interactive_source():
    """Run an interactive source."""
    source = TestSource()

    if not source.connect():
        return

    try:
        print("Interactive source started. Commands:")
        print("  'send <value> [extra_byte]' - Send a specific message")
        print("  'auto <interval> [count]' - Start auto-sending")
        print("  'stop' - Stop auto-sending")
        print("  'quit' - Exit")

        while True:
            try:
                command = input("> ").strip().split()

                if not command:
                    continue

                if command[0] == "quit":
                    break

                elif command[0] == "send":
                    if len(command) < 2:
                        print("Usage: send <value> [extra_byte]")
                        continue

                    value = int(command[1])
                    extra_byte = int(command[2]) if len(command) > 2 else 0

                    if source.send_message(value, extra_byte):
                        print(f"Sent: value={value}, extra_byte={extra_byte}")

                elif command[0] == "auto":
                    if len(command) < 2:
                        print("Usage: auto <interval> [count]")
                        continue

                    interval = float(command[1])
                    count = int(command[2]) if len(command) > 2 else None

                    source.start_auto_sending(interval, count)

                elif command[0] == "stop":
                    source.stop_auto_sending()

                else:
                    print(f"Unknown command: {command[0]}")

            except ValueError as e:
                print(f"Invalid input: {e}")
            except KeyboardInterrupt:
                break

    finally:
        source.stop_auto_sending()
        source.disconnect()


def main():
    """Main function to choose between test modes."""
    print("Socket Server Test Source")
    print("1. Send test data")
    print("2. Interactive mode")

    try:
        choice = input("Choose mode (1 or 2): ").strip()

        if choice == "1":
            send_test_data()
        elif choice == "2":
            interactive_source()
        else:
            print("Invalid choice")

    except KeyboardInterrupt:
        print("\nExiting...")


if __name__ == "__main__":
    main()
