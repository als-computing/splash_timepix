"""
ZMQ Listener for TimePix3 data acquisition messages.

This module provides a listener that subscribes to ZMQ messages published by
splash_timepix and converts them into schema objects (TimePixStart, TimePixEvent, TimePixStop).

Similar to XPSLabviewZMQListener in ArroyoXPS, but adapted for TimePix3 messages.
"""

import logging
import msgpack
import numpy as np
import zmq
from typing import Optional, Callable

from .schemas import TimePixStart, TimePixStop, TimePixEvent

logger = logging.getLogger(__name__)


class SplashTimePixZMQListener:
    """
    ZMQ Listener that subscribes to splash_timepix messages and converts them to schema objects.
    
    This listener subscribes to the ZMQ PUB socket from splash_timepix and:
    1. Receives start/stop/event messages
    2. Converts them to TimePixStart, TimePixStop, TimePixEvent schema objects
    3. Calls a callback function (operator) with each message
    
    Usage:
        def my_operator(message):
            if isinstance(message, TimePixStart):
                # Initialize processing
                pass
            elif isinstance(message, TimePixEvent):
                # Process data array
                pass
            elif isinstance(message, TimePixStop):
                # Finalize processing
                pass
        
        listener = SplashTimePixZMQListener(
            zmq_address="tcp://localhost:5657",
            operator=my_operator
        )
        listener.start()  # Blocks until stopped
    """
    
    def __init__(
        self,
        zmq_address: str = "tcp://localhost:5657",
        operator: Optional[Callable] = None,
        timeout_ms: int = 1000
    ):
        """
        Initialize the listener.
        
        Args:
            zmq_address: ZMQ address to subscribe to (default: "tcp://localhost:5657")
            operator: Callback function to process messages (optional)
            timeout_ms: Receive timeout in milliseconds (default: 1000)
        """
        self.zmq_address = zmq_address
        self.operator = operator
        self.timeout_ms = timeout_ms
        self.stop_signal = False
        
        # ZMQ context and socket (created in start())
        self.context: Optional[zmq.Context] = None
        self.socket: Optional[zmq.Socket] = None
    
    def start(self):
        """
        Start listening for messages. Blocks until stop() is called.
        
        This method:
        1. Creates ZMQ SUB socket and connects to publisher
        2. Receives messages and converts them to schema objects
        3. Calls operator callback for each message
        """
        logger.info(f"Starting SplashTimePixZMQListener, connecting to {self.zmq_address}")
        
        # Create ZMQ context and socket
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.SUB)
        
        # Set receive timeout
        self.socket.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
        
        # Set high-water mark to prevent memory issues
        self.socket.setsockopt(zmq.RCVHWM, 100)
        
        # Connect and subscribe to all messages
        self.socket.connect(self.zmq_address)
        self.socket.setsockopt(zmq.SUBSCRIBE, b"")
        
        logger.info("Connected and subscribed, waiting for messages...")
        
        while not self.stop_signal:
            try:
                # Receive first part (always present - metadata)
                metadata_bytes = self.socket.recv()
                
                # Unpack metadata
                metadata = msgpack.unpackb(metadata_bytes)
                msg_type = metadata.get('msg_type')
                
                # Handle different message types
                if msg_type == 'start':
                    message = self._build_start(metadata)
                    if self.operator:
                        self.operator(message)
                    logger.info(f"Processed start message: {message.scan_name}")
                    
                elif msg_type == 'stop':
                    message = self._build_stop(metadata)
                    if self.operator:
                        self.operator(message)
                    logger.info(f"Processed stop message: {message.scan_name}")
                    
                elif msg_type == 'event' or msg_type is None:
                    # Event message or data flush (may not have msg_type in old format)
                    # Try to receive second part (array data)
                    try:
                        # Set short timeout for second part
                        self.socket.setsockopt(zmq.RCVTIMEO, 1000)
                        array_bytes = self.socket.recv()
                        # Reset timeout
                        self.socket.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
                        
                        message = self._build_event(metadata, array_bytes)
                        if self.operator:
                            self.operator(message)
                        logger.debug(f"Processed event message: flush #{message.flush_number}")
                    except zmq.Again:
                        # No second part - might be a control message without msg_type
                        logger.warning("Expected array data but none received, skipping message")
                        continue
                else:
                    logger.warning(f"Unknown message type: {msg_type}, skipping")
                    
            except zmq.Again:
                # Timeout - check stop signal and continue
                continue
            except Exception as e:
                logger.error(f"Error processing message: {e}", exc_info=True)
                continue
        
        logger.info("Listener stopped")
    
    def stop(self):
        """Stop the listener."""
        logger.info("Stopping listener...")
        self.stop_signal = True
    
    def _build_start(self, metadata: dict) -> TimePixStart:
        """Build TimePixStart from metadata dict."""
        return TimePixStart(**metadata)
    
    def _build_stop(self, metadata: dict) -> TimePixStop:
        """Build TimePixStop from metadata dict."""
        return TimePixStop(**metadata)
    
    def _build_event(self, metadata: dict, array_bytes: bytes) -> TimePixEvent:
        """Build TimePixEvent from metadata dict and array bytes."""
        # Reconstruct numpy array
        shape = tuple(metadata['shape'])
        dtype = metadata['dtype']
        array = np.frombuffer(array_bytes, dtype=dtype).reshape(shape)
        
        # Create event object with array
        event_data = metadata.copy()
        event_data['array'] = array
        event_data['msg_type'] = 'event'  # Ensure msg_type is set
        
        return TimePixEvent(**event_data)
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - cleanup."""
        self.stop()
        if self.socket:
            self.socket.close()
        if self.context:
            self.context.term()
