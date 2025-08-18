"""
Socket server that reads 5-byte messages from a socket and processes them into numpy arrays.

This module implements a multi-threaded server that:
1. Listens for incoming socket connections
2. Reads 5-byte messages from clients on one thread
3. Processes those messages into numpy arrays on another thread
"""

import socket
import threading
import queue
import struct
import numpy as np
import logging
import time
from typing import Optional, Callable


# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class SocketDataServer:
    """
    A multi-threaded server that reads 5-byte messages from a socket and converts them to numpy arrays.
    """
    
    def __init__(self, host: str = 'localhost', port: int = 8888, buffer_size: int = 1000):
        """
        Initialize the socket server.
        
        Args:
            host: The host address to bind to
            port: The port to bind to
            buffer_size: Maximum number of messages to buffer
        """
        self.host = host
        self.port = port
        self.buffer_size = buffer_size
        
        # Thread-safe queue for communication between threads
        self.message_queue = queue.Queue(maxsize=buffer_size)
        
        # Control flags
        self.running = False
        self.socket_thread: Optional[threading.Thread] = None
        self.processor_thread: Optional[threading.Thread] = None
        
        # Socket
        self.server_socket: Optional[socket.socket] = None
        
        # Storage for processed data
        self.data_array = np.array([], dtype=np.int32)
        self.data_lock = threading.Lock()
        
        # Callback for when new data is processed
        self.data_callback: Optional[Callable[[np.ndarray], None]] = None
    
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
    
    def _socket_listener(self) -> None:
        """
        Thread function that listens for socket connections and reads 5-byte messages.
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
                    
                    # Handle this client in a separate method
                    self._handle_client(client_socket)
                    
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
        Handle a single client connection, reading 5-byte messages.
        
        Args:
            client_socket: The client socket to read from
        """
        try:
            while self.running:
                # Read exactly 5 bytes
                data = b''
                while len(data) < 5:
                    chunk = client_socket.recv(5 - len(data))
                    if not chunk:
                        logger.info("Client disconnected")
                        return
                    data += chunk
                
                # Add the 5-byte message to the queue
                try:
                    self.message_queue.put(data, timeout=1.0)
                    logger.debug(f"Received 5-byte message: {data.hex()}")
                except queue.Full:
                    logger.warning("Message queue is full, dropping message")
                
        except socket.error as e:
            logger.error(f"Error handling client: {e}")
        finally:
            client_socket.close()
    
    def _data_processor(self) -> None:
        """
        Thread function that processes 5-byte messages from the queue into numpy arrays.
        """
        logger.info("Data processor thread started")
        
        while self.running or not self.message_queue.empty():
            try:
                # Get a message from the queue (with timeout to allow graceful shutdown)
                message = self.message_queue.get(timeout=1.0)
                
                # Process the 5-byte message
                # Convert the 5 bytes to a number (you can modify this based on your data format)
                # Here we're treating the first 4 bytes as an int32 and ignoring the 5th byte
                # You can modify this based on your specific data format
                if len(message) == 5:
                    # Example: first 4 bytes as little-endian int32, 5th byte as uint8
                    value = struct.unpack('<I', message[:4])[0]  # Little-endian unsigned int
                    extra_byte = message[4]
                    
                    # You could also interpret it differently, e.g.:
                    # value = struct.unpack('<f', message[:4])[0]  # As float
                    # Or use all 5 bytes in some other way
                    
                    logger.debug(f"Processed value: {value}, extra byte: {extra_byte}")
                    
                    # Add to numpy array (thread-safe)
                    with self.data_lock:
                        self.data_array = np.append(self.data_array, value)
                    
                    # Call the callback if set
                    if self.data_callback:
                        self.data_callback(np.array([value]))
                
                # Mark task as done
                self.message_queue.task_done()
                
            except queue.Empty:
                # Timeout occurred, continue loop to check if we should still be running
                continue
            except Exception as e:
                logger.error(f"Error processing message: {e}")
        
        logger.info("Data processor thread finished")
    
    def get_data_array(self) -> np.ndarray:
        """
        Get a copy of the current data array.
        
        Returns:
            A copy of the numpy array containing all processed data
        """
        with self.data_lock:
            return self.data_array.copy()
    
    def clear_data_array(self) -> None:
        """Clear the data array."""
        with self.data_lock:
            self.data_array = np.array([], dtype=np.int32)
    
    def get_queue_size(self) -> int:
        """Get the current size of the message queue."""
        return self.message_queue.qsize()


def main():
    """
    Example usage of the SocketDataServer.
    """
    # Create server
    server = SocketDataServer(host='localhost', port=8888, buffer_size=1000)
    
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
            
            # Print some stats every 10 seconds
            if int(time.time()) % 10 == 0:
                data = server.get_data_array()
                queue_size = server.get_queue_size()
                print(f"Data array size: {len(data)}, Queue size: {queue_size}")
                
                if len(data) > 0:
                    print(f"Latest values: {data[-5:] if len(data) >= 5 else data}")
    
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.stop()


if __name__ == "__main__":
    main()
