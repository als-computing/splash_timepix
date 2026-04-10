"""Operator tab - main acquisition control interface."""

import json
import logging
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .widgets import HeatmapWidget, StatusIndicator, get_colormap
from .workers import FlushData, HeartbeatStatus, ServalStatus

logger = logging.getLogger(__name__)


def _queue_metric_style(*, alert: bool) -> str:
    """Monospace queue value; alarm is red text only (no border/padding—avoids layout shift)."""
    color = theme.STATUS_ERROR if alert else theme.TEXT_PRIMARY
    return f"font-family: monospace; color: {color};"


def _title_light_label(text: str) -> str:
    """Title-case words for status text beside connection lights."""
    if not text:
        return ""
    t = text.strip()
    if t == "…":
        return t
    return t.replace("_", " ").title()


def _serval_light_label(raw: Optional[str]) -> str:
    """Map Serval measurement status to operator-facing labels."""
    if not raw or not str(raw).strip():
        return "Idle"
    code = str(raw).strip().upper()
    if code in ("DA_IDLE", "IDLE"):
        return "Idle"
    if code == "DA_RECORDING":
        return "Acquiring"
    if code.startswith("DA_"):
        return code[3:].replace("_", " ").title()
    return str(raw).replace("_", " ").title()


class OperatorTab(QWidget):
    """Main operator interface tab."""

    start_requested = Signal(str, dict)  # mode, params dict
    stop_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

        self._last_metadata: Optional[dict] = None
        self._last_serval_status: Optional[ServalStatus] = None
        self._serval_process_running = False
        self._serval_hw_ready = False  # True after "Chip temps:" appears in Serval log
        self._acquiring = False
        self._cumulative_sum: Optional[np.ndarray] = None
        self._total_cycles = 0
        self._flush_count = 0

        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # Top bar
        top_bar = QFrame()
        top_bar.setStyleSheet(f"QFrame {{ background-color: {theme.BG_WIDGET}; border-radius: 6px; }}")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(8, 8, 8, 8)
        top_layout.setSpacing(8)

        # Acquisition mode buttons grouped
        mode_group = QFrame()
        mode_group.setStyleSheet(
            f"""
            QFrame {{
                background-color: {theme.BG_BUTTON_GROUP};
                border-radius: 6px;
                border: 1px solid {theme.BLUE_LIGHT_2};
            }}
        """
        )
        mode_layout = QHBoxLayout(mode_group)
        mode_layout.setContentsMargins(6, 6, 6, 6)
        mode_layout.setSpacing(6)

        BUTTON_WIDTH = 125

        self._start_btn = QPushButton("▶ Start")
        self._start_btn.setFixedWidth(BUTTON_WIDTH)
        self._start_btn.setStyleSheet(theme.button_style(theme.BUTTON_START))
        self._start_btn.clicked.connect(lambda: self._on_mode_clicked("start"))
        mode_layout.addWidget(self._start_btn)

        self._preview_btn = QPushButton("Preview")
        self._preview_btn.setFixedWidth(BUTTON_WIDTH)
        self._preview_btn.setStyleSheet(theme.button_style(theme.BUTTON_PREVIEW))
        self._preview_btn.clicked.connect(lambda: self._on_mode_clicked("preview"))
        mode_layout.addWidget(self._preview_btn)

        self._simulator_btn = QPushButton("◈ Simulator")
        self._simulator_btn.setFixedWidth(BUTTON_WIDTH)
        self._simulator_btn.setStyleSheet(theme.button_style(theme.BUTTON_SIMULATOR))
        self._simulator_btn.clicked.connect(lambda: self._on_mode_clicked("simulator"))
        mode_layout.addWidget(self._simulator_btn)

        self._replay_btn = QPushButton("↺ Replay")
        self._replay_btn.setFixedWidth(BUTTON_WIDTH)
        self._replay_btn.setStyleSheet(theme.button_style(theme.BUTTON_REPLAY))
        self._replay_btn.clicked.connect(self._on_replay_clicked)
        mode_layout.addWidget(self._replay_btn)

        top_layout.addWidget(mode_group)
        top_layout.addSpacing(12)

        # Stop button (outside mode group)
        self._stop_btn = QPushButton("⏹ Stop")
        self._stop_btn.setFixedWidth(BUTTON_WIDTH)
        self._stop_btn.setStyleSheet(theme.button_style(theme.BUTTON_STOP))
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop_clicked)
        top_layout.addWidget(self._stop_btn)

        top_layout.addSpacing(24)

        # View controls group
        view_group = QFrame()
        view_group.setStyleSheet(f"QFrame {{ background-color: {theme.BG_BUTTON_GROUP}; border-radius: 4px; }}")
        view_layout = QHBoxLayout(view_group)
        view_layout.setContentsMargins(8, 6, 8, 6)
        view_layout.setSpacing(8)

        cmap_label = QLabel("Colormap:")
        cmap_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        view_layout.addWidget(cmap_label)

        self._colormap_combo = QComboBox()
        self._colormap_combo.addItems(["viridis", "plasma", "inferno", "magma"])
        self._colormap_combo.setStyleSheet(theme.input_style())
        self._colormap_combo.currentTextChanged.connect(self._on_colormap_changed)
        view_layout.addWidget(self._colormap_combo)

        view_layout.addSpacing(12)

        self._reset_avg_btn = QPushButton("Reset Avg")
        self._reset_avg_btn.setStyleSheet(theme.secondary_button_style())
        self._reset_avg_btn.clicked.connect(self._reset_average)
        view_layout.addWidget(self._reset_avg_btn)

        top_layout.addWidget(view_group)
        top_layout.addStretch()

        layout.addWidget(top_bar)

        # Main content
        content_layout = QHBoxLayout()
        content_layout.setSpacing(10)

        # Left panel
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_panel.setFixedWidth(280)

        # Connection status
        status_group = QGroupBox("Connection Status")
        status_group.setStyleSheet(theme.group_box_style())
        status_layout = QVBoxLayout(status_group)

        self._serval_status = StatusIndicator("Serval")
        self._serval_status.set_run_state(False, "")
        self._stream_status = StatusIndicator("Stream")
        self._zmq_status = StatusIndicator("ZMQ Data")

        status_layout.addWidget(self._serval_status)
        status_layout.addWidget(self._stream_status)
        status_layout.addWidget(self._zmq_status)
        left_layout.addWidget(status_group)

        # Acquisition settings
        settings_group = QGroupBox("Acquisition Settings")
        settings_group.setStyleSheet(theme.group_box_style())
        settings_group.setToolTip(
            "Parameters for the streaming server and acquisition. Each row has a detailed tooltip; "
            "hover the label or control."
        )
        settings_layout = QVBoxLayout(settings_group)

        _tt_tdc_freq = (
            "Expected TDC trigger rate in Hz. Defines one full time cycle per trigger for binning "
            "and flush timing in the streaming server. Should match the hardware / Serval setup."
        )
        # TDC Frequency
        tdc_row = QHBoxLayout()
        tdc_label = QLabel("TDC Frequency (Hz):")
        tdc_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        tdc_label.setToolTip(_tt_tdc_freq)
        tdc_row.addWidget(tdc_label)
        self._tdc_freq_input = QDoubleSpinBox()
        self._tdc_freq_input.setRange(0.1, 1e9)
        self._tdc_freq_input.setValue(1000.0)
        self._tdc_freq_input.setDecimals(1)
        self._tdc_freq_input.setStyleSheet(theme.input_style())
        self._tdc_freq_input.setToolTip(_tt_tdc_freq)
        tdc_row.addWidget(self._tdc_freq_input)
        settings_layout.addLayout(tdc_row)

        _tt_tdc_ch = (
            "Which TimePix TDC input defines cycle boundaries: both channels, or only TDC1 / TDC2. "
            "Must match how the detector and Serval are wired."
        )
        # TDC Channel
        tdc_ch_row = QHBoxLayout()
        tdc_ch_label = QLabel("TDC Channel:")
        tdc_ch_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        tdc_ch_label.setToolTip(_tt_tdc_ch)
        tdc_ch_row.addWidget(tdc_ch_label)
        self._tdc_ch_combo = QComboBox()
        self._tdc_ch_combo.addItems(["Both", "1", "2"])
        self._tdc_ch_combo.setStyleSheet(theme.input_style())
        self._tdc_ch_combo.setToolTip(_tt_tdc_ch)
        tdc_ch_row.addWidget(self._tdc_ch_combo)
        settings_layout.addLayout(tdc_ch_row)

        _tt_tdc_edge = "Trigger on the rising or falling edge of the selected TDC line."
        # TDC Edge
        tdc_edge_row = QHBoxLayout()
        tdc_edge_label = QLabel("TDC Edge:")
        tdc_edge_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        tdc_edge_label.setToolTip(_tt_tdc_edge)
        tdc_edge_row.addWidget(tdc_edge_label)
        self._tdc_edge_combo = QComboBox()
        self._tdc_edge_combo.addItems(["Rising", "Falling"])
        self._tdc_edge_combo.setStyleSheet(theme.input_style())
        self._tdc_edge_combo.setToolTip(_tt_tdc_edge)
        tdc_edge_row.addWidget(self._tdc_edge_combo)
        settings_layout.addLayout(tdc_edge_row)

        # Parse batch size (CLI --callback-batch-size): packets per vectorized parse_batch call
        batch_row = QHBoxLayout()
        batch_label = QLabel("Parse batch (pkts):")
        batch_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        batch_label.setToolTip(
            "Target number of 12-byte packets per parse batch (vectorized parse_batch). "
            "Lower → smaller batches and more frequent parser callbacks (often smoother under load); "
            "higher → fewer, larger batches (throughput). Matches server --callback-batch-size."
        )
        batch_row.addWidget(batch_label)
        self._callback_batch_input = QSpinBox()
        self._callback_batch_input.setRange(1, 10_000_000)
        self._callback_batch_input.setValue(10_000)
        self._callback_batch_input.setSingleStep(1000)
        self._callback_batch_input.setStyleSheet(theme.input_style())
        self._callback_batch_input.setToolTip(batch_label.toolTip())
        batch_row.addWidget(self._callback_batch_input)
        settings_layout.addLayout(batch_row)

        _tt_duration = (
            "Acquisition length in seconds for Serval-backed runs and the simulator. "
            "Preview and replay may ignore this depending on the workflow."
        )
        # Duration
        dur_row = QHBoxLayout()
        dur_label = QLabel("Duration (s):")
        dur_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        dur_label.setToolTip(_tt_duration)
        dur_row.addWidget(dur_label)
        self._duration_input = QSpinBox()
        self._duration_input.setRange(1, 19008000)
        self._duration_input.setValue(60)
        self._duration_input.setStyleSheet(theme.input_style())
        self._duration_input.setToolTip(_tt_duration)
        dur_row.addWidget(self._duration_input)
        settings_layout.addLayout(dur_row)

        _tt_output_label = (
            "Directory for Serval output (.tpx3) and saved averages in Start mode. "
            "Hover the path field to see the full resolved path."
        )
        # Output directory
        out_row = QHBoxLayout()
        out_label = QLabel("Output:")
        out_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        out_label.setToolTip(_tt_output_label)
        out_row.addWidget(out_label)
        self._output_input = QLineEdit()
        self._output_input.setText(str(Path.home() / "Desktop" / "data"))
        self._output_input.setStyleSheet(theme.input_style())
        self._output_input.textChanged.connect(self._sync_output_path_tooltip)
        out_row.addWidget(self._output_input)
        self._browse_output_btn = QPushButton("…")
        self._browse_output_btn.setFixedSize(32, 26)
        self._browse_output_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {theme.GREY_DARK};
                color: {theme.TEXT_PRIMARY};
                border: 1px solid {theme.BORDER_SUBTLE};
                border-radius: 4px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {theme.GREY_LIGHT};
                color: {theme.BG_DARK};
            }}
        """
        )
        self._browse_output_btn.setToolTip(
            "Choose output folder. The path field tooltip always shows the full resolved directory when you hover it."
        )
        self._browse_output_btn.clicked.connect(self._browse_output)
        out_row.addWidget(self._browse_output_btn)
        settings_layout.addLayout(out_row)
        self._sync_output_path_tooltip()

        left_layout.addWidget(settings_group)

        # Pipeline queue depths (from streaming server heartbeat)
        pipeline_group = QGroupBox("Pipeline queues")
        pipeline_group.setStyleSheet(theme.group_box_style())
        pipeline_group.setToolTip(
            "Live depth vs capacity. Packet buffer fills when the parse thread cannot keep up with TCP—"
            "try lowering Parse batch (pkts) or increasing buffer-size on the server. "
            "ZMQ PUB (SUB): 3D flush depth (publisher); value in parentheses is the start/stop control queue."
        )
        pipeline_layout = QVBoxLayout(pipeline_group)
        pipeline_layout.setSpacing(4)
        self._queue_labels: dict[str, QLabel] = {}
        queue_rows = [
            (
                "packet_buffer",
                "Packet buffer:",
                "Queued raw TCP batches waiting for the parser thread (SocketDataServer message queue). "
                "Fills if parsing lags behind the network reader.",
            ),
            (
                "zmq_pub",
                "ZMQ PUB (SUB):",
                "3D flush queue (PUB path) and control queue depth in parentheses; "
                "denominator is flush-queue capacity.",
            ),
        ]
        for key, title, tip in queue_rows:
            row = QHBoxLayout()
            name_label = QLabel(title)
            name_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
            name_label.setToolTip(tip)
            value_label = QLabel("--")
            value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(name_label)
            row.addStretch()
            row.addWidget(value_label)
            pipeline_layout.addLayout(row)
            self._queue_labels[key] = value_label

        left_layout.addWidget(pipeline_group)

        # Statistics
        stats_group = QGroupBox("Statistics")
        stats_group.setStyleSheet(theme.group_box_style())
        stats_layout = QVBoxLayout(stats_group)
        stats_layout.setSpacing(4)

        self._stats_labels = {}
        stat_names = [
            ("pixel_rate", "Pixel Rate:"),
            ("tdc1_rate", "TDC1 Rate:"),
            ("tdc2_rate", "TDC2 Rate:"),
            ("elapsed_remaining", "Elapsed / Remaining:"),
            ("flushes_cycles", "Flushes (Cycles):"),
            ("avg_counts", "Avg Counts/Cycle:"),
        ]

        for key, label in stat_names:
            row = QHBoxLayout()
            name_label = QLabel(label)
            name_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
            value_label = QLabel("--")
            value_label.setStyleSheet(f"font-family: monospace; color: {theme.TEXT_PRIMARY};")
            row.addWidget(name_label)
            row.addStretch()
            row.addWidget(value_label)
            stats_layout.addLayout(row)
            self._stats_labels[key] = value_label

        left_layout.addWidget(stats_group)
        left_layout.addStretch()

        content_layout.addWidget(left_panel)

        # Heatmaps
        right_panel = QWidget()
        right_layout = QHBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)

        self._current_heatmap = HeatmapWidget("Current Flush")
        self._average_heatmap = HeatmapWidget("Running Average")

        right_layout.addWidget(self._current_heatmap)
        right_layout.addWidget(self._average_heatmap)

        content_layout.addWidget(right_panel, stretch=1)
        layout.addLayout(content_layout, stretch=1)

    def _sync_output_path_tooltip(self, *_args) -> None:
        """Keep the output line edit tooltip equal to the resolved full path (or a short hint if empty)."""
        text = self._output_input.text().strip()
        if not text:
            self._output_input.setToolTip(
                "Output directory for file-backed acquisition. Type a path or use Browse. "
                "Hover here after entering a path to see the full resolved location."
            )
            return
        p = Path(text).expanduser()
        try:
            full = str(p.resolve())
        except OSError:
            full = str(p)
        self._output_input.setToolTip(full)

    def _browse_output(self):
        current = self._output_input.text() or str(Path.home() / "data")
        directory = QFileDialog.getExistingDirectory(self, "Select Output Directory", current)
        if directory:
            self._output_input.setText(directory)

    def _get_tdc_channel(self) -> int:
        text = self._tdc_ch_combo.currentText()
        if text == "Both":
            return 0
        return int(text)

    def _get_tdc_edge(self) -> str:
        return self._tdc_edge_combo.currentText().lower()

    def _get_params(self) -> dict:
        return {
            "tdc_frequency": self._tdc_freq_input.value(),
            "tdc_channel": self._get_tdc_channel(),
            "tdc_edge": self._get_tdc_edge(),
            "callback_batch_size": int(self._callback_batch_input.value()),
            "duration": self._duration_input.value(),
            "output_dir": self._output_input.text(),
        }

    def _on_mode_clicked(self, mode: str):
        params = self._get_params()
        if mode == "start" and not params["output_dir"]:
            QMessageBox.warning(self, "Missing Output", "Please specify an output directory.")
            return
        self._reset_average()
        self.start_requested.emit(mode, params)

    def _on_replay_clicked(self):
        current_dir = self._output_input.text() or str(Path.home() / "data")
        file_path, _ = QFileDialog.getOpenFileName(self, "Select Replay File", current_dir, "TPX3 Files (*.tpx3)")
        if file_path:
            params = self._get_params()
            params["replay_file"] = file_path
            self._reset_average()
            self.start_requested.emit("replay", params)

    def _on_stop_clicked(self):
        self.stop_requested.emit()

    def _on_colormap_changed(self, name: str):
        self._current_heatmap.set_colormap(name)
        self._average_heatmap.set_colormap(name)

    def _reset_average(self):
        self._cumulative_sum = None
        self._total_cycles = 0
        self._flush_count = 0
        self._average_heatmap.clear()
        self._update_flush_stats()
        logger.info("Running average reset")

    def _update_flush_stats(self):
        self._stats_labels["flushes_cycles"].setText(f"{self._flush_count} ({self._total_cycles:,})")
        if self._total_cycles > 0 and self._cumulative_sum is not None:
            avg = np.sum(self._cumulative_sum) / self._total_cycles
            self._stats_labels["avg_counts"].setText(f"{avg:.2e}")
        else:
            self._stats_labels["avg_counts"].setText("--")

    @Slot(bool)
    def set_acquiring(self, acquiring: bool):
        self._acquiring = acquiring
        self._start_btn.setEnabled(not acquiring)
        self._preview_btn.setEnabled(not acquiring)
        self._simulator_btn.setEnabled(not acquiring)
        self._replay_btn.setEnabled(not acquiring)
        self._stop_btn.setEnabled(acquiring)
        self._tdc_freq_input.setEnabled(not acquiring)
        self._tdc_ch_combo.setEnabled(not acquiring)
        self._tdc_edge_combo.setEnabled(not acquiring)
        self._callback_batch_input.setEnabled(not acquiring)
        self._duration_input.setEnabled(not acquiring)
        self._output_input.setEnabled(not acquiring)
        self._browse_output_btn.setEnabled(not acquiring)

    @Slot(object)
    def on_flush_received(self, flush_data: FlushData):
        array = flush_data.array
        metadata = flush_data.metadata
        self._last_metadata = metadata

        if array.ndim == 2:
            heatmap_2d = array
        else:
            heatmap_2d = np.sum(array, axis=1)

        cycles_in_flush = metadata.get("cycles_in_flush", 1)
        flush_number = metadata.get("flush_number", self._flush_count + 1)
        stats_text = f"Flush #{flush_number} | {cycles_in_flush} cycles | Total: {np.sum(heatmap_2d):.2e}"
        self._current_heatmap.set_data(heatmap_2d, stats_text)

        if cycles_in_flush > 0:
            if self._cumulative_sum is None:
                self._cumulative_sum = array.astype(np.float64)
                self._total_cycles = cycles_in_flush
            else:
                self._cumulative_sum += array.astype(np.float64)
                self._total_cycles += cycles_in_flush

            self._flush_count = flush_number
            average = self._cumulative_sum / self._total_cycles

            if average.ndim == 2:
                avg_2d = average
            else:
                avg_2d = np.sum(average, axis=1)

            avg_stats = f"Over {self._total_cycles} cycles | Avg: {np.sum(avg_2d):.2e}"
            self._average_heatmap.set_data(avg_2d, avg_stats)
            self._update_flush_stats()

    def _update_serval_indicator(self) -> None:
        """Red when JVM down; blinking green until chip temps log; then steady green + Idle/status."""
        if not self._serval_process_running:
            self._serval_status.set_run_state(False, "")
            return

        if not self._serval_hw_ready:
            if not self._serval_status.is_ok_blinking():
                self._serval_status.start_ok_blink("Starting…")
            return

        st = self._last_serval_status
        if st and st.connected:
            detail = _serval_light_label(st.status)
        else:
            detail = "Idle"

        self._serval_status.set_run_state(True, detail)

    @Slot(bool)
    def on_serval_process_running(self, running: bool) -> None:
        self._serval_process_running = running
        self._serval_hw_ready = False
        self._update_serval_indicator()

    def on_serval_chip_temps_line_seen(self) -> None:
        """Called when Serval stdout contains the chip temperature line (startup complete)."""
        if not self._serval_process_running or self._serval_hw_ready:
            return
        self._serval_hw_ready = True
        self._update_serval_indicator()

    @Slot(object)
    def on_serval_status(self, status: ServalStatus):
        self._last_serval_status = status
        self._update_serval_indicator()
        if status.connected:
            self._stats_labels["pixel_rate"].setText(f"{status.pixel_event_rate:.2e} cps")
            self._stats_labels["tdc1_rate"].setText(f"{status.tdc1_event_rate:.1f} Hz")
            self._stats_labels["tdc2_rate"].setText(f"{status.tdc2_event_rate:.1f} Hz")
            elapsed = status.elapsed_time
            remaining = status.time_left
            el = f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}"
            rem = f"{int(remaining // 60):02d}:{int(remaining % 60):02d}"
            self._stats_labels["elapsed_remaining"].setText(f"{el} / {rem}")

    def _apply_queue_depth_label(self, label: QLabel, depth: Optional[Tuple[int, int]]) -> None:
        if depth is None:
            label.setText("--")
            label.setToolTip("")
            label.setStyleSheet(_queue_metric_style(alert=False))
            return
        sz, mx = depth
        label.setText(f"{sz} / {mx}")
        label.setToolTip(f"Packet buffer: {sz} of {mx} batches queued for parse. Full = red (drops likely).")
        label.setStyleSheet(_queue_metric_style(alert=mx > 0 and sz >= mx))

    def _apply_zmq_pub_sub_label(
        self,
        label: QLabel,
        q_xyt: Optional[Tuple[int, int]],
        q_ctrl: Optional[Tuple[int, int]],
    ) -> None:
        if q_xyt is None:
            label.setText("--")
            label.setToolTip("")
            label.setStyleSheet(_queue_metric_style(alert=False))
            return
        sx, mx = q_xyt
        if q_ctrl is None:
            label.setText(f"{sx} (--) / {mx}")
            tip = f"3D flush queue: {sx} of {mx}. Control queue not reported."
        else:
            sc, mc = q_ctrl
            label.setText(f"{sx} ({sc}) / {mx}")
            tip = f"3D flush (PUB): {sx}/{mx}; control (SUB): {sc}/{mc}. " "Red if either queue is full (drops likely)."
        label.setToolTip(tip)
        flush_full = mx > 0 and sx >= mx
        ctrl_full = q_ctrl is not None and q_ctrl[1] > 0 and q_ctrl[0] >= q_ctrl[1]
        label.setStyleSheet(_queue_metric_style(alert=flush_full or ctrl_full))

    @Slot(object)
    def on_heartbeat_status(self, status: HeartbeatStatus):
        if status.connected:
            if status.state == "streaming":
                self._stream_status.set_streaming_active(_title_light_label("streaming"))
            else:
                state_label = _title_light_label(status.state) if status.state else "…"
                if not self._stream_status.is_ok_blinking():
                    self._stream_status.start_ok_blink(state_label)
                else:
                    self._stream_status.set_status_detail(state_label)
            self._apply_queue_depth_label(self._queue_labels["packet_buffer"], status.q_ingest)
            self._apply_zmq_pub_sub_label(self._queue_labels["zmq_pub"], status.q_xyt, status.q_zmq_control)
        else:
            self._stream_status.set_connected(False, "")
            self._apply_queue_depth_label(self._queue_labels["packet_buffer"], None)
            self._apply_zmq_pub_sub_label(self._queue_labels["zmq_pub"], None, None)

    @Slot(bool)
    def on_zmq_connection_changed(self, connected: bool):
        self._zmq_status.set_connected(connected, _title_light_label("receiving") if connected else "")

    def get_cumulative_data(self) -> tuple[Optional[np.ndarray], int]:
        return self._cumulative_sum, self._total_cycles

    def save_average_data(
        self, output_dir: str, filename_base: str
    ) -> tuple[Optional[Path], Optional[Path], Optional[Path]]:
        """Save the average heatmap as PNG, CSV, and metadata as JSON."""
        if self._cumulative_sum is None or self._total_cycles == 0:
            logger.warning("No data to save")
            return None, None, None

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        average = self._cumulative_sum / self._total_cycles
        if average.ndim == 2:
            avg_2d = average
        else:
            avg_2d = np.sum(average, axis=1)

        # Save CSV
        csv_path = output_path / f"{filename_base}_avg.csv"
        try:
            np.savetxt(csv_path, avg_2d, delimiter=",", fmt="%.6e")
            logger.info(f"Saved CSV: {csv_path}")
        except Exception as e:
            logger.error(f"Failed to save CSV: {e}")
            csv_path = None

        # Save PNG
        png_path = output_path / f"{filename_base}_avg.png"
        try:
            display_data = np.flipud(avg_2d.T.astype(np.float32))
            vmin, vmax = display_data.min(), display_data.max()
            if vmax <= vmin:
                vmax = vmin + 1
            normalized = np.clip((display_data - vmin) / (vmax - vmin) * 255, 0, 255).astype(np.uint8)

            cmap = get_colormap(self._colormap_combo.currentText())
            rgb = cmap[normalized]

            from PySide6.QtGui import QImage

            h, w = rgb.shape[:2]
            qimg = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888)
            qimg.save(str(png_path))
            logger.info(f"Saved PNG: {png_path}")
        except Exception as e:
            logger.error(f"Failed to save PNG: {e}")
            png_path = None

        # Save JSON metadata
        json_path = output_path / f"{filename_base}_meta.json"
        try:
            meta = {
                "total_flushes": self._flush_count,
                "total_cycles": self._total_cycles,
                "total_counts": float(np.sum(self._cumulative_sum)),
                "avg_counts_per_cycle": float(np.sum(self._cumulative_sum) / self._total_cycles),
                "array_shape": list(avg_2d.shape),
            }

            if self._last_metadata:
                meta["zmq_metadata"] = self._last_metadata

            with open(json_path, "w") as f:
                json.dump(meta, f, indent=2)
            logger.info(f"Saved JSON: {json_path}")
        except Exception as e:
            logger.error(f"Failed to save JSON: {e}")
            json_path = None

        return png_path, csv_path, json_path
