"""Shared pytest fixtures for the splash_timepix test suite."""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import msgpack
import pytest
import zmq

from splash_timepix.heartbeat import wait_for_ready
from splash_timepix.simulator import PacketSimulator, SimulatorConfig
from splash_timepix.simulator_cli import SimulatorSource
from splash_timepix.socket_server import SocketDataServer
from tests.port_utils import get_free_port

REPO_ROOT = Path(__file__).resolve().parent.parent


def collect_messages_until_stop(sock: zmq.Socket, timeout_s: float = 12.0) -> List[dict]:
    """Drain ``sock`` until a ``stop`` message arrives or ``timeout_s`` elapses.

    Returns messages in chronological order. For two-part ``event`` messages
    the second (array-bytes) frame is consumed but *not* parsed; the
    metadata dict is extended with ``_array_bytes`` (length in bytes) so
    tests can still assert sanity without deserializing ndarrays.
    """
    messages: List[dict] = []
    sock.setsockopt(zmq.RCVTIMEO, 800)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            meta_bytes = sock.recv()
        except zmq.Again:
            if any(m.get("msg_type") == "stop" for m in messages):
                break
            continue
        meta = msgpack.unpackb(meta_bytes)
        msg_type = meta.get("msg_type")
        if msg_type in ("start", "stop"):
            messages.append(meta)
            if msg_type == "stop":
                break
        else:
            try:
                array_bytes = sock.recv()
                meta["_array_bytes"] = len(array_bytes)
            except zmq.Again:
                pass
            messages.append(meta)
    return messages


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


# ---------------------------------------------------------------------------
# Integration rig: streaming server subprocess + ZMQ sub + simulator factory
# ---------------------------------------------------------------------------


@dataclass
class StreamingRig:
    """All the moving parts needed by a subprocess-backed integration test.

    The rig owns:

    - a ``splash_timepix.app`` subprocess on dynamic ports,
    - a ZMQ SUB socket already connected and subscribed to the data PUB,
    - a ZMQ SUB socket already connected and subscribed to the heartbeat PUB,
    - the config values (tdc_frequency / flush_interval / cps) so simulator
      factories here are guaranteed to match what the server was started with.

    Use :meth:`make_simulator` to build an in-process :class:`SimulatorSource`
    preconfigured for this rig, or :meth:`spawn_simulator_cli` to launch
    ``splash_timepix.simulator_cli`` as a subprocess (for CLI-path coverage).
    """

    server_proc: subprocess.Popen
    tcp_port: int
    zmq_port: int
    hb_port: int
    tdc_frequency: float
    flush_interval: float
    cps: float
    collapse_y: bool
    sub_sock: zmq.Socket
    hb_sock: zmq.Socket
    _ctx: zmq.Context
    _simulators: List[SimulatorSource] = field(default_factory=list)
    _sim_procs: List[subprocess.Popen] = field(default_factory=list)

    def make_simulator(
        self,
        *,
        cps: Optional[float] = None,
        tdc_frequency: Optional[float] = None,
        counting: bool = False,
    ) -> SimulatorSource:
        """Return a fresh :class:`SimulatorSource` matched to this rig."""
        src = SimulatorSource(host="localhost", port=self.tcp_port)
        src.set_counts_per_second(cps if cps is not None else self.cps)
        src.set_tdc_frequency(tdc_frequency if tdc_frequency is not None else self.tdc_frequency)
        src.set_counting(counting)
        self._simulators.append(src)
        return src

    def spawn_simulator_cli(
        self,
        *,
        duration: float,
        cps: Optional[float] = None,
        tdc_frequency: Optional[float] = None,
        counting: bool = False,
    ) -> subprocess.Popen:
        """Spawn ``splash_timepix.simulator_cli --auto-start`` against this rig."""
        cmd = [
            sys.executable,
            "-m",
            "splash_timepix.simulator_cli",
            "--auto-start",
            "--port",
            str(self.tcp_port),
            "--tdc-frequency",
            str(tdc_frequency if tdc_frequency is not None else self.tdc_frequency),
            "--cps",
            str(cps if cps is not None else self.cps),
            # simulator_cli's --duration is typed as int; round up so callers
            # can still pass floats for ergonomics.
            "--duration",
            str(max(1, int(round(duration)))),
        ]
        if not counting:
            cmd.append("--no-count")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            cwd=REPO_ROOT,
        )
        self._sim_procs.append(proc)
        return proc


