"""Main window and application entry point for TimePix3 UI.

Coordinates all components: tabs, workers, and process management.
"""

import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QWidget, QVBoxLayout,
    QMessageBox, QStatusBar
)
from PySide6.QtCore import Qt, Slot, QTimer

from .operator_tab import OperatorTab
from .engineering_tab import EngineeringTab
from .workers import (
    ProcessManager, ZmqSubscriberWorker, HeartbeatMonitorWorker,
    ServalPollerWorker, FlushData, ServalStatus, HeartbeatStatus
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Main application window with Operator and Engineering tabs."""
    
    def __init__(self, autostart_serval: bool = False):
        super().__init__()
        
        self.setWindowTitle("TimePix3 Acquisition")
        self.setMinimumSize(1200, 800)
        
        # State
        self._acquiring = False
        self._preview_mode = False
        self._current_output_dir: Optional[str] = None
        
        # Workers
        self._process_manager: Optional[ProcessManager] = None
        self._zmq_worker: Optional[ZmqSubscriberWorker] = None
        self._heartbeat_worker: Optional[HeartbeatMonitorWorker] = None
        self._serval_worker: Optional[ServalPollerWorker] = None
        
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
        
        # Tab widget
        self._tabs = QTabWidget()
        
        # Operator tab
        self._operator_tab = OperatorTab()
        self._operator_tab.start_requested.connect(self._on_start_requested)
        self._operator_tab.stop_requested.connect(self._on_stop_requested)
        self._tabs.addTab(self._operator_tab, "🎛 Operator")
        
        # Engineering tab
        self._engineering_tab = EngineeringTab()
        self._engineering_tab.kill_all_requested.connect(self._on_kill_all)
        self._tabs.addTab(self._engineering_tab, "🔧 Engineering")
        
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
        self._process_manager.process_output.connect(self._on_process_output)
        
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
        self._engineering_tab.append_zmq_log("ZMQ subscriber started, waiting for data...")
    
    def _start_serval(self):
        """Start Serval server on application startup."""
        # Check if already running
        if self._process_manager.is_running("serval"):
            logger.info("Serval already running")
            return
        
        logger.info("Starting Serval server...")
        self._engineering_tab.append_system_log("Starting Serval server...")
        
        if self._process_manager.start_serval():
            self._status_bar.showMessage("Starting Serval server...")
        else:
            self._engineering_tab.append_system_log("⚠️ Failed to start Serval - check if JAR exists")
            QMessageBox.warning(
                self, "Serval Error",
                "Could not start Serval server. Check the Engineering tab for details."
            )
    
    @Slot(str, dict)
    def _on_start_requested(self, mode: str, params: dict):
        """Handle start request from operator tab.
        
        Args:
            mode: One of "start", "preview", "simulator", "replay"
            params: Dict with tdc_frequency, tdc_channel, tdc_edge, duration, output_dir, replay_file
        """
        if self._acquiring:
            logger.warning("Already acquiring")
            return
        
        self._current_mode = mode
        self._current_output_dir = params['output_dir']
        
        tdc_freq = params['tdc_frequency']
        tdc_channel = params['tdc_channel']
        tdc_edge = params['tdc_edge']
        duration = params['duration']
        
        logger.info(f"Starting {mode}: TDC={tdc_freq}Hz, ch={tdc_channel}, edge={tdc_edge}, duration={duration}s")
        self._engineering_tab.append_system_log(
            f"Starting {mode}: TDC={tdc_freq}Hz, duration={duration}s"
        )
        
        # Start streaming server (needed for all modes)
        if not self._process_manager.start_streaming_server(
            tdc_freq, tdc_channel, tdc_edge, exit_on_disconnect=True
        ):
            QMessageBox.warning(self, "Error", "Failed to start streaming server")
            return
        
        self._status_bar.showMessage("Starting streaming server...")
        
        # Store params for later steps
        self._start_params = (mode, params)
        
        # Wait for heartbeat to show ready
        self._ready_check_timer = QTimer(self)
        self._ready_check_timer.timeout.connect(self._check_server_ready)
        self._ready_check_count = 0
        self._ready_check_timer.start(500)
    
    def _check_server_ready(self):
        """Check if streaming server is ready via heartbeat."""
        self._ready_check_count += 1
        
        # Get current heartbeat state from operator tab
        # (We'll check via the heartbeat worker's last known state)
        if hasattr(self, '_last_heartbeat_state') and self._last_heartbeat_state in ('ready', 'streaming'):
            self._ready_check_timer.stop()
            self._continue_startup()
        elif self._ready_check_count > 60:  # 30 second timeout
            self._ready_check_timer.stop()
            self._engineering_tab.append_system_log("⚠️ Timeout waiting for server ready")
            QMessageBox.warning(self, "Timeout", "Streaming server did not become ready in time")
            self._process_manager.stop_process("streaming")
    
    def _continue_startup(self):
        """Continue startup sequence after server is ready."""
        mode, params = self._start_params
        
        self._engineering_tab.append_system_log("Streaming server ready")
        
        if mode == "simulator":
            # Simulator mode: start simulator CLI (no Serval needed)
            self._engineering_tab.append_system_log("Starting simulator...")
            self._status_bar.showMessage("Starting simulator...")
            
            if not self._process_manager.start_simulator(
                tdc_frequency=params['tdc_frequency'],
                cps=1000.0,
                duration=params['duration']
            ):
                QMessageBox.warning(self, "Error", "Failed to start simulator")
                self._process_manager.stop_process("streaming")
                return
            
            self._acquiring = True
            self._operator_tab.set_acquiring(True)
            
        elif mode == "replay":
            # Replay mode: start live-cli with source file (no Serval needed)
            replay_file = params.get('replay_file', '')
            self._engineering_tab.append_system_log(f"Starting replay: {Path(replay_file).name}")
            self._status_bar.showMessage("Replaying file...")
            
            if not self._process_manager.start_live_cli(replay_file=replay_file):
                QMessageBox.warning(self, "Error", "Failed to start live-cli for replay")
                self._process_manager.stop_process("streaming")
                return
            
            self._acquiring = True
            self._operator_tab.set_acquiring(True)
            
        else:
            # Start/Preview mode: need live-cli and acquisition
            self._engineering_tab.append_system_log("Starting live-cli...")
            self._status_bar.showMessage("Starting live-cli...")
            QTimer.singleShot(1000, lambda: self._start_live_cli_and_acq(
                params['duration'], params['output_dir'], mode == "preview"
            ))
    
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
        self._engineering_tab.append_system_log("Starting acquisition...")
        self._status_bar.showMessage("Acquiring..." if not preview else "Preview mode...")
        
        if not self._process_manager.start_acquisition(duration, output_dir, preview):
            QMessageBox.warning(self, "Error", "Failed to start acquisition")
            self._process_manager.stop_all()
            return
        
        self._acquiring = True
        self._operator_tab.set_acquiring(True)
    
    @Slot()
    def _on_stop_requested(self):
        """Handle stop request from operator tab."""
        if not self._acquiring:
            return
        
        logger.info("Stop requested")
        self._engineering_tab.append_system_log("Stop requested...")
        self._status_bar.showMessage("Stopping...")
        
        mode = getattr(self, '_current_mode', 'start')
        
        if mode in ("simulator", "replay"):
            # For simulator/replay, just kill the processes directly
            self._engineering_tab.append_system_log(f"Stopping {mode} processes...")
            self._process_manager.stop_process("simulator" if mode == "simulator" else "live-cli")
            self._process_manager.stop_process("streaming")
            self._acquiring = False
            self._operator_tab.set_acquiring(False)
            self._status_bar.showMessage(f"{mode.capitalize()} stopped")
        else:
            # For real acquisition, call stop.py via Serval
            self._run_stop_script()
    
    def _run_stop_script(self):
        """Run the stop.py script to gracefully stop acquisition."""
        import subprocess
        from pathlib import Path
        
        project_root = Path(__file__).parent.parent.parent.parent
        stop_script = project_root / "ASI" / "serval_client" / "stop.py"
        
        if stop_script.exists():
            try:
                result = subprocess.run(
                    [sys.executable, str(stop_script)],
                    cwd=stop_script.parent,
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if result.returncode == 0:
                    self._engineering_tab.append_system_log("Stop command sent successfully")
                else:
                    self._engineering_tab.append_system_log(f"Stop script error: {result.stderr}")
            except Exception as e:
                self._engineering_tab.append_system_log(f"Error running stop script: {e}")
        else:
            self._engineering_tab.append_system_log("stop.py not found, killing processes...")
            self._process_manager.stop_all()
    
    @Slot()
    def _on_kill_all(self):
        """Handle kill all request from engineering tab."""
        logger.info("Killing all processes")
        self._engineering_tab.append_system_log("Killing all processes...")
        self._process_manager.stop_all()
        
        self._acquiring = False
        self._operator_tab.set_acquiring(False)
        self._status_bar.showMessage("All processes stopped")
    
    @Slot(str)
    def _on_process_started(self, name: str):
        """Handle process started signal."""
        logger.info(f"Process started: {name}")
        self._engineering_tab.set_process_status(name, True)
        self._engineering_tab.append_output(name, f"--- Process started ---\n")
    
    @Slot(str, int)
    def _on_process_stopped(self, name: str, exit_code: int):
        """Handle process stopped signal."""
        logger.info(f"Process stopped: {name} (exit code: {exit_code})")
        self._engineering_tab.set_process_status(name, False)
        self._engineering_tab.append_output(name, f"\n--- Process exited (code: {exit_code}) ---\n")
        
        mode = getattr(self, '_current_mode', 'start')
        
        # Check if a relevant process stopped while acquiring
        if self._acquiring:
            if name == "acquisition":
                self._on_acquisition_complete()
            elif name == "simulator" and mode == "simulator":
                self._on_acquisition_complete()
            elif name == "live-cli" and mode == "replay":
                self._on_acquisition_complete()
    
    @Slot(str, str)
    def _on_process_output(self, name: str, text: str):
        """Handle process output signal."""
        self._engineering_tab.append_output(name, text)
    
    def _on_acquisition_complete(self):
        """Handle acquisition completion - save average data."""
        self._acquiring = False
        self._operator_tab.set_acquiring(False)
        
        mode = getattr(self, '_current_mode', 'start')
        
        if mode in ("preview", "simulator", "replay"):
            self._status_bar.showMessage(f"{mode.capitalize()} complete")
            self._engineering_tab.append_system_log(f"{mode.capitalize()} complete")
            return
        
        # Save average data for real acquisitions
        cumulative_sum, total_cycles = self._operator_tab.get_cumulative_data()
        
        if cumulative_sum is not None and total_cycles > 0:
            self._save_average_data(cumulative_sum, total_cycles)
        
        self._status_bar.showMessage("Acquisition complete")
        self._engineering_tab.append_system_log("Acquisition complete")
    
    def _save_average_data(self, cumulative_sum: np.ndarray, total_cycles: int):
        """Save the average heatmap as PNG and CSV."""
        if not self._current_output_dir:
            logger.warning("No output directory set, skipping save")
            return
        
        output_dir = Path(self._current_output_dir)
        
        # Generate filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Calculate average per cycle
        average = cumulative_sum / total_cycles
        
        # Sum over y for 2D heatmap (x, t)
        avg_2d = np.sum(average, axis=1)
        
        # Save as CSV
        csv_path = output_dir / f"average_{timestamp}.csv"
        try:
            np.savetxt(csv_path, avg_2d, delimiter=",", fmt="%.6e")
            self._engineering_tab.append_system_log(f"Saved average to {csv_path}")
            logger.info(f"Saved average CSV: {csv_path}")
        except Exception as e:
            logger.error(f"Error saving CSV: {e}")
            self._engineering_tab.append_system_log(f"Error saving CSV: {e}")
        
        # Save as PNG
        png_path = output_dir / f"average_{timestamp}.png"
        try:
            from .widgets import get_colormap, apply_colormap
            from PySide6.QtGui import QImage
            
            # Apply colormap
            cmap = get_colormap("viridis")
            rgb = apply_colormap(avg_2d.T, cmap)  # Transpose for display
            
            # Save as image
            h, w = rgb.shape[:2]
            qimg = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888)
            qimg.save(str(png_path))
            
            self._engineering_tab.append_system_log(f"Saved average image to {png_path}")
            logger.info(f"Saved average PNG: {png_path}")
        except Exception as e:
            logger.error(f"Error saving PNG: {e}")
            self._engineering_tab.append_system_log(f"Error saving PNG: {e}")
    
    @Slot(object)
    def _on_flush_received(self, flush_data: FlushData):
        """Handle incoming flush from ZMQ worker."""
        self._operator_tab.on_flush_received(flush_data)
        
        # Log to engineering tab
        meta = flush_data.metadata
        flush_num = meta.get('flush_number', '?')
        cycles = meta.get('cycles_in_flush', '?')
        self._engineering_tab.append_zmq_log(
            f"Flush #{flush_num}: {cycles} cycles, {np.sum(flush_data.array):.2e} counts"
        )
    
    @Slot(bool)
    def _on_zmq_connection_changed(self, connected: bool):
        """Handle ZMQ connection state change for engineering tab."""
        if connected:
            self._engineering_tab.append_zmq_log("Receiving data from streaming server")
        else:
            self._engineering_tab.append_zmq_log("Not receiving data")

    @Slot(bool)
    def _on_serval_connection_changed(self, connected: bool):
        """Handle Serval connection state change."""
        if connected:
            self._engineering_tab.append_system_log("Connected to Serval")
        else:
            self._engineering_tab.append_system_log("Disconnected from Serval")
    
    @Slot(bool)
    def _on_heartbeat_connection_changed(self, connected: bool):
        """Handle heartbeat connection state change."""
        if connected:
            self._engineering_tab.append_system_log("Heartbeat connected")
    
    @Slot(object)
    def _on_heartbeat_status_for_state(self, status: HeartbeatStatus):
        """Track heartbeat state for startup sequence."""
        self._last_heartbeat_state = status.state if status.connected else "disconnected"
    
    @Slot(str)
    def _on_zmq_error(self, error: str):
        """Handle ZMQ error."""
        self._engineering_tab.append_zmq_log(f"Error: {error}")
    
    def closeEvent(self, event):
        """Handle window close - cleanup workers and processes."""
        logger.info("Closing application...")
        
        # Stop all workers
        if self._zmq_worker:
            self._zmq_worker.stop()
            self._zmq_worker.wait(2000)
        
        if self._heartbeat_worker:
            self._heartbeat_worker.stop()
            self._heartbeat_worker.wait(2000)
        
        if self._serval_worker:
            self._serval_worker.stop()
            self._serval_worker.wait(2000)
        
        # Stop all processes (including Serval)
        if self._process_manager:
            self._process_manager.stop_all()
        
        event.accept()


def main():
    """Application entry point."""
    import sys
    
    autostart_serval = "--autostart-serval" in sys.argv
    
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    
    window = MainWindow(autostart_serval=autostart_serval)
    window.show()
    
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
    