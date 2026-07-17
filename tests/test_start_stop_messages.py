"""
Integration test for the ZMQ control plane used by the Operator UI.

``splash_timepix.ui.main`` relies on:

- ``start`` / ``event`` / ``stop`` messages on the data PUB socket (port 5657 by default).
- The subscriber connecting before the data source starts (ZMQ slow joiner).

The integration tests share a ``streaming_rig`` factory fixture (see
``tests/conftest.py``) so TDC / flush-interval / port parameters live in one
place and can never drift between the streaming server and the simulator that
feeds it.
"""

from __future__ import annotations

import subprocess
import time
from typing import List

import numpy as np
import pytest

from splash_timepix.schemas import TimePixEvent, TimePixStart, TimePixStop
from tests.conftest import collect_messages_until_stop


def _split(messages: List[dict]):
    """Partition a message stream into (starts, stops, events)."""
    starts = [m for m in messages if m.get("msg_type") == "start"]
    stops = [m for m in messages if m.get("msg_type") == "stop"]
    events = [m for m in messages if m.get("msg_type") not in ("start", "stop")]
    return starts, stops, events


def _assert_baseline_run_invariants(
    starts: List[dict],
    stops: List[dict],
    events: List[dict],
    run_label: str,
) -> str:
    """Parity invariants: start+stop+event presence, start.scan == stop.scan.

    These match what the pre-rig tests asserted.  We intentionally do *not*
    check event-level scan_name cohesion nor strict monotonic flush_number:
    a pre-existing race between ``data_callback``'s flush path and the
    main-loop client-connect block (which polls once per second) can publish
    flushes with ``scan_name=None`` and/or a pre-reset ``flush_number``
    before the reset lands.  That bug is parked for a follow-up; see the
    ``test_final_flush_sent_before_stop`` and ``test_streaming_app_full_…``
    tests for stricter assertions under race-free configurations.
    """
    assert starts, f"{run_label}: no start message received"
    assert len(stops) == 1, f"{run_label}: expected exactly one stop, got {len(stops)}"
    assert events, f"{run_label}: no event messages received"

    scan = starts[0]["scan_name"]
    assert (
        stops[0]["scan_name"] == scan
    ), f"{run_label}: stop.scan_name {stops[0]['scan_name']!r} != start.scan_name {scan!r}"
    return scan


def _assert_strict_run_invariants(
    starts: List[dict],
    stops: List[dict],
    events: List[dict],
    run_label: str,
) -> str:
    """Stricter invariants for tests whose timing avoids the race:

    - ≥ 1 start (duplicate starts with same scan_name allowed), exactly 1 stop.
    - scan_name cohesion across start(s), every event, and stop.
    - flush_number is a contiguous 1..N (no gaps, no duplicates).
    - stop.total_flushes == len(events).

    Requires the test to wait long enough after connect for the server's
    1 s main-loop poll to land the reset *before* any data flows — or to
    use a ``flush_interval`` large enough that no full flush can fire in
    the 1 s race window.
    """
    assert starts, f"{run_label}: no start message received"
    assert len(stops) == 1, f"{run_label}: expected exactly one stop, got {len(stops)}"
    assert events, f"{run_label}: no event messages received"

    scan = starts[0]["scan_name"]
    start_scans = {s["scan_name"] for s in starts}
    assert start_scans == {scan}, f"{run_label}: start messages disagree on scan_name: {start_scans!r}"
    assert (
        stops[0]["scan_name"] == scan
    ), f"{run_label}: stop.scan_name {stops[0]['scan_name']!r} != start.scan_name {scan!r}"

    event_scans = {e.get("scan_name") for e in events}
    assert event_scans == {scan}, f"{run_label}: event scan_names {event_scans!r} diverge from start {scan!r}"

    flush_numbers = [e["flush_number"] for e in events]
    assert flush_numbers == list(
        range(1, len(flush_numbers) + 1)
    ), f"{run_label}: flush_number sequence is not monotonic 1..N: {flush_numbers!r}"
    assert stops[0]["total_flushes"] == len(
        flush_numbers
    ), f"{run_label}: stop.total_flushes={stops[0]['total_flushes']} != events={len(flush_numbers)}"
    return scan


