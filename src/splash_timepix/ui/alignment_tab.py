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
from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
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
        # Diagnostic counter so we don't spam the system log at 30 Hz; first
        # few flushes are always logged, then periodic.
        self._flush_count = 0
        # Debug-mode fake-data timer. Bypasses the backend (Serval / live-cli /
        # app.py / ZMQ) and feeds synthetic flushes directly into
        # on_flush_received so the rendering pipeline can be exercised in
        # isolation — useful when "I see all black" needs to be triaged into
        # "UI broken" vs "no signal upstream".
        self._fake_timer: Optional[QTimer] = None
        self._fake_rng = np.random.default_rng()

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
        # Per-frame stats readout. Surfaces the actual array stats so the
        # user can immediately tell whether data is flowing and what its
        # range is — answers questions like "is the heatmap empty because
        # there's no signal, or because the levels are wrong?".
        self._frame_stats_label = QLabel("Frame: — | sum=— | max=— | status: idle")
        self._frame_stats_label.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-family: 'Consolas', 'Monaco', "
            f"'Courier New', monospace; font-size: 11px; padding: 2px 8px;"
        )
        self._frame_stats_label.setToolTip(
            "Statistics of the most recent flush. 'sum' is total counts in the\n"
            "displayed frame; 'max' is the brightest pixel. 'No signal' means\n"
            "the flush arrived but contained zero pixels."
        )
        layout.addWidget(self._frame_stats_label)

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

        # Min/Max level spinboxes (manual only). QDoubleSpinBox so they can
        # represent log-space levels (typical max ~5–8) and fractional linear
        # levels both. Range covers practical alignment-mode counts; the
        # decimals=2 keeps the UI compact while still letting the user dial in
        # log-space levels with one decimal of resolution.
        self._min_input = QDoubleSpinBox()
        self._min_input.setRange(float(preferences.ALIGNMENT_LEVEL_RANGE[0]), float(preferences.ALIGNMENT_LEVEL_RANGE[1]))
        self._min_input.setDecimals(2)
        self._min_input.setValue(0.0)
        self._min_input.setStyleSheet(theme.input_style())
        self._min_input.setEnabled(False)
        self._min_input.setToolTip(
            "Manual lower level. In Log mode this is a log10 value (e.g. 0 = 1 count)."
        )
        self._min_input.valueChanged.connect(self._on_manual_levels_changed)
        h.addWidget(self._min_input)

        self._max_input = QDoubleSpinBox()
        self._max_input.setRange(float(preferences.ALIGNMENT_LEVEL_RANGE[0]), float(preferences.ALIGNMENT_LEVEL_RANGE[1]))
        self._max_input.setDecimals(2)
        self._max_input.setValue(100.0)
        self._max_input.setStyleSheet(theme.input_style())
        self._max_input.setEnabled(False)
        self._max_input.setToolTip(
            "Manual upper level. In Log mode this is a log10 value (e.g. 4 = 10 000 counts)."
        )
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

        # Binarize (default ON). Maps any pixel > 0 to LUT max so individual
        # hits are visible regardless of count rate. Overrides Log + Manual
        # range while checked — the LUT levels are forced to (0, 1) so the
        # only two visible colors are background-min and brightest-max.
        self._binarize_chk = QCheckBox("Binarize")
        self._binarize_chk.setChecked(True)
        self._binarize_chk.setToolTip(
            "Render every pixel with count > 0 at the brightest LUT color and\n"
            "every zero pixel at the darkest. Makes single-hit visibility easy\n"
            "during alignment regardless of dynamic range. Overrides Log and\n"
            "manual levels while checked."
        )
        self._binarize_chk.toggled.connect(self._on_binarize_toggled)
        h.addWidget(self._binarize_chk)

        # Log
        self._log_chk = QCheckBox("Log")
        self._log_chk.setToolTip(
            "Apply log10(data + 1) before display so faint features pop.\n"
            "Toggling Log resets range to Auto so the image always stays visible.\n"
            "No effect while Binarize is on."
        )
        self._log_chk.toggled.connect(self._on_log_toggled)
        h.addWidget(self._log_chk)

        # Crosshair
        self._crosshair_chk = QCheckBox("Crosshair")
        self._crosshair_chk.setChecked(True)
        self._crosshair_chk.setToolTip(
            "Show two red dotted squares: outer (50,50)–(206,206); inner centered on "
            "the same point with side length 28/156 of the outer edge."
        )
        self._crosshair_chk.toggled.connect(self._on_crosshair_toggled)
        h.addWidget(self._crosshair_chk)

        h.addStretch()

        # Debug-mode fake data toggle. Highlighted in orange so it's clearly
        # not a normal acquisition control. When checked, Start bypasses the
        # streaming server entirely and feeds synthetic flushes into the
        # rendering pipeline — handy for triaging "all black" reports
        # without needing the detector + Serval to be running.
        self._fake_data_chk = QCheckBox("Fake data (DEBUG)")
        self._fake_data_chk.setStyleSheet(
            f"QCheckBox {{ color: {theme.TERTIARY_ORANGE}; font-weight: bold; }}"
        )
        self._fake_data_chk.setToolTip(
            "DEBUG ONLY. When checked, Start feeds the rendering pipeline with\n"
            "synthetic random flushes (a noisy background plus a Gaussian beam\n"
            "spot) — no Serval, live-cli, or app.py involvement. Use this to\n"
            "verify the UI render path independent of the data source."
        )
        h.addWidget(self._fake_data_chk)

        return row

    def _build_image_area(self) -> QWidget:
        """Row 3: square pyqtgraph image with histogram/LUT colorbar."""
        # GraphicsLayoutWidget hosts the PlotItem (image + axes) plus the
        # HistogramLUTItem (colorbar with two draggable level handles).
        self._gw = pg.GraphicsLayoutWidget()
        self._gw.setBackground(theme.BG_DARK)
        self._gw.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        # Guarantee vertical room so the square plot has enough height to
        # render all 256 detector rows without the top/bottom getting clipped
        # by an aggressive aspect-locked compromise. 480 px is comfortable
        # at typical screen sizes; users can resize larger but not smaller.
        self._gw.setMinimumHeight(480)

        self._plot = self._gw.addPlot(row=0, col=0)
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
        # manage levels via the controls row, and the user does not pan/zoom
        # the alignment view manually.
        self._plot.hideButtons()
        self._plot.setMenuEnabled(False)

        # ViewBox configuration: lock the data range to exactly [0, 256] on
        # both axes — no negative values, ever. We do NOT enable
        # setAspectLocked here. Aspect-lock would force the X view to expand
        # past 256 (or shrink the Y view) whenever the plot region isn't
        # perfectly square, both of which the user explicitly does not want.
        # The trade-off is that detector pixels may render slightly
        # rectangular when the window is non-square, but the full detector
        # is always visible with axes labeled 0–256.
        vb = self._plot.getViewBox()
        vb.setMouseEnabled(x=False, y=False)
        vb.setRange(xRange=(0, _DETECTOR_X), yRange=(0, _DETECTOR_Y), padding=0)
        vb.disableAutoRange()
        vb.setBackgroundColor(theme.BG_DARK)
        self._viewbox = vb

        # axisOrder='row-major' makes ImageItem treat array[y, x] as the natural
        # layout; we feed (X, Y) so we transpose at display time. Using
        # 'col-major' would skip the transpose but is less idiomatic.
        #
        # CRITICAL ORDERING NOTE: do NOT call setRect() before the first
        # setImage(). pyqtgraph's setRect computes the transform as
        # ``scale = rect.width() / self.width()``; before any image is set,
        # ``self.width()`` defaults to 1, so setRect(0,0,256,256) installs a
        # 256× scale. The next setImage updates the bounding rect to 256×256
        # but leaves the (now wrong) 256× transform in place, mapping the
        # image to a 65 536×65 536 region in scene coordinates — entirely
        # outside the visible viewbox, which is exactly the "everything black"
        # symptom. Loading the data first (so width()=256) makes setRect a
        # no-op, and we additionally drop the setRect call entirely because
        # the default boundingRect already maps 1:1 to data coords.
        self._image_item = pg.ImageItem(
            np.zeros((_DETECTOR_Y, _DETECTOR_X), dtype=np.float32),
            axisOrder="row-major",
        )
        self._image_item.setLookupTable(_grayscale_lut())
        self._plot.addItem(self._image_item)

        # Beam-alignment target above the heatmap (high z-value).
        self._target_overlay_items = _build_alignment_target_overlay(self._plot)

        # "Waiting" overlay. Visible until the first flush arrives so the
        # user can immediately tell if the pipeline is alive vs broken
        # ("I see this text → ZMQ subscriber not delivering" vs
        # "I see a black square → flushes arriving but no signal").
        self._waiting_text = pg.TextItem(
            "Waiting for first flush…",
            color=(255, 255, 255, 180),
            anchor=(0.5, 0.5),
        )
        self._waiting_text.setPos(_DETECTOR_X / 2, _DETECTOR_Y / 2)
        # ignoreBounds=True so the text doesn't push the auto-range; we
        # already disabled auto-range, but this is defensive.
        self._plot.addItem(self._waiting_text, ignoreBounds=True)

        # Histogram LUT colorbar to the right of the plot. Cap its width so
        # the colorbar doesn't visually dominate the image when the tab is
        # narrow.
        #
        # NOTE on LUT plumbing: HistogramLUTItem.setImageItem detects a
        # "trivial" black→white gradient (which we use intentionally) and
        # sets the image's lut to None, falling back to pyqtgraph's fast
        # grayscale path. That's fine for rendering, but we re-apply our
        # explicit grayscale LUT afterwards so any future non-trivial color
        # map swap goes through the explicit array path consistently.
        self._hist = pg.HistogramLUTItem()
        self._hist.gradient.setColorMap(pg.ColorMap([0.0, 1.0], [(0, 0, 0), (255, 255, 255)]))
        self._hist.setImageItem(self._image_item)
        self._image_item.setLookupTable(_grayscale_lut())
        # Mirror dragged LUT levels back into the manual spinboxes whenever
        # the user grabs the triangular handles. We disconnect during programmatic
        # writes via the _suppress_levels_signal flag below.
        self._hist.sigLevelsChanged.connect(self._on_hist_levels_changed)
        self._gw.addItem(self._hist, row=0, col=1)

        # Bias the layout so the plot column gets the lion's share of the
        # horizontal space and the colorbar stays narrow regardless of how
        # the user resizes the window.
        gl_layout = self._gw.ci.layout
        gl_layout.setColumnStretchFactor(0, 10)
        gl_layout.setColumnStretchFactor(1, 1)

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
            # Re-arm the per-run diagnostics: log first few flushes again, and
            # re-show the "Waiting for first flush…" overlay so the user gets
            # immediate feedback on whether ZMQ is delivering.
            self._flush_count = 0
            self._frame_stats_label.setText("Frame: — | sum=— | max=— | status: waiting for first flush…")
            if self._waiting_text is None:
                self._waiting_text = pg.TextItem(
                    "Waiting for first flush…",
                    color=(255, 255, 255, 180),
                    anchor=(0.5, 0.5),
                )
                self._waiting_text.setPos(_DETECTOR_X / 2, _DETECTOR_Y / 2)
                self._plot.addItem(self._waiting_text, ignoreBounds=True)
        else:
            self._frame_stats_label.setText(
                self._frame_stats_label.text().replace(
                    "status: waiting for first flush…", "status: idle (stopped)"
                )
            )

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
        # label, the stats line below the image, and the system log.
        frame_sum = float(latest_2d.sum())
        frame_max = float(latest_2d.max()) if latest_2d.size else 0.0
        flush_interval_s = float(metadata.get("flush_interval_s") or 0.0)
        cps = frame_sum / flush_interval_s if flush_interval_s > 0 else 0.0
        self._cps_label.setText(_format_cps(cps))

        # Update stats line — the "status" suffix immediately answers "why
        # do I see nothing?" without the user having to toggle anything.
        if frame_sum == 0:
            status = "No signal (flush arrived empty)"
        else:
            status = f"OK ({cps:.2e} cps)"
        self._frame_stats_label.setText(
            f"Frame: {latest_2d.shape[0]}×{latest_2d.shape[1]} | "
            f"sum={frame_sum:.3e} | max={frame_max:.3e} | status: {status}"
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
                self._flush_count, latest_2d.shape, frame_sum, frame_max, mode, flush_interval_s,
            )

        self._refresh_image()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _refresh_image(self) -> None:
        """Push the current (latest or integrated, log or linear) frame to ImageItem."""
        source = self._integrated_sum if self._integrated_chk.isChecked() else self._last_2d
        if source is None:
            return

        # Binarize takes precedence over Log/Manual: any pixel > 0 → 1.0,
        # zeros stay 0.0, levels pinned to (0, 1) so the only colors shown
        # are LUT-min (background) and LUT-max (any hit). This is the most
        # robust "did we hit *any* pixel here?" view for alignment work.
        if self._binarize_chk.isChecked():
            disp = (source > 0).astype(np.float32)
            self._image_item.setImage(disp.T, autoLevels=False, levels=(0.0, 1.0))
            try:
                self._suppress_levels_signal = True
                self._hist.setLevels(0.0, 1.0)
                self._hist.setHistogramRange(0.0, 1.0)
            finally:
                self._suppress_levels_signal = False
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
        auto = self._range_combo.currentText() == "Auto range"
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
            try:
                self._suppress_levels_signal = True
                self._hist.setHistogramRange(data_lo, data_hi)
                self._hist.setLevels(data_lo, data_hi)
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

        # Force an immediate repaint of the ImageItem. Without this the
        # QGraphicsView only repaints on the next Qt event-loop tick, which
        # at 30 Hz is fine but on a stalled UI thread (long-running mainloop
        # work) can leave the image visibly stale.
        self._image_item.update()

    @Slot(str)
    def _on_range_mode_changed(self, text: str) -> None:
        manual = text == "Manual"
        self._min_input.setEnabled(manual)
        self._max_input.setEnabled(manual)
        self._refresh_image()

    @Slot(float)
    def _on_manual_levels_changed(self, _value: float) -> None:
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
            self._min_input.setValue(float(lo))
            self._max_input.setValue(float(hi))
        finally:
            self._suppress_levels_signal = False

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
        # Range combo + manual spinboxes are also irrelevant under binarize.
        self._range_combo.setEnabled(not checked)
        manual_text = self._range_combo.currentText() == "Manual"
        self._min_input.setEnabled((not checked) and manual_text)
        self._max_input.setEnabled((not checked) and manual_text)
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
        if self._range_combo.currentText() != "Auto range":
            self._range_combo.setCurrentText("Auto range")
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

        if self._fake_data_chk.isChecked():
            # Debug path: bypass the backend, locally drive the same render
            # pipeline by emitting synthetic flushes via a QTimer at the rate
            # set in the UI. This intentionally does NOT emit start_requested,
            # so MainWindow stays unaware (no streaming server, no live-cli,
            # no acq.py). Stop kills only the local timer.
            logger.info("AlignmentTab: starting FAKE DATA mode (debug)")
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
            logger.info("AlignmentTab: stopping FAKE DATA mode")
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
        return {
            "alignment_rate_hz": int(self._rate_input.value()),
            "alignment_auto_range": self._range_combo.currentText() == "Auto range",
            "alignment_manual_min": float(self._min_input.value()),
            "alignment_manual_max": float(self._max_input.value()),
            "alignment_log": bool(self._log_chk.isChecked()),
            "alignment_binarize": bool(self._binarize_chk.isChecked()),
            "alignment_show_integrated": bool(self._integrated_chk.isChecked()),
            "alignment_show_crosshair": bool(self._crosshair_chk.isChecked()),
        }

    def load_alignment_preferences(self, path: Optional[Union[Path, str]] = None) -> None:
        """Restore alignment widgets from the shared on-disk preferences."""
        prefs = preferences.load_operator_preferences(path)
        self._rate_input.setValue(int(prefs["alignment_rate_hz"]))
        self._range_combo.setCurrentText("Auto range" if prefs["alignment_auto_range"] else "Manual")
        self._min_input.setValue(float(prefs["alignment_manual_min"]))
        self._max_input.setValue(float(prefs["alignment_manual_max"]))
        self._log_chk.setChecked(bool(prefs["alignment_log"]))
        self._binarize_chk.setChecked(bool(prefs["alignment_binarize"]))
        self._integrated_chk.setChecked(bool(prefs["alignment_show_integrated"]))
        self._crosshair_chk.setChecked(bool(prefs["alignment_show_crosshair"]))
        # Sync derived UI state. Binarize-driven enabled/disabled state must
        # come last because it overrides the per-mode enabled state set by
        # _on_range_mode_changed.
        self._on_range_mode_changed(self._range_combo.currentText())
        self._on_crosshair_toggled(self._crosshair_chk.isChecked())
        self._on_binarize_toggled(self._binarize_chk.isChecked())

    def save_alignment_preferences(self, path: Optional[Union[Path, str]] = None) -> None:
        """Persist alignment-tab widgets to the shared preferences file.

        The save merges with on-disk state (see ``preferences.save_operator_preferences``)
        so this can run independently of ``OperatorTab.save_operator_preferences``
        without clobbering operator keys.
        """
        preferences.save_operator_preferences(self._build_preferences(), path)
