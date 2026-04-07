"""Shared pytest fixtures for the splash_timepix test suite."""

import time

import pytest

from splash_timepix.simulator import PacketSimulator, SimulatorConfig
from splash_timepix.socket_server import SocketDataServer
from tests.port_utils import get_free_port


@pytest.fixture
def test_port() -> int:
    """One free TCP port per test (for ``SocketDataServer`` and subprocesses)."""
    return get_free_port()


@pytest.fixture
def server(test_port: int):
    """Started :class:`~splash_timepix.socket_server.SocketDataServer`; stopped after the test."""
    srv = SocketDataServer(
        host="localhost",
        port=test_port,
        buffer_size=100,
        debug=False,
        callback_batch_size=10,
    )
    yield srv

    if srv.running:
        srv.stop()
    time.sleep(0.2)


@pytest.fixture
def simulator():
    """Packet simulator with test-friendly defaults."""
    config = SimulatorConfig(
        pixel_count_rate=1000,
        tdc_frequency=10.0,
        include_control_packets=False,
    )
    return PacketSimulator(config)
