"""Custom widgets for the TimePix3 UI."""

import logging
from datetime import datetime
from functools import lru_cache
from typing import Optional

import numpy as np
from PySide6.QtCore import QPointF, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QSizePolicy, QVBoxLayout, QWidget

from . import theme

logger = logging.getLogger(__name__)


# =============================================================================
# Colormap utilities
# =============================================================================


@lru_cache(maxsize=8)
def get_colormap(name: str = "viridis") -> np.ndarray:
    """Get a 256x3 RGB colormap array. Results are cached."""
    colormaps = {
        "viridis": [
            (68, 1, 84),
            (72, 35, 116),
            (64, 67, 135),
            (52, 94, 141),
            (41, 120, 142),
            (32, 144, 140),
            (34, 167, 132),
            (68, 190, 112),
            (121, 209, 81),
            (189, 222, 38),
            (253, 231, 37),
        ],
        "plasma": [
            (13, 8, 135),
            (75, 3, 161),
            (125, 3, 168),
            (168, 34, 150),
            (203, 70, 121),
            (229, 107, 93),
            (248, 148, 65),
            (253, 195, 40),
            (240, 249, 33),
        ],
        "inferno": [
            (0, 0, 4),
            (40, 11, 84),
            (101, 21, 110),
            (159, 42, 99),
            (212, 72, 66),
            (245, 125, 21),
            (250, 193, 39),
            (252, 255, 164),
        ],
        "magma": [
            (0, 0, 4),
            (28, 16, 68),
            (79, 18, 123),
            (129, 37, 129),
            (181, 54, 122),
            (229, 80, 100),
            (251, 135, 97),
            (254, 196, 136),
            (252, 253, 191),
        ],
    }

    if name not in colormaps:
        name = "viridis"

    key_colors = np.array(colormaps[name], dtype=np.float32)
    n_keys = len(key_colors)

    cmap = np.zeros((256, 3), dtype=np.uint8)
    for i in range(256):
        t = i / 255.0 * (n_keys - 1)
        idx = int(t)
        frac = t - idx
        if idx >= n_keys - 1:
            cmap[i] = key_colors[-1]
        else:
            cmap[i] = (key_colors[idx] * (1 - frac) + key_colors[idx + 1] * frac).astype(np.uint8)

    return cmap


def apply_colormap(
    data: np.ndarray,
    cmap: np.ndarray,
    vmin: Optional[float] = None,
    vmax: Optional[float] = None,
) -> np.ndarray:
    """Apply colormap to 2D data array."""
    if vmin is None:
        vmin = data.min()
    if vmax is None:
        vmax = data.max()

    if vmax <= vmin:
        vmax = vmin + 1

    normalized = np.clip((data - vmin) / (vmax - vmin) * 255, 0, 255).astype(np.uint8)
    return cmap[normalized]


# =============================================================================
# Vertical Label Widget
# =============================================================================


