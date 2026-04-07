"""Custom widgets for the TimePix3 UI."""

import logging
from datetime import datetime
from functools import lru_cache
from typing import Optional

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
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
# Heatmap Widget
# =============================================================================


class HeatmapWidget(QWidget):
    """Widget that displays a 2D heatmap with colormap and axis labels."""

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

        self._image_label = QLabel()
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setMinimumSize(200, 150)
        self._image_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._image_label.setStyleSheet(theme.heatmap_background_style())
        center_layout.addWidget(self._image_label)

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

    def clear(self):
        self._data = None
        self._image_label.clear()
        self._image_label.setStyleSheet(theme.heatmap_background_style())
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

        pixmap = QPixmap.fromImage(qimg)
        scaled = pixmap.scaled(
            self._image_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )

        self._image_label.setPixmap(scaled)


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