@pytest.mark.integration
@pytest.mark.slow
def test_simulator_pipeline_delivers_start_and_flush_events(streaming_rig):
    """Server + simulator subprocesses: start message, event payloads, schema fields.

    Covers the ``simulator_cli`` CLI parsing path (hence the subprocess
    launch) plus schema validity on every received ZMQ message.  We use
    :func:`_assert_baseline_run_invariants` rather than the strict variant
    because this test sends data immediately on connect, so the 1 s
    main-loop poll race can fire a flush before ``scan_name`` is assigned
    (parked for follow-up).
    """
    # High rates keep the absolute throughput up; the flush_interval of 10 s
    # is what actually dodges the race — it's larger than the 1 s main-loop
    # poll window, so no full flush can fire pre-reset.
    rig = streaming_rig(
        tdc_frequency=10000.0,
        flush_interval=10.0,
        cps=100000.0,
        collapse_y=True,
        exit_on_disconnect=True,
    )

    sim_proc = rig.spawn_simulator_cli(duration=5.0)

    # The simulator will auto-disconnect when its 5 s duration expires, which
    # triggers the server-side stop publish.
    messages = collect_messages_until_stop(rig.sub_sock, timeout_s=25.0)

    try:
        sim_proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        sim_proc.kill()

    starts, stops, events = _split(messages)

    # Parity with pre-rig assertions: start/event/stop present, schemas valid,
    # stop.scan_name matches start.scan_name.
    _assert_baseline_run_invariants(starts, stops, events, run_label="sim-subprocess")
    start_msg = TimePixStart(**starts[0])
    assert start_msg.tdc_frequency_hz == rig.tdc_frequency
    assert start_msg.detector_size_x > 0
    assert start_msg.detector_size_y > 0

    # Event-frame sanity: the (detector_size_x, detector_size_y) fields live
    # on the start message only; the event frames carry the run-time stats.
    # Assert the fields the UI actually reads per-event.
    for ev_meta in events:
        assert ev_meta["shape"]
        assert ev_meta["dtype"] == "uint32"
        assert ev_meta["flush_number"] >= 1
        assert ev_meta["cycles_in_flush"] > 0


@pytest.mark.unit
class TestSchemas:
    """Unit tests for message schemas."""

    def test_timepix_start_schema(self):
        """Test TimePixStart schema validation."""
        data = {
            "msg_type": "start",
            "scan_name": "test_scan",
            "tdc_frequency_hz": 1000.0,
            "t_delta_ns": 10.0,
            "t_cycle_ns": 1000000.0,
            "n_bins": 100,
            "detector_size_x": 256,
            "detector_size_y": 256,
            "flush_interval_s": 1.0,
            "cycles_per_flush": 1000,
            "tdc_channel": 1,
            "tdc_edge": "rising",
            "collapse_y": False,
            "zmq_port": 5657,
            "tcp_port": 9090,
        }

        start = TimePixStart(**data)
        assert start.scan_name == "test_scan"
        assert start.tdc_frequency_hz == 1000.0

    def test_timepix_stop_schema(self):
        """Test TimePixStop schema validation."""
        data = {
            "msg_type": "stop",
            "scan_name": "test_scan",
            "total_flushes": 10,
            "total_cycles": 10000,
            "total_packets": 50000,
            "acquisition_duration_s": 10.5,
            "pixels_discarded_before_trigger": 100,
            "pixels_discarded_outside_window": 50,
        }

        stop = TimePixStop(**data)
        assert stop.scan_name == "test_scan"
        assert stop.total_flushes == 10

    def test_timepix_event_schema(self):
        """Test TimePixEvent schema validation."""
        array = np.zeros((256, 256, 100), dtype=np.uint32)

        data = {
            "msg_type": "event",
            "array": array,
            "timestamp": 1234567890.0,
            "shape": (256, 256, 100),
            "dtype": "uint32",
            "tdc_frequency_hz": 1000.0,
            "t_delta_ns": 10.0,
            "t_cycle_ns": 1000000.0,
            "n_bins": 100,
            "detector_size_x": 256,
            "detector_size_y": 256,
            "flush_interval_s": 1.0,
            "cycles_per_flush": 1000,
            "tdc_channel": 1,
            "tdc_edge": "rising",
            "collapse_y": False,
            "flush_number": 1,
            "cycles_in_flush": 1000,
            "total_cycles": 1000,
            "pixels_discarded_before_trigger": 0,
            "pixels_discarded_outside_window": 0,
        }

        event = TimePixEvent(**data)
        assert event.flush_number == 1
        assert event.array.shape == (256, 256, 100)