class VerticalLabel(QWidget):
    """A label that draws text rotated -90 degrees (reading bottom to top)."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._text = text
        self.setFixedWidth(20)

    def set_text(self, text: str):
        self._text = text
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        font = painter.font()
        font.setPixelSize(10)
        painter.setFont(font)
        painter.setPen(QColor(theme.TEXT_SECONDARY))

        # Rotate -90 degrees around center
        painter.translate(self.width() / 2, self.height() / 2)
        painter.rotate(-90)

        # Draw text centered
        metrics = painter.fontMetrics()
        text_width = metrics.horizontalAdvance(self._text)
        text_height = metrics.height()
        painter.drawText(-text_width // 2, text_height // 4, self._text)


# =============================================================================
# Heatmap Canvas (image + draggable cursor pairs)
# =============================================================================

_CURSOR_COLORS = ("#00FFFF", "#FF8C00")  # cyan, orange — one per ROI pair


class _HeatmapCanvas(QWidget):
    """Paints a heatmap pixmap and two draggable horizontal cursor pairs.

    Each pair consists of two lines (same color) whose y-positions are stored
    as fractions in [0, 1] from the top of the *image* (not the widget).  When
    a cursor is dragged, ``cursors_changed(pair_idx, frac_a, frac_b)`` is
    emitted so callers can recompute spectra.
    """

    cursors_changed = Signal(int, float, float)  # pair_idx, frac_a, frac_b

    _HIT_PX = 8  # pixels distance to register a cursor grab

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap: Optional[QPixmap] = None
        self._cursors_visible = False
        # Two pairs; each is [frac_a, frac_b], 0.0 = top, 1.0 = bottom
        self._cursors: list[list[float]] = [[0.20, 0.35], [0.60, 0.75]]
        self._drag: Optional[tuple[int, int]] = None  # (pair_idx, cursor_idx)
        self.setMinimumSize(200, 150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_pixmap(self, pixmap: Optional[QPixmap]) -> None:
        self._pixmap = pixmap
        self.update()

    def set_cursors_visible(self, visible: bool) -> None:
        self._cursors_visible = visible
        self.update()

    def get_cursor_fracs(self) -> list[tuple[float, float]]:
        return [(c[0], c[1]) for c in self._cursors]

    # ------------------------------------------------------------------
    # Geometry helpers
    # ------------------------------------------------------------------

    def _image_rect(self) -> QRect:
        """Rect (in widget coords) where the pixmap is drawn (KeepAspectRatio)."""
        if self._pixmap is None or self._pixmap.isNull():
            return self.rect()
        pw, ph = self._pixmap.width(), self._pixmap.height()
        if pw == 0 or ph == 0:
            return self.rect()
        scale = min(self.width() / pw, self.height() / ph)
        iw = int(pw * scale)
        ih = int(ph * scale)
        x = (self.width() - iw) // 2
        y = (self.height() - ih) // 2
        return QRect(x, y, iw, ih)

    def _frac_to_y(self, frac: float, rect: QRect) -> int:
        return rect.top() + int(frac * rect.height())

    def _y_to_frac(self, y: int, rect: QRect) -> float:
        if rect.height() == 0:
            return 0.0
        return max(0.0, min(1.0, (y - rect.top()) / rect.height()))

    def _find_nearest_cursor(self, y: int, rect: QRect) -> Optional[tuple[int, int]]:
        best_dist = self._HIT_PX + 1
        best = None
        for pair_idx, fracs in enumerate(self._cursors):
            for ci, frac in enumerate(fracs):
                dist = abs(self._frac_to_y(frac, rect) - y)
                if dist <= self._HIT_PX and dist < best_dist:
                    best_dist = dist
                    best = (pair_idx, ci)
        return best

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(theme.BG_DARK))

        if self._pixmap and not self._pixmap.isNull():
            rect = self._image_rect()
            painter.drawPixmap(rect, self._pixmap)

            if self._cursors_visible:
                for pair_idx, (frac_a, frac_b) in enumerate(self._cursors):
                    color = QColor(_CURSOR_COLORS[pair_idx])

                    # Translucent fill between the two lines
                    y_top = self._frac_to_y(min(frac_a, frac_b), rect)
                    y_bot = self._frac_to_y(max(frac_a, frac_b), rect)
                    fill = QColor(color)
                    fill.setAlpha(35)
                    painter.fillRect(rect.left(), y_top, rect.width(), max(1, y_bot - y_top), fill)

                    # Dashed cursor lines
                    pen = QPen(color, 1.5, Qt.PenStyle.DashLine)
                    painter.setPen(pen)
                    for frac in (frac_a, frac_b):
                        y = self._frac_to_y(frac, rect)
                        painter.drawLine(rect.left(), y, rect.right(), y)

    # ------------------------------------------------------------------
    # Mouse interaction
    # ------------------------------------------------------------------

    def mousePressEvent(self, event):
        if not self._cursors_visible or event.button() != Qt.MouseButton.LeftButton:
            return
        rect = self._image_rect()
        if rect.height() == 0 or not rect.contains(event.pos()):
            return
        hit = self._find_nearest_cursor(event.pos().y(), rect)
        if hit:
            self._drag = hit

    def mouseMoveEvent(self, event):
        rect = self._image_rect()
        if self._drag is not None:
            frac = self._y_to_frac(event.pos().y(), rect)
            pair_idx, ci = self._drag
            self._cursors[pair_idx][ci] = frac
            self.update()
            self.cursors_changed.emit(pair_idx, self._cursors[pair_idx][0], self._cursors[pair_idx][1])
        elif self._cursors_visible and rect.contains(event.pos()):
            hit = self._find_nearest_cursor(event.pos().y(), rect)
            self.setCursor(Qt.CursorShape.SizeVerCursor if hit else Qt.CursorShape.ArrowCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = None


# =============================================================================
# Heatmap Widget
# =============================================================================


class HeatmapWidget(QWidget):
    """Widget that displays a 2D heatmap with colormap and axis labels."""

    cursors_changed = Signal(int, float, float)  # pair_idx, frac_a, frac_b

    def __init__(self, title: str = "Heatmap", parent=None):
        super().__init__(parent)
        self.title = title
        self._data: Optional[np.ndarray] = None
        self._colormap_name = "viridis"
        self._colormap = get_colormap(self._colormap_name)
        self._auto_scale = True
        self._vmin = 0.0
        self._vmax = 1.0

        # Axis configuration
        self._time_bin_ns: Optional[float] = None
        self._n_bins: Optional[int] = None

        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._title_label = QLabel(self.title)
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_label.setStyleSheet(f"font-weight: bold; color: {theme.TEXT_PRIMARY};")
        layout.addWidget(self._title_label)

        # Heatmap with axis labels
        heatmap_container = QWidget()
        heatmap_layout = QHBoxLayout(heatmap_container)
        heatmap_layout.setContentsMargins(0, 0, 0, 0)
        heatmap_layout.setSpacing(4)

        # Y-axis label (rotated -90 degrees)
        self._y_label = VerticalLabel("Time")
        self._y_label.setFixedWidth(20)
        heatmap_layout.addWidget(self._y_label)

        # Image and X-axis
        center_widget = QWidget()
        center_layout = QVBoxLayout(center_widget)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(2)

        self._canvas = _HeatmapCanvas()
        self._canvas.cursors_changed.connect(self.cursors_changed)
        center_layout.addWidget(self._canvas)

        self._x_label = QLabel("Energy (pixels)")
        self._x_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._x_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 10px;")
        center_layout.addWidget(self._x_label)

        heatmap_layout.addWidget(center_widget)
        layout.addWidget(heatmap_container)

        self._stats_label = QLabel("No data")
        self._stats_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._stats_label.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 11px;")
        layout.addWidget(self._stats_label)

    def set_colormap(self, name: str):
        self._colormap_name = name
        self._colormap = get_colormap(name)
        self._update_display()

    def set_axis_info(self, time_bin_ns: float, n_bins: int):
        """Set axis scaling information for labels."""
        self._time_bin_ns = time_bin_ns
        self._n_bins = n_bins
        self._update_axis_labels()

    def _update_axis_labels(self):
        """Update axis labels with current scaling info."""
        if self._time_bin_ns and self._n_bins:
            total_time_ns = self._time_bin_ns * self._n_bins
            if total_time_ns >= 1e6:
                self._y_label.set_text(f"Time ({self._time_bin_ns/1e3:.1f} µs/bin)")
            else:
                self._y_label.set_text(f"Time ({self._time_bin_ns:.0f} ns/bin)")

    def set_data(self, data: np.ndarray, stats_text: Optional[str] = None):
        self._data = data

        if stats_text:
            self._stats_label.setText(stats_text)
        else:
            total = np.sum(data)
            max_val = np.max(data)
            self._stats_label.setText(f"Total: {total:.2e} | Max: {max_val:.2e}")

        self._update_display()

    def enable_cursors(self, visible: bool) -> None:
        """Show or hide the draggable ROI cursor pairs."""
        self._canvas.set_cursors_visible(visible)

    def get_cursor_fracs(self) -> list[tuple[float, float]]:
        """Return current cursor fractions [(frac_a, frac_b), ...] for each pair."""
        return self._canvas.get_cursor_fracs()

    def clear(self):
        self._data = None
        self._canvas.set_pixmap(None)
        self._stats_label.setText("No data")

    def _update_display(self):
        if self._data is None:
            return

        # Transpose and flip: time bin 0 at bottom
        display_data = np.flipud(self._data.T.astype(np.float32))

        if self._auto_scale:
            vmin, vmax = display_data.min(), display_data.max()
        else:
            vmin, vmax = self._vmin, self._vmax

        rgb = apply_colormap(display_data, self._colormap, vmin, vmax)

        h, w = rgb.shape[:2]
        bytes_per_line = 3 * w
        qimg = QImage(rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
        self._canvas.set_pixmap(QPixmap.fromImage(qimg))


# =============================================================================
# Spectrum Plot Widget
# =============================================================================


class SpectrumPlotWidget(QWidget):
    """Paints two 1D spectra (one per ROI cursor pair) with two draggable vertical cursors.

    Each spectrum is the counts-per-time-bin projection onto the energy (x) axis.
    The vertical cursors emit ``cursors_changed(frac_a, frac_b)`` when moved so
    callers can display pixel / eV positions.
    """

    cursors_changed = Signal(float, float)  # frac_a, frac_b (x fractions in [0, 1])

    _COLORS = _CURSOR_COLORS  # keep in sync with heatmap cursor colors
    _LABELS = ("ROI 1", "ROI 2")
    _VCURSOR_COLOR = "#D0D0D0"  # light gray — neutral against cyan/orange spectra
    _HIT_PX = 8

    # Vertical margins (px) inside the widget
    _MT = 6
    _MB = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self._spectra: list[Optional[np.ndarray]] = [None, None]
        # Vertical cursor x-fractions: 0.0 = left edge, 1.0 = right edge
        self._vcursors: list[float] = [0.25, 0.75]
        self._vdrag: Optional[int] = None  # index of cursor being dragged
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_spectrum(self, pair_idx: int, spectrum: Optional[np.ndarray]) -> None:
        self._spectra[pair_idx] = spectrum
        self.update()

    def clear(self) -> None:
        self._spectra = [None, None]
        self.update()

    def get_cursor_fracs(self) -> tuple[float, float]:
        """Return current vertical cursor positions as (frac_a, frac_b) in [0, 1]."""
        return (self._vcursors[0], self._vcursors[1])

    # ------------------------------------------------------------------
    # Geometry helpers
    # ------------------------------------------------------------------

    def _frac_to_cx(self, frac: float) -> int:
        return int(frac * self.width())

    def _cx_to_frac(self, x: int) -> float:
        w = self.width()
        if w == 0:
            return 0.0
        return max(0.0, min(1.0, x / w))

    def _find_vcursor(self, x: int) -> Optional[int]:
        best_dist = self._HIT_PX + 1
        best = None
        for i, frac in enumerate(self._vcursors):
            dist = abs(self._frac_to_cx(frac) - x)
            if dist <= self._HIT_PX and dist < best_dist:
                best_dist = dist
                best = i
        return best

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(theme.BG_DARK))

        pl = 0
        pr = self.width()
        pt = self._MT
        pb = self.height() - self._MB
        pw = pr - pl
        ph = pb - pt
        if pw < 4 or ph < 4:
            return

        # Axes border
        painter.setPen(QPen(QColor(theme.BORDER_SUBTLE), 1))
        painter.drawRect(pl, pt, pw - 1, ph - 1)

        # Valid spectra
        valid = [(i, s) for i, s in enumerate(self._spectra) if s is not None and len(s) > 1]

        if not valid:
            f = painter.font()
            f.setPixelSize(11)
            painter.setFont(f)
            painter.setPen(QColor(theme.TEXT_MUTED))
            painter.drawText(pl, pt, pw, ph, Qt.AlignmentFlag.AlignCenter, "Drag cursors on average heatmap")
            # Still draw vertical cursors so user can position them before data arrives
        else:
            # Y scale across all active spectra
            ymax = max(float(s.max()) for _, s in valid)
            if ymax <= 0:
                ymax = 1.0

            # Horizontal grid line at 50 %
            grid_y = pb - int(0.5 * ph)
            painter.setPen(QPen(QColor(theme.BORDER_SUBTLE), 1, Qt.PenStyle.DotLine))
            painter.drawLine(pl + 1, grid_y, pr - 1, grid_y)

            # Max-value annotation (top-left)
            f = painter.font()
            f.setPixelSize(9)
            painter.setFont(f)
            painter.setPen(QColor(theme.TEXT_MUTED))
            painter.drawText(pl + 3, pt + 2, pw - 6, 12, Qt.AlignmentFlag.AlignLeft, f"peak {ymax:.2g} cts/bin")

            # Draw each spectrum as a polyline
            for pair_idx, spectrum in valid:
                color = QColor(self._COLORS[pair_idx])
                painter.setPen(QPen(color, 1.5))
                n = len(spectrum)
                poly = QPolygonF()
                for i in range(n):
                    x = pl + i / (n - 1) * pw
                    y = pb - (float(spectrum[i]) / ymax) * ph
                    poly.append(QPointF(x, y))
                painter.drawPolyline(poly)

            # Legend (top-right corner)
            lx = pr - 58
            ly = pt + 3
            for i, (color_hex, label) in enumerate(zip(self._COLORS, self._LABELS)):
                if self._spectra[i] is not None:
                    painter.setPen(QPen(QColor(color_hex), 2))
                    painter.drawLine(lx, ly + i * 14 + 5, lx + 14, ly + i * 14 + 5)
                    f2 = painter.font()
                    f2.setPixelSize(10)
                    painter.setFont(f2)
                    painter.setPen(QColor(color_hex))
                    painter.drawText(
                        lx + 17, ly + i * 14, 40, 14, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label
                    )

        # Vertical cursors — drawn last so they sit on top of everything
        vc_color = QColor(self._VCURSOR_COLOR)
        vc_pen = QPen(vc_color, 1.5, Qt.PenStyle.DashLine)
        painter.setPen(vc_pen)
        for frac in self._vcursors:
            cx = self._frac_to_cx(frac)
            painter.drawLine(cx, pt, cx, pb)

    # ------------------------------------------------------------------
    # Mouse interaction
    # ------------------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        hit = self._find_vcursor(event.pos().x())
        if hit is not None:
            self._vdrag = hit

    def mouseMoveEvent(self, event):
        if self._vdrag is not None:
            self._vcursors[self._vdrag] = self._cx_to_frac(event.pos().x())
            self.update()
            self.cursors_changed.emit(self._vcursors[0], self._vcursors[1])
        else:
            hit = self._find_vcursor(event.pos().x())
            self.setCursor(Qt.CursorShape.SizeHorCursor if hit is not None else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._vdrag = None


# =============================================================================
# Status Indicator Widget
# =============================================================================


class _IndicatorCircle(QWidget):
    """Small painted circle for status indication."""

    def __init__(self, size: int = 12, parent=None):
        super().__init__(parent)
        self._size = size
        self._color = QColor(theme.STATUS_INACTIVE)
        self.setFixedSize(size, size)

    def set_color(self, color: QColor):
        self._color = color
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(self._color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(0, 0, self._size, self._size)


class StatusIndicator(QWidget):
    """Colored circle indicator with label."""

    INDICATOR_SIZE = 12

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self._label = label
        self._connected = False
        self._ok_blink_timer: Optional[QTimer] = None
        self._ok_blink_phase = True

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(8)

        # Custom painted circle indicator
        self._indicator = _IndicatorCircle(self.INDICATOR_SIZE)
        layout.addWidget(self._indicator)

        self._label_widget = QLabel(label)
        self._label_widget.setStyleSheet(f"color: {theme.TEXT_PRIMARY};")
        layout.addWidget(self._label_widget)

        self._status_widget = QLabel("")
        self._status_widget.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(self._status_widget)

        layout.addStretch()

    def set_status_detail(self, text: str) -> None:
        """Update the right-hand status text without changing color or blink timer."""
        self._status_widget.setText(text)

    def is_ok_blinking(self) -> bool:
        return self._ok_blink_timer is not None

    def stop_ok_blink(self) -> None:
        if self._ok_blink_timer is not None:
            self._ok_blink_timer.stop()
            self._ok_blink_timer.deleteLater()
            self._ok_blink_timer = None

    def _toggle_ok_blink(self) -> None:
        self._ok_blink_phase = not self._ok_blink_phase
        if self._ok_blink_phase:
            self._indicator.set_color(QColor(theme.STATUS_OK))
        else:
            self._indicator.set_color(QColor(theme.STATUS_OK).darker(200))

    def start_ok_blink(self, detail: str = "", interval_ms: int = 450) -> None:
        """Blink green while waiting (e.g. Serval JVM up but hardware not ready)."""
        self.stop_ok_blink()
        self._connected = True
        self._ok_blink_phase = True
        self._indicator.set_color(QColor(theme.STATUS_OK))
        self._status_widget.setText(detail)
        self._ok_blink_timer = QTimer(self)
        self._ok_blink_timer.timeout.connect(self._toggle_ok_blink)
        self._ok_blink_timer.start(interval_ms)

    def set_connected(self, connected: bool, status: str = ""):
        self.stop_ok_blink()
        self._connected = connected

        if connected:
            self._indicator.set_color(QColor(theme.STATUS_OK))
        else:
            self._indicator.set_color(QColor(theme.STATUS_INACTIVE))

        self._status_widget.setText(status)

    def set_run_state(self, running: bool, detail: str = ""):
        """Green when running, red when not (e.g. Serval JVM vs stopped)."""
        self.stop_ok_blink()
        self._connected = running
        if running:
            self._indicator.set_color(QColor(theme.STATUS_OK))
        else:
            self._indicator.set_color(QColor(theme.STATUS_ERROR))
        self._status_widget.setText(detail)

    def set_streaming_active(self, detail: str = "streaming"):
        """Solid green while streaming server is receiving data (replaces former blue)."""
        self.stop_ok_blink()
        self._connected = True
        self._indicator.set_color(QColor(theme.STATUS_OK))
        self._status_widget.setText(detail)


# =============================================================================
# Terminal Output Widget
# =============================================================================


class TerminalWidget(QWidget):
    """Widget that displays scrolling terminal output from a process."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._title = title
        self._max_lines = 1000

        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(0)

        title_bar = QFrame()
        title_bar.setStyleSheet(
            f"""
            background-color: {theme.BG_WIDGET};
            border-radius: 4px 4px 0 0;
            border: 1px solid {theme.BORDER_SUBTLE};
            border-bottom: none;
        """
        )
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(8, 4, 8, 4)

        self._title_label = QLabel(self._title)
        self._title_label.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; font-weight: bold; font-size: 11px;")
        title_layout.addWidget(self._title_label)

        self._status_label = QLabel("not running")
        self._status_label.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 10px;")
        title_layout.addStretch()
        title_layout.addWidget(self._status_label)

        layout.addWidget(title_bar)

        self._output = QPlainTextEdit()
        self._output.setReadOnly(True)
        self._output.setMaximumBlockCount(self._max_lines)
        self._output.setStyleSheet(theme.terminal_style())
        layout.addWidget(self._output)

    def append_text(self, text: str):
        stripped = text.rstrip("\n")
        if not stripped:
            return
        for line in stripped.split("\n"):
            ts = datetime.now().strftime("%H:%M:%S")
            self._output.appendPlainText(f"[{ts}] {line}")
        scrollbar = self._output.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def clear(self):
        self._output.clear()

    def set_status(self, status: str, running: bool = False):
        self._status_label.setText(status)
        if running:
            self._status_label.setStyleSheet(f"color: {theme.STATUS_OK}; font-size: 10px;")
        else:
            self._status_label.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 10px;")
