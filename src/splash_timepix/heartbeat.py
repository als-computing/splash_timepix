"""ZMQ heartbeat publisher for app.py status monitoring.

Publishes periodic status messages so UI/orchestrators can detect
when the streaming server is ready to accept connections.
"""

import logging
import threading
import time
from enum import Enum
from typing import Any, Callable, Dict, Optional

import msgpack
import zmq

logger = logging.getLogger(__name__)


class ServerState(str, Enum):
    """Possible states of the streaming server."""

    STARTING = "starting"  # Server is initializing
    READY = "ready"  # Ready and waiting for client connection
    STREAMING = "streaming"  # Client connected, receiving data
    STOPPING = "stopping"  # Shutdown in progress


class HeartbeatPublisher:
    """Publishes periodic heartbeat messages via ZMQ PUB socket.

    Heartbeat message format (msgpack encoded dict):
        {
            'state': str,           # Current server state (see ServerState)
            'timestamp': float,     # Unix timestamp
            'uptime_s': float,      # Seconds since server started
            'pid': int,             # Process ID
            'data_port': int,       # Port for data ZMQ PUB socket
            'tcp_port': int,        # Port for TCP socket (live-cli connection)
            # Optional pipeline queue depths (when set via set_queue_stats_provider):
            'q_ingest_sz': int, 'q_ingest_max': int,   # TCP raw-batch queue
            'q_xyt_sz': int, 'q_xyt_max': int,         # 3D flush queue → ZMQ worker
            'q_ctrl_sz': int, 'q_ctrl_max': int,       # ZMQ start/stop control queue
        }

    Usage:
        heartbeat = HeartbeatPublisher(port=5658, data_port=5657, tcp_port=9090)
        heartbeat.start()
        heartbeat.set_state(ServerState.READY)
        # ... later ...
        heartbeat.set_state(ServerState.STREAMING)
        # ... on shutdown ...
        heartbeat.stop()
    """

    def __init__(
        self,
        port: int = 5658,
        data_port: int = 5657,
        tcp_port: int = 9090,
        interval: float = 1.0,
    ):
        """Initialize the heartbeat publisher.

        Args:
            port: ZMQ PUB port for heartbeat messages
            data_port: ZMQ PUB port for data (included in heartbeat for discovery)
            tcp_port: TCP port for live-cli connection (included in heartbeat)
            interval: Seconds between heartbeat messages
        """
        self.port = port
        self.data_port = data_port
        self.tcp_port = tcp_port
        self.interval = interval

        self.state = ServerState.STARTING
        self.start_time = time.time()
        self.pid = None

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._state_lock = threading.Lock()

        # ZMQ context and socket created in thread
        self._context: Optional[zmq.Context] = None
        self._socket: Optional[zmq.Socket] = None
        self._queue_stats_provider: Optional[Callable[[], Dict[str, Any]]] = None

    def set_queue_stats_provider(self, provider: Optional[Callable[[], Dict[str, Any]]]) -> None:
        """Provide a callable that returns extra heartbeat keys (e.g. queue depths).

        Called from the heartbeat thread once per publish; must be thread-safe and fast.
        """
        self._queue_stats_provider = provider

    def set_state(self, state: ServerState) -> None:
        """Update the current server state (thread-safe)."""
        with self._state_lock:
            if self.state != state:
                logger.info(f"Heartbeat state: {self.state.value} → {state.value}")
                self.state = state

    def get_state(self) -> ServerState:
        """Get the current server state (thread-safe)."""
        with self._state_lock:
            return self.state

    def start(self, pid: Optional[int] = None) -> None:
        """Start the heartbeat publisher thread.

        Args:
            pid: Process ID to include in heartbeat (uses current if not specified)
        """
        import os

        self.pid = pid or os.getpid()
        self.start_time = time.time()
        self._running = True

        self._thread = threading.Thread(target=self._publish_loop, daemon=True)
        self._thread.start()
        logger.info(f"Heartbeat publisher started on tcp://*:{self.port}")

    def stop(self) -> None:
        """Stop the heartbeat publisher thread."""
        self.set_state(ServerState.STOPPING)
        self._running = False

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

        logger.info("Heartbeat publisher stopped")

    def _publish_loop(self) -> None:
        """Main loop that publishes heartbeat messages."""
        self._context = zmq.Context()
        self._socket = self._context.socket(zmq.PUB)

        try:
            self._socket.bind(f"tcp://*:{self.port}")

            # Small delay for socket setup
            time.sleep(0.1)

            while self._running:
                self._send_heartbeat()

                # Sleep in small increments to allow faster shutdown
                sleep_remaining = self.interval
                while sleep_remaining > 0 and self._running:
                    time.sleep(min(0.1, sleep_remaining))
                    sleep_remaining -= 0.1

        except Exception as e:
            logger.error(f"Error in heartbeat publisher: {e}", exc_info=True)

        finally:
            if self._socket:
                self._socket.close()
            if self._context:
                self._context.term()

    def _send_heartbeat(self) -> None:
        """Send a single heartbeat message."""
        message = {
            "state": self.get_state().value,
            "timestamp": time.time(),
            "uptime_s": time.time() - self.start_time,
            "pid": self.pid,
            "data_port": self.data_port,
            "tcp_port": self.tcp_port,
        }
        if self._queue_stats_provider is not None:
            try:
                extra = self._queue_stats_provider()
                if extra:
                    message.update(extra)
            except Exception:
                logger.debug("queue_stats_provider failed", exc_info=True)

        try:
            self._socket.send(msgpack.packb(message), zmq.DONTWAIT)
            logger.debug(f"Heartbeat sent: {message['state']}, uptime={message['uptime_s']:.1f}s")
        except zmq.Again:
            pass  # No subscribers, that's fine
        except Exception as e:
            logger.error(f"Failed to send heartbeat: {e}")


def wait_for_ready(port: int = 5658, timeout: float = 30.0) -> bool:
    """Wait for the streaming server to become ready.

    Utility function for orchestrators/UI to wait until app.py is ready
    to accept connections.

    Args:
        port: Heartbeat ZMQ port
        timeout: Maximum seconds to wait

    Returns:
        True if server became ready, False if timeout
    """
    context = zmq.Context()
    socket = context.socket(zmq.SUB)

    try:
        socket.connect(f"tcp://localhost:{port}")
        socket.setsockopt(zmq.SUBSCRIBE, b"")
        socket.setsockopt(zmq.RCVTIMEO, int(timeout * 1000))

        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                msg_bytes = socket.recv()
                msg = msgpack.unpackb(msg_bytes)

                state = msg.get("state", "")
                if state in (ServerState.READY.value, ServerState.STREAMING.value):
                    logger.info(f"Server ready (state: {state})")
                    return True
                else:
                    logger.debug(f"Server state: {state}, waiting...")

            except zmq.Again:
                # Timeout on recv, check overall timeout
                continue

        logger.warning(f"Timeout waiting for server ready after {timeout}s")
        return False

    finally:
        socket.close()
        context.term()