@pytest.mark.integration
@pytest.mark.slow
def test_simulator_stop_sends_zmq_stop_and_second_run_resets_counters(streaming_rig):
    """Bug #11 / issues 1 & 2: stop_auto_sending() publishes a ZMQ stop; second run
    reconnects with a fresh scan_name and flush_number starting at 1.

    Asserts strict invariants (single stop, scan_name cohesion across every
    message, flush_number == 1..N) plus the cross-run invariants that
    scan_name must change and the new run's flush_number restarts at 1.

    To keep the invariants strict we *explicitly* ``connect()`` and then
    sleep 1.2 s before each run's ``start_auto_sending`` call, so the
    server's 1 s-period main-loop poll has time to observe the new client,
    assign the scan_name, and reset counters *before* any data flows.
    Without that window a pre-existing race can leak a flush with
    ``scan_name=None`` / pre-reset ``flush_number`` into the stream (parked
    for a follow-up; see ``_assert_baseline_run_invariants``).
    """
    rig = streaming_rig(
        tdc_frequency=10000.0,
        flush_interval=0.5,
        cps=100000.0,
        collapse_y=True,
    )

    source = rig.make_simulator()

    # ── Run 1 ──────────────────────────────────────────────────────────
    assert source.connect(), "failed initial connect"
    time.sleep(1.2)  # let the server observe connect and reset before data flows
    source.start_auto_sending(2.0)
    if source.send_thread:
        source.send_thread.join(timeout=6.0)
    source.stop_auto_sending()  # disconnects (triggers stop publish)

    run1 = collect_messages_until_stop(rig.sub_sock, timeout_s=10.0)
    starts1, stops1, events1 = _split(run1)
    scan1 = _assert_strict_run_invariants(starts1, stops1, events1, run_label="run1")

    # ── Run 2: explicit reconnect so we can sleep before sending ──────
    assert source.connect(), "failed reconnect for run 2"
    time.sleep(1.2)
    source.start_auto_sending(2.0)
    if source.send_thread:
        source.send_thread.join(timeout=6.0)
    source.stop_auto_sending()

    run2 = collect_messages_until_stop(rig.sub_sock, timeout_s=10.0)
    starts2, stops2, events2 = _split(run2)
    scan2 = _assert_strict_run_invariants(starts2, stops2, events2, run_label="run2")

    # ── Bug #11/2: cross-run invariants ──────────────────────────────
    assert scan2 != scan1, "Bug #11/2: scan_name must change across connect/disconnect cycles"
    assert (
        events2[0]["flush_number"] == 1
    ), f"Bug #11/2: flush_number must restart at 1 on reconnect, got {events2[0]['flush_number']}"


@pytest.mark.integration
@pytest.mark.slow
def test_final_flush_sent_before_stop(streaming_rig):
    """Bug #11 / issue 3: accumulated data from the last partial TDC cycles is
    published as one final event message before the stop.

    Setup:  tdc=10 kHz, flush_interval=10 s.
    Stream: 5 s of wallclock.
    Under the wall-clock flush gate (server commit 754c857): the regular
    gate fires when ``time.monotonic() - last_flush_time >= flush_interval``
    so a 5 s stream with a 10 s gate emits 0 regular flushes.  The final
    partial-cycle flush at stop is therefore the *only* event message —
    if it is missing we get 0 events, which is the bug.

    Without the fix : 0 event messages received.
    With    the fix : at least 1 event message (the final flush) received.

    High TDC/CPS avoids the known low-rate race (parked for follow-up); the
    large flush_interval is what keeps the "no regular flush" property.
    """
    rig = streaming_rig(
        tdc_frequency=10000.0,
        flush_interval=10.0,
        cps=100000.0,
        collapse_y=True,
        exit_on_disconnect=True,
    )

    source = rig.make_simulator()
    assert source.connect(), "failed to connect"

    # Stream for less than one flush cycle (5 s < 10 s) so only the final
    # partial-cycle flush can fire.
    source.start_auto_sending(5.0)
    if source.send_thread:
        source.send_thread.join(timeout=10.0)
    source.stop_auto_sending()

    messages = collect_messages_until_stop(rig.sub_sock, timeout_s=15.0)
    starts, stops, events = _split(messages)

    assert events, (
        "Bug #11/3: no event messages received — final flush was not sent. "
        f"All message types: {[m.get('msg_type') for m in messages]}"
    )

    # Strict invariants are safe here: flush_interval=10 s >> the 1 s race
    # window in the server's main-loop poll, so no full flush can fire
    # before the client-connect block resets state.
    _assert_strict_run_invariants(starts, stops, events, run_label="final-flush")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
