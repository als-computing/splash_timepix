"""NumPy-accelerated socket server for TimePix3 data.

Drop-in replacement for socket_server.py that uses vectorized batch parsing.
Passes BatchParseResult directly to callbacks instead of packet objects.

Key differences from original:
1. Uses parser_np.PacketParser.parse_batch() for vectorized parsing
2. Callback receives BatchParseResult with NumPy arrays (not list of packets)
3. Batches raw bytes before parsing (more efficient than parsing then batching)
4. Configurable batch size in bytes (default 120KB = 10k packets)
"""

import logging
import queue
import socket
import threading
import time
from collections import deque
from typing import Callable, Optional

from splash_timepix.parser_np import BatchParseResult, PacketParser

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class RingBufferHandler(logging.Handler):
    """Logging handler that keeps only the last N log records in a ring buffer."""

    def __init__(self, capacity=10):
        super().__init__()
        self.buffer = deque(maxlen=capacity)

    def emit(self, record):
        msg = self.format(record)
        self.buffer.append(msg)

    def get_logs(self):
        return list(self.buffer)

    def clear(self):
        self.buffer.clear()


class SocketDataServer:
    """
    NumPy-accelerated multi-threaded server for TimePix3 data.

    Key optimization: batches raw bytes and uses vectorized parsing,
    passing NumPy arrays directly to callbacks.
    """

    PACKET_SIZE = 12  # 96 bits = 12 bytes

    def __init__(
        self,
        host: str = "localhost",
        port: int = 9090,
        buffer_size: int = 1000,
        debug: bool = False,
        callback_batch_size: int = 10000,
        exit_on_disconnect: bool = False,
    ):
        """
        Initialize the socket server.

        Args:
            host: The host address to bind to
            port: The port to bind to
            buffer_size: Maximum number of byte batches to buffer
            debug: Enable debug logging and packet samples
            callback_batch_size: Number of packets to batch before callback
            exit_on_disconnect: If True, stop server when client disconnects
        """
        self.host = host
        self.port = port
        self.buffer_size = buffer_size
        self.callback_batch_size = callback_batch_size
        self.batch_byte_size = callback_batch_size * self.PACKET_SIZE

        # Thread-safe queue for raw byte batches
        self.message_queue: queue.Queue[bytes] = queue.Queue(maxsize=buffer_size)

        # Control flags
        self.running = False
        self.exit_on_disconnect = exit_on_disconnect
        self.client_connected = False
        self.client_disconnected_event = threading.Event()

        self.socket_thread: Optional[threading.Thread] = None
        self.processor_thread: Optional[threading.Thread] = None
        self.server_socket: Optional[socket.socket] = None

        # NumPy parser instance
        self.parser = PacketParser()

        # Debug/stats
        self.debug = debug
        self.unknown_packet_count = 0
        self.total_packets_parsed = 0

        if self.debug:
            self.valid_packet_buffer = deque(maxlen=10)
        else:
            self.valid_packet_buffer = None

        # Callback receives BatchParseResult
        self.data_callback: Optional[Callable[[BatchParseResult], None]] = None

        # Byte accumulator for batching
        self._byte_buffer = bytearray()
        self._byte_buffer_lock = threading.Lock()

    def set_data_callback(self, callback: Callable[[BatchParseResult], None]) -> None:
        """
        Set callback function called with BatchParseResult containing NumPy arrays.

        Args:
            callback: Function that takes BatchParseResult as argument
        """
        self.data_callback = callback

    def start(self) -> None:
        """Start the server and processing threads."""
        if self.running:
            logger.warning("Server is already running")
            return

        self.running = True
        self.client_disconnected_event.clear()

        self.socket_thread = threading.Thread(target=self._socket_listener, daemon=True)
        self.socket_thread.start()

        self.processor_thread = threading.Thread(target=self._data_processor, daemon=True)
        self.processor_thread.start()

        logger.info(f"Server started on {self.host}:{self.port}")

    def stop(self) -> None:
        """Stop the server and all threads."""
        if not self.running:
            logger.warning("Server is not running")
            return

        self.running = False

        if self.server_socket:
            self.server_socket.close()

        if self.socket_thread and self.socket_thread.is_alive():
            self.socket_thread.join(timeout=5)

        if self.processor_thread and self.processor_thread.is_alive():
            self.processor_thread.join(timeout=5)

        logger.info("Server stopped")

    def wait_for_client_disconnect(self, timeout: Optional[float] = None) -> bool:
        """Block until a client disconnects."""
        return self.client_disconnected_event.wait(timeout=timeout)

    def _socket_listener(self) -> None:
        """Thread that listens for connections and reads data."""
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            # Increase receive buffer for high throughput
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)

            self.server_socket.bind((self.host, self.port))
            self.server_socket.listen(5)

            logger.info(f"Listening on {self.host}:{self.port}")

            while self.running:
                try:
                    client_socket, client_address = self.server_socket.accept()
                    logger.info(f"Client connected from {client_address}")

                    # Set client socket buffer size
                    client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)

                    self.client_connected = True
                    self._handle_client(client_socket)

                    self.client_connected = False
                    self.client_disconnected_event.set()

                    if self.exit_on_disconnect:
                        logger.info("Client disconnected, shutting down")
                        self.running = False
                        break

                except socket.error as e:
                    if self.running:
                        logger.error(f"Socket error: {e}")
                    break

        except Exception as e:
            logger.error(f"Error in socket listener: {e}")
        finally:
            if self.server_socket:
                self.server_socket.close()

    def _handle_client(self, client_socket: socket.socket) -> None:
        """
        Handle client connection with optimized batched reading.

        Reads large chunks and batches them for efficient processing.
        Includes timeout-based flushing for low count rates.
        """
        # Read buffer size (read up to 64KB at once)
        READ_SIZE = 65536

        # Timeout for partial batch flush (seconds)
        FLUSH_TIMEOUT = 0.1

        # Local byte accumulator
        byte_buffer = bytearray()
        last_flush_time = time.time()

        # Set socket timeout for non-blocking reads
        client_socket.settimeout(FLUSH_TIMEOUT)

        try:
            while self.running:
                # Read a chunk of data
                try:
                    chunk = client_socket.recv(READ_SIZE)
                    if not chunk:
                        logger.info("Client disconnected")
                        break

                    byte_buffer.extend(chunk)

                    # When we have enough for a batch, queue it
                    while len(byte_buffer) >= self.batch_byte_size:
                        batch = bytes(byte_buffer[: self.batch_byte_size])
                        del byte_buffer[: self.batch_byte_size]

                        try:
                            self.message_queue.put(batch, timeout=1.0)
                            last_flush_time = time.time()
                        except queue.Full:
                            logger.warning("Message queue full, dropping batch")

                except socket.timeout:
                    # No data received - check if we should flush partial batch
                    pass
                except socket.error as e:
                    logger.error(f"Socket read error: {e}")
                    break

                # Flush partial batch if timeout exceeded and we have complete packets
                current_time = time.time()
                if current_time - last_flush_time >= FLUSH_TIMEOUT:
                    complete_packets = len(byte_buffer) // self.PACKET_SIZE
                    if complete_packets > 0:
                        flush_bytes = complete_packets * self.PACKET_SIZE
                        batch = bytes(byte_buffer[:flush_bytes])
                        del byte_buffer[:flush_bytes]

                        try:
                            self.message_queue.put(batch, timeout=1.0)
                            last_flush_time = current_time
                        except queue.Full:
                            logger.warning("Message queue full, dropping partial batch")

            # Flush remaining bytes on disconnect (if any complete packets)
            remaining_packets = len(byte_buffer) // self.PACKET_SIZE
            if remaining_packets > 0:
                final_bytes = remaining_packets * self.PACKET_SIZE
                try:
                    self.message_queue.put(bytes(byte_buffer[:final_bytes]), timeout=1.0)
                except queue.Full:
                    logger.warning("Could not flush final batch")

        except Exception as e:
            logger.error(f"Error handling client: {e}")
        finally:
            client_socket.close()

    def _data_processor(self) -> None:
        """
        Thread that processes byte batches using vectorized parsing.

        Receives raw bytes, parses with NumPy, calls callback with arrays.
        """
        logger.info("Data processor thread started (NumPy accelerated)")

        while self.running or not self.message_queue.empty():
            try:
                # Get a batch of bytes
                batch_bytes = self.message_queue.get(timeout=1.0)

                # Parse entire batch at once with NumPy
                result = self.parser.parse_batch(batch_bytes)

                # Update stats
                self.total_packets_parsed += result.n_pixels + result.n_tdc + result.n_control
                self.unknown_packet_count += result.n_unknown

                # Debug logging
                if self.debug and self.valid_packet_buffer is not None:
                    if result.n_pixels > 0:
                        self.valid_packet_buffer.append(
                            f"Pixel batch: {result.n_pixels} pixels, " f"x=[{result.pixel_x[0]}..{result.pixel_x[-1]}]"
                        )
                    if result.n_tdc > 0:
                        self.valid_packet_buffer.append(
                            f"TDC batch: {result.n_tdc} TDCs, " f"ch={result.tdc_channel[:3]}..."
                        )

                # Call callback with BatchParseResult
                if self.data_callback:
                    self.data_callback(result)

                self.message_queue.task_done()

            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Error processing batch: {e}", exc_info=True)

        logger.info("Data processor thread finished")

    def get_queue_size(self) -> int:
        """Get current message queue size."""
        return self.message_queue.qsize()

    def get_callback_buffer_size(self) -> int:
        """Get pending bytes (compatibility - returns 0 since we batch differently)."""
        return 0

    def get_unknown_packet_count(self) -> int:
        """Get count of unknown packet types."""
        return self.unknown_packet_count

    def get_valid_packet_samples(self) -> list:
        """Get samples of recently processed batches."""
        if self.valid_packet_buffer is not None:
            return list(self.valid_packet_buffer)
        return []


