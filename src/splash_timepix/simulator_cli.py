"""
Simulated source for testing the SocketDataServer.

This script creates a source that connects to the server and sends simulated messages
to test the socket server functionality.
"""

import datetime
import socket
import threading
import time
from typing import Optional

import typer

from splash_timepix.parser import PacketParser
from splash_timepix.simulator import PacketSimulator, PacketType, SimulatorConfig

app = typer.Typer()


class SimulatorSource:
    """A test source (i.e. TimePix3 simlulator) that sends 12-byte messages to the socket server."""

    def __init__(self, host: str = "localhost", port: int = 9090):
        """
        Initialize the simulator/ test source.

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
        self.counting = True

    def set_counts_per_second(self, cps: float):
        """Average number of pixel events per second (cps, counts/second)"""
        self.pixel_count_rate = cps
        if cps > 1e9:
            print(f"Pixel count rate set to {cps/1E9} giga counts/second")
        elif cps > 1e6:
            print(f"Pixel count rate set to {cps/1E6} mega counts/second")
        elif cps > 1e3:
            print(f"Pixel count rate set to {cps/1E3} kilo counts/second")
        else:
            print(f"Pixel count rate set to {cps} counts/second")

    def set_tdc_frequency(self, tdc: float):
        """Frequency of time-to-digital converter (TDC) events (Hz)"""
        self.tdc_frequency = tdc
        print(f"TDC frequency set to {tdc} Hz")

    def set_counting(self, counting: bool):
        """Count sent pixel (requires parsing -> slow down -> less performant)"""
        self.counting = counting
        if counting:
            print("Counting sent packets (use to compare sent and received packets)")
        else:
            print("Not counting sent packets (allows for higher count rates [cps])")

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
        self.send_thread = threading.Thread(target=self._auto_send_worker, args=(duration,), daemon=True)
        self.send_thread.start()
        print(f"Started auto-sending messages for {duration} seconds")
        dt = datetime.datetime.fromtimestamp(time.time())
        formatted = dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
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

    def run_blocking(self, duration: float) -> None:
        """
        Run auto-sending and block until complete.

        Args:
            duration: Total amount of time to send packets for in seconds
        """
        self.start_auto_sending(duration)

        # Wait for completion
        try:
            while self.running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\nInterrupted by user")
            self.stop_auto_sending()

    def _auto_send_worker(self, duration: float) -> None:
        """Worker thread for sending packets using PacketSimulator."""
        # initialize simulator
        simulator = PacketSimulator(SimulatorConfig())
        # write count rate and TDC frequency to simulator configuration
        simulator.config.pixel_count_rate = self.pixel_count_rate
        simulator.config.tdc_frequency = self.tdc_frequency
        if self.counting:
            parser = PacketParser()
            sent_count_pixel = 0
            sent_count_tdc = 0
            sent_count_ctrl = 0

        packet_generator = simulator.generate_stream(duration_seconds=duration)
        for packet in packet_generator:
            if not self.running:
                break
            try:
                self.socket.sendall(packet)
                if self.counting:
                    parsed = parser.parse(packet)
                    if parsed and parsed.packet_type == PacketType.PIXEL:
                        sent_count_pixel += 1
                    elif parsed and parsed.packet_type == PacketType.TDC:
                        sent_count_tdc += 1
                    elif parsed and parsed.packet_type == PacketType.CONTROL:
                        sent_count_ctrl += 1
            except Exception as e:
                print(f"Failed to send simulated packet: {e}")
                break

        self.running = False
        print("Auto-sending finished.")
        if self.counting:
            print("Sent events during last session:")
            print(f"  {sent_count_pixel} pixel events")
            print(f"  {sent_count_tdc} TDC events")
            print(f"  {sent_count_ctrl} control events")
        dt = datetime.datetime.fromtimestamp(time.time())
        formatted = dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
        print(f"Current time: {formatted}")


@app.command()
def main(
    host: str = typer.Option("localhost", "--host", "-h", help="Server host address"),
    port: int = typer.Option(9090, "--port", "-p", help="Server port"),
    tdc_frequency: float = typer.Option(1.0, "--tdc-frequency", "-tdc", help="TDC frequency in Hz"),
    cps: float = typer.Option(1000.0, "--cps", help="Counts per second (pixel event rate)"),
    duration: int = typer.Option(60, "--duration", "-t", help="Duration in seconds (for auto-start mode)"),
    auto_start: bool = typer.Option(
        False,
        "--auto-start",
        help="Start streaming immediately without interactive mode",
    ),
    no_count: bool = typer.Option(False, "--no-count", help="Disable packet counting for higher performance"),
):
    """
    Simulated TimePix3 data source for testing.

    In auto-start mode (--auto-start), immediately begins streaming for the specified duration.
    Without --auto-start, enters interactive CLI mode.

    Examples:
        # Interactive mode
        python -m splash_timepix.simulator_cli

        # Auto-start mode for UI integration
        python -m splash_timepix.simulator_cli --auto-start --tdc-frequency 1000 --cps 10000 --duration 60
    """

    print("Start sending simulated TimePix3 data to Socket Server")

    source = SimulatorSource(host=host, port=port)

    # Apply settings
    source.set_counts_per_second(cps)
    source.set_tdc_frequency(tdc_frequency)
    source.set_counting(not no_count)

    if not source.connect():
        return

    try:
        if auto_start:
            # Non-interactive mode for UI integration
            print(f"\nAuto-start mode: {cps} cps, {tdc_frequency} Hz TDC, {duration}s")
            source.run_blocking(duration)
        else:
            # Interactive mode
            print("\nInteractive source started.")
            print("Commands:")
            print("  'cps <value>' - Set counts per second")
            print("  'tdc <value>' - Set TDC frequency (Hz)")
            print("  'count <y/n>' - Count sent packets (default: y)")
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

                    elif command[0] == "count":
                        if len(command) < 2 or (command[1] not in ["y", "n"]):
                            print("Usage: count <y/n>")
                            continue
                        counting = True if command[1] == "y" else False
                        source.set_counting(counting)

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
    app()
