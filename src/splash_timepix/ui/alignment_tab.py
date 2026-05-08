"""Alignment tab — live 2D X/Y heatmap with grayscale LUT for beam alignment.

Drives the same pipeline as the Operator preview button (streaming-server +
live-cli + ``acq.py --preview``), but with the streaming server in
``--alignment`` mode: TDCs are ignored and a wall-clock-gated 2D histogram is
emitted at 1–30 Hz. The tab renders the latest flush as a square image with a
pyqtgraph histogram/LUT colorbar; "Show integrated" optionally accumulates
since the toggle was checked.

Each flush also drives the giant pixel-rate (cps) readout shown in the top
bar — computed locally as ``sum(array) / flush_interval_s`` so it tracks the
displayed frame exactly.

Auto-stop semantics: when the user switches to the Operator tab while
alignment is running, ``MainWindow`` calls ``stop_requested`` on this tab so
the two modes never compete for the streaming server. See
``MainWindow._on_tab_changed`` in ``main.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Union

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
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


def _grayscale_lut() -> np.ndarray:
    """Return a 256x4 (RGBA) grayscale lookup table for ImageItem.setLookupTable."""
    ramp = np.arange(256, dtype=np.uint8)
    lut = np.zeros((256, 4), dtype=np.uint8)
    lut[:, 0] = ramp
    lut[:, 1] = ramp
    lut[:, 2] = ramp
    lut[:, 3] = 255
    return lut


def _format_cps(rate: float) -> str:
    """Format pixels-per-second for the giant readout. Matches operator-tab style."""
    if rate <= 0 or not np.isfinite(rate):
        return "0 cps"
    return f"{rate:.2e} cps"


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

        self._setup_ui()
        try:
            self.load_alignment_preferences()
        except Exception:
            logger.exception("Failed to load alignment preferences; using widget defaults")

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        layout.addWidget(self._build_top_bar())
        layout.addWidget(self._build_controls_row())
        layout.addWidget(self._build_image_area(), stretch=1)

    def _build_top_bar(self) -> QWidget:
        """Row 1: Start/Stop on the left, giant cps readout on the right."""
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
        self._start_btn.setToolTip("Start the alignment pipeline (no file saving). Stops any running operator session is not allowed; stop that first.")
        self._start_btn.clicked.connect(self._on_start_clicked)
        mode_layout.addWidget(self._start_btn)

        self._stop_btn = QPushButton("⏹ Stop")
        self._stop_btn.setFixedWidth(BUTTON_WIDTH)
        self._stop_btn.setStyleSheet(theme.button_style(theme.BUTTON_STOP))
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop_clicked)
        mode_layout.addWidget(self._stop_btn)

        top_layout.addWidget(mode_group)
        top_layout.addStretch()

        # Giant cps readout
        self._cps_label = QLabel("0 cps")
        # Big monospace number; right-aligned so trailing exponent stays put.
        self._cps_label.setStyleSheet(
            f"font-family: 'Consolas', 'Monaco', 'Courier New', monospace; "
            f"font-size: 56px; font-weight: bold; color: {theme.TEXT_PRIMARY}; "
            f"padding: 0 12px;"
        )
        self._cps_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._cps_label.setToolTip(
            "Live count rate: sum of pixels in the latest displayed frame divided by\n"
            "the alignment flush interval (1 / Rate Hz). Updates every flush."
        )
        top_layout.addWidget(self._cps_label)

        return top_bar

    def _build_controls_row(self) -> QWidget:
        """Row 2: rate spinbox + auto/manual range + log + integrated + crosshair."""
        row = QFrame()
        row.setStyleSheet(f"QFrame {{ background-color: {theme.BG_WIDGET}; border-radius: 6px; }}")
        h = QHBoxLayout(row)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(10)

        # Rate (Hz)
        rate_label = QLabel("Rate (Hz):")
        rate_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        rate_label.setToolTip("Wall-clock flush rate driving the alignment image (1–30 Hz). Applied on next Start.")
        h.addWidget(rate_label)
        self._rate_input = QSpinBox()
        self._rate_input.setRange(*preferences.ALIGNMENT_RATE_HZ_RANGE)
        self._rate_input.setValue(30)
        self._rate_input.setStyleSheet(theme.input_style())
        self._rate_input.setToolTip(rate_label.toolTip())
        h.addWidget(self._rate_input)

        # Range combo
        range_label = QLabel("Range:")
        range_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        h.addWidget(range_label)
        self._range_combo = QComboBox()
        self._range_combo.addItems(["Auto range", "Manual"])
        self._range_combo.setStyleSheet(theme.input_style())
        self._range_combo.setToolTip(
            "Auto range: levels follow each frame's min/max.\n"
            "Manual: levels are pinned to the spinboxes (and the LUT handles)."
        )
        self._range_combo.currentTextChanged.connect(self._on_range_mode_changed)
        h.addWidget(self._range_combo)

        # Min/Max spinboxes (manual only)
        self._min_input = QSpinBox()
        self._min_input.setRange(*preferences.ALIGNMENT_LEVEL_RANGE)
        self._min_input.setValue(0)
        self._min_input.setStyleSheet(theme.input_style())
        self._min_input.setEnabled(False)
        self._min_input.valueChanged.connect(self._on_manual_levels_changed)
        h.addWidget(self._min_input)

        self._max_input = QSpinBox()
        self._max_input.setRange(*preferences.ALIGNMENT_LEVEL_RANGE)
        self._max_input.setValue(100)
        self._max_input.setStyleSheet(theme.input_style())
        self._max_input.setEnabled(False)
        self._max_input.valueChanged.connect(self._on_manual_levels_changed)
        h.addWidget(self._max_input)

        # Show integrated
        self._integrated_chk = QCheckBox("Show integrated")
        self._integrated_chk.setToolTip(
            "Off (default): display the latest flush only.\n"
            "On: cumulative sum since the toggle was checked. Reset on Start."
        )
        self._integrated_chk.toggled.connect(self._on_integrated_toggled)
        h.addWidget(self._integrated_chk)

        # Log
        self._log_chk = QCheckBox("Log")
        self._log_chk.setToolTip("Apply log10(data + 1) before display so faint features pop.")
        self._log_chk.toggled.connect(self._refresh_image)
        h.addWidget(self._log_chk)

        # Crosshair
        self._crosshair_chk = QCheckBox("Crosshair")
        self._crosshair_chk.setChecked(True)
        self._crosshair_chk.setToolTip(
            f"Show two faint lines through the geometric center "
            f"({_DETECTOR_X // 2}, {_DETECTOR_Y // 2}) for alignment reference."
        )
        self._crosshair_chk.toggled.connect(self._on_crosshair_toggled)
        h.addWidget(self._crosshair_chk)

        h.addStretch()
        return row

    def _build_image_area(self) -> QWidget:
        """Row 3: square pyqtgraph image with histogram/LUT colorbar."""
        # GraphicsLayoutWidget hosts the PlotItem (image + axes) plus the
        # HistogramLUTItem (colorbar with two draggable level handles).
        self._gw = pg.GraphicsLayoutWidget()
        self._gw.setBackground(theme.BG_DARK)
        self._gw.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._plot = self._gw.addPlot(row=0, col=0)
        self._plot.setAspectLocked(True)  # square pixels regardless of widget shape
        # Show all four axes around the image so it matches the reference
        # screenshot (ticks on the bottom and left, frame on top and right).
        self._plot.showAxis("right")
        self._plot.showAxis("top")
        for ax_name in ("right", "top"):
            ax = self._plot.getAxis(ax_name)
            ax.setStyle(showValues=False)
        self._plot.setLabel("left", "Y (pixel)")
        self._plot.setLabel("bottom", "X (pixel)")
        # Hide pyqtgraph's right-click context menu / autorange button — we
        # manage levels via the controls row.
        self._plot.hideButtons()
        # Cap the view to the detector extent. setLimits keeps the user from
        # accidentally panning into negative space; setRange anchors the
        # initial view.
        self._plot.setRange(xRange=(0, _DETECTOR_X), yRange=(0, _DETECTOR_Y), padding=0)
        self._plot.setLimits(xMin=0, xMax=_DETECTOR_X, yMin=0, yMax=_DETECTOR_Y)

        self._image_item = pg.ImageItem()
        # axisOrder='row-major' makes ImageItem treat array[y, x] as the natural
        # layout; we feed (X, Y) so we transpose at display time. Using
        # 'col-major' would skip the transpose but is less idiomatic.
        self._image_item.setOpts(axisOrder="row-major")
        self._image_item.setLookupTable(_grayscale_lut())
        # Initial empty image so the LUT histogram has something to bind to.
        self._image_item.setImage(np.zeros((_DETECTOR_Y, _DETECTOR_X), dtype=np.float32))
        self._plot.addItem(self._image_item)

        # Crosshair at the geometric center. movable=False so they're a pure
        # visual reference; users can't drag them off-center accidentally.
        crosshair_pen = pg.mkPen(color=(220, 220, 220, 110), width=1, style=Qt.PenStyle.DashLine)
        self._crosshair_v = pg.InfiniteLine(
            pos=_DETECTOR_X / 2, angle=90, pen=crosshair_pen, movable=False
        )
        self._crosshair_h = pg.InfiniteLine(
            pos=_DETECTOR_Y / 2, angle=0, pen=crosshair_pen, movable=False
        )
        self._plot.addItem(self._crosshair_v)
        self._plot.addItem(self._crosshair_h)

        # Histogram LUT colorbar to the right of the plot.
        self._hist = pg.HistogramLUTItem()
        self._hist.setImageItem(self._image_item)
        self._hist.gradient.setColorMap(pg.ColorMap([0.0, 1.0], [(0, 0, 0), (255, 255, 255)]))
        # Mirror dragged LUT levels back into the manual spinboxes whenever
        # the user grabs the triangular handles. We disconnect during programmatic
        # writes via the _suppress_levels_signal flag below.
        self._hist.sigLevelsChanged.connect(self._on_hist_levels_changed)
        self._gw.addItem(self._hist, row=0, col=1)

        # Used to break the levels-spinbox ↔ histogram-handles signal loop.
        self._suppress_levels_signal = False

        return self._gw

    # ------------------------------------------------------------------
    # Public slots / API used by MainWindow
    # ------------------------------------------------------------------

    @Slot(bool)
    def set_acquiring(self, acquiring: bool) -> None:
        """Toggle the Start/Stop buttons and rate spinbox on acquisition state."""
        self._acquiring = acquiring
        self._start_btn.setEnabled(not acquiring)
        self._stop_btn.setEnabled(acquiring)
        # Rate is a server-side parameter — only changeable between runs.
        self._rate_input.setEnabled(not acquiring)
        if acquiring:
            # Reset the integrated buffer on each Start so a fresh accumulation
            # begins; if integrated was off, this is a no-op.
            self._integrated_sum = None

    @Slot(object)
    def on_flush_received(self, flush_data: FlushData) -> None:
        """Render a flush from the streaming server (alignment-mode shape (X, Y, 1))."""
        array = flush_data.array
        metadata = flush_data.metadata

        # Defensive: alignment flushes are always (X, Y, 1) but route by mode.
        if metadata.get("mode") != "alignment":
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

        # Update the giant cps readout from this frame.
        flush_interval_s = metadata.get("flush_interval_s") or 0.0
        if flush_interval_s > 0:
            cps = float(latest_2d.sum()) / float(flush_interval_s)
        else:
            cps = 0.0
        self._cps_label.setText(_format_cps(cps))

        self._refresh_image()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _refresh_image(self) -> None:
        """Push the current (latest or integrated, log or linear) frame to ImageItem."""
        source = self._integrated_sum if self._integrated_chk.isChecked() else self._last_2d
        if source is None:
            return

        if self._log_chk.isChecked():
            disp = np.log10(source.astype(np.float32) + 1.0)
        else:
            disp = source.astype(np.float32, copy=False)

        # ImageItem with axisOrder='row-major' wants (rows=Y, cols=X). Our data
        # is (X, Y), so transpose. setImage(autoLevels=...) chosen by mode below.
        auto = self._range_combo.currentText() == "Auto range"
        if auto:
            self._image_item.setImage(disp.T, autoLevels=True)
            # Sync the LUT widget's histogram range to the new image so the
            # handles sit at the data extents.
            try:
                self._suppress_levels_signal = True
                self._hist.setHistogramRange(float(disp.min()), float(disp.max()))
            finally:
                self._suppress_levels_signal = False
        else:
            lo = float(self._min_input.value())
            hi = float(self._max_input.value())
            if hi <= lo:
                hi = lo + 1.0
            self._image_item.setImage(disp.T, autoLevels=False, levels=(lo, hi))
            try:
                self._suppress_levels_signal = True
                self._hist.setLevels(lo, hi)
            finally:
                self._suppress_levels_signal = False

    @Slot(str)
    def _on_range_mode_changed(self, text: str) -> None:
        manual = text == "Manual"
        self._min_input.setEnabled(manual)
        self._max_input.setEnabled(manual)
        self._refresh_image()

    @Slot(int)
    def _on_manual_levels_changed(self, _value: int) -> None:
        # Only meaningful in manual mode; ignore programmatic spinbox writes
        # caused by histogram-handle drags.
        if self._suppress_levels_signal:
            return
        if self._range_combo.currentText() != "Manual":
            return
        self._refresh_image()

    @Slot()
    def _on_hist_levels_changed(self) -> None:
        """LUT-handle drags update the manual spinboxes (and switch to Manual)."""
        if self._suppress_levels_signal:
            return
        try:
            lo, hi = self._hist.getLevels()
        except Exception:
            return
        # Switch to Manual so the user sees the spinbox values in effect.
        if self._range_combo.currentText() != "Manual":
            self._range_combo.setCurrentText("Manual")
        try:
            self._suppress_levels_signal = True
            self._min_input.setValue(int(round(lo)))
            self._max_input.setValue(int(round(hi)))
        finally:
            self._suppress_levels_signal = False

    @Slot(bool)
    def _on_integrated_toggled(self, checked: bool) -> None:
        # Reset the buffer whenever the toggle changes state, so cumulative
        # always starts at zero from the moment the box is checked.
        self._integrated_sum = None
        self._refresh_image()

    @Slot(bool)
    def _on_crosshair_toggled(self, checked: bool) -> None:
        self._crosshair_v.setVisible(checked)
        self._crosshair_h.setVisible(checked)

    @Slot()
    def _on_start_clicked(self) -> None:
        params = {
            "alignment_rate_hz": int(self._rate_input.value()),
            # ``acq.py``'s own "longest possible" default (~220 days). The user
            # stops via the Stop button; this just removes any acquisition-side
            # auto-stop surprise.
            "duration": 19_008_000,
        }
        # Fresh accumulation buffer for "Show integrated" if it's already on.
        self._integrated_sum = None
        self.start_requested.emit("alignment", params)

    @Slot()
    def _on_stop_clicked(self) -> None:
        self.stop_requested.emit()

    # ------------------------------------------------------------------
    # Preferences
    # ------------------------------------------------------------------

    def _build_preferences(self) -> dict:
        """Snapshot of the alignment-tab widget state for the JSON file."""
        return {
            "alignment_rate_hz": int(self._rate_input.value()),
            "alignment_auto_range": self._range_combo.currentText() == "Auto range",
            "alignment_manual_min": int(self._min_input.value()),
            "alignment_manual_max": int(self._max_input.value()),
            "alignment_log": bool(self._log_chk.isChecked()),
            "alignment_show_integrated": bool(self._integrated_chk.isChecked()),
            "alignment_show_crosshair": bool(self._crosshair_chk.isChecked()),
        }

    def load_alignment_preferences(self, path: Optional[Union[Path, str]] = None) -> None:
        """Restore alignment widgets from the shared on-disk preferences."""
        prefs = preferences.load_operator_preferences(path)
        self._rate_input.setValue(int(prefs["alignment_rate_hz"]))
        self._range_combo.setCurrentText("Auto range" if prefs["alignment_auto_range"] else "Manual")
        self._min_input.setValue(int(prefs["alignment_manual_min"]))
        self._max_input.setValue(int(prefs["alignment_manual_max"]))
        self._log_chk.setChecked(bool(prefs["alignment_log"]))
        self._integrated_chk.setChecked(bool(prefs["alignment_show_integrated"]))
        self._crosshair_chk.setChecked(bool(prefs["alignment_show_crosshair"]))
        # Sync derived UI state.
        self._on_range_mode_changed(self._range_combo.currentText())
        self._on_crosshair_toggled(self._crosshair_chk.isChecked())

    def save_alignment_preferences(self, path: Optional[Union[Path, str]] = None) -> None:
        """Persist alignment-tab widgets to the shared preferences file.

        The save merges with on-disk state (see ``preferences.save_operator_preferences``)
        so this can run independently of ``OperatorTab.save_operator_preferences``
        without clobbering operator keys.
        """
        preferences.save_operator_preferences(self._build_preferences(), path)
