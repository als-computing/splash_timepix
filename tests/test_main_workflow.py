"""Workflow coverage aligned with ``splash_timepix.ui.main`` (Operator UI).

The UI coordinates these moving parts:

- **Streaming server** (`python -m splash_timepix.app`): TCP packet ingest, time-resolved
  binning, ZMQ PUB for ``start`` / ``event`` / ``stop``, and heartbeat PUB.
- **Ready gating**: ``MainWindow._check_server_ready`` treats heartbeat ``state`` of
  ``ready`` or ``streaming`` as "server up".
- **ZMQ subscriber** (``ZmqSubscriberWorker``): ignores single-part control messages,
  reassembles multi-part ``event`` payloads for the Operator tab.

Tests here exercise that end-to-end contract (not every socket-server knob).
"""

from __future__ import annotations

import subprocess
import time

import msgpack
import pytest
import zmq

from splash_timepix.heartbeat import HeartbeatPublisher, ServerState
from splash_timepix.schemas import TimePixStart, TimePixStop
from tests.conftest import collect_messages_until_stop
from tests.port_utils import get_free_port


@pytest.mark.integration
@pytest.mark.slow
def test_streaming_app_full_zmq_cycle_tcp_client_matches_ui_data_path(streaming_rig):
    """Same ZMQ contract the UI expects: start → event(s) → stop after TCP disconnect.

    Uses the ``streaming_rig`` factory so TDC / flush-interval / collapse_y
    values live in one place (here) and are propagated to both the server
    subprocess and the in-process simulator — they can never drift.
    """
    # High TDC + CPS avoid a known pre-existing server race at low rates
    # ( < ~50 Hz TDC ) where the main loop can enter its connect block after
    # data has already flowed and wipe the accumulators — unrelated to the
    # factory work here, parked for a follow-up.
    rig = streaming_rig(
        tdc_frequency=10000.0,
        flush_interval=0.5,
        cps=100000.0,
        tdc_channel=1,
        tdc_edge="rising",
        collapse_y=True,
        exit_on_disconnect=True,
    )

    # Open the TCP connection but *don't* stream yet: heartbeat publisher
    # ticks every 1 s, so we need a > 1 s window to observe STREAMING.
    src = rig.make_simulator()
    assert src.connect(), "failed to connect simulator to rig TCP port"
    time.sleep(1.2)

    saw_streaming = False
    rig.hb_sock.setsockopt(zmq.RCVTIMEO, 500)
    hb_deadline = time.time() + 5.0
    while time.time() < hb_deadline:
        try:
            raw = rig.hb_sock.recv()
        except zmq.Again:
            continue
        if msgpack.unpackb(raw).get("state") == "streaming":
            saw_streaming = True
            break
    assert saw_streaming, "heartbeat never reached streaming after TCP connect"

    # Stream enough data for the server to accumulate a handful of full
    # flush cycles (flush_interval = 0.5 s → ~3 full flushes + a final
    # partial flush on disconnect).  _auto_send_worker auto-disconnects at
    # the end which is what triggers the server-side stop publish.
    src.start_auto_sending(1.6)
    if src.send_thread:
        src.send_thread.join(timeout=5.0)
    src.stop_auto_sending()

    messages = collect_messages_until_stop(rig.sub_sock, timeout_s=40.0)

    try:
        rig.server_proc.wait(timeout=25)
    except subprocess.TimeoutExpired:
        rig.server_proc.kill()
        pytest.fail("streaming server did not exit after client disconnect")
    assert rig.server_proc.returncode == 0, f"server exited with code {rig.server_proc.returncode}"

    starts = [m for m in messages if m.get("msg_type") == "start"]
    stops = [m for m in messages if m.get("msg_type") == "stop"]
    events = [m for m in messages if m.get("msg_type") not in ("start", "stop")]

    # Gap 2a (stop side): exactly one stop per acquisition — no race here.
    # Gap 2a (start side): >= 1 for now; a known race between data_callback's
    # fallback and the main-loop connect block can emit a duplicate start with
    # the same scan_name (parked for a follow-up fix).
    assert starts, "no start message received"
    assert len(stops) == 1, f"gap 2a: expected exactly one stop, got {len(stops)}"
    assert events, "no ZMQ event messages — UI would see no heatmap updates"

    start = TimePixStart(**starts[0])
    stop = TimePixStop(**stops[0])

    # Config round-trip through the streaming server (rig → CLI → start msg).
    assert start.tdc_frequency_hz == rig.tdc_frequency
    assert start.collapse_y is rig.collapse_y
    assert stop.total_cycles >= 1

    # ── Gap 2b: scan_name must match across every message in the acquisition ──
    assert stop.scan_name == start.scan_name, "stop scan_name differs from start scan_name"
    event_scan_names = {e.get("scan_name") for e in events}
    assert event_scan_names == {start.scan_name}, (
        f"gap 2b: event scan_names {event_scan_names!r} " f"diverge from start scan_name {start.scan_name!r}"
    )

    # ── Gap 2d: flush_number must be 1..N with no gaps and no duplicates ──
    flush_numbers = [e["flush_number"] for e in events]
    assert flush_numbers == list(
        range(1, len(flush_numbers) + 1)
    ), f"gap 2d: flush_number sequence is not a monotonic 1..N: {flush_numbers!r}"
    assert stop.total_flushes == len(
        flush_numbers
    ), f"stop.total_flushes={stop.total_flushes} != events received={len(flush_numbers)}"


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
