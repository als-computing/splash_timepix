"""Custom widgets for the TimePix3 UI."""

import logging
from datetime import datetime
from functools import lru_cache
from typing import Optional

import numpy as np
from PySide6.QtCore import QPoint, QPointF, QRect, Qt, QTimer, Signal
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
# Ruler Widget
# =============================================================================


class _Ruler(QWidget):
    """Ruler strip. Horizontal: tick marks + normalized labels (mantissa only). Vertical: tick marks only."""

    _THICKNESS_H = 26  # height of horizontal ruler (room for ticks + labels)
    _THICKNESS_V = 14  # width of vertical ruler (ticks only)
    _TICK_MAJOR = 8
    _TICK_MINOR = 4

    def __init__(self, orientation: Qt.Orientation, parent=None):
        super().__init__(parent)
        self._orientation = orientation
        self._view_start = 0
        self._view_stop = 100
        self._scale = 1.0
        self._offset = 0.0
        if orientation == Qt.Orientation.Horizontal:
            self.setFixedHeight(self._THICKNESS_H)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        else:
            self.setFixedWidth(self._THICKNESS_V)
            self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

    def set_range(self, start: int, stop: int) -> None:
        self._view_start = start
        self._view_stop = stop
        self.update()

    def set_scale(self, scale: float, offset: float = 0.0) -> None:
        self._scale = scale
        self._offset = offset
        self.update()

    @staticmethod
    def _nice_interval(span: int) -> int:
        for scale in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000):
            if span / scale <= 10:
                return scale
        return max(1, span // 8)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(theme.BG_WIDGET))

        is_h = self._orientation == Qt.Orientation.Horizontal

        if is_h:
            font = painter.font()
            font.setPixelSize(8)
            painter.setFont(font)
            metrics = painter.fontMetrics()
            label_h = metrics.height()

        painter.setPen(QColor(theme.TEXT_MUTED))

        span = max(1, self._view_stop - self._view_start)
        length = self.width() if is_h else self.height()
        thickness = self.height() if is_h else self.width()

        interval = self._nice_interval(span)
        minor_interval = max(1, interval // 5)

        # Border line
        if is_h:
            painter.drawLine(0, 0, length - 1, 0)
        else:
            painter.drawLine(thickness - 1, 0, thickness - 1, length - 1)

        last_label_end = -999
        d = (self._view_start // minor_interval) * minor_interval
        while d <= self._view_stop:
            if d >= self._view_start:
                frac = (d - self._view_start) / span
                pos = int(frac * length)
                is_major = d % interval == 0
                tick_len = self._TICK_MAJOR if is_major else self._TICK_MINOR
                if is_h:
                    painter.drawLine(pos, 0, pos, tick_len - 1)
                    if is_major:
                        label = f"{self._offset + d * self._scale:.2f}"
                        lw = metrics.horizontalAdvance(label)
                        lx = max(0, min(pos - lw // 2, length - lw))
                        if lx >= last_label_end + 2:
                            painter.drawText(lx, tick_len + 1, lw, label_h, Qt.AlignmentFlag.AlignLeft, label)
                            last_label_end = lx + lw
                else:
                    painter.drawLine(thickness - tick_len, pos, thickness - 1, pos)
            d += minor_interval


# =============================================================================
# Heatmap Canvas (image + draggable cursor pairs)
# =============================================================================

CURSOR_COLORS = ("#00FFFF", "#FF8C00", "#39FF14", "#FF00FF", "#FFE600")  # cyan, orange, lime, magenta, yellow


class _HeatmapCanvas(QWidget):
    """Heatmap canvas with viewport-based downsampling, LabVIEW-style zoom, and right-click pan.

    Stores full-resolution float32 data for saving. Renders a downsampled UI copy
    clipped to the current view [x0, x1, y0, y1] in data-index coordinates.
    """

    cursors_changed = Signal(int, float, float)  # pair_idx, frac_a, frac_b
    view_changed = Signal(int, int, int, int)  # x0, x1, y0, y1  (data indices)

    _HIT_PX = 8
    _ZOOM_MODES = ("rect", "h", "v")

    def __init__(self, parent=None):
        super().__init__(parent)

        # Full-resolution data (kept for saving; never downsampled)
        self._data_full: Optional[np.ndarray] = None  # (n_rows, n_cols) float32
        self._vmin = 0.0
        self._vmax = 1.0
        self._colormap: np.ndarray = get_colormap("viridis")

        # View: [x0, x1, y0, y1] in data-index coords (x=cols, y=rows)
        self._view: list[int] = [0, 1, 0, 1]

        # Downsampled UI pixmap
        self._pixmap_ui: Optional[QPixmap] = None
        self._rgb_buf: Optional[np.ndarray] = None  # keeps buffer alive for QImage

        # Cursor state
        self._cursors_visible = False
        self._cursors: list[list[float]] = [[0.05, 0.15], [0.25, 0.35], [0.45, 0.55], [0.65, 0.75], [0.85, 0.95]]
        self._cursors_active: list[bool] = [True, True, False, False, False]
        self._drag: Optional[tuple[int, int]] = None
        # Read-only cursor overlay mirrored from the other heatmap
        self._overlay_cursors: list[tuple[float, float]] = []
        self._overlay_active: list[bool] = []
        # Vertical cursor overlay from spectrum plot (x-fractions)
        self._vcursor_fracs: Optional[tuple[float, float]] = None
        self._vcursors_on_heatmap: bool = True
        self._cursor_time_scale: float = 0.0  # 0 = no time labels
        self._cursor_time_offset: float = 0.0

        # Zoom state
        self._zoom_mode: str = "rect"
        self._zoom_start: Optional[QPoint] = None
        self._zoom_end: Optional[QPoint] = None
        self._zooming = False

        # Pan state (right-click drag)
        self._panning = False
        self._pan_start: Optional[QPoint] = None
        self._pan_view_start: Optional[list[int]] = None

        self.setMinimumSize(200, 150)
        sp = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return width * 4 // 3  # 3:4 portrait

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_display_data(self, data: np.ndarray, vmin: float, vmax: float, colormap: np.ndarray) -> None:
        """Accept full-res display data. Resets view only when shape changes."""
        shape_changed = self._data_full is None or data.shape != self._data_full.shape
        self._data_full = data
        self._vmin = vmin
        self._vmax = vmax
        self._colormap = colormap
        if shape_changed:
            n_rows, n_cols = data.shape
            self._view = [0, n_cols, 0, n_rows]
        self._render()

    def clear_data(self) -> None:
        self._data_full = None
        self._pixmap_ui = None
        self.update()

    def set_zoom_mode(self, mode: str) -> None:
        self._zoom_mode = mode
        self._zooming = False
        self._zoom_start = self._zoom_end = None
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_view(self, x0: int, x1: int, y0: int, y1: int) -> None:
        """Set view externally (linked zoom). Does NOT re-emit view_changed."""
        self._view = [x0, x1, y0, y1]
        self._render()

    def reset_view(self) -> None:
        if self._data_full is None:
            return
        n_rows, n_cols = self._data_full.shape
        self._view = [0, n_cols, 0, n_rows]
        self._render()
        self.view_changed.emit(*self._view)

    def set_cursor_time_scale(self, scale: float, offset: float = 0.0) -> None:
        self._cursor_time_scale = scale
        self._cursor_time_offset = offset
        self.update()

    def set_cursors_visible(self, visible: bool) -> None:
        self._cursors_visible = visible
        self.update()

    def set_cursor_pair_active(self, pair_idx: int, active: bool) -> None:
        if 0 <= pair_idx < len(self._cursors_active):
            self._cursors_active[pair_idx] = active
            self.update()

    def set_overlay_cursors(self, cursors: list[tuple[float, float]], active: list[bool]) -> None:
        self._overlay_cursors = cursors
        self._overlay_active = active
        self.update()

    def set_vcursor_overlay(self, frac_a: float, frac_b: float) -> None:
        self._vcursor_fracs = (frac_a, frac_b)
        self.update()

    def set_vcursors_on_heatmap(self, visible: bool) -> None:
        self._vcursors_on_heatmap = visible
        self.update()

    def get_cursor_fracs(self) -> list[tuple[float, float]]:
        return [(c[0], c[1]) for c in self._cursors]

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render(self) -> None:
        if self._data_full is None or self.width() < 1 or self.height() < 1:
            return

        n_rows, n_cols = self._data_full.shape
        x0, x1, y0, y1 = self._view

        # Clamp view to data bounds
        x0 = max(0, min(x0, n_cols - 1))
        x1 = max(x0 + 1, min(x1, n_cols))
        y0 = max(0, min(y0, n_rows - 1))
        y1 = max(y0 + 1, min(y1, n_rows))
        self._view = [x0, x1, y0, y1]

        dw, dh = self.width(), self.height()

        col_idx = np.linspace(x0, x1 - 1, dw).astype(np.int32)
        row_idx = np.linspace(y0, y1 - 1, dh).astype(np.int32)

        view_data = self._data_full[np.ix_(row_idx, col_idx)]
        self._rgb_buf = np.ascontiguousarray(apply_colormap(view_data, self._colormap, self._vmin, self._vmax))

        qimg = QImage(self._rgb_buf.data, dw, dh, 3 * dw, QImage.Format.Format_RGB888)
        self._pixmap_ui = QPixmap.fromImage(qimg)
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render()

    # ------------------------------------------------------------------
    # Coordinate helpers
    # ------------------------------------------------------------------

    def _widget_to_data(self, pos: QPoint) -> tuple[int, int]:
        x0, x1, y0, y1 = self._view
        x_d = int(x0 + pos.x() / max(1, self.width()) * (x1 - x0))
        y_d = int(y0 + pos.y() / max(1, self.height()) * (y1 - y0))
        return x_d, y_d

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
            if not self._cursors_active[pair_idx]:
                continue
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

        if self._pixmap_ui and not self._pixmap_ui.isNull():
            painter.drawPixmap(self.rect(), self._pixmap_ui)

        # Cursor overlays
        if self._cursors_visible and self._pixmap_ui:
            rect = self.rect()
            lbl_font = painter.font()
            lbl_font.setPixelSize(9)
            has_time = self._cursor_time_scale != 0.0
            x0, x1, y0, y1 = self._view
            y_span = max(1, y1 - y0)
            for pair_idx, (frac_a, frac_b) in enumerate(self._cursors):
                if not self._cursors_active[pair_idx]:
                    continue
                color = QColor(CURSOR_COLORS[pair_idx])
                pen = QPen(color, 1.5, Qt.PenStyle.DotLine)
                painter.setPen(pen)
                for frac in (frac_a, frac_b):
                    y = self._frac_to_y(frac, rect)
                    painter.drawLine(rect.left(), y, rect.right(), y)
                    if has_time:
                        display_row = y0 + frac * y_span
                        t_val = self._cursor_time_offset + display_row * self._cursor_time_scale
                        label = f"{t_val:.3E}".replace("E", " E")
                        painter.setFont(lbl_font)
                        fm = painter.fontMetrics()
                        lw = fm.horizontalAdvance(label)
                        lh = fm.height()
                        lx = rect.left() + 4
                        ly = max(1, y - lh - 1)
                        painter.fillRect(lx - 1, ly, lw + 4, lh, QColor(0, 0, 0, 130))
                        painter.setPen(color)
                        painter.drawText(lx + 1, ly, lw, lh, Qt.AlignmentFlag.AlignLeft, label)
                        painter.setPen(pen)

        # Read-only cursor overlay (mirrored from the other heatmap)
        if self._overlay_cursors and self._pixmap_ui:
            rect = self.rect()
            for pair_idx, (frac_a, frac_b) in enumerate(self._overlay_cursors):
                if pair_idx >= len(self._overlay_active) or not self._overlay_active[pair_idx]:
                    continue
                color = QColor(CURSOR_COLORS[pair_idx])
                pen = QPen(color, 1.5, Qt.PenStyle.DotLine)
                painter.setPen(pen)
                for frac in (frac_a, frac_b):
                    y = self._frac_to_y(frac, rect)
                    painter.drawLine(rect.left(), y, rect.right(), y)

        # Vertical cursor overlay from spectrum plot (read-only, not draggable)
        if self._vcursor_fracs and self._vcursors_on_heatmap and self._data_full is not None and self._pixmap_ui:
            x0, x1, _y0, _y1 = self._view
            n_cols = self._data_full.shape[1]
            x_span = max(1, x1 - x0)
            rect = self.rect()
            vc_pen = QPen(QColor("#D0D0D0"), 1.0, Qt.PenStyle.SolidLine)
            painter.setPen(vc_pen)
            for frac in self._vcursor_fracs:
                data_x = frac * n_cols
                if x0 <= data_x <= x1:
                    screen_x = int((data_x - x0) / x_span * rect.width())
                    painter.drawLine(screen_x, rect.top(), screen_x, rect.bottom())

        # Zoom rubber-band overlay
        if self._zooming and self._zoom_start and self._zoom_end:
            sx, sy = self._zoom_start.x(), self._zoom_start.y()
            ex, ey = self._zoom_end.x(), self._zoom_end.y()
            if self._zoom_mode == "h":
                sy, ey = 0, self.height()
            elif self._zoom_mode == "v":
                sx, ex = 0, self.width()
            rx, ry = min(sx, ex), min(sy, ey)
            rw, rh = abs(ex - sx), abs(ey - sy)
            painter.fillRect(rx, ry, rw, rh, QColor(255, 255, 255, 40))
            painter.setPen(QPen(QColor(255, 255, 255, 200), 1, Qt.PenStyle.DashLine))
            painter.drawRect(rx, ry, rw, rh)

    # ------------------------------------------------------------------
    # Mouse interaction
    # ------------------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self._panning = True
            self._pan_start = event.pos()
            self._pan_view_start = list(self._view)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return

        if event.button() == Qt.MouseButton.LeftButton:
            # Cursor drag takes priority when cursors are visible
            if self._cursors_visible:
                hit = self._find_nearest_cursor(event.pos().y(), self.rect())
                if hit:
                    self._drag = hit
                    return
            self._zooming = True
            self._zoom_start = event.pos()
            self._zoom_end = event.pos()

    def mouseMoveEvent(self, event):
        # Pan
        if self._panning and self._pan_start and self._pan_view_start and self._data_full is not None:
            dx = event.pos().x() - self._pan_start.x()
            dy = event.pos().y() - self._pan_start.y()
            x0, x1, y0, y1 = self._pan_view_start
            x_span, y_span = x1 - x0, y1 - y0
            n_rows, n_cols = self._data_full.shape
            dx_d = int(-dx / max(1, self.width()) * x_span)
            dy_d = int(-dy / max(1, self.height()) * y_span)
            new_x0 = max(0, min(x0 + dx_d, n_cols - x_span))
            new_y0 = max(0, min(y0 + dy_d, n_rows - y_span))
            self._view = [new_x0, new_x0 + x_span, new_y0, new_y0 + y_span]
            self._render()
            self.view_changed.emit(*self._view)
            return

        # Cursor drag
        if self._drag is not None:
            frac = self._y_to_frac(event.pos().y(), self.rect())
            pair_idx, ci = self._drag
            self._cursors[pair_idx][ci] = frac
            self.update()
            self.cursors_changed.emit(pair_idx, self._cursors[pair_idx][0], self._cursors[pair_idx][1])
            return

        # Zoom rubber-band
        if self._zooming:
            self._zoom_end = event.pos()
            self.update()
            return

        # Cursor hover
        if self._cursors_visible:
            hit = self._find_nearest_cursor(event.pos().y(), self.rect())
            self.setCursor(Qt.CursorShape.SizeVerCursor if hit else Qt.CursorShape.CrossCursor)
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton and self._panning:
            self._panning = False
            self._pan_start = self._pan_view_start = None
            self.setCursor(Qt.CursorShape.CrossCursor)
            return

        if event.button() == Qt.MouseButton.LeftButton:
            if self._drag is not None:
                self._drag = None
                return
            if self._zooming and self._zoom_start and self._zoom_end and self._data_full is not None:
                self._apply_zoom()
            self._zooming = False
            self._zoom_start = self._zoom_end = None

    def _apply_zoom(self) -> None:
        sd = self._widget_to_data(self._zoom_start)
        ed = self._widget_to_data(self._zoom_end)
        x0, x1, y0, y1 = self._view
        n_rows, n_cols = self._data_full.shape

        new_x0 = max(0, min(sd[0], ed[0]))
        new_x1 = min(n_cols, max(sd[0], ed[0]))
        new_y0 = max(0, min(sd[1], ed[1]))
        new_y1 = min(n_rows, max(sd[1], ed[1]))

        if self._zoom_mode == "h":
            new_y0, new_y1 = y0, y1
        elif self._zoom_mode == "v":
            new_x0, new_x1 = x0, x1

        # Reject degenerate selections (< 2 data points)
        if new_x1 - new_x0 < 2:
            new_x0, new_x1 = x0, x1
        if new_y1 - new_y0 < 2:
            new_y0, new_y1 = y0, y1

        self._view = [new_x0, new_x1, new_y0, new_y1]
        self._render()
        self.view_changed.emit(*self._view)


# =============================================================================
# Heatmap Widget
# =============================================================================


class HeatmapWidget(QWidget):
    """Heatmap with tick-mark rulers, linked zoom/pan, and cursor ROIs."""

    cursors_changed = Signal(int, float, float)  # pair_idx, frac_a, frac_b
    view_changed = Signal(int, int, int, int)  # x0, x1, y0, y1 — forwarded from canvas

    def __init__(self, title: str = "Heatmap", parent=None):
        super().__init__(parent)
        self.title = title
        self._data: Optional[np.ndarray] = None
        self._colormap_name = "viridis"
        self._colormap = get_colormap(self._colormap_name)
        self._auto_scale = True
        self._vmin = 0.0
        self._vmax = 1.0
        self._time_bin_ns: Optional[float] = None
        self._n_bins: Optional[int] = None
        self._ev_scale: float = 1.0  # eV per pixel (raw, before normalization)
        self._ev_offset: float = 0.0  # eV at pixel 0
        self._current_x_view: tuple[int, int] = (0, 1)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._title_label = QLabel(self.title)
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_label.setStyleSheet(f"font-weight: bold; color: {theme.TEXT_PRIMARY};")
        layout.addWidget(self._title_label)

        # Heatmap area: [corner + Y-ruler] left | [X-ruler / canvas / x-label] right
        heatmap_container = QWidget()
        heatmap_layout = QHBoxLayout(heatmap_container)
        heatmap_layout.setContentsMargins(0, 0, 0, 0)
        heatmap_layout.setSpacing(2)

        # Y axis title
        self._y_label = VerticalLabel("Time (ns)")
        heatmap_layout.addWidget(self._y_label)

        # Center column: canvas + X ruler + x-axis label
        center_widget = QWidget()
        center_layout = QVBoxLayout(center_widget)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(2)

        self._canvas = _HeatmapCanvas()
        self._canvas.cursors_changed.connect(self.cursors_changed)
        self._canvas.view_changed.connect(self._on_view_changed)
        center_layout.addWidget(self._canvas)

        self._x_ruler = _Ruler(Qt.Orientation.Horizontal)
        center_layout.addWidget(self._x_ruler)

        self._x_label = QLabel("Energy (eV)")
        self._x_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._x_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 10px;")
        center_layout.addWidget(self._x_label)

        heatmap_layout.addWidget(center_widget)
        layout.addWidget(heatmap_container)

        self._stats_label = QLabel("No data")
        self._stats_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._stats_label.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 11px;")
        layout.addWidget(self._stats_label)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_view_changed(self, x0: int, x1: int, y0: int, y1: int) -> None:
        self._current_x_view = (x0, x1)
        self._x_ruler.set_range(x0, x1)
        self._update_x_axis_label(x0, x1)
        self.view_changed.emit(x0, x1, y0, y1)

    def _update_x_axis_label(self, x0: int, x1: int) -> None:
        v0 = self._ev_offset + x0 * self._ev_scale
        v1 = self._ev_offset + x1 * self._ev_scale
        vmax_abs = max(abs(v0), abs(v1))
        if vmax_abs < 1e-10:
            self._x_ruler.set_scale(self._ev_scale, self._ev_offset)
            self._x_label.setText("Energy (eV)")
            return
        exp = int(np.floor(np.log10(vmax_abs)))
        factor = 10.0**exp
        self._x_ruler.set_scale(self._ev_scale / factor, self._ev_offset / factor)
        sign = "+" if exp >= 0 else "-"
        self._x_label.setText(f"Energy (E{sign}{abs(exp):02d} eV)")

    def _update_display(self):
        if self._data is None:
            return
        display_data = np.flipud(self._data.T.astype(np.float32))
        if self._auto_scale:
            vmin, vmax = display_data.min(), display_data.max()
        else:
            vmin, vmax = self._vmin, self._vmax
        self._canvas.set_display_data(display_data, vmin, vmax, self._colormap)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_colormap(self, name: str):
        self._colormap_name = name
        self._colormap = get_colormap(name)
        self._update_display()

    def set_axis_info(self, time_bin_ns: float, n_bins: int):
        self._time_bin_ns = time_bin_ns
        self._n_bins = n_bins
        if time_bin_ns and n_bins:
            # Flipped display: row 0 = last bin → scale negative, offset = (n_bins-1)*dt
            self._canvas.set_cursor_time_scale(-time_bin_ns, (n_bins - 1) * time_bin_ns)

    def set_x_scale(self, scale: float, offset: float = 0.0) -> None:
        self._ev_scale = scale
        self._ev_offset = offset
        self._update_x_axis_label(*self._current_x_view)

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
        self._canvas.set_cursors_visible(visible)

    def set_cursor_pair_active(self, pair_idx: int, active: bool) -> None:
        self._canvas.set_cursor_pair_active(pair_idx, active)

    def update_cursor_overlay(self, cursors: list[tuple[float, float]], active: list[bool]) -> None:
        self._canvas.set_overlay_cursors(cursors, active)

    def set_vcursor_overlay(self, frac_a: float, frac_b: float) -> None:
        self._canvas.set_vcursor_overlay(frac_a, frac_b)

    def set_vcursors_on_heatmap(self, visible: bool) -> None:
        self._canvas.set_vcursors_on_heatmap(visible)

    def get_cursor_fracs(self) -> list[tuple[float, float]]:
        return self._canvas.get_cursor_fracs()

    def clear(self):
        self._data = None
        self._canvas.clear_data()
        self._stats_label.setText("No data")

    def set_view(self, x0: int, x1: int, y0: int, y1: int) -> None:
        """Receive linked view update. Updates rulers without re-emitting view_changed."""
        self._current_x_view = (x0, x1)
        self._canvas.set_view(x0, x1, y0, y1)
        self._x_ruler.set_range(x0, x1)
        self._update_x_axis_label(x0, x1)

    def reset_view(self) -> None:
        self._canvas.reset_view()

    def set_zoom_mode(self, mode: str) -> None:
        self._canvas.set_zoom_mode(mode)


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

    _COLORS = CURSOR_COLORS  # keep in sync with heatmap cursor colors
    _LABELS = ("ROI 1", "ROI 2", "ROI 3", "ROI 4", "ROI 5")
    _VCURSOR_COLOR = "#D0D0D0"  # light gray — neutral against cyan/orange spectra
    _HIT_PX = 8

    # Vertical margins (px) inside the widget
    _MT = 6
    _MB = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self._spectra: list[Optional[np.ndarray]] = [None] * 5
        self._pair_active: list[bool] = [True, True, False, False, False]
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
        self._spectra = [None] * 5
        self.update()

    def set_pair_active(self, pair_idx: int, active: bool) -> None:
        if 0 <= pair_idx < len(self._pair_active):
            self._pair_active[pair_idx] = active
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
        valid = [(i, s) for i, s in enumerate(self._spectra) if s is not None and len(s) > 1 and self._pair_active[i]]

        if not valid:
            f = painter.font()
            f.setPixelSize(11)
            painter.setFont(f)
            painter.setPen(QColor(theme.TEXT_MUTED))
            painter.drawText(pl, pt, pw, ph, Qt.AlignmentFlag.AlignCenter, "Drag cursors on average heatmap")
            # Still draw vertical cursors so user can position them before data arrives
        else:
            # Y scale across all active spectra — min to max
            ymin = min(float(s.min()) for _, s in valid)
            ymax = max(float(s.max()) for _, s in valid)
            y_range = ymax - ymin
            if y_range <= 0:
                y_range = 1.0

            # Horizontal grid line at 50 %
            grid_y = pb - int(0.5 * ph)
            painter.setPen(QPen(QColor(theme.BORDER_SUBTLE), 1, Qt.PenStyle.DotLine))
            painter.drawLine(pl + 1, grid_y, pr - 1, grid_y)

            # Draw each spectrum as a polyline
            for pair_idx, spectrum in valid:
                color = QColor(self._COLORS[pair_idx])
                painter.setPen(QPen(color, 1.5))
                n = len(spectrum)
                poly = QPolygonF()
                for i in range(n):
                    x = pl + i / (n - 1) * pw
                    y = pb - (float(spectrum[i]) - ymin) / y_range * ph
                    poly.append(QPointF(x, y))
                painter.drawPolyline(poly)

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