# =============================================================================
# EXAMPLE / TEST
# =============================================================================


def main():
    """Example usage of the NumPy-accelerated SocketDataServer."""
    import os

    import psutil

    server = SocketDataServer(
        host="localhost",
        port=9090,
        buffer_size=100,
        callback_batch_size=10000,
        debug=True,
    )

    # Stats
    total_pixels = 0
    total_tdcs = 0
    start_time = time.time()

    def data_callback(result: BatchParseResult):
        nonlocal total_pixels, total_tdcs
        total_pixels += result.n_pixels
        total_tdcs += result.n_tdc

        if result.n_pixels > 0:
            logger.debug(f"Received {result.n_pixels} pixels, {result.n_tdc} TDCs")

    server.set_data_callback(data_callback)

    try:
        server.start()
        process = psutil.Process(os.getpid())

        while True:
            time.sleep(2)
            elapsed = time.time() - start_time
            rate = total_pixels / elapsed if elapsed > 0 else 0
            mem = process.memory_info().rss / 1024**3

            print(
                f"Pixels: {total_pixels:.2e}, TDCs: {total_tdcs}, "
                f"Rate: {rate:.2e}/s, Mem: {mem:.2f} GB, "
                f"Queue: {server.get_queue_size()}"
            )

    except KeyboardInterrupt:
        print("\nShutting down...")
        server.stop()


if __name__ == "__main__":
    main()
