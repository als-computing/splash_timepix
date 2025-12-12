"""Socket server that reads TimePix3 messages and processes them into data.

This module implements a multi-threaded server that:
1. Listens for incoming socket connections
2. Reads 12-byte messages from data sources (TimePix3) on one thread
3. Processes those messages into data on another thread. 
This data can be used for downstream UI and data analysis applications.
"""

import logging
import queue
from collections import deque
import socket
import struct
import threading
import time
from typing import Callable, Optional
import numpy as np

from splash_timepix.parser import PacketParser, PacketType, PixelPacket, TDCPacket, ControlPacket


# Configure logging -> to console
#                   -> to ring buffer displaying last N errors
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

class RingBufferHandler(logging.Handler):
    """Logging handler that keeps only the last N log records in a ring buffer."""
    
    def __init__(self, capacity=10):
        super().__init__()
        self.buffer = deque(maxlen=capacity)
    
    def emit(self, record):
        # Format and store the log message
        msg = self.format(record)
        self.buffer.append(msg)
    
    def get_logs(self):
        """Return all logs in the buffer as a list."""
        return list(self.buffer)
    
    def clear(self):
        """Clear the buffer."""
        self.buffer.clear()


class SocketDataServer:
    """
    A multi-threaded server that reads 12-byte TimePix3 messages from a socket,
    and averages all pixel events into one 2D image (numpy array) for now.
    """
    def __init__(
        self, host: str = "localhost", port: int = 9090, buffer_size: int = 1000, 
        debug: bool = False, callback_batch_size: int = 1000,
        exit_on_disconnect: bool = False
    ):
        """
        Initialize the socket server.

        Args:
            host: The host address to bind to
            port: The port to bind to
            buffer_size: Maximum number of messages to buffer
            debug: Enable debug logging and packet buffer
            callback_batch_size: Number of packets to batch before callback
            exit_on_disconnect: If True, stop server when client disconnects
        """
        self.host = host
        self.port = port
        self.buffer_size = buffer_size

        # Thread-safe queue for communication between threads
        self.message_queue = queue.Queue(maxsize=buffer_size)

        # Control flags
        self.running = False
        self.exit_on_disconnect = exit_on_disconnect
        self.client_connected = False
        self.client_disconnected_event = threading.Event()
        
        self.socket_thread: Optional[threading.Thread] = None
        self.processor_thread: Optional[threading.Thread] = None

        # Socket
        self.server_socket: Optional[socket.socket] = None

        # Parser instance
        self.parser = PacketParser()
        
        # Debugging
        self.debug = debug
        self.unknown_packet_count = 0  # count instances of unknown packet type
        if self.debug:
            self.valid_packet_buffer = deque(maxlen=10)  # Keep last 10 valid packets
        else:
            self.valid_packet_buffer = None

        # Callback for when new data is processed
        self.data_callback: Optional[Callable[[np.ndarray], None]] = None

        # Callback batching
        self.callback_batch_size = callback_batch_size
        self.callback_buffer = []


    def set_data_callback(self, callback: Callable[[np.ndarray], None]) -> None:
        """
        Set a callback function that will be called when new data is processed.

        Args:
            callback: Function that takes a numpy array as argument
        """
        self.data_callback = callback


    def start(self) -> None:
        """Start the server and both processing threads."""
        if self.running:
            logger.warning("Server is already running")
            return

        self.running = True
        self.client_disconnected_event.clear()

        # Start the socket listener thread
        self.socket_thread = threading.Thread(target=self._socket_listener, daemon=True)
        self.socket_thread.start()

        # Start the data processor thread
        self.processor_thread = threading.Thread(target=self._data_processor, daemon=True)
        self.processor_thread.start()

        logger.info(f"Server started on {self.host}:{self.port}")


    def stop(self) -> None:
        """Stop the server and all threads."""
        if not self.running:
            logger.warning("Server is not running")
            return

        self.running = False

        # Close the server socket
        if self.server_socket:
            self.server_socket.close()

        # Wait for threads to finish
        if self.socket_thread and self.socket_thread.is_alive():
            self.socket_thread.join(timeout=5)

        if self.processor_thread and self.processor_thread.is_alive():
            self.processor_thread.join(timeout=5)

        logger.info("Server stopped")

    
    def wait_for_client_disconnect(self, timeout: Optional[float] = None) -> bool:
        """
        Block until a client disconnects (useful for exit_on_disconnect mode).
        
        Args:
            timeout: Maximum time to wait in seconds, or None for indefinite
            
        Returns:
            True if client disconnected, False if timeout occurred
        """
        return self.client_disconnected_event.wait(timeout=timeout)


    def _socket_listener(self) -> None:
        """
        Thread function that listens for socket connections and reads messages.
        """
        try:
            # Create and configure server socket
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.bind((self.host, self.port))
            self.server_socket.listen(5)

            logger.info(f"Listening for connections on {self.host}:{self.port}")

            while self.running:
                try:
                    # Accept client connection
                    client_socket, client_address = self.server_socket.accept()
                    logger.info(f"Client connected from {client_address}")
                    self.client_connected = True

                    # Handle this client in a separate method
                    self._handle_client(client_socket)
                    
                    # Client has disconnected
                    self.client_connected = False
                    self.client_disconnected_event.set()
                    
                    if self.exit_on_disconnect:
                        logger.info("Client disconnected, shutting down (--exit-on-disconnect)")
                        self.running = False
                        break

                except socket.error as e:
                    if self.running:  # Only log if we're still supposed to be running
                        logger.error(f"Socket error: {e}")
                    break

        except Exception as e:
            logger.error(f"Error in socket listener: {e}")
        finally:
            if self.server_socket:
                self.server_socket.close()


    def _handle_client(self, client_socket: socket.socket) -> None:
        """
        Handle a single client connection, reading messages.

        Args:
            client_socket: The client socket to read from
        """
        try:
            while self.running:
                # Read exactly 12 bytes
                data = b""
                while len(data) < 12:
                    chunk = client_socket.recv(12 - len(data))
                    if not chunk:
                        logger.info("Client disconnected")
                        return
                    data += chunk

                # Add the 12-byte message to the queue
                try:
                    self.message_queue.put(data, timeout=1.0)
                    logger.debug(f"Received 12-byte message: {data.hex()}")
                except queue.Full:
                    logger.warning("Message queue is full, dropping message")

        except socket.error as e:
            logger.error(f"Error handling client: {e}")
        finally:
            client_socket.close()


    def _data_processor(self) -> None:
        """
        Thread function that processes messages from the queue into numpy arrays.
        """
        logger.info("Data processor thread started")
        while self.running or not self.message_queue.empty():
            try:
                # Get a message from the queue (with timeout to allow graceful shutdown)
                message = self.message_queue.get(timeout=1.0)
                # Parse the packet directly
                packet = self.parser.parse(message)

                # Add Pixel/ TDC/ Control packets to callback buffer in batches
                if isinstance(packet, PixelPacket):
                    if self.data_callback:
                        self.callback_buffer.append(packet)
                        if len(self.callback_buffer) >= self.callback_batch_size:
                            self.data_callback(self.callback_buffer[:])
                            self.callback_buffer.clear()
                    
                    if self.debug:
                        logger.debug(f"Received Pixel packet: x={packet.x}, y={packet.y}")
                        if self.valid_packet_buffer is not None:
                            self.valid_packet_buffer.append(f"Pixel: x={packet.x}, y={packet.y}, raw={message.hex()}")
                    
                elif isinstance(packet, TDCPacket):
                    if self.data_callback:
                        self.callback_buffer.append(packet)
                        if len(self.callback_buffer) >= self.callback_batch_size:
                            self.data_callback(self.callback_buffer[:])
                            self.callback_buffer.clear()
                    if self.debug:
                        logger.debug(f"Received TDC packet: {packet}")
                        if self.valid_packet_buffer is not None:
                            self.valid_packet_buffer.append(f"TDC: ch={packet.channel}, edge={packet.edge}, raw={message.hex()}")
                    
                elif isinstance(packet, ControlPacket):
                    if self.data_callback:
                        self.callback_buffer.append(packet)
                        if len(self.callback_buffer) >= self.callback_batch_size:
                            self.data_callback(self.callback_buffer[:])
                            self.callback_buffer.clear()
                    if self.debug:
                        logger.debug(f"Received Control packet: {packet}")
                        if self.valid_packet_buffer is not None:
                            self.valid_packet_buffer.append(f"Control: {packet}, raw={message.hex()}")

                else:
                    logger.warning(f"Unknown packet type: {type(packet)}, raw data: {message.hex()}")
                    self.unknown_packet_count += 1

                # Mark task as done
                self.message_queue.task_done()

            except queue.Empty:
                # Timeout occurred, flush any pending callbacks during idle
                if self.data_callback and self.callback_buffer:
                    self.data_callback(self.callback_buffer[:])
                    self.callback_buffer.clear()
                continue
            except Exception as e:
                logger.error(f"Error processing message: {e}")

        # Flush any remaining buffered callbacks before exit
        if self.data_callback and self.callback_buffer:
            self.data_callback(self.callback_buffer[:])
            self.callback_buffer.clear()
        
        logger.info("Data processor thread finished")


    def get_queue_size(self) -> int:
        """Get the current size of the message queue."""
        return self.message_queue.qsize()
    

    def get_callback_buffer_size(self) -> int:
        """Get the current size of the callback buffer."""
        return len(self.callback_buffer)
    

    def get_unknown_packet_count(self) -> int:
        """Get the count of unknown packet types received."""
        return self.unknown_packet_count


    def get_valid_packet_samples(self) -> list:
        """Get samples of recently received valid packets."""
        if self.valid_packet_buffer is not None:
            return list(self.valid_packet_buffer)
        return []


def main():
    """
    Example usage of the SocketDataServer.
    """
    # Create server
    server = SocketDataServer(host="localhost", port=9090, buffer_size=1000)

    # Set up a callback to print new data
    def data_callback(new_data):
        print(f"New data received: {new_data}")

    server.set_data_callback(data_callback)

    try:
        # Start the server
        server.start()

        # Keep the main thread alive
        while True:
            time.sleep(1)

            # Print/update stats every 10 seconds
            if int(time.time()) % 10 == 0:
                data = server.get_data_array()
                queue_size = server.get_queue_size()
                print(f"Total counts (pixel events): {np.sum(data)}, Queue size: {queue_size}")


    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.stop()


if __name__ == "__main__":
    main()
    