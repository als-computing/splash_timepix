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
import datetime
from typing import Optional

from splash_timepix.parser import PacketParser
from splash_timepix.simulator import PacketSimulator, SimulatorConfig, PacketType

import typer
app = typer.Typer()


class TestSource:
    """A test source (i.e. TimePix3 simlulator) that sends 12-byte messages to the socket server."""

    def __init__(self, host: str = "localhost", port: int = 9090):
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
        self.pixel_count_rate = 2
        self.tdc_frequency = 0.2


    def set_counts_per_second(self, cps: float):
        """Average number of pixel events per second (cps, counts/second)"""
        self.pixel_count_rate = cps
        if cps > 1E9:
            print(f"Pixel count rate set to {cps/1E9} giga counts/second")
        elif cps > 1E6:
            print(f"Pixel count rate set to {cps/1E6} mega counts/second")
        elif cps > 1E3:
            print(f"Pixel count rate set to {cps/1E3} kilo counts/second")
        else:
            print(f"Pixel count rate set to {cps} counts/second")


    def set_tdc_frequency(self, tdc: float):
        """Frequency of time-to-digital converter (TDC) events (Hz)"""
        self.tdc_frequency = tdc
        print(f"TDC frequency set to {tdc} Hz")


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


    def start_auto_sending(self, duration: float) -> None:
        """
        Start automatically sending random messages.

        Args:
            duration: Total amount of time to send packets for in seconds
        """
        if self.running:
            print("Auto-sending is already running")
            return

        self.running = True
        self.send_thread = threading.Thread(
            target=self._auto_send_worker, args=(duration,), daemon=True
        )
        self.send_thread.start()
        print(f"Started auto-sending messages for {duration} seconds")
        dt = datetime.datetime.fromtimestamp(time.time())
        formatted = dt.strftime('%Y-%m-%d %H:%M:%S.') + f"{dt.microsecond // 1000:03d}"
        print(f"Current time: {formatted}")


    def stop_auto_sending(self) -> None:
        """Stop automatic message sending."""
        if not self.running:
            print("Auto-sending is not running")
            return

        self.running = False
        if self.send_thread and self.send_thread.is_alive():
            self.send_thread.join(timeout=5)
        print("Stopped auto-sending messages")


    def _auto_send_worker(self, duration: float) -> None:
        """Worker thread for sending packets using PacketSimulator."""
        # initialize simulator
        simulator = PacketSimulator(SimulatorConfig())
        # write count rate and TDC frequency to simulator configuration
        simulator.config.pixel_count_rate = self.pixel_count_rate
        simulator.config.tdc_frequency = self.tdc_frequency
        # DEBUGGING
        parser = PacketParser()
        sent_count_pixel = 0
        sent_count_tdc = 0

        packet_generator = simulator.generate_stream(duration_seconds=duration)
        for packet in packet_generator:
            if not self.running:
                break
            try:
                self.socket.sendall(packet)
                #DEBUGGING
                parsed = parser.parse(packet)
                if parsed and parsed.packet_type == PacketType.PIXEL:
                    sent_count_pixel += 1
                elif parsed and parsed.packet_type == PacketType.TDC:
                    sent_count_tdc += 1
                # print(f"Sent simulated packet #{sent_count_pixel + sent_count_tdc}")
            except Exception as e:
                print(f"Failed to send simulated packet: {e}")
                break

        self.running = False
        print("Auto-sending finished.")
        print(f"Sent {sent_count_pixel} pixel events and {sent_count_tdc} TDC events.")
        dt = datetime.datetime.fromtimestamp(time.time())
        formatted = dt.strftime('%Y-%m-%d %H:%M:%S.') + f"{dt.microsecond // 1000:03d}"
        print(f"Current time: {formatted}")


@app.command()
def main():
    """Main function to start simulator."""
    
    print("Start sending simulated TimePix3 data to Socket Server")

    source = TestSource()

    if not source.connect():
        return

    try:
        print("Interactive source started. Commands:")
        print("  'cps <value>' - Set counts per second")
        print("  'tdc <value>' - Set TDC frequency (Hz)")
        print("  'start <duration_in_seconds>' - Start auto-sending")
        print("  'stop' - Stop auto-sending")
        print("  'quit' - Exit")

        while True:
            try:
                command = input("> ").strip().split()

                if not command:
                    continue

                if command[0] == "quit":
                    break

                elif command[0] == "cps":
                    if len(command) < 2:
                        print("Usage: cps <value>")
                        continue

                    cps = float(command[1])
                    source.set_counts_per_second(cps)

                elif command[0] == "tdc":
                    if len(command) < 2:
                        print("Usage: tdc <value>")
                        continue

                    tdc = float(command[1])
                    source.set_tdc_frequency(tdc)

                elif command[0] == "start":
                    if len(command) < 2:
                        print("Usage: start <duration>")
                        continue

                    duration = float(command[1])

                    source.start_auto_sending(duration)

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


if __name__ == "__main__":
    main()
    