@pytest.fixture
def streaming_rig():
    """Factory fixture for a streaming-server integration rig.

    Usage::

        def test_something(streaming_rig):
            rig = streaming_rig(tdc_frequency=10, cps=1000, flush_interval=1.0)
            src = rig.make_simulator()
            src.run_blocking(2.0)
            msgs = _collect_zmq_until_stop(rig.sub_sock)
            ...

    Single source of truth for test config: the params passed here are
    propagated to both the server subprocess CLI flags and any simulator
    created via :meth:`StreamingRig.make_simulator` /
    :meth:`StreamingRig.spawn_simulator_cli`, so the TDC frequency the
    server expects can never drift from the one the simulator emits.
    """
    rigs: List[StreamingRig] = []

    def _factory(
        *,
        tdc_frequency: float = 10.0,
        cps: float = 1000.0,
        flush_interval: float = 1.0,
        exit_on_disconnect: bool = False,
        collapse_y: bool = False,
        tdc_channel: int = 0,
        tdc_edge: str = "rising",
        ready_timeout: float = 10.0,
    ) -> StreamingRig:
        tcp_port = get_free_port()
        zmq_port = get_free_port()
        hb_port = get_free_port()

        cmd = [
            sys.executable,
            "-m",
            "splash_timepix.app",
            "--host",
            "localhost",
            "--port",
            str(tcp_port),
            "--zmq-port",
            str(zmq_port),
            "--heartbeat-port",
            str(hb_port),
            "--tdc-frequency",
            str(tdc_frequency),
            "--flush-interval",
            str(flush_interval),
            "--tdc-ch",
            str(tdc_channel),
            "--tdc-edge",
            tdc_edge,
        ]
        if collapse_y:
            cmd.append("--collapse-y")
        if exit_on_disconnect:
            cmd.append("--exit-on-disconnect")

        # Server stdout/stderr → DEVNULL.  PIPE would deadlock the server
        # once its OS buffer fills (nothing in the test process drains it)
        # and our high-rate tests run long enough to make that a real risk.
        # To debug failures, swap these to an open log file handle (and set
        # stderr=subprocess.STDOUT) — the server logs heartbeat state
        # transitions, client connect/disconnect, and each flush.
        server_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=REPO_ROOT,
        )

        # Wait for the server's own READY heartbeat before proceeding.  The
        # server publishes READY *before* it enters its 2 s "wait for subs to
        # connect" sleep, so seeing READY is only half the story: the main
        # loop isn't iterating yet when this returns.
        if not wait_for_ready(port=hb_port, timeout=ready_timeout):
            server_proc.terminate()
            pytest.fail(f"streaming server did not reach ready within {ready_timeout}s")

        ctx = zmq.Context()
        sub_sock = ctx.socket(zmq.SUB)
        sub_sock.connect(f"tcp://127.0.0.1:{zmq_port}")
        sub_sock.setsockopt(zmq.SUBSCRIBE, b"")

        hb_sock = ctx.socket(zmq.SUB)
        hb_sock.connect(f"tcp://127.0.0.1:{hb_port}")
        hb_sock.setsockopt(zmq.SUBSCRIBE, b"")

        # Cover the server's internal `wait_after_start = 2 s` window (the
        # deliberate slow-joiner grace period it sleeps between READY and the
        # main loop's first iteration).  Matches the old hardcoded
        # time.sleep(2.5) in pre-rig tests but is now anchored to the READY
        # signal above rather than to absolute wall-clock time since spawn.
        time.sleep(2.2)

        rig = StreamingRig(
            server_proc=server_proc,
            tcp_port=tcp_port,
            zmq_port=zmq_port,
            hb_port=hb_port,
            tdc_frequency=tdc_frequency,
            flush_interval=flush_interval,
            cps=cps,
            collapse_y=collapse_y,
            sub_sock=sub_sock,
            hb_sock=hb_sock,
            _ctx=ctx,
        )
        rigs.append(rig)
        return rig

    yield _factory

    for rig in rigs:
        for sim_proc in rig._sim_procs:
            if sim_proc.poll() is None:
                sim_proc.terminate()
                try:
                    sim_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    sim_proc.kill()
        for sim in rig._simulators:
            try:
                sim.stop_auto_sending()
            except Exception:
                pass

        if rig.server_proc.poll() is None:
            rig.server_proc.terminate()
            try:
                rig.server_proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                rig.server_proc.kill()

        try:
            rig.sub_sock.close()
            rig.hb_sock.close()
            rig._ctx.term()
        except Exception:
            pass
