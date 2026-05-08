"""Operator tab - main acquisition control interface."""

import json
import logging
import uuid
from pathlib import Path
from typing import Optional, Tuple, Union

import numpy as np
from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
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

from . import preferences, theme
from .widgets import CURSOR_COLORS, HeatmapWidget, SpectrumPlotWidget, StatusIndicator, VerticalLabel, get_colormap
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
        self._last_avg_2d: Optional[np.ndarray] = None  # most recent averaged heatmap data
        self._n_energy: Optional[int] = None  # number of energy pixels in current data

        self._setup_ui()
        try:
            self.load_operator_preferences()
        except Exception:  # pragma: no cover - defensive
            logger.exception("Failed to load operator preferences; using widget defaults")

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

        cmap_sep = QFrame()
        cmap_sep.setFrameShape(QFrame.Shape.VLine)
        cmap_sep.setFixedWidth(1)
        cmap_sep.setStyleSheet(f"background-color: {theme.BORDER_SUBTLE}; border: none;")
        view_layout.addWidget(cmap_sep)

        self._vcursor_heatmap_btn = QPushButton("| Cursors")
        self._vcursor_heatmap_btn.setCheckable(True)
        self._vcursor_heatmap_btn.setChecked(True)
        self._vcursor_heatmap_btn.setStyleSheet(theme.checkable_button_style())
        self._vcursor_heatmap_btn.setToolTip("Show/hide energy cursor lines in both heatmaps")
        self._vcursor_heatmap_btn.toggled.connect(self._on_vcursor_heatmap_toggled)
        view_layout.addWidget(self._vcursor_heatmap_btn)

        view_layout.addSpacing(12)

        # Zoom button group
        zoom_group = QFrame()
        zoom_group.setStyleSheet(
            f"QFrame {{ border: 1px solid {theme.BORDER_SUBTLE}; border-radius: 4px;"
            f" background-color: {theme.BG_DARK}; }}"
        )
        zoom_layout = QHBoxLayout(zoom_group)
        zoom_layout.setContentsMargins(2, 2, 2, 2)
        zoom_layout.setSpacing(2)

        self._zoom_rect_btn = QPushButton("⊞ Zoom")
        self._zoom_rect_btn.setCheckable(True)
        self._zoom_rect_btn.setChecked(True)
        self._zoom_rect_btn.setStyleSheet(theme.checkable_button_style())
        self._zoom_rect_btn.clicked.connect(lambda: self._set_zoom_mode("rect"))
        zoom_layout.addWidget(self._zoom_rect_btn)

        self._zoom_h_btn = QPushButton("↔ H-Zoom")
        self._zoom_h_btn.setCheckable(True)
        self._zoom_h_btn.setStyleSheet(theme.checkable_button_style())
        self._zoom_h_btn.clicked.connect(lambda: self._set_zoom_mode("h"))
        zoom_layout.addWidget(self._zoom_h_btn)

        self._zoom_v_btn = QPushButton("↕ V-Zoom")
        self._zoom_v_btn.setCheckable(True)
        self._zoom_v_btn.setStyleSheet(theme.checkable_button_style())
        self._zoom_v_btn.clicked.connect(lambda: self._set_zoom_mode("v"))
        zoom_layout.addWidget(self._zoom_v_btn)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFixedWidth(1)
        sep.setStyleSheet(f"background-color: {theme.BORDER_SUBTLE}; border: none;")
        zoom_layout.addWidget(sep)

        self._reset_view_btn = QPushButton("⟳ Reset Zoom")
        self._reset_view_btn.setStyleSheet(theme.checkable_button_style())
        self._reset_view_btn.clicked.connect(self._reset_view)
        zoom_layout.addWidget(self._reset_view_btn)

        view_layout.addWidget(zoom_group)

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

        # n_bins
        n_bins_row = QHBoxLayout()
        n_bins_label = QLabel("Time bins (n_bins):")
        n_bins_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        n_bins_label.setToolTip(
            "Number of time bins per TDC cycle. Used as the fallback when the streaming server "
            "cannot derive bin count from t_delta_ns. Default: 10000."
        )
        n_bins_row.addWidget(n_bins_label)
        self._n_bins_input = QSpinBox()
        self._n_bins_input.setRange(500, 50_000)
        self._n_bins_input.setValue(10_000)
        self._n_bins_input.setSingleStep(10)
        self._n_bins_input.setStyleSheet(theme.input_style())
        self._n_bins_input.setToolTip(n_bins_label.toolTip())
        n_bins_row.addWidget(self._n_bins_input)
        settings_layout.addLayout(n_bins_row)

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

        # Heatmaps + spectrum panel
        right_panel = QWidget()
        right_layout = QHBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)

        # Both heatmap columns use the same structure: heatmap (expanding) + bottom row (fixed).
        # _SPECTRUM_ROW_H is shared so the two HeatmapWidgets always have equal height.
        _SPECTRUM_ROW_H = 210

        # Current Flush column: heatmap + cursor-readout / calibration panel
        flush_col = QWidget()
        flush_col_layout = QVBoxLayout(flush_col)
        flush_col_layout.setContentsMargins(0, 0, 0, 0)
        flush_col_layout.setSpacing(0)
        self._current_heatmap = HeatmapWidget("Current Flush")
        flush_col_layout.addWidget(self._current_heatmap)

        # --- Cursor readout + calibration panel ---
        readout_panel = QFrame()
        readout_panel.setFixedHeight(_SPECTRUM_ROW_H)
        readout_panel.setStyleSheet(
            f"QFrame {{ background-color: {theme.BG_PANEL}; "
            f"border: 1px solid {theme.BORDER_SUBTLE}; border-radius: 4px; }}"
        )
        rp_outer = QHBoxLayout(readout_panel)
        rp_outer.setContentsMargins(0, 0, 0, 0)
        rp_outer.setSpacing(0)

        # Left 2/3: calibration controls + cursor readout
        rp_left = QWidget()
        rp_layout = QVBoxLayout(rp_left)
        rp_layout.setContentsMargins(10, 8, 8, 8)
        rp_layout.setSpacing(4)

        # Vertical divider between left and right sections
        sep_v = QFrame()
        sep_v.setFrameShape(QFrame.Shape.VLine)
        sep_v.setFixedWidth(1)
        sep_v.setStyleSheet(f"background-color: {theme.BORDER_SUBTLE}; border: none;")

        # Right 1/3: ROI pair legend + active toggle buttons
        rp_right = QWidget()
        rp_right_layout = QVBoxLayout(rp_right)
        rp_right_layout.setContentsMargins(8, 6, 8, 6)
        rp_right_layout.setSpacing(4)

        _ROI_LABELS = ("ROI 1", "ROI 2", "ROI 3", "ROI 4", "ROI 5")
        self._roi_toggle_btns: list[QPushButton] = []

        _toggle_style = f"""
            QPushButton {{
                background-color: {theme.GREY_DARK};
                color: {theme.TEXT_MUTED};
                border: 1px solid {theme.BORDER_SUBTLE};
                border-radius: 3px;
                padding: 1px 4px;
                font-size: 10px;
            }}
            QPushButton:checked {{
                background-color: {theme.BLUE_PRIMARY};
                color: {theme.TEXT_PRIMARY};
                border-color: {theme.BLUE_LIGHT_2};
            }}
        """

        for i in range(5):
            roi_row = QHBoxLayout()
            roi_row.setSpacing(6)

            swatch = QLabel()
            swatch.setFixedSize(16, 3)
            swatch.setStyleSheet(f"background-color: {CURSOR_COLORS[i]}; border: none;")
            roi_row.addWidget(swatch, alignment=Qt.AlignmentFlag.AlignVCenter)

            lbl = QLabel(_ROI_LABELS[i])
            lbl.setStyleSheet(f"color: {theme.TEXT_PRIMARY};")
            roi_row.addWidget(lbl)
            roi_row.addStretch()

            active_default = i < 2
            btn = QPushButton("On" if active_default else "Off")
            btn.setCheckable(True)
            btn.setChecked(active_default)
            btn.setFixedWidth(36)
            btn.setStyleSheet(_toggle_style)
            roi_row.addWidget(btn)
            rp_right_layout.addLayout(roi_row)
            self._roi_toggle_btns.append(btn)

        rp_right_layout.addStretch()

        rp_outer.addWidget(rp_left, stretch=2)
        rp_outer.addWidget(sep_v)
        rp_outer.addWidget(rp_right, stretch=1)

        # Calibration controls: label above each spinbox, the two controls side by side
        cal_row = QHBoxLayout()
        cal_row.setSpacing(8)

        pxev_col = QVBoxLayout()
        pxev_col.setSpacing(2)
        ev_px_lbl = QLabel("pixel/eV")
        ev_px_lbl.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 10px;")
        pxev_col.addWidget(ev_px_lbl)
        self._pixel_per_ev = QDoubleSpinBox()
        self._pixel_per_ev.setRange(-10000.0, 10000.0)
        self._pixel_per_ev.setDecimals(4)
        self._pixel_per_ev.setValue(12.796)
        self._pixel_per_ev.setSingleStep(0.001)
        self._pixel_per_ev.setStyleSheet(theme.input_style())
        self._pixel_per_ev.setFixedWidth(90)
        pxev_col.addWidget(self._pixel_per_ev)
        cal_row.addLayout(pxev_col)

        evmid_col = QVBoxLayout()
        evmid_col.setSpacing(2)
        ev_mid_lbl = QLabel("eV @ midpoint")
        ev_mid_lbl.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 10px;")
        evmid_col.addWidget(ev_mid_lbl)
        self._ev_at_mid = QDoubleSpinBox()
        self._ev_at_mid.setRange(-1_000_000.0, 1_000_000.0)
        self._ev_at_mid.setDecimals(2)
        self._ev_at_mid.setValue(0.0)
        self._ev_at_mid.setSingleStep(0.1)
        self._ev_at_mid.setStyleSheet(theme.input_style())
        self._ev_at_mid.setFixedWidth(90)
        evmid_col.addWidget(self._ev_at_mid)
        cal_row.addLayout(evmid_col)
        cal_row.addStretch()
        rp_layout.addLayout(cal_row)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {theme.BORDER_SUBTLE};")
        rp_layout.addWidget(sep)

        # Readout grid: columns = name | pixel | eV
        grid = QGridLayout()
        grid.setSpacing(2)
        grid.setColumnStretch(0, 2)
        grid.setColumnStretch(1, 3)
        grid.setColumnStretch(2, 3)

        for col, text in enumerate(["", "Pixel", "eV"]):
            hdr = QLabel(text)
            hdr.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 9px;")
            hdr.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(hdr, 0, col)

        self._cursor_px_labels: list[QLabel] = []
        self._cursor_ev_labels: list[QLabel] = []
        row_names = ["Cursor A", "Cursor B", "Δ"]
        for row_idx, name in enumerate(row_names):
            name_lbl = QLabel(name)
            name_lbl.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 9px;")
            grid.addWidget(name_lbl, row_idx + 1, 0)

            px_lbl = QLabel("--")
            px_lbl.setStyleSheet(f"font-family: monospace; color: {theme.TEXT_PRIMARY};")
            px_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(px_lbl, row_idx + 1, 1)
            self._cursor_px_labels.append(px_lbl)

            ev_lbl = QLabel("--")
            ev_lbl.setStyleSheet(f"font-family: monospace; color: {theme.TEXT_PRIMARY};")
            ev_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(ev_lbl, row_idx + 1, 2)
            self._cursor_ev_labels.append(ev_lbl)

        rp_layout.addLayout(grid)
        rp_layout.addStretch()

        self._pixel_per_ev.valueChanged.connect(self._update_cursor_readout)
        self._ev_at_mid.valueChanged.connect(self._update_cursor_readout)
        self._pixel_per_ev.valueChanged.connect(self._update_ruler_energy_scale)
        self._ev_at_mid.valueChanged.connect(self._update_ruler_energy_scale)

        flush_col_layout.addWidget(readout_panel)
        right_layout.addWidget(flush_col)

        # Running Average column: heatmap + spectrum panel
        avg_col = QWidget()
        avg_col_layout = QVBoxLayout(avg_col)
        avg_col_layout.setContentsMargins(0, 0, 0, 0)
        avg_col_layout.setSpacing(0)

        self._average_heatmap = HeatmapWidget("Running Average")
        self._average_heatmap.enable_cursors(True)
        self._average_heatmap.cursors_changed.connect(self._on_cursor_changed)
        avg_col_layout.addWidget(self._average_heatmap)

        # Spectrum wrapper — left spacer matches HeatmapWidget's y-label (20 px + 4 px spacing)
        # so the plot x-axis aligns with the heatmap image area.
        spectrum_outer = QWidget()
        spectrum_outer.setFixedHeight(_SPECTRUM_ROW_H)
        so_layout = QHBoxLayout(spectrum_outer)
        so_layout.setContentsMargins(4, 4, 4, 4)
        so_layout.setSpacing(4)
        y_counts_label = VerticalLabel("Counts")
        y_counts_label.setFixedWidth(20)
        so_layout.addWidget(y_counts_label)
        self._spectrum_plot = SpectrumPlotWidget()
        self._spectrum_plot.cursors_changed.connect(self._on_spectrum_cursors_changed)
        so_layout.addWidget(self._spectrum_plot)
        avg_col_layout.addWidget(spectrum_outer)

        self._current_heatmap.view_changed.connect(self._average_heatmap.set_view)
        self._average_heatmap.view_changed.connect(self._current_heatmap.set_view)
        # Zoom on either heatmap must refresh the spectrum/readout because
        # cursor fractions are remapped through the current view.  Note that
        # set_view does not re-emit view_changed, so we connect both signals.
        self._current_heatmap.view_changed.connect(self._on_heatmap_view_changed)
        self._average_heatmap.view_changed.connect(self._on_heatmap_view_changed)
        right_layout.addWidget(avg_col)

        # Wire ROI toggle buttons now that both heatmap and spectrum plot exist
        for _i, _btn in enumerate(self._roi_toggle_btns):

            def _make_toggle(pair_idx, button):
                def handler(checked):
                    button.setText("On" if checked else "Off")
                    self._average_heatmap.set_cursor_pair_active(pair_idx, checked)
                    self._spectrum_plot.set_pair_active(pair_idx, checked)
                    self._sync_cursor_overlay()

                return handler

            _btn.toggled.connect(_make_toggle(_i, _btn))

        self._average_heatmap.cursors_changed.connect(lambda *_: self._sync_cursor_overlay())
        self._sync_cursor_overlay()

        self._spectrum_plot.cursors_changed.connect(self._sync_vcursor_overlay)
        self._sync_vcursor_overlay()

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
            "n_bins": int(self._n_bins_input.value()),
            "duration": self._duration_input.value(),
            "output_dir": self._output_input.text(),
        }

    def _build_preferences(self) -> dict:
        """Snapshot the operator-sidebar widget state for persistence.

        Stores the **displayed** combo text (not the converted forms used
        by ``_get_params``) so restoration via ``QComboBox.setCurrentText``
        round-trips exactly. See ``preferences.py`` for the full schema.
        """
        return {
            "tdc_frequency": float(self._tdc_freq_input.value()),
            "tdc_channel_text": self._tdc_ch_combo.currentText(),
            "tdc_edge_text": self._tdc_edge_combo.currentText(),
            "callback_batch_size": int(self._callback_batch_input.value()),
            "n_bins": int(self._n_bins_input.value()),
            "duration": int(self._duration_input.value()),
            "output_dir": self._output_input.text(),
        }

    def load_operator_preferences(self, path: Optional[Union[Path, str]] = None) -> None:
        """Restore acquisition-sidebar widgets from on-disk preferences.

        Always succeeds: a missing or malformed file falls back to the
        widget defaults (already set by ``_setup_ui``). Out-of-range
        values are clamped before being applied. The output path tooltip
        is refreshed to reflect the loaded path.
        """
        prefs = preferences.load_operator_preferences(path)
        self._tdc_freq_input.setValue(float(prefs["tdc_frequency"]))
        self._tdc_ch_combo.setCurrentText(prefs["tdc_channel_text"])
        self._tdc_edge_combo.setCurrentText(prefs["tdc_edge_text"])
        self._callback_batch_input.setValue(int(prefs["callback_batch_size"]))
        self._n_bins_input.setValue(int(prefs["n_bins"]))
        self._duration_input.setValue(int(prefs["duration"]))
        self._output_input.setText(str(prefs["output_dir"]))
        self._sync_output_path_tooltip()

    def save_operator_preferences(self, path: Optional[Union[Path, str]] = None) -> None:
        """Persist current sidebar widget state to disk (atomic write).

        Idempotent and side-effect-free aside from the JSON write. The
        caller is responsible for swallowing exceptions if the goal is
        to never block quit; this method itself raises on I/O failure
        so test code can assert behavior.
        """
        preferences.save_operator_preferences(self._build_preferences(), path)

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

    @Slot(int, float, float)
    def _on_cursor_changed(self, pair_idx: int, frac_a: float, frac_b: float) -> None:
        """Recompute spectra whenever a heatmap ROI cursor is moved."""
        self._update_spectra()

    @Slot(int, int, int, int)
    def _on_heatmap_view_changed(self, x0: int, x1: int, y0: int, y1: int) -> None:
        """Refresh spectrum + readout when either heatmap zoom/view changes.

        Cursor fractions are remapped through the current view, so the spectrum
        data and the energy readout must be recomputed on every zoom.
        """
        self._update_spectra()
        self._update_cursor_readout()

    @Slot(float, float)
    def _on_spectrum_cursors_changed(self, frac_a: float, frac_b: float) -> None:
        """Update pixel/eV readout whenever a spectrum vertical cursor is moved."""
        self._update_cursor_readout()

    def _on_vcursor_heatmap_toggled(self, checked: bool) -> None:
        self._current_heatmap.set_vcursors_on_heatmap(checked)
        self._average_heatmap.set_vcursors_on_heatmap(checked)

    def _sync_vcursor_overlay(self, frac_a=None, frac_b=None) -> None:
        if frac_a is None:
            frac_a, frac_b = self._spectrum_plot.get_cursor_fracs()
        self._current_heatmap.set_vcursor_overlay(frac_a, frac_b)
        self._average_heatmap.set_vcursor_overlay(frac_a, frac_b)

    def _sync_cursor_overlay(self) -> None:
        """Push the average heatmap cursor positions into the current-flush heatmap as a read-only overlay."""
        fracs = self._average_heatmap.get_cursor_fracs()
        active = [btn.isChecked() for btn in self._roi_toggle_btns]
        self._current_heatmap.update_cursor_overlay(fracs, active)

    def _update_cursor_readout(self) -> None:
        """Refresh the pixel and eV position labels for the two vertical cursors.

        Spectrum cursor fractions span the heatmap's *visible* x-range, so we
        must map them through the current view to get the true pixel index.
        When unzoomed (x0=0, x1=n) this reduces to ``frac * (n - 1)``.
        """
        n = self._n_energy
        if n is None or n < 2:
            for lbl in self._cursor_px_labels + self._cursor_ev_labels:
                lbl.setText("--")
            return

        x0, x1, _, _ = self._average_heatmap.get_view()
        x0 = max(0, min(x0, n - 1))
        x1 = max(x0 + 1, min(x1, n))
        x_span_eff = max(0, x1 - x0 - 1)

        frac_a, frac_b = self._spectrum_plot.get_cursor_fracs()
        x_a = x0 + frac_a * x_span_eff
        x_b = x0 + frac_b * x_span_eff

        pixel_per_ev = self._pixel_per_ev.value()
        ev_at_mid = self._ev_at_mid.value()
        eV_a = ev_at_mid + (x_a - n / 2) / pixel_per_ev
        eV_b = ev_at_mid + (x_b - n / 2) / pixel_per_ev

        dx = abs(x_b - x_a)
        deV = abs(eV_b - eV_a)

        self._cursor_px_labels[0].setText(f"{x_a:.1f}")
        self._cursor_px_labels[1].setText(f"{x_b:.1f}")
        self._cursor_px_labels[2].setText(f"{dx:.1f}")
        self._cursor_ev_labels[0].setText(f"{eV_a:.2f}")
        self._cursor_ev_labels[1].setText(f"{eV_b:.2f}")
        self._cursor_ev_labels[2].setText(f"{deV:.2f}")

    def _update_ruler_energy_scale(self) -> None:
        n = self._n_energy
        if n is None:
            return
        pixel_per_ev = self._pixel_per_ev.value()
        if pixel_per_ev == 0:
            return
        ev_at_mid = self._ev_at_mid.value()
        ev_per_pixel = 1.0 / pixel_per_ev
        ev_at_zero = ev_at_mid - (n / 2) * ev_per_pixel
        for hm in (self._current_heatmap, self._average_heatmap):
            hm.set_x_scale(ev_per_pixel, ev_at_zero)

    def _update_spectra(self) -> None:
        """Bin the average heatmap between each cursor pair and update the spectrum plot.

        Data shape is (n_energy, n_time).  The display shows ``flipud(data.T)``,
        so display-row indices map to time bins as ``time_bin = (n_t - 1) - row``.
        Cursor fractions are widget-relative and span the *visible* portion of
        the heatmap (the current view), not the full data — so we must apply the
        view to map them correctly when zoomed.  We also slice the spectrum to
        the visible x (energy) range so the bottom plot's x-axis matches the
        heatmap's x-axis after a zoom.
        """
        data = self._last_avg_2d
        if data is None or data.ndim != 2 or data.shape[0] == 0 or data.shape[1] == 0:
            return
        n_e, n_t = data.shape

        # Visible region in data-index coords; clamp defensively.
        x0, x1, y0, y1 = self._average_heatmap.get_view()
        x0 = max(0, min(x0, n_e - 1))
        x1 = max(x0 + 1, min(x1, n_e))
        y0 = max(0, min(y0, n_t - 1))
        y1 = max(y0 + 1, min(y1, n_t))

        # Map widget fraction f in [0, 1] → continuous display row → time bin.
        # When unzoomed (y0=0, y1=n_t) this reduces to (1 - f) * (n_t - 1),
        # matching the previous formula.  ``y_span_eff`` uses (y1 - y0 - 1) so
        # f=0 lands on row y0 and f=1 lands on row (y1 - 1), the last visible row.
        y_span_eff = max(0, y1 - y0 - 1)

        def frac_to_time_bin(f: float) -> int:
            row = y0 + f * y_span_eff
            return int(round((n_t - 1) - row))

        for pair_idx, (frac_a, frac_b) in enumerate(self._average_heatmap.get_cursor_fracs()):
            ta = max(0, min(n_t - 1, frac_to_time_bin(frac_a)))
            tb = max(0, min(n_t - 1, frac_to_time_bin(frac_b)))
            t_lo, t_hi = min(ta, tb), max(ta, tb)
            n_bins = t_hi - t_lo + 1
            spectrum = data[x0:x1, t_lo : t_hi + 1].sum(axis=1) / n_bins
            self._spectrum_plot.set_spectrum(pair_idx, spectrum)

    def _on_colormap_changed(self, name: str):
        self._current_heatmap.set_colormap(name)
        self._average_heatmap.set_colormap(name)

    def _reset_average(self):
        self._cumulative_sum = None
        self._total_cycles = 0
        self._flush_count = 0
        self._last_avg_2d = None
        self._n_energy = None
        self._average_heatmap.clear()
        self._spectrum_plot.clear()
        for lbl in self._cursor_px_labels + self._cursor_ev_labels:
            lbl.setText("--")
        self._update_flush_stats()
        logger.info("Running average reset")

    def _set_zoom_mode(self, mode: str) -> None:
        for btn, m in [
            (self._zoom_rect_btn, "rect"),
            (self._zoom_h_btn, "h"),
            (self._zoom_v_btn, "v"),
        ]:
            btn.setChecked(m == mode)
        self._current_heatmap.set_zoom_mode(mode)
        self._average_heatmap.set_zoom_mode(mode)

    def _reset_view(self) -> None:
        self._current_heatmap.reset_view()
        self._average_heatmap.reset_view()

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
        self._n_bins_input.setEnabled(not acquiring)
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
            self._last_avg_2d = avg_2d
            self._n_energy = avg_2d.shape[0]
            self._average_heatmap.set_data(avg_2d, avg_stats)
            self._update_flush_stats()
            self._update_spectra()
            self._update_cursor_readout()
            t_delta_ns = metadata.get("t_delta_ns")
            if t_delta_ns is None:
                tdc_hz = metadata.get("tdc_frequency_hz")
                n_t = avg_2d.shape[1]
                n_bins_meta = metadata.get("n_bins") or n_t
                if tdc_hz and tdc_hz > 0:
                    t_delta_ns = 1e9 / (tdc_hz * n_bins_meta)
            if t_delta_ns:
                n_t = avg_2d.shape[1]
                for hm in (self._current_heatmap, self._average_heatmap):
                    hm.set_axis_info(t_delta_ns, n_t)
            self._update_ruler_energy_scale()

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
    ) -> tuple[Optional[Path], Optional[Path], Optional[Path], Optional[Path], Optional[Path], Optional[Path]]:
        """Save the average heatmap as PNG, CSV, energy-axis CSV, time-axis CSV, UUID, and metadata as JSON."""
        if self._cumulative_sum is None or self._total_cycles == 0:
            logger.warning("No data to save")
            return None, None, None, None, None, None

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        average = self._cumulative_sum / self._total_cycles
        if average.ndim == 2:
            avg_2d = average
        else:
            avg_2d = np.sum(average, axis=1)

        # Save UUID — use the scan_name from ZMQ metadata so it matches what the
        # streaming server already broadcast to downstream services.  Fall back to
        # a fresh UUID4 if metadata isn't available (e.g. simulator without ZMQ).
        meta_for_uuid = self._last_metadata or {}
        scan_uuid = meta_for_uuid.get("scan_name") or str(uuid.uuid4())
        uuid_path = output_path / f"{filename_base}_uuid.txt"
        try:
            uuid_path.write_text(scan_uuid)
            logger.info(f"Saved UUID: {uuid_path} ({scan_uuid})")
        except Exception as e:
            logger.error(f"Failed to save UUID: {e}")
            uuid_path = None

        # Save average CSV
        csv_path = output_path / f"{filename_base}_avg.csv"
        try:
            np.savetxt(csv_path, avg_2d, delimiter=",", fmt="%.6e")
            logger.info(f"Saved CSV: {csv_path}")
        except Exception as e:
            logger.error(f"Failed to save CSV: {e}")
            csv_path = None

        # Save energy-axis CSV (one eV value per x-pixel)
        energy_path = output_path / f"{filename_base}_energy.csv"
        try:
            n_x = avg_2d.shape[0]
            pixel_per_ev = self._pixel_per_ev.value()
            ev_at_mid = self._ev_at_mid.value()
            energy_axis = ev_at_mid + (np.arange(n_x) - n_x / 2) / pixel_per_ev
            np.savetxt(energy_path, energy_axis, delimiter=",", fmt="%.6f")
            logger.info(f"Saved energy axis CSV: {energy_path}")
        except Exception as e:
            logger.error(f"Failed to save energy axis CSV: {e}")
            energy_path = None

        # Save time-axis CSV (one ns value per time bin)
        # t_delta_ns comes from the ZMQ metadata published by app.py; it is
        # 1 / (tdc_frequency_hz * n_bins) * 1e9 and is the width of one time bin.
        time_path = output_path / f"{filename_base}_time_ns.csv"
        try:
            n_t = avg_2d.shape[1]
            meta = self._last_metadata or {}
            t_delta_ns = meta.get("t_delta_ns")
            if t_delta_ns is None:
                # Fallback: reconstruct from tdc_frequency and n_bins if present
                tdc_hz = meta.get("tdc_frequency_hz")
                n_bins = meta.get("n_bins") or n_t
                if tdc_hz and tdc_hz > 0:
                    t_delta_ns = 1e9 / (tdc_hz * n_bins)
                else:
                    t_delta_ns = 1.0  # unknown — bin index only
                    logger.warning("t_delta_ns not in metadata; time axis will be bin-index units (1 ns/bin assumed)")
            time_axis = np.arange(n_t) * t_delta_ns
            np.savetxt(time_path, time_axis, delimiter=",", fmt="%.6f")
            logger.info(f"Saved time axis CSV: {time_path}")
        except Exception as e:
            logger.error(f"Failed to save time axis CSV: {e}")
            time_path = None

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

        return png_path, csv_path, energy_path, time_path, uuid_path, json_path
