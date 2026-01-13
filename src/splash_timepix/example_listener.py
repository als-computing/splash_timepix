#!/usr/bin/env python3
"""
Example of using SplashTimePixZMQListener to process TimePix3 messages.

This demonstrates how to use the listener pattern similar to ArroyoXPS,
where the listener subscribes to ZMQ messages and converts them to schema objects,
which can then be processed by an operator.
"""

import logging
import numpy as np
from splash_timepix.listener import SplashTimePixZMQListener
from splash_timepix.schemas import TimePixStart, TimePixStop, TimePixEvent

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class SimpleTimePixOperator:
    """
    Simple operator that processes TimePix messages.
    
    Similar to XPSOperator in ArroyoXPS, but for TimePix3 data.
    """
    
    def __init__(self):
        self.current_scan_name = None
        self.total_flushes = 0
        self.total_counts = 0
    
    def process(self, message):
        """
        Process a message (TimePixStart, TimePixEvent, or TimePixStop).
        
        Args:
            message: TimePixStart, TimePixEvent, or TimePixStop instance
        """
        if isinstance(message, TimePixStart):
            self._handle_start(message)
        elif isinstance(message, TimePixEvent):
            self._handle_event(message)
        elif isinstance(message, TimePixStop):
            self._handle_stop(message)
    
    def _handle_start(self, message: TimePixStart):
        """Handle start message - initialize processing."""
        self.current_scan_name = message.scan_name
        self.total_flushes = 0
        self.total_counts = 0
        
        logger.info("=" * 60)
        logger.info(f"START: {message.scan_name}")
        logger.info(f"   TDC frequency: {message.tdc_frequency_hz} Hz")
        logger.info(f"   Detector: {message.detector_size_x} × {message.detector_size_y}")
        logger.info(f"   Time bins: {message.n_bins} × {message.t_delta_ns} ns")
        logger.info("=" * 60)
    
    def _handle_event(self, message: TimePixEvent):
        """Handle event message - process data array."""
        self.total_flushes += 1
        
        # Calculate statistics
        total_counts = np.sum(message.array)
        max_counts = np.max(message.array)
        self.total_counts += total_counts
        
        logger.info(
            f"Flush #{message.flush_number}: "
            f"total_counts={total_counts}, "
            f"max={max_counts}, "
            f"cycles={message.cycles_in_flush}"
        )
    
    def _handle_stop(self, message: TimePixStop):
        """Handle stop message - finalize processing."""
        logger.info("=" * 60)
        logger.info(f"STOP: {message.scan_name}")
        logger.info(f"   Total flushes: {message.total_flushes}")
        logger.info(f"   Total cycles: {message.total_cycles}")
        logger.info(f"   Duration: {message.acquisition_duration_s:.2f} s")
        logger.info(f"   Operator processed: {self.total_flushes} flushes, {self.total_counts} total counts")
        logger.info("=" * 60)


def main():
    """Main function - create operator and listener."""
    # Create operator
    operator = SimpleTimePixOperator()
    
    # Create listener with operator callback
    listener = SplashTimePixZMQListener(
        zmq_address="tcp://localhost:5657",
        operator=operator.process
    )
    
    logger.info("Starting listener...")
    logger.info("Press Ctrl+C to stop")
    
    try:
        # Start listening (blocks until stop() is called)
        listener.start()
    except KeyboardInterrupt:
        logger.info("Received interrupt, stopping...")
        listener.stop()
    finally:
        # Cleanup
        if listener.socket:
            listener.socket.close()
        if listener.context:
            listener.context.term()


if __name__ == "__main__":
    main()
