"""Alignment tab — live 2D X/Y heatmap with grayscale LUT for beam alignment.

Drives the same pipeline as the Operator preview button (streaming-server +
live-cli + ``acq.py --preview``), but with the streaming server in
``--alignment`` mode: TDCs are ignored and a wall-clock-gated 2D histogram is
emitted at 1–30 Hz. Layout matches the Operator tab: a fixed-width **left
sidebar** holds alignment-only controls and a **Statistics** group; the **main
area** is the square X/Y view with Z histogram to its right (LUT only; **Z
Auto/Manual** lives in the sidebar under frame rate). The top bar has Start, **Simulator**
(local synthetic stream), Stop, a 60 s cps strip chart, and the large cps readout.

Auto-stop semantics: when the user switches to the Operator tab while
alignment is running, ``MainWindow`` calls ``stop_requested`` on this tab so
the two modes never compete for the streaming server. See
``MainWindow._on_tab_changed`` in ``main.py``.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Union

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QFont, QFontMetrics, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import preferences, theme
from .workers import FlushData

logger = logging.getLogger(__name__)


# Apply dark theme defaults to pyqtgraph at import time so the alignment image
# matches the rest of the UI. setConfigOptions is process-global, but operator
# / engineering tabs don't use pyqtgraph today, so this is benign.
pg.setConfigOptions(background=theme.BG_DARK, foreground=theme.TEXT_PRIMARY, antialias=False)


# Detector geometry. Mirrored from SimulatorConfig.detector_size_x/y; quad-chip
# detectors would change these to 512. Keep aligned with app.py.
_DETECTOR_X = 256
_DETECTOR_Y = 256

# Rolling CPS strip chart in the alignment top bar (wall-clock window).
_CPS_HISTORY_WINDOW_S = 60.0


def _grayscale_lut() -> np.ndarray:
    """Return a 256x4 (RGBA) grayscale lookup table for ImageItem.setLookupTable."""
    ramp = np.arange(256, dtype=np.uint8)
    lut = np.zeros((256, 4), dtype=np.uint8)
    lut[:, 0] = ramp
    lut[:, 1] = ramp
    lut[:, 2] = ramp
    lut[:, 3] = 255
    return lut


def _alignment_target_pen() -> QPen:
    """Red dotted pen for the beam-alignment target overlay."""
    return pg.mkPen(color=(255, 72, 72), width=1, style=Qt.PenStyle.DotLine)


# Alignment overlay: outer box in detector pixel coords; inner square shares the
# same center and keeps a fixed side-length ratio vs. the outer (same proportion
# as the original 28 px inner on a 156 px outer).
_ALIGNMENT_TARGET_OUTER = (50.0, 50.0, 206.0, 206.0)  # x0, y0, x1, y1
_ALIGNMENT_INNER_SIDE_FRAC = 28.0 / 156.0  # inner_edge / outer_edge


def _build_alignment_target_overlay(plot: pg.PlotItem) -> list[pg.PlotDataItem]:
    """Two red dotted squares: outer from constants; inner centered with proportional size."""
    pen = _alignment_target_pen()
    items: list[pg.PlotDataItem] = []

    def add_axis_aligned_square(x0: float, y0: float, x1: float, y1: float) -> None:
        # Walk the perimeter once (closed).
        xs = [x0, x1, x1, x0, x0]
        ys = [y0, y0, y1, y1, y0]
        it = pg.PlotDataItem(xs, ys, pen=pen, connect="all")
        it.setZValue(10)
        plot.addItem(it)
        items.append(it)

    ox0, oy0, ox1, oy1 = _ALIGNMENT_TARGET_OUTER
    add_axis_aligned_square(ox0, oy0, ox1, oy1)

    cx = (ox0 + ox1) * 0.5
    cy = (oy0 + oy1) * 0.5
    outer_side = float(max(ox1 - ox0, oy1 - oy0))
    inner_side = outer_side * _ALIGNMENT_INNER_SIDE_FRAC
    half = 0.5 * inner_side
    add_axis_aligned_square(cx - half, cy - half, cx + half, cy + half)

    return items


def _format_cps_body(rate: float) -> str:
    """Scientific notation body only: ``d.ddE±xx`` — always exactly two digits after ``'.'``."""
    if not np.isfinite(rate) or rate <= 0:
        return "0.00E+00"
    t = f"{rate:.2E}"
    if "e" in t and "E" not in t:
        t = t.replace("e", "E")
    mant, e_part = t.split("E", 1)
    exp_i = int(e_part)
    return f"{mant}E{exp_i:+03d}"


def _format_cps(rate: float) -> str:
    """Fixed-width style cps readout: ``d.ddE±xx cps`` (mantissa always two decimals)."""
    return f"{_format_cps_body(rate)} cps"


# Same QSS as OperatorTab ROI On/Off toggles (readout panel right column).
_ALIGNMENT_ONOFF_STYLE = f"""
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
    QPushButton:disabled {{
        background-color: {theme.BG_BUTTON_GROUP};
        color: {theme.TEXT_MUTED};
        border-color: {theme.BORDER_SUBTLE};
    }}
