"""Custom widgets for the TimePix3 UI."""

import logging
from typing import Optional

import numpy as np
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPlainTextEdit,
    QFrame, QSizePolicy, QGroupBox
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap

logger = logging.getLogger(__name__)


# =============================================================================
# Colormap utilities
# =============================================================================

def get_colormap(name: str = "viridis") -> np.ndarray:
    """Get a 256x3 RGB colormap array."""
    colormaps = {
        "viridis": [
            (68, 1, 84), (72, 35, 116), (64, 67, 135), (52, 94, 141),
            (41, 120, 142), (32, 144, 140), (34, 167, 132), (68, 190, 112),
            (121, 209, 81), (189, 222, 38), (253, 231, 37)
        ],
        "plasma": [
            (13, 8, 135), (75, 3, 161), (125, 3, 168), (168, 34, 150),
            (203, 70, 121), (229, 107, 93), (248, 148, 65), (253, 195, 40),
            (240, 249, 33)
        ],
        "inferno": [
            (0, 0, 4), (40, 11, 84), (101, 21, 110), (159, 42, 99),
            (212, 72, 66), (245, 125, 21), (250, 193, 39), (252, 255, 164)
        ],
        "magma": [
            (0, 0, 4), (28, 16, 68), (79, 18, 123), (129, 37, 129),
            (181, 54, 122), (229, 80, 100), (251, 135, 97), (254, 196, 136),
            (252, 253, 191)
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


def apply_colormap(data: np.ndarray, cmap: np.ndarray, 
                   vmin: Optional[float] = None, 
                   vmax: Optional[float] = None) -> np.ndarray:
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
# Heatmap Widget
# =============================================================================

class HeatmapWidget(QWidget):
    """Widget that displays a 2D heatmap with colormap."""
    
    def __init__(self, title: str = "Heatmap", parent=None):
        super().__init__(parent)
        self.title = title
        self._data: Optional[np.ndarray] = None
        self._colormap_name = "viridis"
        self._colormap = get_colormap(self._colormap_name)
        self._auto_scale = True
        self._vmin = 0.0
        self._vmax = 1.0
        
        self._setup_ui()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        
        self._title_label = QLabel(self.title)
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self._title_label)
        
        self._image_label = QLabel()
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setMinimumSize(200, 150)
        self._image_label.setSizePolicy(QSizePolicy.Policy.Expanding, 
                                         QSizePolicy.Policy.Expanding)
        self._image_label.setStyleSheet("background-color: #1a1a2e; border: 1px solid #333;")
        layout.addWidget(self._image_label)
        
        self._stats_label = QLabel("No data")
        self._stats_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._stats_label.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(self._stats_label)
    
    def set_colormap(self, name: str):
        self._colormap_name = name
        self._colormap = get_colormap(name)
        self._update_display()
    
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
        self._image_label.setStyleSheet("background-color: #1a1a2e; border: 1px solid #333;")
        self._stats_label.setText("No data")
    
    def _update_display(self):
        if self._data is None:
            return
        
        display_data = self._data.T.astype(np.float32)
        
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
            Qt.TransformationMode.FastTransformation
        )
        
        self._image_label.setPixmap(scaled)


# =============================================================================
# Status Indicator Widget
# =============================================================================

