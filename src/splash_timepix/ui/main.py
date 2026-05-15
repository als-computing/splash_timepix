"""Main window and application entry point for TimePix3 UI.

Coordinates all components: tabs, workers, and process management.
"""

import logging
import signal
import sys
from pathlib import Path
from typing import Optional

import numpy as np
from PySide6.QtCore import QTimer, Slot
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QStatusBar, QTabWidget, QVBoxLayout, QWidget

from splash_timepix.serval_client import ServalClient

from . import single_instance, theme
from .alignment_tab import AlignmentTab
from .engineering_tab import EngineeringTab
from .log_manager import LogManager
from .operator_tab import OperatorTab
from .timeline_tab import TimelineTab
from .workers import (
    FlushData,
    HeartbeatMonitorWorker,
    HeartbeatStatus,
    ProcessManager,
    ServalPollerWorker,
    ZmqSubscriberWorker,
)

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Main application window with Operator and Engineering tabs."""

    def __init__(self, autostart_serval: bool = False):
        super().__init__()

        self.setWindowTitle("TimePix3 Acquisition")
        self.setMinimumSize(1333, 1000)

        # State
        self._acquiring = False
        self._preview_mode = False
        self._current_output_dir: Optional[str] = None

        # Workers
        self._process_manager: Optional[ProcessManager] = None
        self._zmq_worker: Optional[ZmqSubscriberWorker] = None
        self._heartbeat_worker: Optional[HeartbeatMonitorWorker] = None
        self._serval_worker: Optional[ServalPollerWorker] = None

        self._ready_check_timer: Optional[QTimer] = None
        self._waiting_for_streaming_ready = False
        self._serval_stdout_tail = ""

        self._log_manager = LogManager(self)

        self._setup_ui()
        self._setup_workers()
        if autostart_serval:
            self._start_serval()

    def _setup_ui(self):
        """Initialize the UI components."""
        # Central widget with tabs
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)

        # Tab widget. Order: Alignment (default landing tab — beam alignment is
        # the first thing operators do each session) → Operator → Engineering.
        self._tabs = QTabWidget()

        # Alignment tab (first / default). Auto-stopped when the user switches
        # to the Operator tab — see _on_tab_changed below.
        self._alignment_tab = AlignmentTab()
        self._alignment_tab.start_requested.connect(self._on_start_requested)
        self._alignment_tab.stop_requested.connect(self._on_stop_requested)
        self._alignment_idx = self._tabs.addTab(self._alignment_tab, "Alignment")

        # Operator tab
        self._operator_tab = OperatorTab()
        self._operator_tab.start_requested.connect(self._on_start_requested)
        self._operator_tab.stop_requested.connect(self._on_stop_requested)
        self._operator_idx = self._tabs.addTab(self._operator_tab, "Operator")

        # Logs tab (formerly Engineering) — viewing diagnostics during a live
        # alignment run is explicitly allowed; this tab does NOT trigger auto-stop.
        self._engineering_tab = EngineeringTab()
        self._engineering_tab.kill_all_requested.connect(self._on_kill_all)
        self._engineering_tab.clear_logs_requested.connect(self._log_manager.session_marker)
        self._engineering_idx = self._tabs.addTab(self._engineering_tab, "Logs")

        # Timeline tab — integrated, time-ordered view of all subsystem logs.
        self._timeline_tab = TimelineTab()
        self._timeline_idx = self._tabs.addTab(self._timeline_tab, "Timeline")

        # Auto-stop alignment on Operator-tab entry. See _on_tab_changed.
        self._tabs.currentChanged.connect(self._on_tab_changed)

        layout.addWidget(self._tabs)

        # Status bar
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("Ready")

    def _setup_workers(self):
        """Initialize background workers."""
        # Process manager
        self._process_manager = ProcessManager(self)
        self._process_manager.process_started.connect(self._on_process_started)
        self._process_manager.process_stopped.connect(self._on_process_stopped)
        # Persist to disk first, then handle Serval tail / UI via line_emitted.
        self._process_manager.process_output.connect(self._log_manager.append)
        self._process_manager.process_output.connect(self._on_process_output)
        self._log_manager.line_emitted.connect(self._engineering_tab.on_log_line)
        self._log_manager.line_emitted.connect(self._timeline_tab.on_log_line)
        # Clear the Timeline view together with the Logs tab terminals.
        self._engineering_tab.clear_logs_requested.connect(self._timeline_tab.clear)

        # Serval poller (always runs)
        self._serval_worker = ServalPollerWorker(poll_interval=1.0)
        self._serval_worker.status_updated.connect(self._operator_tab.on_serval_status)
        self._serval_worker.connection_changed.connect(self._on_serval_connection_changed)
        self._serval_worker.start()

        # Heartbeat monitor (always runs)
        self._heartbeat_worker = HeartbeatMonitorWorker()
        self._heartbeat_worker.status_updated.connect(self._operator_tab.on_heartbeat_status)
        self._heartbeat_worker.status_updated.connect(self._on_heartbeat_status_for_state)
        self._heartbeat_worker.connection_changed.connect(self._on_heartbeat_connection_changed)
        self._last_heartbeat_state = "disconnected"
        self._heartbeat_worker.start()

        # ZMQ subscriber (always runs, receives data when available)
        self._zmq_worker = ZmqSubscriberWorker()
        self._zmq_worker.flush_received.connect(self._on_flush_received)
        self._zmq_worker.connection_changed.connect(self._operator_tab.on_zmq_connection_changed)
        self._zmq_worker.connection_changed.connect(self._on_zmq_connection_changed)
        self._zmq_worker.error_occurred.connect(self._on_zmq_error)
        self._zmq_worker.start()
        self._engineering_tab.set_zmq_thread_status("running")
        self._log_manager.append("zmq-backend", "ZMQ subscriber thread started.")

    def _start_serval(self):
        """Start Serval server on application startup."""
        # Check if already running
        if self._process_manager.is_running("serval"):
            logger.info("Serval already running")
            return

        logger.info("Starting Serval server...")
        self._log_manager.append("system", "Starting Serval server...")

        if self._process_manager.start_serval():
            self._status_bar.showMessage("Starting Serval server...")
        else:
            self._log_manager.append("system", "WARNING: Failed to start Serval - check if JAR exists")
            QMessageBox.warning(
                self,
                "Serval Error",
                "Could not start Serval server. Check the Engineering tab for details.",
            )

    def _stop_ready_check_timer(self) -> None:
        """Stop and detach the streaming-ready poll timer (avoids orphan QTimers)."""
        t = self._ready_check_timer
        if t is None:
            return
        t.stop()
        try:
            t.timeout.disconnect(self._check_server_ready)
        except (TypeError, RuntimeError):
            pass
        self._ready_check_timer = None

    @Slot(str, dict)
    def _on_start_requested(self, mode: str, params: dict):
        """Handle start request from operator tab."""
        if self._acquiring:
            logger.warning("Already acquiring")
            return
        if self._waiting_for_streaming_ready:
            logger.warning("Already waiting for streaming server to become ready")
            return

        self._current_mode = mode
        self._current_output_dir = params.get("output_dir")
        self._current_replay_file = params.get("replay_file")
        self._current_frame_number = None  # Reset

        # For real acquisition, get frame number from Serval now
        if mode == "start":
            try:
                client = ServalClient()
                self._current_frame_number = client.get_frame_count()
            except Exception:
                self._current_frame_number = None

        # Alignment uses a curated subset of params; fall back to operator-tab
        # widget defaults for fields it doesn't carry (TDC freq/channel/edge,
        # parse batch, n_bins). The streaming server ignores these in alignment
        # mode but still wants well-formed CLI args.
        if mode == "alignment":
            op_params = self._operator_tab._get_params()
            tdc_freq = op_params["tdc_frequency"]
            tdc_channel = op_params["tdc_channel"]
            tdc_edge = op_params["tdc_edge"]
            callback_batch_size = op_params.get("callback_batch_size", 10_000)
            n_bins = op_params.get("n_bins", 350)
            duration = params["duration"]
            alignment_rate_hz = float(params.get("alignment_rate_hz", 30.0))
        else:
            tdc_freq = params["tdc_frequency"]
            tdc_channel = params["tdc_channel"]
            tdc_edge = params["tdc_edge"]
            callback_batch_size = params.get("callback_batch_size", 10_000)
            n_bins = params.get("n_bins", 350)
            duration = params["duration"]
            alignment_rate_hz = 30.0

        logger.info(
            f"Starting {mode}: TDC={tdc_freq}Hz, ch={tdc_channel}, edge={tdc_edge}, "
            f"callback_batch_size={callback_batch_size}, duration={duration}s"
        )
        self._log_manager.append(
            "system",
            f"Starting {mode}: TDC={tdc_freq}Hz, parse_batch={callback_batch_size}, duration={duration}s",
        )

        # Start streaming server (needed for all modes; alignment passes extra flags).
        if not self._process_manager.start_streaming_server(
            tdc_freq,
            tdc_channel,
            tdc_edge,
            callback_batch_size=callback_batch_size,
            n_bins=n_bins,
            exit_on_disconnect=True,
            alignment=(mode == "alignment"),
            alignment_rate_hz=alignment_rate_hz,
        ):
            QMessageBox.warning(self, "Error", "Failed to start streaming server")
            return

        self._status_bar.showMessage("Starting streaming server...")

        # Store params for later steps
        self._start_params = (mode, params)

        # Wait for heartbeat to show ready
        self._stop_ready_check_timer()
        self._waiting_for_streaming_ready = True
        self._ready_check_count = 0
        self._ready_check_timer = QTimer(self)
        self._ready_check_timer.timeout.connect(self._check_server_ready)
        self._ready_check_timer.start(500)

    def _check_server_ready(self):
        """Check if streaming server is ready via heartbeat."""
        if not self._waiting_for_streaming_ready:
            return

        self._ready_check_count += 1

        # Get current heartbeat state from operator tab
        # (We'll check via the heartbeat worker's last known state)
        if hasattr(self, "_last_heartbeat_state") and self._last_heartbeat_state in (
            "ready",
            "streaming",
        ):
            self._waiting_for_streaming_ready = False
            self._stop_ready_check_timer()
            self._continue_startup()
        elif self._ready_check_count > 60:  # 30 second timeout
            self._waiting_for_streaming_ready = False
            self._stop_ready_check_timer()
            self._log_manager.append("system", "WARNING: Timeout waiting for server ready")
            QMessageBox.warning(self, "Timeout", "Streaming server did not become ready in time")
            self._process_manager.stop_process("streaming")

    def _continue_startup(self):
        """Continue startup sequence after server is ready."""
        mode, params = self._start_params

        self._log_manager.append("system", "Streaming server ready")

        if mode == "simulator":
            # Simulator mode: start simulator CLI (no Serval needed)
            self._log_manager.append("system", "Starting simulator...")
            self._status_bar.showMessage("Starting simulator...")

            if not self._process_manager.start_simulator(
                tdc_frequency=params["tdc_frequency"],
                cps=10000.0,
                duration=params["duration"],
            ):
                QMessageBox.warning(self, "Error", "Failed to start simulator")
                self._process_manager.stop_process("streaming")
                return

            self._acquiring = True
            self._operator_tab.set_acquiring(True)
            self._alignment_tab.set_acquiring(True)

        elif mode == "replay":
            # Replay mode: start live-cli with source file (no Serval needed)
            replay_file = params.get("replay_file", "")
            self._log_manager.append("system", f"Starting replay: {Path(replay_file).name}")
            self._status_bar.showMessage("Replaying file...")

            if not self._process_manager.start_live_cli(replay_file=replay_file):
                QMessageBox.warning(self, "Error", "Failed to start live-cli for replay")
                self._process_manager.stop_process("streaming")
                return

            self._acquiring = True
            self._operator_tab.set_acquiring(True)
            self._alignment_tab.set_acquiring(True)

        elif mode == "alignment":
            # Alignment mode: identical pipeline to preview (live-cli + acq.py
            # --preview), but the streaming server is already running with
            # --alignment.  acq.py kicks Serval into a continuous acquisition;
            # without it the detector pushes nothing through live-cli.  Output
            # dir is unused in --preview mode but acq.py expects the flag, so
            # pass an empty directory placeholder via the duration-only path.
            self._log_manager.append("system", "Starting alignment live-cli...")
            self._status_bar.showMessage("Starting live-cli (alignment)...")
            QTimer.singleShot(
                1000,
                lambda: self._start_live_cli_and_acq(params["duration"], "", True),
            )

        else:
            # Start/Preview mode: need live-cli and acquisition
            self._log_manager.append("system", "Starting live-cli...")
            self._status_bar.showMessage("Starting live-cli...")
            QTimer.singleShot(
                1000,
                lambda: self._start_live_cli_and_acq(params["duration"], params["output_dir"], mode == "preview"),
            )

    def _start_live_cli_and_acq(self, duration: int, output_dir: str, preview: bool):
        """Start live-cli and acquisition after server is ready."""
        # Start live-cli (no replay file = real detector)
        if not self._process_manager.start_live_cli():
            QMessageBox.warning(self, "Error", "Failed to start live-cli")
            self._process_manager.stop_process("streaming")
            return

        # Small delay then start acquisition
        QTimer.singleShot(1000, lambda: self._start_acquisition(duration, output_dir, preview))

    def _start_acquisition(self, duration: int, output_dir: str, preview: bool):
        """Start the acquisition script."""
        self._log_manager.append("system", "Starting acquisition...")
        self._status_bar.showMessage("Acquiring..." if not preview else "Preview mode...")

        if not self._process_manager.start_acquisition(duration, output_dir, preview):
            QMessageBox.warning(self, "Error", "Failed to start acquisition")
            self._process_manager.stop_all()
            return

        self._acquiring = True
        self._operator_tab.set_acquiring(True)
        self._alignment_tab.set_acquiring(True)

    @Slot()
    def _on_stop_requested(self):
        """Handle stop request from operator tab."""
        if not self._acquiring:
            return

        logger.info("Stop requested")
        self._log_manager.append("system", "Stop requested...")
        self._status_bar.showMessage("Stopping...")

        mode = getattr(self, "_current_mode", "start")

        # Save data for real acquisition and replay modes
        if mode in ("start", "replay"):
            self._save_on_stop(mode)

        if mode in ("simulator", "replay"):
            # Kill only the data source (simulator or live-cli).  The streaming
            # server was launched with --exit-on-disconnect: once it detects the
            # TCP disconnect it will do the final flush, publish the ZMQ stop
            # message, and exit on its own.  _on_acquisition_complete() fires
            # when the "streaming" process exits (see _on_process_stopped).
            proc_name = "simulator" if mode == "simulator" else "live-cli"
            self._log_manager.append("system", f"Stopping {proc_name}...")
            self._process_manager.stop_process(proc_name)
            self._status_bar.showMessage("Waiting for streaming server to finish...")
            # Safety net: force-kill streaming after 6 s if it has not exited.
            QTimer.singleShot(6000, self._force_stop_streaming_if_still_running)
        else:
            # For real acquisition, call stop.py via Serval
            self._run_stop_script()

    def _save_on_stop(self, mode: str):
        """Save average data when stopping acquisition or replay."""
        # For replay, save next to the source file
        # For acquisition, use the configured output dir
        if mode == "replay" and self._current_replay_file:
            output_dir = str(Path(self._current_replay_file).parent)
            filename_base = Path(self._current_replay_file).stem
        else:
            output_dir = self._current_output_dir
            if not output_dir:
                output_dir = str(Path.home() / "Desktop")
                self._log_manager.append("system", f"No output dir set, using {output_dir}")

            # Find the newest/latest .tpx3 file in the output directory
            output_path = Path(output_dir)
            tpx3_files = list(output_path.glob("*.tpx3"))

            if tpx3_files:
                newest_tpx3 = max(tpx3_files, key=lambda f: f.stat().st_mtime)
                filename_base = newest_tpx3.stem
                self._log_manager.append("system", f"Found newest tpx3: {newest_tpx3}")
            else:
                self._log_manager.append("system", "WARNING: No .tpx3 files found in output directory, skipping save")
                return

        self._log_manager.append("system", f"Saving to {output_dir} as {filename_base}...")

        png_path, csv_path, energy_path, time_path, json_path = self._operator_tab.save_average_data(
            output_dir, filename_base
        )

        if png_path:
            self._log_manager.append("system", f"Saved: {png_path}")
        if csv_path:
            self._log_manager.append("system", f"Saved: {csv_path}")
        if energy_path:
            self._log_manager.append("system", f"Saved: {energy_path}")
        if time_path:
            self._log_manager.append("system", f"Saved: {time_path}")
        if json_path:
            self._log_manager.append("system", f"Saved: {json_path}")
        if not any([png_path, csv_path, energy_path, time_path, json_path]):
            self._log_manager.append("system", "No data to save")

    def _force_stop_streaming_if_still_running(self):
        """Safety net: force-kill streaming server if it has not exited on its own.

        Called ~6 s after the data source (simulator / live-cli) was stopped.
        Under normal operation the streaming server exits well within that window
        via --exit-on-disconnect; this only fires if something went wrong.
        """
        if self._process_manager.is_running("streaming"):
            logger.warning("Streaming server still running after timeout, force stopping")
            self._log_manager.append(
                "system", "WARNING: Streaming server did not exit after disconnect, force stopping..."
            )
            self._process_manager.stop_process("streaming")
            # _on_process_stopped("streaming") will fire and call _on_acquisition_complete.

    def _run_stop_script(self):
        """Stop acquisition via Serval."""
        try:
            client = ServalClient()
            client.stop_acquisition()
            self._log_manager.append("system", "Stop command sent successfully")
        except Exception as e:
            self._log_manager.append("system", f"Stop failed: {e}, killing processes...")
            self._process_manager.stop_all()

    @Slot()
    def _on_kill_all(self):
        """Handle kill all request from engineering tab."""
        logger.info("Killing all processes")
        self._log_manager.append("system", "Killing all processes...")
        self._waiting_for_streaming_ready = False
        self._stop_ready_check_timer()
        self._process_manager.stop_all()

        self._acquiring = False
        self._operator_tab.set_acquiring(False)
        self._alignment_tab.set_acquiring(False)
        self._status_bar.showMessage("All processes stopped")

    @Slot(str)
    def _on_process_started(self, name: str):
        """Handle process started signal."""
        logger.info(f"Process started: {name}")
        self._engineering_tab.set_process_status(name, True)
        self._log_manager.append(name, "--- Process started ---")
        if name == "serval":
            self._serval_stdout_tail = ""
            self._operator_tab.on_serval_process_running(True)
            self._alignment_tab.on_serval_process_running(True)

    @Slot(str, int)
    def _on_process_stopped(self, name: str, exit_code: int):
        """Handle process stopped signal."""
        logger.info(f"Process stopped: {name} (exit code: {exit_code})")
        self._engineering_tab.set_process_status(name, False)
        self._log_manager.append(name, f"--- Process exited (code: {exit_code}) ---")
        if name == "serval":
            self._operator_tab.on_serval_process_running(False)
            self._alignment_tab.on_serval_process_running(False)

        mode = getattr(self, "_current_mode", "start")

        # Check if a relevant process stopped while acquiring
        if self._acquiring:
            if name == "acquisition":
                self._on_acquisition_complete()
            elif name == "streaming" and mode in ("simulator", "replay"):
                # Streaming server exited (either naturally via --exit-on-disconnect
                # after the data source disconnected, or via the safety-net force-kill).
                # This is the authoritative completion signal for these modes: by the
                # time streaming exits, the final flush and ZMQ stop have been sent.
                self._on_acquisition_complete()

    @Slot(str, str)
    def _on_process_output(self, name: str, text: str):
        """Maintain Serval stdout tail for chip-temp detection.

        Persistence and UI rendering are handled upstream by LogManager
        (connected to the same process_output signal before this slot).
        """
        if name == "serval":
            self._serval_stdout_tail = (self._serval_stdout_tail + text)[-8192:]
            if "chip temps:" in self._serval_stdout_tail.lower():
                self._operator_tab.on_serval_chip_temps_line_seen()
                self._alignment_tab.on_serval_chip_temps_line_seen()

    def _on_acquisition_complete(self):
        """Handle acquisition completion - save average data."""
        self._acquiring = False
        self._operator_tab.set_acquiring(False)
        self._alignment_tab.set_acquiring(False)

        mode = getattr(self, "_current_mode", "start")

        if mode in ("preview", "simulator", "replay", "alignment"):
            self._status_bar.showMessage(f"{mode.capitalize()} complete")
            self._log_manager.append("system", f"{mode.capitalize()} complete")
            return

        # Save average data for real acquisitions
        self._save_on_stop(mode)

        self._status_bar.showMessage("Acquisition complete")
        self._log_manager.append("system", "Acquisition complete")

    @Slot(object)
    def _on_flush_received(self, flush_data: FlushData):
        """Route incoming flushes by mode so each tab only sees its own shape.

        Alignment flushes are (X, Y, 1) uint32 arrays which would crash the
        operator tab's heatmap math; timing flushes are (X, n_bins) or
        (X, Y, n_bins) which the alignment tab cannot render. The metadata's
        ``mode`` field (added in TimePixStart and the static metadata in
        ``app.py``) is the authoritative router. Defaults to "timing" so any
        old streaming server / pre-mode-field message routes to the operator
        tab as before.
        """
        meta = flush_data.metadata
        mode = meta.get("mode", "timing")
        if mode == "alignment":
            self._alignment_tab.on_flush_received(flush_data)
        else:
            self._operator_tab.on_flush_received(flush_data)

        # Log to engineering tab
        flush_num = meta.get("flush_number", "?")
        cycles = meta.get("cycles_in_flush", "?")
        self._log_manager.append(
            "zmq-backend",
            f"[{mode}] Flush #{flush_num}: {cycles} cycles, {np.sum(flush_data.array):.2e} counts",
        )

    @Slot(int)
    def _on_tab_changed(self, new_index: int) -> None:
        """Auto-stop alignment when the user switches to the Operator tab.

        Engineering tab is intentionally exempt — viewing diagnostics during a
        live alignment run is allowed (no auto-stop fires). The auto-stop is
        silent (status-bar + engineering-log line) — no confirmation dialog.
        """
        if new_index != self._operator_idx:
            return
        if not self._acquiring:
            return
        if getattr(self, "_current_mode", None) != "alignment":
            return
        msg = "Alignment auto-stopped: switched to Operator"
        logger.info(msg)
        self._log_manager.append("system", msg)
        self._status_bar.showMessage(msg)
        self._on_stop_requested()

    @Slot(bool)
    def _on_zmq_connection_changed(self, connected: bool):
        """Handle ZMQ connection state change for engineering tab."""
        if connected:
            self._log_manager.append("zmq-backend", "Receiving data from streaming server")
        else:
            self._log_manager.append("zmq-backend", "Not receiving data")

    @Slot(bool)
    def _on_serval_connection_changed(self, connected: bool):
        """Handle Serval connection state change."""
        if connected:
            self._log_manager.append("system", "Connected to Serval")
        else:
            self._log_manager.append("system", "Disconnected from Serval")

    @Slot(bool)
    def _on_heartbeat_connection_changed(self, connected: bool):
        """Handle heartbeat connection state change."""
        if connected:
            self._log_manager.append("system", "Heartbeat connected")

    @Slot(object)
    def _on_heartbeat_status_for_state(self, status: HeartbeatStatus):
        """Track heartbeat state for startup sequence."""
        self._last_heartbeat_state = status.state if status.connected else "disconnected"

    @Slot(str)
    def _on_zmq_error(self, error: str):
        """Handle ZMQ error."""
        self._log_manager.append("zmq-backend", f"Error: {error}")

    def closeEvent(self, event):
        """Handle window close - cleanup workers and processes."""
        if self._acquiring or self._waiting_for_streaming_ready:
            msg = QMessageBox(self)
            msg.setIcon(QMessageBox.Icon.Warning)
            msg.setWindowTitle("Acquisition in progress")
            msg.setText("Acquisition or startup is still running.")
            msg.setInformativeText(
                "Use Stop on the Operator tab first when you want a clean stop and saved outputs "
                "(where applicable).\n\n"
                "Closing the window now will terminate all streaming and related processes immediately."
            )
            stay = msg.addButton("Stay", QMessageBox.ButtonRole.RejectRole)
            close_anyway = msg.addButton("Close anyway", QMessageBox.ButtonRole.DestructiveRole)
            msg.setDefaultButton(stay)
            msg.exec()
            if msg.clickedButton() != close_anyway:
                event.ignore()
                return

        # Save operator + alignment preferences before tearing down workers, so
        # a save exception cannot leave workers/processes running. Both saves
        # write into the same JSON file via a merge-on-write strategy in
        # preferences.save_operator_preferences, so the order does not matter.
        # Failures here must never block quit — log and continue.
        try:
            self._operator_tab.save_operator_preferences()
        except Exception:
            logger.exception("Failed to save operator preferences")
        try:
            self._alignment_tab.save_alignment_preferences()
        except Exception:
            logger.exception("Failed to save alignment preferences")

        logger.info("Closing application...")

        self._waiting_for_streaming_ready = False
        self._stop_ready_check_timer()

        # Stop all workers
        if self._zmq_worker:
            self._zmq_worker.stop()
            self._zmq_worker.wait(2000)
            self._engineering_tab.set_zmq_thread_status("stopped")

        if self._heartbeat_worker:
            self._heartbeat_worker.stop()
            self._heartbeat_worker.wait(2000)

        if self._serval_worker:
            self._serval_worker.stop()
            self._serval_worker.wait(2000)

        # Stop all processes (including Serval)
        if self._process_manager:
            self._process_manager.stop_all()

        self._log_manager.close()
        event.accept()


def _show_already_running_dialog(holder_pid: Optional[int]) -> str:
    """Show the second-instance dialog; return the chosen action.

    Returns one of:
      - ``"kill_and_reopen"`` — terminate the other instance, then start a
        fresh UI here.
      - ``"kill"`` — terminate the other instance and exit this process.
      - ``"cancel"`` — do nothing; exit this process.

    The Kill / Kill & Reopen buttons are hidden when ``holder_pid``
    cannot be verified as our app via psutil, which protects against
    PID reuse and cross-user signaling. In that case only Cancel is
    offered.
    """
    msg = QMessageBox()
    msg.setIcon(QMessageBox.Icon.Warning)
    msg.setWindowTitle("TimePix UI already running")
    msg.setText("Another TimePix UI session is already running for this user.")

    can_verify = holder_pid is not None and single_instance.is_other_instance_alive(holder_pid)

    if can_verify:
        msg.setInformativeText(
            f"Close that window first, or click Kill to terminate it (pid {holder_pid}).\n\n" "Cancel to do nothing."
        )
    else:
        msg.setInformativeText(
            "Could not verify the other process — Kill is disabled. "
            "Close that window manually first.\n\n"
            "Cancel to do nothing."
        )

    # ActionRole buttons are laid out in insertion order (RejectRole /
    # AcceptRole would otherwise be re-arranged by QDialogButtonBox per
    # platform style — e.g. AcceptRole leftmost on GNOME). We want Cancel
    # always at the very left, then Kill, then Kill & Reopen on the right.
    cancel_btn = msg.addButton("Cancel", QMessageBox.ButtonRole.ActionRole)
    if can_verify:
        kill_btn = msg.addButton("Kill", QMessageBox.ButtonRole.ActionRole)
        kill_reopen_btn = msg.addButton("Kill && Reopen", QMessageBox.ButtonRole.ActionRole)
    else:
        kill_btn = None
        kill_reopen_btn = None
    msg.setDefaultButton(cancel_btn)
    msg.setEscapeButton(cancel_btn)

    msg.exec()
    clicked = msg.clickedButton()
    if kill_reopen_btn is not None and clicked is kill_reopen_btn:
        return "kill_and_reopen"
    if kill_btn is not None and clicked is kill_btn:
        return "kill"
    return "cancel"


def _confirm_force_kill(pid: int) -> bool:
    """Second-stage confirmation before SIGKILL escalation."""
    msg = QMessageBox()
    msg.setIcon(QMessageBox.Icon.Critical)
    msg.setWindowTitle("Force-kill TimePix UI?")
    msg.setText(f"Process {pid} did not exit after SIGTERM.")
    msg.setInformativeText(
        "Force-killing skips graceful shutdown of Serval / streaming-server / "
        "live-cli, which may leave them as orphaned processes. Only do this "
        "if the other window is truly stuck."
    )
    cancel_btn = msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    kill_btn = msg.addButton("Force kill", QMessageBox.ButtonRole.DestructiveRole)
    msg.setDefaultButton(cancel_btn)
    msg.exec()
    return msg.clickedButton() is kill_btn


def _handle_singleton() -> bool:
    """Acquire the singleton lock or run the recovery dialog flow.

    Returns ``True`` if the caller should continue starting the UI, or
    ``False`` if the caller should ``sys.exit(0)`` (the user chose
    Cancel, chose Kill without reopen, or a kill attempt failed).

    Must be called *before* :class:`MainWindow` is constructed (but
    *after* :class:`QApplication` exists so dialog widgets can be
    shown). Non-Linux platforms skip enforcement entirely.
    """
    try:
        single_instance.acquire_lock()
        return True
    except single_instance.AlreadyRunning as e:
        action = _show_already_running_dialog(e.pid)
        if action == "cancel" or e.pid is None:
            return False

        # Both "kill" and "kill_and_reopen" terminate the other instance.
        # The first instance's SIGTERM handler routes through closeEvent →
        # ProcessManager.stop_all(), so its children are stopped before its
        # FD closes and the kernel drops our lock.
        if not single_instance.terminate_other_instance(e.pid):
            # Either uid/cmdline check failed, or SIGTERM did not bring the
            # process down within the timeout. Offer SIGKILL as a gated
            # escalation; otherwise tell the user to retry.
            if _confirm_force_kill(e.pid) and single_instance.force_kill_other_instance(e.pid):
                pass  # fall through
            else:
                _show_terminate_failed_dialog(e.pid)
                return False

        if action == "kill":
            # User asked us to terminate the other instance and exit; do
            # not start a fresh UI here. They can relaunch when ready.
            return False

        # "kill_and_reopen": retry the flock once and continue starting the UI.
        try:
            single_instance.acquire_lock()
            return True
        except single_instance.AlreadyRunning:
            _show_retry_dialog()
            return False


def _show_terminate_failed_dialog(pid: int) -> None:
    msg = QMessageBox()
    msg.setIcon(QMessageBox.Icon.Warning)
    msg.setWindowTitle("Could not terminate other instance")
    msg.setText(f"Failed to terminate the existing TimePix UI (pid {pid}).")
    msg.setInformativeText("Close that window manually, then relaunch.")
    msg.exec()


def _show_retry_dialog() -> None:
    msg = QMessageBox()
    msg.setIcon(QMessageBox.Icon.Information)
    msg.setWindowTitle("Lock still held")
    msg.setText("The other instance is exiting but still holds the lock.")
    msg.setInformativeText("Wait a moment and relaunch.")
    msg.exec()


def _load_app_icon() -> QIcon:
    """Return the bundled window/taskbar icon, or an empty QIcon if missing.

    The asset lives next to this module so editable installs and built wheels
    (via the ``[tool.setuptools.package-data]`` glob in ``pyproject.toml``)
    resolve the same path. A missing file is a soft failure — log and fall back
    to Qt's default rather than block UI startup.
    """
    icon_path = Path(__file__).resolve().parent / "assets" / "icon.png"
    if not icon_path.is_file():
        logger.warning("App icon not found at %s; falling back to Qt default", icon_path)
        return QIcon()
    return QIcon(str(icon_path))


def main():
    """Application entry point."""
    autostart_serval = "--autostart-serval" in sys.argv

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    # Wayland compositors (GNOME, KDE Plasma) use the desktop-file name as the
    # window's app_id for grouping in the taskbar / dock and for matching
    # against an installed `splash_timepix.desktop`. Without it the window
    # often shows up as a generic "Wayland Application". Harmless on X11.
    app.setDesktopFileName("splash_timepix")
    # Without this, X11 derives WM_CLASS from argv[0] — which becomes
    # ".../main.py" under `python -m splash_timepix.ui.main`. Panels read
    # WM_CLASS for taskbar grouping and the entry's "application name"
    # tooltip, so the launcher would show "main.py" instead of our app.
    # On Wayland this is also used as a fallback for app_id when
    # setDesktopFileName is unset (it's not, but keeping both is cheap).
    app.setApplicationName("splash_timepix")
    # Human-readable label. Qt appends this to top-level window titles when
    # the title does not already include it, which is what most Linux
    # taskbars / Alt-Tab switchers display as the entry's tooltip.
    app.setApplicationDisplayName("Splash TimePix")
    # setWindowIcon on the QApplication propagates to every top-level widget
    # constructed afterward, so MainWindow inherits it without an explicit
    # call. Must run *before* MainWindow is built.
    app.setWindowIcon(_load_app_icon())

    # Route SIGTERM through Qt's event loop so MainWindow.closeEvent runs and
    # ProcessManager.stop_all() takes the children with us. Without this the
    # second-instance "Kill" path would orphan Serval/streaming-server/live-cli.
    signal.signal(signal.SIGTERM, lambda *_: app.quit())

    # Python signal handlers only run when the interpreter regains control.
    # Qt's exec() blocks in C++, so without periodic Python invocations a
    # SIGTERM may sit pending until the next user event. A no-op QTimer on a
    # short interval forces Qt to call a Python slot, giving the interpreter
    # a chance to dispatch pending signals.
    _sig_pump = QTimer()
    _sig_pump.start(200)
    _sig_pump.timeout.connect(lambda: None)

    # Singleton enforcement must happen *before* constructing MainWindow so
    # the second instance does not spin up workers it would immediately tear
    # down. Non-Linux platforms skip enforcement (logs a warning).
    if not _handle_singleton():
        sys.exit(0)

    # Apply base dark theme stylesheet
    app.setStyleSheet(
        f"""
        QMainWindow, QWidget {{
            background-color: {theme.BG_PANEL};
            color: {theme.TEXT_PRIMARY};
        }}
        QTabWidget::pane {{
            border: 1px solid {theme.BORDER_SUBTLE};
            border-radius: 4px;
            background-color: {theme.BG_PANEL};
        }}
        QTabBar::tab {{
            background-color: {theme.BG_WIDGET};
            color: {theme.TEXT_SECONDARY};
            padding: 8px 16px;
            margin-right: 2px;
            border-top-left-radius: 4px;
            border-top-right-radius: 4px;
        }}
        QTabBar::tab:selected {{
            background-color: {theme.BLUE_PRIMARY};
            color: white;
        }}
        QTabBar::tab:hover:!selected {{
            background-color: {theme.BG_BUTTON_GROUP};
        }}
        QStatusBar {{
            background-color: {theme.BG_WIDGET};
            color: {theme.TEXT_SECONDARY};
            border-top: 1px solid {theme.BORDER_SUBTLE};
        }}
        QScrollBar:vertical {{
            background-color: {theme.BG_DARK};
            width: 12px;
            border-radius: 6px;
        }}
        QScrollBar::handle:vertical {{
            background-color: {theme.GREY_DARK};
            border-radius: 6px;
            min-height: 20px;
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0px;
        }}
        QScrollBar:horizontal {{
            background-color: {theme.BG_DARK};
            height: 12px;
            border-radius: 6px;
        }}
        QScrollBar::handle:horizontal {{
            background-color: {theme.GREY_DARK};
            border-radius: 6px;
            min-width: 20px;
        }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
            width: 0px;
        }}
        QToolTip {{
            background-color: {theme.BG_WIDGET};
            color: {theme.TEXT_PRIMARY};
            border: 1px solid {theme.BORDER_SUBTLE};
            padding: 4px;
        }}
    """
    )

    window = MainWindow(autostart_serval=autostart_serval)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
