"""Workflow coverage aligned with ``splash_timepix.ui.main`` (Operator UI).

The UI coordinates these moving parts:

- **Streaming server** (`python -m splash_timepix.app`): TCP packet ingest, time-resolved
  binning, ZMQ PUB for ``start`` / ``event`` / ``stop``, and heartbeat PUB.
- **Ready gating**: ``MainWindow._check_server_ready`` treats heartbeat ``state`` of
  ``ready`` or ``streaming`` as “server up”.
- **ZMQ subscriber** (``ZmqSubscriberWorker``): ignores single-part control messages,
  reassembles multi-part ``event`` payloads for the Operator tab.

Tests here exercise that end-to-end contract (not every socket-server knob).
"""

import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import msgpack
import numpy as np
import pytest
import zmq

from splash_timepix.heartbeat import HeartbeatPublisher, ServerState
from splash_timepix.schemas import TimePixStart, TimePixStop
from splash_timepix.simulator import PacketSimulator, SimulatorConfig
from tests.port_utils import get_free_port


def _recv_zmq_messages(socket: zmq.Socket, until_stop: bool, deadline: float) -> dict:
    """Drain the SUB socket; return counts and last stop/start metadata."""
    out = {
        "start": [],
        "stop": [],
        "events": 0,
        "scan_names": set(),
    }
    socket.setsockopt(zmq.RCVTIMEO, 500)
    while time.time() < deadline:
        if until_stop and out["stop"]:
            break
        try:
            meta_bytes = socket.recv()
        except zmq.Again:
            continue
        meta = msgpack.unpackb(meta_bytes)
        msg_type = meta.get("msg_type")
        is_data = msg_type not in ("start", "stop")
        if msg_type == "start":
            out["start"].append(meta)
            if meta.get("scan_name"):
                out["scan_names"].add(meta["scan_name"])
        elif msg_type == "stop":
            out["stop"].append(meta)
        elif is_data:
            try:
                socket.recv()
            except zmq.Again:
                pass
            else:
                out["events"] += 1
    return out


@pytest.mark.integration
@pytest.mark.slow
def test_streaming_app_full_zmq_cycle_tcp_client_matches_ui_data_path():
    """Same ZMQ contract the UI expects: start → event(s) → stop after TCP disconnect.

    Uses a local TCP sender (no external simulator process): fastest check that
    ``app.main`` + ``zmq_worker`` + binning still match ``ZmqSubscriberWorker``.
    """
    repo_root = Path(__file__).resolve().parent.parent
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
        "100",
        "--flush-interval",
        "0.08",
        "--tdc-ch",
        "1",
        "--tdc-edge",
        "rising",
        "--collapse-y",
        "--exit-on-disconnect",
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=repo_root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    ctx = zmq.Context()
    data_sub = ctx.socket(zmq.SUB)
    hb_sub = ctx.socket(zmq.SUB)
    try:
        data_sub.connect(f"tcp://127.0.0.1:{zmq_port}")
        data_sub.setsockopt(zmq.SUBSCRIBE, b"")
        hb_sub.connect(f"tcp://127.0.0.1:{hb_port}")
        hb_sub.setsockopt(zmq.SUBSCRIBE, b"")

        # app.py sleeps ~2s after READY for ZMQ slow joiner
        time.sleep(2.6)
        if proc.poll() is not None:
            err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            pytest.fail(f"streaming server exited early (code={proc.returncode}): {err}")

        hb_states: list[str] = []
        hb_deadline = time.time() + 5.0
        hb_sub.setsockopt(zmq.RCVTIMEO, 500)
        while time.time() < hb_deadline and "ready" not in hb_states:
            try:
                raw = hb_sub.recv()
                hb_states.append(msgpack.unpackb(raw).get("state", ""))
            except zmq.Again:
                continue
        assert "ready" in hb_states, f"expected heartbeat ready, saw {hb_states!r}"

        def _send_stream() -> None:
            cfg = SimulatorConfig(
                pixel_count_rate=4000,
                tdc_frequency=100.0,
                include_control_packets=False,
            )
            sim = PacketSimulator(cfg)
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                client.connect(("127.0.0.1", tcp_port))
                # app.py updates heartbeat from the main loop with ~1s sleeps; keep the
                # socket open long enough that STREAMING is observed before disconnect.
                time.sleep(1.2)
                for chunk in sim.generate_stream(0.6):
                    client.sendall(chunk)
                time.sleep(1.2)
            finally:
                client.close()

        threading.Thread(target=_send_stream, daemon=True).start()

        saw_streaming = False
        hb_deadline = time.time() + 15.0
        while time.time() < hb_deadline:
            try:
                raw = hb_sub.recv()
                st = msgpack.unpackb(raw).get("state", "")
                if st == "streaming":
                    saw_streaming = True
                    break
            except zmq.Again:
                continue
        assert saw_streaming, "heartbeat never reached streaming after TCP connect"

        deadline = time.time() + 40.0
        collected = _recv_zmq_messages(data_sub, until_stop=True, deadline=deadline)

        try:
            proc.wait(timeout=25)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AssertionError("streaming server did not exit after client disconnect")

        err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        assert proc.returncode == 0, err

        assert len(collected["start"]) >= 1
        start = TimePixStart(**collected["start"][0])
        assert start.tdc_frequency_hz == 100.0
        assert start.collapse_y is True

        assert collected["events"] >= 1, "no ZMQ event messages — UI would see no heatmap updates"

        assert len(collected["stop"]) >= 1
        stop = TimePixStop(**collected["stop"][-1])
        assert stop.scan_name == start.scan_name
        assert stop.total_cycles >= 1
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        data_sub.close()
        hb_sub.close()
        ctx.term()


@pytest.mark.unit
def test_heartbeat_states_used_by_ui_ready_gate():
    """Strings in heartbeat messages match ``MainWindow._check_server_ready``."""
    port = get_free_port()
    hb = HeartbeatPublisher(port=port, data_port=0, tcp_port=0, interval=0.05)
    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    try:
        hb.start()
        sub.connect(f"tcp://127.0.0.1:{port}")
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        time.sleep(0.15)

        hb.set_state(ServerState.READY)
        sub.setsockopt(zmq.RCVTIMEO, 500)
        saw_ready = False
        deadline = time.time() + 3.0
        while time.time() < deadline:
            try:
                msg = msgpack.unpackb(sub.recv())
                if msg.get("state") == "ready":
                    saw_ready = True
                    break
            except zmq.Again:
                continue
        assert saw_ready

        hb.set_state(ServerState.STREAMING)
        saw_streaming = False
        deadline = time.time() + 3.0
        while time.time() < deadline:
            try:
                msg = msgpack.unpackb(sub.recv())
                if msg.get("state") == "streaming":
                    saw_streaming = True
                    break
            except zmq.Again:
                continue
        assert saw_streaming
    finally:
        hb.stop()
        sub.close()
        ctx.term()