class StatusIndicator(QWidget):
    """Small colored circle indicator with label."""
    
    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self._label = label
        self._connected = False
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(6)
        
        self._indicator = QLabel("●")
        self._indicator.setStyleSheet("color: #666; font-size: 14px;")
        layout.addWidget(self._indicator)
        
        self._label_widget = QLabel(label)
        layout.addWidget(self._label_widget)
        
        self._status_widget = QLabel("")
        self._status_widget.setStyleSheet("color: #888;")
        layout.addWidget(self._status_widget)
        
        layout.addStretch()
    
    def set_connected(self, connected: bool, status: str = ""):
        self._connected = connected
        
        if connected:
            self._indicator.setStyleSheet("color: #4ade80; font-size: 14px;")
        else:
            self._indicator.setStyleSheet("color: #666; font-size: 14px;")
        
        self._status_widget.setText(status)
    
    def set_streaming(self):
        self._indicator.setStyleSheet("color: #60a5fa; font-size: 14px;")


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
        layout.setSpacing(2)
        
        title_bar = QFrame()
        title_bar.setStyleSheet("background-color: #2d2d3d; border-radius: 4px 4px 0 0;")
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(8, 4, 8, 4)
        
        self._title_label = QLabel(self._title)
        self._title_label.setStyleSheet("color: #fff; font-weight: bold; font-size: 11px;")
        title_layout.addWidget(self._title_label)
        
        self._status_label = QLabel("not running")
        self._status_label.setStyleSheet("color: #888; font-size: 10px;")
        title_layout.addStretch()
        title_layout.addWidget(self._status_label)
        
        layout.addWidget(title_bar)
        
        self._output = QPlainTextEdit()
        self._output.setReadOnly(True)
        self._output.setMaximumBlockCount(self._max_lines)
        self._output.setStyleSheet("""
            QPlainTextEdit {
                background-color: #1a1a2e;
                color: #e0e0e0;
                font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
                font-size: 11px;
                border: 1px solid #333;
                border-top: none;
                border-radius: 0 0 4px 4px;
            }
        """)
        layout.addWidget(self._output)
    
    def append_text(self, text: str):
        self._output.appendPlainText(text.rstrip('\n'))
        scrollbar = self._output.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
    
    def clear(self):
        self._output.clear()
    
    def set_status(self, status: str, running: bool = False):
        self._status_label.setText(status)
        if running:
            self._status_label.setStyleSheet("color: #4ade80; font-size: 10px;")
        else:
            self._status_label.setStyleSheet("color: #888; font-size: 10px;")


# =============================================================================
# Statistics Panel Widget
# =============================================================================

class StatisticsPanel(QGroupBox):
    """Panel displaying acquisition statistics."""
    
    def __init__(self, parent=None):
        super().__init__("Statistics", parent)
        self._setup_ui()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        
        self._stats = {}
        stat_names = [
            ("pixel_rate", "Pixel Rate:"),
            ("tdc1_rate", "TDC1 Rate:"),
            ("tdc2_rate", "TDC2 Rate:"),
            ("elapsed", "Elapsed:"),
            ("remaining", "Remaining:"),
            ("flushes", "Flushes:"),
            ("total_cycles", "Total Cycles:"),
            ("avg_counts", "Avg Counts/Cycle:"),
        ]
        
        for key, label in stat_names:
            row = QHBoxLayout()
            name_label = QLabel(label)
            name_label.setStyleSheet("color: #888;")
            value_label = QLabel("--")
            value_label.setStyleSheet("font-family: monospace;")
            row.addWidget(name_label)
            row.addStretch()
            row.addWidget(value_label)
            layout.addLayout(row)
            self._stats[key] = value_label
    
    def update_serval_stats(self, pixel_rate: float, tdc1_rate: float, 
                            tdc2_rate: float, elapsed: float, remaining: float):
        self._stats["pixel_rate"].setText(f"{pixel_rate:.2e} cps")
        self._stats["tdc1_rate"].setText(f"{tdc1_rate:.1f} Hz")
        self._stats["tdc2_rate"].setText(f"{tdc2_rate:.1f} Hz")
        
        elapsed_str = f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}"
        remaining_str = f"{int(remaining // 60):02d}:{int(remaining % 60):02d}"
        self._stats["elapsed"].setText(elapsed_str)
        self._stats["remaining"].setText(remaining_str)
    
    def update_flush_stats(self, flush_number: int, total_cycles: int, 
                           avg_counts: float):
        self._stats["flushes"].setText(str(flush_number))
        self._stats["total_cycles"].setText(f"{total_cycles:,}")
        self._stats["avg_counts"].setText(f"{avg_counts:.2e}")
    
    def clear(self):
        for label in self._stats.values():
            label.setText("--")
            