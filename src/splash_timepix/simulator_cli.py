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
        # See SimulatorConfig.tcp_batch_interval_s.  0 = per-packet sendall
        # (original behaviour, what real hardware drivers do); >0 buffers
        # bytes for this many seconds and emits them as a single sendall
        # bundle, used by tests to reproduce the Serval+luna-iterator
        # upstream-batching regime on the wire.
        self.tcp_batch_interval_s: float = 0.0

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

    def set_tcp_batch_interval(self, interval_s: float) -> None:
        """Set wire-level bolus batching interval.

        0 disables (original per-packet sendall).  Non-zero accumulates
        packets for this many seconds, then sends as a single sendall.
        Used by tests to reproduce the Serval+luna-iterator batching
        regime; not intended for production use.
        """
        if interval_s < 0:
            raise ValueError(f"tcp_batch_interval_s must be >= 0, got {interval_s}")
        self.tcp_batch_interval_s = float(interval_s)
        if interval_s > 0:
            print(f"TCP batching enabled: flush every {interval_s}s")

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

        # Connect (or reconnect after a previous DAQ) when the DAQ starts so
        # the server only sees a new client — and publishes the ZMQ start
        # message — at this point, not when the CLI was launched.
        if self.socket is None:
            print("Connecting to server for new acquisition...")
            if not self.connect():
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
        if self.running:
            self.running = False
            if self.send_thread and self.send_thread.is_alive():
                self.send_thread.join(timeout=5)
            print("Stopped auto-sending messages")
        # Disconnect regardless of whether the stream was manually stopped or
        # ended naturally (timer expired).  This is what signals end-of-acquisition
        # to the server so it publishes a ZMQ stop message.  Safe to call even
        # when already disconnected.  A reconnect happens automatically the next
        # time start_auto_sending() is called.
        self.disconnect()

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
        """Worker thread for sending packets using PacketSimulator.

        Normally each packet is pushed to the socket as soon as it is
        produced.  When ``self.tcp_batch_interval_s > 0`` packet bytes
        are instead accumulated in an in-memory buffer and emitted in
        one ``sendall`` per ``tcp_batch_interval_s`` seconds.  This
        mode exists to reproduce the production Serval+luna-iterator
        symptom on the wire for integration tests; it is single-
        threaded so no lock is required.
        """
        simulator = PacketSimulator(SimulatorConfig())
        simulator.config.pixel_count_rate = self.pixel_count_rate
        simulator.config.tdc_frequency = self.tdc_frequency
        if self.counting:
            parser = PacketParser()
            sent_count_pixel = 0
            sent_count_tdc = 0
            sent_count_ctrl = 0

        batch_interval = float(self.tcp_batch_interval_s)
        batching = batch_interval > 0.0
        tx_buffer: list[bytes] = []
        last_flush_monotonic = time.monotonic()

        def _flush_tx_buffer() -> None:
            """Send accumulated packet bytes as one sendall, clear buffer."""
            nonlocal last_flush_monotonic
            if not tx_buffer:
                return
            payload = b"".join(tx_buffer)
            tx_buffer.clear()
            last_flush_monotonic = time.monotonic()
            self.socket.sendall(payload)

        packet_generator = simulator.generate_stream(duration_seconds=duration)
        try:
            for packet in packet_generator:
                if not self.running:
                    break
                try:
                    if batching:
                        tx_buffer.append(packet)
                        if time.monotonic() - last_flush_monotonic >= batch_interval:
                            _flush_tx_buffer()
                    else:
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
        finally:
            # Drain residual bytes so the server sees the tail of the
            # acquisition.  If this raises we still want to fall through
            # to the running=False / disconnect path below.
            if batching:
                try:
                    _flush_tx_buffer()
                except Exception as e:
                    print(f"Failed to flush final TCP batch: {e}")

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
        # Disconnect so the server sees the TCP close and publishes the ZMQ stop
        # message.  When the user typed "stop" this is handled by stop_auto_sending();
        # when the timer expires naturally, we must do it here.  disconnect() is
        # idempotent (checks self.socket), so a double-call is harmless.
        self.disconnect()


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
    tcp_batch_interval: float = typer.Option(
        0.0,
        "--tcp-batch-interval",
        help="If >0, buffer packets for this many seconds and emit them in one sendall. "
        "Used by tests to reproduce the Serval+luna-iterator batching regime on the wire. "
        "0 (default) preserves real-hardware-like per-packet sending.",
    ),
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
    source.set_tcp_batch_interval(tcp_batch_interval)

    # Do NOT connect here.  Connection is deferred to start_auto_sending() so
    # that the server only sees a new TCP client — and therefore only publishes
    # the ZMQ start message — when a DAQ actually begins, not at CLI startup.

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