"""


class _AlignmentOnOffToggle(QWidget):
    """Single checkable On/Off control (Operator ROI style); API matches QCheckBox for callers."""

    toggled = Signal(bool)

    def __init__(self, *, initial: bool = False, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._btn = QPushButton("On" if initial else "Off")
        self._btn.setCheckable(True)
        self._btn.setChecked(initial)
        self._btn.setFixedWidth(36)
        self._btn.setStyleSheet(_ALIGNMENT_ONOFF_STYLE)
        self._btn.toggled.connect(self._on_btn_toggled)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._btn)

    def _on_btn_toggled(self, checked: bool) -> None:
        self._btn.setText("On" if checked else "Off")
        self.toggled.emit(checked)

    def isChecked(self) -> bool:
        return self._btn.isChecked()

    def setChecked(self, value: bool) -> None:
        self._btn.blockSignals(True)
        try:
            self._btn.setChecked(value)
            self._btn.setText("On" if value else "Off")
        finally:
            self._btn.blockSignals(False)

    def set_option_tooltip(self, tip: str) -> None:
        QWidget.setToolTip(self, tip)
        self._btn.setToolTip(tip)


class AlignmentTab(QWidget):
    """Live 2D X/Y heatmap for beam alignment."""

    # Same signal shape as OperatorTab so MainWindow's start/stop dispatch is
    # identical: (mode_str, params dict). mode is always "alignment" here.
    start_requested = Signal(str, dict)
    stop_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

        self._acquiring = False
        # Latest 2D (X, Y) array displayed (after squeeze of the (X, Y, 1) flush).
        self._last_2d: Optional[np.ndarray] = None
        # Cumulative sum for "Show integrated" mode. Reset when the toggle goes
        # off→on or on→off, and on Start.
        self._integrated_sum: Optional[np.ndarray] = None
        # Diagnostic counter so we don't spam the system log at 30 Hz; first
        # few flushes are always logged, then periodic.
        self._flush_count = 0
        # Local simulator (◈) timer: synthetic flushes only; bypasses Serval / live-cli /
        # app.py / ZMQ. Feeds ``on_flush_received`` so the render path can be tested
        # without the detector.
        self._fake_timer: Optional[QTimer] = None
        self._fake_rng = np.random.default_rng()
        # Mirrored from OperatorTab / MainWindow process signals so Start stays
        # disabled until Serval prints the chip-temperature line (HW ready).
        self._serval_process_running = False
        self._serval_hw_ready = False
        # (time.monotonic(), cps) for the top-bar rolling chart.
        self._cps_history: deque[tuple[float, float]] = deque()
        # HistogramLUTItem can emit levels changes during wiring; ignore those
        # until prefs are loaded so Z range stays default Auto.
        self._hist_lut_user_callbacks_enabled = False

        self._setup_ui()
        try:
            self.load_alignment_preferences()
        except Exception:
            logger.exception("Failed to load alignment preferences; using widget defaults")
        finally:
            self._hist_lut_user_callbacks_enabled = True
        self._refresh_alignment_start_state()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        layout.addWidget(self._build_top_bar())

        content = QHBoxLayout()
        content.setSpacing(10)
        left_panel = self._build_left_sidebar()
        content.addWidget(left_panel)

        right_col = QVBoxLayout()
        right_col.setSpacing(8)
        right_col.setContentsMargins(0, 0, 0, 0)
        right_col.addWidget(self._build_image_area(), stretch=1)

        right_wrap = QWidget()
        right_wrap.setLayout(right_col)
        content.addWidget(right_wrap, stretch=1)
        layout.addLayout(content, stretch=1)

    def _build_top_bar(self) -> QWidget:
        """Row 1: Start/Stop (left), rolling 60 s cps chart (center), giant cps readout (right)."""
        top_bar = QFrame()
        top_bar.setStyleSheet(f"QFrame {{ background-color: {theme.BG_WIDGET}; border-radius: 6px; }}")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(8, 8, 8, 8)
        top_layout.setSpacing(8)

        # Start/Stop group (matches OperatorTab style)
        mode_group = QFrame()
        mode_group.setStyleSheet(
            f"QFrame {{ background-color: {theme.BG_BUTTON_GROUP}; "
            f"border-radius: 6px; border: 1px solid {theme.BLUE_LIGHT_2}; }}"
        )
        mode_layout = QHBoxLayout(mode_group)
        mode_layout.setContentsMargins(6, 6, 6, 6)
        mode_layout.setSpacing(6)

        BUTTON_WIDTH = 125
        self._start_btn = QPushButton("▶ Start")
        self._start_btn.setFixedWidth(BUTTON_WIDTH)
        self._start_btn.setStyleSheet(theme.button_style(theme.BUTTON_PREVIEW))
        self._start_btn.setToolTip(
            "Start the alignment pipeline (no file saving). "
            "Stops any running operator session is not allowed; stop that first."
        )
        self._start_btn.clicked.connect(self._on_start_clicked)
        mode_layout.addWidget(self._start_btn)

        self._simulator_btn = QPushButton("◈ Simulator")
        self._simulator_btn.setFixedWidth(BUTTON_WIDTH)
        self._simulator_btn.setCheckable(True)
        self._simulator_btn.setStyleSheet(theme.button_style(theme.BUTTON_SIMULATOR))
        self._simulator_btn.setToolTip(
            "Local synthetic alignment stream (random background + drifting beam spot).\n"
            "No Serval, live-cli, or ZMQ — use to verify the UI. When active, Start does not\n"
            "require Serval to be ready."
        )
        self._simulator_btn.toggled.connect(self._on_simulator_toggled)
        mode_layout.addWidget(self._simulator_btn)

        self._stop_btn = QPushButton("⏹ Stop")
        self._stop_btn.setFixedWidth(BUTTON_WIDTH)
        self._stop_btn.setStyleSheet(theme.button_style(theme.BUTTON_STOP))
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop_clicked)
        mode_layout.addWidget(self._stop_btn)

        top_layout.addWidget(mode_group)

        # Middle: rolling 60 s cps vs time (same metric as the giant readout).
        self._cps_history_plot = pg.PlotWidget()
        self._cps_history_plot.setBackground(theme.BG_DARK)
        self._cps_history_plot.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self._cps_history_plot.setMinimumWidth(180)
        # Height chosen to match Operator top-bar button row (see top_bar min height below).
        self._cps_history_plot.setFixedHeight(42)
        self._cps_history_plot.setToolTip(
            "Rolling last 60 s of live cps (sum of counts in each frame / flush interval).\n"
            "Horizontal axis: seconds within the window (0 = oldest, 60 = now)."
        )
        cps_pi = self._cps_history_plot.getPlotItem()
        cps_pi.setMenuEnabled(False)
        cps_pi.hideButtons()
        cps_pi.showGrid(x=False, y=False)
        cps_pi.setLabel("left", "cps")
        # No bottom-axis text label — saves vertical space in the top bar strip chart.
        # Qualitative strip chart only: no tick marks or numeric ticks.
        for ax_name in ("left", "bottom"):
            ax = cps_pi.getAxis(ax_name)
            ax.setStyle(showValues=False, tickLength=0)
            ax.setTicks([[]])
        self._cps_history_plot.getViewBox().setMouseEnabled(x=False, y=False)
        self._cps_history_curve = self._cps_history_plot.plot(
            [],
            [],
            pen=pg.mkPen(color=theme.BLUE_LIGHT_1, width=2),
        )
        top_layout.addWidget(
            self._cps_history_plot,
            stretch=1,
            alignment=Qt.AlignmentFlag.AlignVCenter,
        )

        # Giant cps readout — fixed-width scientific so the bar does not jump.
        self._cps_label = QLabel(_format_cps(0.0))
        _cps_font = QFont()
        _cps_font.setFamilies(["Consolas", "Monaco", "Courier New"])
        _cps_font.setPixelSize(32)
        _cps_font.setBold(True)
        self._cps_label.setFont(_cps_font)
        self._cps_label.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; padding: 0 8px;")
        self._cps_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        _fm = QFontMetrics(_cps_font)
        self._cps_label.setMinimumWidth(_fm.horizontalAdvance("9.99E+999 cps"))
        self._cps_label.setToolTip(
            "Live count rate: sum of pixels in the latest displayed frame divided by\n"
            "the alignment flush interval (1 / Rate Hz). Updates every flush."
        )
        top_layout.addWidget(self._cps_label)

        return top_bar

    def _build_left_sidebar(self) -> QWidget:
        """Left column: alignment parameters (mirrors Operator tab fixed-width sidebar)."""
        left_panel = QWidget()
        left_panel.setFixedWidth(280)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        group = QGroupBox("Alignment")
        group.setStyleSheet(theme.group_box_style())
        group.setToolTip(
            "Alignment-only controls. Update rate applies on the next Start.\n"
            "Display options apply immediately to the live view."
        )
        g = QVBoxLayout(group)
        g.setSpacing(8)

        rate_row = QHBoxLayout()
        self._rate_label = QLabel("Rate (Hz)")
        self._rate_label.setStyleSheet(
            f"QLabel {{ color: {theme.TEXT_SECONDARY}; }} " f"QLabel:disabled {{ color: {theme.TEXT_MUTED}; }}"
        )
        self._rate_label.setToolTip(
            "Wall-clock flush rate driving the alignment image (1–30 Hz). Applied on next Start."
        )
        rate_row.addWidget(self._rate_label)
        self._rate_input = QSpinBox()
        self._rate_input.setRange(*preferences.ALIGNMENT_RATE_HZ_RANGE)
        self._rate_input.setValue(30)
        self._rate_input.setStyleSheet(theme.input_style())
        self._rate_input.setToolTip(self._rate_label.toolTip())
        rate_row.addWidget(self._rate_input)
        g.addLayout(rate_row)

        z_range_row = QHBoxLayout()
        z_range_row.setSpacing(4)
        self._z_range_label = QLabel("Z range")
        self._z_range_label.setStyleSheet(
            f"QLabel {{ color: {theme.TEXT_SECONDARY}; }} " f"QLabel:disabled {{ color: {theme.TEXT_MUTED}; }}"
        )
        self._range_combo = QComboBox()
        self._range_combo.addItems(["Auto", "Manual"])
        self._range_combo.setCurrentText("Auto")
        self._range_combo.setStyleSheet(theme.input_style())
        self._range_combo.setToolTip(
            "Auto: Z levels follow each frame's min/max.\n"
            "Manual: drag the triangular handles on the colorbar to set min/max."
        )
        self._z_range_label.setToolTip(self._range_combo.toolTip())
        self._range_combo.currentTextChanged.connect(self._on_range_mode_changed)
        z_range_row.addWidget(self._z_range_label)
        z_range_row.addStretch(1)
        z_range_row.addWidget(self._range_combo)
        _rate_w = self._rate_input.sizeHint().width()
        self._range_combo.setCurrentText("Manual")
        _manual_w = self._range_combo.sizeHint().width()
        self._range_combo.setCurrentText("Auto")
        _lo = max(_rate_w + 12, _manual_w + 6)
        _hi = max(_lo + 4, _manual_w + 36)
        self._range_combo.setFixedWidth((_lo + _hi) // 2)
        g.addLayout(z_range_row)

        int_row = QHBoxLayout()
        int_lbl = QLabel("Show integrated")
        int_lbl.setStyleSheet(
            f"QLabel {{ color: {theme.TEXT_SECONDARY}; }} " f"QLabel:disabled {{ color: {theme.TEXT_MUTED}; }}"
        )
        int_tip = (
            "Off (default): display the latest flush only.\n" "On: cumulative sum since switched on. Reset on Start."
        )
        int_lbl.setToolTip(int_tip)
        int_row.addWidget(int_lbl)
        int_row.addStretch()
        self._integrated_chk = _AlignmentOnOffToggle(initial=False)
        self._integrated_chk.set_option_tooltip(int_tip)
        self._integrated_chk.toggled.connect(self._on_integrated_toggled)
        int_row.addWidget(self._integrated_chk)
        g.addLayout(int_row)

        bin_row = QHBoxLayout()
        bin_lbl = QLabel("Binarize")
        bin_lbl.setStyleSheet(
            f"QLabel {{ color: {theme.TEXT_SECONDARY}; }} " f"QLabel:disabled {{ color: {theme.TEXT_MUTED}; }}"
        )
        bin_tip = (
            "On: any pixel with count > 0 maps to brightest LUT color; zeros to darkest.\n"
            "Makes single-hit visibility easy during alignment. Overrides Log and manual Z range."
        )
        bin_lbl.setToolTip(bin_tip)
        bin_row.addWidget(bin_lbl)
        bin_row.addStretch()
        self._binarize_chk = _AlignmentOnOffToggle(initial=True)
        self._binarize_chk.set_option_tooltip(bin_tip)
        self._binarize_chk.toggled.connect(self._on_binarize_toggled)
        bin_row.addWidget(self._binarize_chk)
        g.addLayout(bin_row)

        log_row = QHBoxLayout()
        log_lbl = QLabel("Log")
        log_lbl.setStyleSheet(
            f"QLabel {{ color: {theme.TEXT_SECONDARY}; }} " f"QLabel:disabled {{ color: {theme.TEXT_MUTED}; }}"
        )
        log_tip = (
            "On: apply log10(data + 1) before display so faint features pop.\n"
            "Turning On resets Z range to Auto. No effect while Binarize is On."
        )
        log_lbl.setToolTip(log_tip)
        log_row.addWidget(log_lbl)
        log_row.addStretch()
        self._log_chk = _AlignmentOnOffToggle(initial=False)
        self._log_chk.set_option_tooltip(log_tip)
        self._log_chk.toggled.connect(self._on_log_toggled)
        log_row.addWidget(self._log_chk)
        g.addLayout(log_row)

        xh_row = QHBoxLayout()
        xh_lbl = QLabel("Crosshair")
        xh_lbl.setStyleSheet(
            f"QLabel {{ color: {theme.TEXT_SECONDARY}; }} " f"QLabel:disabled {{ color: {theme.TEXT_MUTED}; }}"
        )
        xh_tip = (
            "On: two red dotted squares — outer (50,50)–(206,206); inner centered with "
            "side length 28/156 of the outer edge."
        )
        xh_lbl.setToolTip(xh_tip)
        xh_row.addWidget(xh_lbl)
        xh_row.addStretch()
        self._crosshair_chk = _AlignmentOnOffToggle(initial=True)
        self._crosshair_chk.set_option_tooltip(xh_tip)
        self._crosshair_chk.toggled.connect(self._on_crosshair_toggled)
        xh_row.addWidget(self._crosshair_chk)
        g.addLayout(xh_row)

        stats_group = QGroupBox("Statistics")
        stats_group.setStyleSheet(theme.group_box_style())
        stats_group.setToolTip(
            "Statistics of the most recent flush. Sum is total counts in the\n"
            "displayed frame; Max is the brightest pixel. No signal means the\n"
            "flush arrived but contained zero pixels."
        )
        stats_layout = QVBoxLayout(stats_group)
        stats_layout.setSpacing(4)

        self._stats_labels: dict[str, QLabel] = {}
        _stat_val_style = f"font-family: monospace; color: {theme.TEXT_PRIMARY};"
        for key, title in (
            ("frame", "Frame"),
            ("sum", "Sum"),
            ("max", "Max"),
            ("status", "Status"),
        ):
            row = QHBoxLayout()
            name_label = QLabel(title)
            name_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
            value_label = QLabel("—" if key != "status" else "idle")
            value_label.setStyleSheet(_stat_val_style)
            value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if key == "status":
                value_label.setWordWrap(True)
                value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            row.addWidget(name_label)
            row.addStretch()
            row.addWidget(value_label)
            stats_layout.addLayout(row)
            self._stats_labels[key] = value_label

        left_layout.addWidget(group)
        left_layout.addWidget(stats_group)
        left_layout.addStretch()
        return left_panel

    def _build_image_area(self) -> QWidget:
        """Main XY plot (square) plus Z histogram column (colorbar / LUT only)."""
        outer = QWidget()
        outer_h = QHBoxLayout(outer)
        outer_h.setContentsMargins(0, 0, 0, 0)
        outer_h.setSpacing(8)

        self._gw = pg.GraphicsLayoutWidget()
        self._gw.setBackground(theme.BG_DARK)
        self._gw.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._gw.setMinimumHeight(480)

        self._plot = self._gw.addPlot(row=0, col=0)
        self._plot.showAxis("right")
        self._plot.showAxis("top")
        for ax_name in ("right", "top"):
            ax = self._plot.getAxis(ax_name)
            ax.setStyle(showValues=False)
        self._plot.setLabel("left", "Y (pixel)")
        self._plot.setLabel("bottom", "X (pixel)")
        self._plot.hideButtons()
        self._plot.setMenuEnabled(False)

        vb = self._plot.getViewBox()
        vb.setMouseEnabled(x=False, y=False)
        vb.setDefaultPadding(0)
        vb.disableAutoRange()
        vb.setRange(xRange=(0, _DETECTOR_X), yRange=(0, _DETECTOR_Y), padding=0)
        vb.setBackgroundColor(theme.BG_DARK)
        self._viewbox = vb

        self._image_item = pg.ImageItem(
            np.zeros((_DETECTOR_Y, _DETECTOR_X), dtype=np.float32),
            axisOrder="row-major",
        )
        self._image_item.setLookupTable(_grayscale_lut())
        self._plot.addItem(self._image_item)

        self._target_overlay_items = _build_alignment_target_overlay(self._plot)

        self._waiting_text = pg.TextItem(
            "Waiting for first flush…",
            color=(255, 255, 255, 180),
            anchor=(0.5, 0.5),
        )
        self._waiting_text.setPos(_DETECTOR_X / 2, _DETECTOR_Y / 2)
        self._plot.addItem(self._waiting_text, ignoreBounds=True)

        gl_layout = self._gw.ci.layout
        gl_layout.setColumnStretchFactor(0, 1)

        right = QWidget()
        right.setFixedWidth(100)
        right_v = QVBoxLayout(right)
        right_v.setContentsMargins(0, 0, 0, 0)
        right_v.setSpacing(4)

        self._hist_gw = pg.GraphicsLayoutWidget()
        self._hist_gw.setBackground(theme.BG_DARK)
        self._hist_gw.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self._hist_gw.setMinimumWidth(88)

        self._hist = pg.HistogramLUTItem()
        self._hist.gradient.setColorMap(pg.ColorMap([0.0, 1.0], [(0, 0, 0), (255, 255, 255)]))
        self._hist.setImageItem(self._image_item)
        self._image_item.setLookupTable(_grayscale_lut())
        self._hist.sigLevelsChanged.connect(self._on_hist_levels_changed)
        self._hist_gw.addItem(self._hist, row=0, col=0)
        right_v.addWidget(self._hist_gw, stretch=1)

        outer_h.addWidget(self._gw, stretch=1)
        outer_h.addWidget(right)

        return outer

    # ------------------------------------------------------------------
    # Public slots / API used by MainWindow
    # ------------------------------------------------------------------

    @Slot(bool)
    def set_acquiring(self, acquiring: bool) -> None:
        """Toggle Start/Stop, rate controls, and integrated-buffer state on acquisition."""
        self._acquiring = acquiring
        self._stop_btn.setEnabled(acquiring)
        self._simulator_btn.setEnabled(not acquiring)
        self._rate_label.setEnabled(not acquiring)
        self._rate_input.setEnabled(not acquiring)
        self._update_z_range_controls_enabled()
        self._refresh_alignment_start_state()
        if acquiring:
            # Reset the integrated buffer on each Start so a fresh accumulation
            # begins; if integrated was off, this is a no-op.
            self._integrated_sum = None
            self._clear_cps_history_plot()
            # Re-arm the per-run diagnostics: log first few flushes again, and
            # re-show the "Waiting for first flush…" overlay so the user gets
            # immediate feedback on whether ZMQ is delivering.
            self._flush_count = 0
            self._set_alignment_stats_row(
                frame="—",
                sum_s="—",
                max_s="—",
                status="waiting for first flush…",
            )
            if self._waiting_text is None:
                self._waiting_text = pg.TextItem(
                    "Waiting for first flush…",
                    color=(255, 255, 255, 180),
                    anchor=(0.5, 0.5),
                )
                self._waiting_text.setPos(_DETECTOR_X / 2, _DETECTOR_Y / 2)
                self._plot.addItem(self._waiting_text, ignoreBounds=True)
        else:
            self._clear_cps_history_plot()
            self._set_alignment_stats_row(
                frame="—",
                sum_s="—",
                max_s="—",
                status="idle (stopped)",
            )

    def on_serval_process_running(self, running: bool) -> None:
        """Forwarded from MainWindow when the Serval subprocess starts or stops."""
        self._serval_process_running = running
        self._serval_hw_ready = False
        self._refresh_alignment_start_state()

    def on_serval_chip_temps_line_seen(self) -> None:
        """Forwarded from MainWindow when Serval logs chip temperatures (HW ready)."""
        if not self._serval_process_running or self._serval_hw_ready:
            return
        self._serval_hw_ready = True
        self._refresh_alignment_start_state()

    def _refresh_alignment_start_state(self) -> None:
        """Start enabled when idle and (local simulator or Serval HW-ready)."""
        if self._acquiring:
            self._start_btn.setEnabled(False)
            return
        allow = self._simulator_btn.isChecked() or (self._serval_process_running and self._serval_hw_ready)
        self._start_btn.setEnabled(allow)

    @Slot(bool)
    def _on_simulator_toggled(self, _checked: bool) -> None:
        self._refresh_alignment_start_state()

    @Slot(object)
    def on_flush_received(self, flush_data: FlushData) -> None:
        """Render a flush from the streaming server (alignment-mode shape (X, Y, 1))."""
        array = flush_data.array
        metadata = flush_data.metadata
        mode = metadata.get("mode")

        # Defensive: alignment flushes are always (X, Y, 1) but route by mode.
        if mode != "alignment":
            # If anything other than alignment mode arrives here, the routing
            # in MainWindow._on_flush_received broke — log loudly so this
            # surfaces in the engineering tab.
            logger.warning(
                "AlignmentTab.on_flush_received received non-alignment flush (mode=%r); ignoring",
                mode,
            )
            return
        if array.ndim == 3 and array.shape[-1] == 1:
            latest_2d = array[..., 0]
        elif array.ndim == 2:
            latest_2d = array
        else:
            logger.warning("Alignment tab got unexpected array shape %s; ignoring", array.shape)
            return

        self._last_2d = latest_2d

        if self._integrated_chk.isChecked():
            if self._integrated_sum is None:
                self._integrated_sum = latest_2d.astype(np.float64, copy=True)
            else:
                self._integrated_sum += latest_2d

        # Per-frame diagnostics. Computed once here, reused for the cps
        # label, the sidebar Statistics group, and the system log.
        frame_sum = float(latest_2d.sum())
        frame_max = float(latest_2d.max()) if latest_2d.size else 0.0
        flush_interval_s = float(metadata.get("flush_interval_s") or 0.0)
        cps = frame_sum / flush_interval_s if flush_interval_s > 0 else 0.0
        self._cps_label.setText(_format_cps(cps))
        self._append_cps_sample(cps)

        # Sidebar stats — status answers "why do I see nothing?" at a glance.
        if frame_sum == 0:
            status = "No signal (flush arrived empty)"
        else:
            status = "OK"
        self._set_alignment_stats_row(
            frame=f"{latest_2d.shape[0]}×{latest_2d.shape[1]}",
            sum_s=f"{frame_sum:.3e}",
            max_s=f"{frame_max:.3e}",
            status=status,
        )

        # Hide the "Waiting for first flush…" overlay on first flush.
        if self._waiting_text is not None:
            self._plot.removeItem(self._waiting_text)
            self._waiting_text = None

        # Log the first 5 flushes always, then every 30th (≈ once a second
        # at 30 Hz). Goes to the engineering tab's system log via the root
        # logger; the per-flush ZMQ log message in MainWindow stays unchanged.
        self._flush_count += 1
        if self._flush_count <= 5 or self._flush_count % 30 == 0:
            logger.info(
                "AlignmentTab flush #%d: shape=%s, sum=%g, max=%g, mode=%s, flush_interval=%s",
                self._flush_count,
                latest_2d.shape,
                frame_sum,
                frame_max,
                mode,
                flush_interval_s,
            )

        self._refresh_image()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _set_alignment_stats_row(self, *, frame: str, sum_s: str, max_s: str, status: str) -> None:
        self._stats_labels["frame"].setText(frame)
        self._stats_labels["sum"].setText(sum_s)
        self._stats_labels["max"].setText(max_s)
        self._stats_labels["status"].setText(status)

    def _clear_cps_history_plot(self) -> None:
        """Drop all CPS timeline samples (e.g. on Stop / fresh Start)."""
        self._cps_history.clear()
        self._cps_history_curve.setData([], [])
        self._cps_history_plot.setXRange(0, _CPS_HISTORY_WINDOW_S, padding=0)
        self._cps_history_plot.setYRange(0, 1, padding=0)

    def _append_cps_sample(self, cps: float) -> None:
        """Record one cps sample and refresh the rolling 60 s strip chart."""
        now = time.monotonic()
        self._cps_history.append((now, float(cps)))
        cutoff = now - _CPS_HISTORY_WINDOW_S
        while self._cps_history and self._cps_history[0][0] < cutoff:
            self._cps_history.popleft()
        if not self._cps_history:
            self._cps_history_curve.setData([], [])
            self._cps_history_plot.setXRange(0, _CPS_HISTORY_WINDOW_S, padding=0)
            self._cps_history_plot.setYRange(0, 1, padding=0)
            return
        t0 = now - _CPS_HISTORY_WINDOW_S
        xs = np.fromiter((t - t0 for t, _ in self._cps_history), dtype=np.float64, count=len(self._cps_history))
        ys = np.fromiter((v for _, v in self._cps_history), dtype=np.float64, count=len(self._cps_history))
        self._cps_history_curve.setData(xs, ys)
        self._cps_history_plot.setXRange(0, _CPS_HISTORY_WINDOW_S, padding=0)
        vmin = float(np.nanmin(ys))
        vmax = float(np.nanmax(ys))
        if not (np.isfinite(vmin) and np.isfinite(vmax)):
            return
        if vmax <= vmin:
            span = max(1.0, abs(vmin) * 0.05 + 1.0)
            self._cps_history_plot.setYRange(vmin - 0.05 * span, vmax + span, padding=0)
        else:
            pad = (vmax - vmin) * 0.08
            lo = max(0.0, vmin - pad)
            self._cps_history_plot.setYRange(lo, vmax + pad, padding=0)

    def _update_z_range_controls_enabled(self) -> None:
        """Z range UI is inactive while Binarize overrides levels (pinned 0–1)."""
        ok = not self._binarize_chk.isChecked()
        self._z_range_label.setEnabled(ok)
        self._range_combo.setEnabled(ok)

    @contextmanager
    def _hist_levels_change_guard(self):
        """Temporarily disconnect ``sigLevelsChanged`` so programmatic LUT updates do not flip Auto→Manual."""
        try:
            self._hist.sigLevelsChanged.disconnect(self._on_hist_levels_changed)
        except (TypeError, RuntimeError):
            pass
        try:
            yield
        finally:
            self._hist.sigLevelsChanged.connect(self._on_hist_levels_changed)

    def _refresh_image(self) -> None:
        """Push the current (latest or integrated, log or linear) frame to ImageItem.

        The entire method runs inside ``_hist_levels_change_guard`` so that the
        ``setImage`` call (which triggers HistogramLUTItem.imageChanged →
        region.setRegion → sigLevelsChanged) never reaches ``_on_hist_levels_changed``
        and cannot flip the Z range combo from Auto back to Manual.
        """
        source = self._integrated_sum if self._integrated_chk.isChecked() else self._last_2d
        if source is None:
            return

        with self._hist_levels_change_guard():
            # Binarize takes precedence over Log/Manual: any pixel > 0 → 1.0,
            # zeros stay 0.0, levels pinned to (0, 1) so the only colors shown
            # are LUT-min (background) and LUT-max (any hit).
            if self._binarize_chk.isChecked():
                disp = (source > 0).astype(np.float32)
                self._image_item.setImage(disp.T, autoLevels=False, levels=(0.0, 1.0))
                self._hist.setLevels(0.0, 1.0)
                self._hist.setHistogramRange(0.0, 1.0)
                self._image_item.update()
                return

            if self._log_chk.isChecked():
                disp = np.log10(source.astype(np.float32) + 1.0)
            else:
                disp = source.astype(np.float32, copy=False)

            # ImageItem with axisOrder='row-major' wants (rows=Y, cols=X). Our data
            # is (X, Y), so transpose. We compute levels deterministically with
            # numpy min/max (rather than passing autoLevels=True, which uses
            # pyqtgraph's quickMinMax — subsampled and can miss sparse hot pixels
            # on a 256x256 detector).
            auto = self._range_combo.currentText() == "Auto"
            if auto:
                data_lo = float(disp.min())
                data_hi = float(disp.max())
                if data_hi <= data_lo:
                    # Fully-flat frame (e.g. all zeros). Pick a (0, 1) span so the
                    # LUT renders sensibly instead of mapping everything to a
                    # single end of the colormap.
                    data_lo = 0.0
                    data_hi = 1.0
                self._image_item.setImage(disp.T, autoLevels=False, levels=(data_lo, data_hi))
                # Sync the LUT widget's histogram range to the new image so the
                # handles sit at the data extents.
                self._hist.setHistogramRange(data_lo, data_hi)
                self._hist.setLevels(data_lo, data_hi)
            else:
                try:
                    lo, hi = self._hist.getLevels()
                except Exception:
                    lo, hi = 0.0, 1.0
                lo, hi = float(lo), float(hi)
                if hi <= lo:
                    hi = lo + 1.0
                self._image_item.setImage(disp.T, autoLevels=False, levels=(lo, hi))
                self._hist.setLevels(lo, hi)

            # Force an immediate repaint of the ImageItem.
            self._image_item.update()

    @Slot(str)
    def _on_range_mode_changed(self, text: str) -> None:
        _ = text
        self._refresh_image()

    @Slot()
    def _on_hist_levels_changed(self) -> None:
        """Dragging LUT handles switches to Manual and reapplies levels."""
        if not self._hist_lut_user_callbacks_enabled:
            return
        if self._binarize_chk.isChecked():
            return
        if self._range_combo.currentText() != "Manual":
            self._range_combo.setCurrentText("Manual")
        self._refresh_image()

    @Slot(bool)
    def _on_integrated_toggled(self, checked: bool) -> None:
        # Reset the buffer whenever the toggle changes state, so cumulative
        # always starts at zero from the moment the box is checked.
        self._integrated_sum = None
        self._refresh_image()

    @Slot(bool)
    def _on_binarize_toggled(self, checked: bool) -> None:
        """Disable Log + manual-range controls while binarize is active.

        They have no effect on the rendered image in binarize mode, so
        greying them out makes the override semantics obvious. Their saved
        states are preserved (we don't uncheck Log here), so the user gets
        their previous settings back when they uncheck Binarize.
        """
        # Log only matters in non-binarize mode; grey it out for clarity.
        self._log_chk.setEnabled(not checked)
        self._update_z_range_controls_enabled()
        self._refresh_image()

    @Slot(bool)
    def _on_log_toggled(self, checked: bool) -> None:
        """Force range mode back to Auto when log is toggled.

        Linear-space manual levels (e.g. 0–100 counts) become nonsensical
        after a log10 transform (data range 0–~5), and vice versa. Rather
        than try to be clever about converting between the two, we snap
        to Auto so the image is always visible after the toggle. The user
        can then re-enter Manual once they see the actual data range.
        """
        if self._range_combo.currentText() != "Auto":
            self._range_combo.setCurrentText("Auto")
        else:
            self._refresh_image()

    @Slot(bool)
    def _on_crosshair_toggled(self, checked: bool) -> None:
        for it in getattr(self, "_target_overlay_items", ()):
            it.setVisible(checked)

    @Slot()
    def _on_start_clicked(self) -> None:
        # Fresh accumulation buffer for "Show integrated" if it's already on.
        self._integrated_sum = None

        if self._simulator_btn.isChecked():
            # Local simulator: synthetic flushes via QTimer; no start_requested.
            logger.info("AlignmentTab: starting local simulator")
            self.set_acquiring(True)
            rate_hz = max(1.0, float(self._rate_input.value()))
            self._fake_timer = QTimer(self)
            self._fake_timer.setInterval(int(round(1000.0 / rate_hz)))
            self._fake_timer.timeout.connect(self._emit_fake_flush)
            self._fake_timer.start()
            return

        params = {
            "alignment_rate_hz": int(self._rate_input.value()),
            # ``acq.py``'s own "longest possible" default (~220 days). The user
            # stops via the Stop button; this just removes any acquisition-side
            # auto-stop surprise.
            "duration": 19_008_000,
        }
        self.start_requested.emit("alignment", params)

    @Slot()
    def _on_stop_clicked(self) -> None:
        if self._fake_timer is not None and self._fake_timer.isActive():
            logger.info("AlignmentTab: stopping local simulator")
            self._fake_timer.stop()
            self._fake_timer = None
            self.set_acquiring(False)
            return
        self.stop_requested.emit()

    @Slot()
    def _emit_fake_flush(self) -> None:
        """Synthesize one alignment flush and feed it into ``on_flush_received``.

        Pattern: ~200–800 uniform-random hits per frame (1–3 counts each) plus
        a Gaussian "beam spot" of ~2000 hits at a slowly-drifting center, so
        the user sees both background noise and a clearly-recognizable feature.
        """
        rng = self._fake_rng
        arr = np.zeros((_DETECTOR_X, _DETECTOR_Y, 1), dtype=np.uint32)

        # Uniform background
        n_bg = int(rng.integers(200, 800))
        bx = rng.integers(0, _DETECTOR_X, n_bg)
        by = rng.integers(0, _DETECTOR_Y, n_bg)
        np.add.at(arr, (bx, by, 0), 1)

        # Gaussian beam spot, slow drift via flush counter so it visibly moves.
        n_spot = int(rng.integers(1500, 2500))
        drift_x = 128 + 30 * np.sin(self._flush_count / 60.0)
        drift_y = 128 + 30 * np.cos(self._flush_count / 60.0)
        sx = rng.normal(drift_x, 12.0, n_spot).clip(0, _DETECTOR_X - 1).astype(np.int32)
        sy = rng.normal(drift_y, 12.0, n_spot).clip(0, _DETECTOR_Y - 1).astype(np.int32)
        np.add.at(arr, (sx, sy, 0), 1)

        rate_hz = max(1.0, float(self._rate_input.value()))
        flush_interval_s = 1.0 / rate_hz
        meta = {
            "mode": "alignment",
            "flush_interval_s": flush_interval_s,
            "flush_number": self._flush_count + 1,
            "shape": (_DETECTOR_X, _DETECTOR_Y, 1),
            "cycles_in_flush": 1,
        }
        self.on_flush_received(FlushData(array=arr, metadata=meta))

    # ------------------------------------------------------------------
    # Preferences
    # ------------------------------------------------------------------

    def _build_preferences(self) -> dict:
        """Snapshot of the alignment-tab widget state for the JSON file."""
        lo, hi = 0.0, 100.0
        try:
            lo, hi = self._hist.getLevels()
        except Exception:
            pass
        return {
            "alignment_rate_hz": int(self._rate_input.value()),
            "alignment_auto_range": self._range_combo.currentText() == "Auto",
            "alignment_manual_min": float(lo),
            "alignment_manual_max": float(hi),
            "alignment_log": bool(self._log_chk.isChecked()),
            "alignment_binarize": bool(self._binarize_chk.isChecked()),
            "alignment_show_integrated": bool(self._integrated_chk.isChecked()),
            "alignment_show_crosshair": bool(self._crosshair_chk.isChecked()),
            "alignment_simulator": bool(self._simulator_btn.isChecked()),
        }

    def load_alignment_preferences(self, path: Optional[Union[Path, str]] = None) -> None:
        """Restore alignment widgets from the shared on-disk preferences."""
        prefs = preferences.load_operator_preferences(path)
        self._rate_input.setValue(int(prefs["alignment_rate_hz"]))
        self._range_combo.setCurrentText("Auto" if prefs["alignment_auto_range"] else "Manual")
        self._log_chk.setChecked(bool(prefs["alignment_log"]))
        self._binarize_chk.setChecked(bool(prefs["alignment_binarize"]))
        self._integrated_chk.setChecked(bool(prefs["alignment_show_integrated"]))
        self._crosshair_chk.setChecked(bool(prefs["alignment_show_crosshair"]))
        self._simulator_btn.setChecked(bool(prefs["alignment_simulator"]))

        if not prefs["alignment_auto_range"]:
            lo = float(prefs["alignment_manual_min"])
            hi = float(prefs["alignment_manual_max"])
            if hi <= lo:
                hi = lo + 1.0
            with self._hist_levels_change_guard():
                self._hist.setLevels(lo, hi)

        self._on_crosshair_toggled(self._crosshair_chk.isChecked())
        self._on_binarize_toggled(self._binarize_chk.isChecked())
        self._update_z_range_controls_enabled()
        self._refresh_image()

    def save_alignment_preferences(self, path: Optional[Union[Path, str]] = None) -> None:
        """Persist alignment-tab widgets to the shared preferences file.

        The save merges with on-disk state (see ``preferences.save_operator_preferences``)
        so this can run independently of ``OperatorTab.save_operator_preferences``
        without clobbering operator keys.
        """
        preferences.save_operator_preferences(self._build_preferences(), path)
