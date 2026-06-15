"""Centroider tab - sweep tpx3 clustering parameters and compare the results.

The scientist picks a .tpx3 file and a list of eps-s (pixel) and eps-t (time)
values. Pressing "Centroid" runs the centroider sweep (tools/centroider) over
the full eps-s x eps-t grid, producing one clustered .h5 per combination plus a
PixelHits baseline. As each run finishes its x-histogram is loaded and overlaid
on the summary plot so the best eps-s / eps-t can be chosen by eye.

All sweep orchestration / I/O lives in the centroider backend; this tab only
builds the UI and relays the CentroiderWorker's signals.
"""

import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .workers import CentroiderWorker, import_centroider_api

logger = logging.getLogger(__name__)

# Apply dark theme defaults to pyqtgraph (process-global; matches alignment tab).
pg.setConfigOptions(background=theme.BG_DARK, foreground=theme.TEXT_PRIMARY, antialias=False)

# Curve colors cycled across combinations (baseline uses a separate grey).
_CURVE_COLORS = [
    theme.BLUE_LIGHT_1,
    theme.TERTIARY_GREEN,
    theme.TERTIARY_YELLOW,
    theme.TERTIARY_ORANGE,
    theme.TERTIARY_RED,
    theme.TERTIARY_PURPLE,
    theme.TERTIARY_TEAL,
    theme.BLUE_LIGHT_2,
    theme.TERTIARY_MUD,
    theme.BLUE_LIGHT_3,
]

# Number of steps the waterfall offset divides the visible dynamic range into.
_WATERFALL_DIVISIONS = 10


def _format_eta(seconds: Optional[float]) -> str:
    if seconds is None:
        return "estimating..."
    s = int(max(0.0, seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


class _ComboCell(QWidget):
    """One cell of the combinations grid: a show/hide checkbox + status label."""

    def __init__(self, eps_s: int, eps_t: str, on_toggled, parent=None):
        super().__init__(parent)
        self.eps_s = eps_s
        self.eps_t = eps_t

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(2)

        self._checkbox = QCheckBox("show")
        self._checkbox.setChecked(True)
        self._checkbox.setEnabled(False)
        self._checkbox.toggled.connect(lambda _checked: on_toggled())
        layout.addWidget(self._checkbox)

        self._status = QLabel("queued")
        self._status.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 10px;")
        layout.addWidget(self._status)

    def is_checked(self) -> bool:
        return self._checkbox.isChecked() and self._checkbox.isEnabled()

    def set_running(self) -> None:
        self._status.setText("running...")
        self._status.setStyleSheet(f"color: {theme.STATUS_STREAMING}; font-size: 10px;")

    def set_status(self, status: str, wall_seconds: float = 0.0) -> None:
        if status == "ok":
            self._status.setText(f"ok ({wall_seconds:.1f}s)")
            self._status.setStyleSheet(f"color: {theme.STATUS_OK}; font-size: 10px;")
            self._checkbox.setEnabled(True)
        elif status == "skipped":
            self._status.setText("cached")
            self._status.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 10px;")
            self._checkbox.setEnabled(True)
        else:
            self._status.setText("failed")
            self._status.setStyleSheet(f"color: {theme.STATUS_ERROR}; font-size: 10px;")
            self._checkbox.setEnabled(False)


class CentroiderTab(QWidget):
    """Tab for sweeping clustering parameters and comparing clustered outputs."""

    def __init__(self, parent=None):
        super().__init__(parent)

        self._worker: Optional[CentroiderWorker] = None

        # Plot state.
        # baseline: dict with xs/counts/item, or None until the PixelHits run finishes.
        self._baseline: Optional[dict] = None
        # combo curves keyed by (eps_s, eps_t); _combo_order preserves insertion order.
        self._combo_curves: Dict[Tuple[int, str], dict] = {}
        self._combo_order: List[Tuple[int, str]] = []
        # grid cells keyed by (eps_s, eps_t).
        self._cells: Dict[Tuple[int, str], _ComboCell] = {}

        self._setup_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        root.addWidget(self._build_inputs_group())
        root.addWidget(self._build_progress_group())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_grid_group())
        splitter.addWidget(self._build_plot_group())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([420, 900])
        root.addWidget(splitter, stretch=1)

    def _build_inputs_group(self) -> QGroupBox:
        group = QGroupBox("Inputs")
        group.setStyleSheet(theme.group_box_style())
        layout = QVBoxLayout(group)
        layout.setContentsMargins(12, 16, 12, 12)
        layout.setSpacing(8)

        # --- TPX3 file picker ---
        file_row = QHBoxLayout()
        file_label = QLabel("TPX3 File")
        file_label.setMinimumWidth(110)
        file_label.setStyleSheet(f"color: {theme.TEXT_PRIMARY};")
        self._file_input = QLineEdit()
        self._file_input.setPlaceholderText("/path/to/data.tpx3")
        self._file_input.setStyleSheet(theme.input_style())
        browse_btn = QPushButton("Browse")
        browse_btn.setStyleSheet(theme.secondary_button_style())
        browse_btn.clicked.connect(self._browse_file)
        file_row.addWidget(file_label)
        file_row.addWidget(self._file_input, stretch=1)
        file_row.addWidget(browse_btn)
        layout.addLayout(file_row)

        # --- eps-s ---
        eps_s_row = QHBoxLayout()
        eps_s_label = QLabel("eps-s (pixels)")
        eps_s_label.setMinimumWidth(110)
        eps_s_label.setStyleSheet(f"color: {theme.TEXT_PRIMARY};")
        self._eps_s_input = QLineEdit("1,2,3")
        self._eps_s_input.setPlaceholderText("comma-separated positive ints, e.g. 1,2,3")
        self._eps_s_input.setStyleSheet(theme.input_style())
        eps_s_row.addWidget(eps_s_label)
        eps_s_row.addWidget(self._eps_s_input, stretch=1)
        layout.addLayout(eps_s_row)

        # --- eps-t ---
        eps_t_row = QHBoxLayout()
        eps_t_label = QLabel("eps-t (ns)")
        eps_t_label.setMinimumWidth(110)
        eps_t_label.setStyleSheet(f"color: {theme.TEXT_PRIMARY};")
        self._eps_t_input = QLineEdit("20,100,500")
        self._eps_t_input.setPlaceholderText("comma-separated integers in ns, e.g. 20,100,500")
        self._eps_t_input.setStyleSheet(theme.input_style())
        eps_t_row.addWidget(eps_t_label)
        eps_t_row.addWidget(self._eps_t_input, stretch=1)
        layout.addLayout(eps_t_row)

        # --- Centroid / Stop buttons ---
        _BTN_WIDTH = 110
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._centroid_btn = QPushButton("Centroid")
        self._centroid_btn.setFixedWidth(_BTN_WIDTH)
        self._centroid_btn.setStyleSheet(theme.button_style(theme.BUTTON_START))
        self._centroid_btn.clicked.connect(self._on_centroid_clicked)
        btn_row.addWidget(self._centroid_btn)
        self._stop_btn = QPushButton("\u23f9 Stop")
        self._stop_btn.setFixedWidth(_BTN_WIDTH)
        self._stop_btn.setStyleSheet(theme.button_style(theme.BUTTON_STOP))
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop_clicked)
        btn_row.addWidget(self._stop_btn)
        layout.addLayout(btn_row)

        return group

    def _build_progress_group(self) -> QGroupBox:
        group = QGroupBox("Progress")
        group.setStyleSheet(theme.group_box_style())
        layout = QVBoxLayout(group)
        layout.setContentsMargins(12, 16, 12, 12)
        layout.setSpacing(6)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setStyleSheet(
            f"""
            QProgressBar {{
                background-color: {theme.BG_DARK};
                color: {theme.TEXT_PRIMARY};
                border: 1px solid {theme.BORDER_SUBTLE};
                border-radius: 4px;
                text-align: center;
                height: 18px;
            }}
            QProgressBar::chunk {{
                background-color: {theme.BLUE_PRIMARY};
                border-radius: 3px;
            }}
            """
        )
        layout.addWidget(self._progress_bar)

        self._status_label = QLabel("Idle. Pick a .tpx3 file and press Centroid.")
        self._status_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 11px;")
        layout.addWidget(self._status_label)

        return group

    def _build_grid_group(self) -> QGroupBox:
        group = QGroupBox("Combinations (check to show in plot)")
        group.setStyleSheet(theme.group_box_style())
        layout = QVBoxLayout(group)
        layout.setContentsMargins(12, 16, 12, 12)

        self._grid = QTableWidget(0, 0)
        self._grid.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._grid.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._grid.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._grid.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._grid.setStyleSheet(
            f"""
            QTableWidget {{
                background-color: {theme.BG_DARK};
                color: {theme.TEXT_PRIMARY};
                gridline-color: {theme.BORDER_SUBTLE};
                border: 1px solid {theme.BORDER_SUBTLE};
            }}
            QHeaderView::section {{
                background-color: {theme.BG_PANEL};
                color: {theme.TEXT_PRIMARY};
                padding: 4px;
                border: none;
                font-weight: bold;
            }}
            """
        )
        layout.addWidget(self._grid)

        return group

    def _build_plot_group(self) -> QGroupBox:
        group = QGroupBox("Summary plot")
        group.setStyleSheet(theme.group_box_style())
        layout = QVBoxLayout(group)
        layout.setContentsMargins(12, 16, 12, 12)
        layout.setSpacing(6)

        toolbar = QHBoxLayout()
        self._waterfall_btn = QPushButton("Waterfall")
        self._waterfall_btn.setCheckable(True)
        self._waterfall_btn.setStyleSheet(theme.checkable_button_style())
        self._waterfall_btn.setToolTip(
            "Offset each visible curve vertically by a constant step\n"
            f"(visible range / {_WATERFALL_DIVISIONS}) so overlapping histograms separate."
        )
        self._waterfall_btn.toggled.connect(lambda _checked: self._refresh_plot())
        toolbar.addWidget(self._waterfall_btn)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self._plot = pg.PlotWidget()
        self._plot.setBackground(theme.BG_DARK)
        plot_item = self._plot.getPlotItem()
        plot_item.showGrid(x=True, y=True, alpha=0.2)
        plot_item.setLabel("bottom", "y (pixel / cluster centroid — dispersive axis)")
        plot_item.setLabel("left", "counts")
        self._legend = plot_item.addLegend(offset=(-10, 10))
        layout.addWidget(self._plot, stretch=1)

        return group

    # ------------------------------------------------------------------
    # Input handling
    # ------------------------------------------------------------------

    def _browse_file(self) -> None:
        current = self._file_input.text() or str(Path.home())
        start_dir = current if Path(current).is_dir() else str(Path(current).parent)
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select TPX3 File", start_dir, "TPX3 Files (*.tpx3);;All Files (*)"
        )
        if file_path:
            self._file_input.setText(file_path)

    def _parse_eps_lists(self) -> Optional[Tuple[List[int], List[str]]]:
        """Validate eps-s / eps-t via the centroider backend; show errors inline.

        The eps-t field accepts plain integers (e.g. ``20,100,500``); the ``ns``
        suffix is appended here before validation so the user never has to type it.
        """
        try:
            api = import_centroider_api()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Centroider unavailable", f"Could not load centroider backend:\n{exc}")
            return None
        try:
            eps_s = api._normalize_eps_s(self._eps_s_input.text())
            # Append "ns" to each token so the user only enters bare integers.
            raw_t = self._eps_t_input.text()
            tokens_with_ns = ",".join(
                t.strip() + "ns" if t.strip() and not t.strip().endswith("ns") else t.strip()
                for t in raw_t.split(",")
                if t.strip()
            )
            eps_t = api._normalize_eps_t(tokens_with_ns)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid parameters", str(exc))
            return None
        return eps_s, eps_t

    def _on_centroid_clicked(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return

        file_path = self._file_input.text().strip()
        if not file_path:
            QMessageBox.warning(self, "Missing file", "Please select a .tpx3 file first.")
            return
        if not Path(file_path).exists():
            QMessageBox.warning(self, "File not found", f"Input file does not exist:\n{file_path}")
            return

        parsed = self._parse_eps_lists()
        if parsed is None:
            return
        eps_s_list, eps_t_list = parsed

        self._reset_outputs(eps_s_list, eps_t_list)
        self._set_running(True)

        self._worker = CentroiderWorker(
            input_file=file_path,
            eps_s=",".join(str(s) for s in eps_s_list),
            eps_t=",".join(eps_t_list),
            parent=self,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.combo_finished.connect(self._on_combo_finished)
        self._worker.sweep_finished.connect(self._on_sweep_finished)
        self._worker.error_occurred.connect(self._on_error)
        self._worker.start()

    def _on_stop_clicked(self) -> None:
        if self._worker is None or not self._worker.isRunning():
            return
        self._stop_btn.setEnabled(False)
        self._status_label.setText("Stopping — waiting for tpx3dump to exit...")
        self._worker.stop()

    # ------------------------------------------------------------------
    # Output / grid setup
    # ------------------------------------------------------------------

    def _reset_outputs(self, eps_s_list: List[int], eps_t_list: List[str]) -> None:
        # Clear plot state.
        self._plot.clear()
        if self._legend is not None:
            self._legend.clear()
        self._baseline = None
        self._combo_curves.clear()
        self._combo_order.clear()
        self._cells.clear()

        # Build the 2D grid: rows = eps-s, columns = eps-t.
        self._grid.clear()
        self._grid.setRowCount(len(eps_s_list))
        self._grid.setColumnCount(len(eps_t_list))
        self._grid.setHorizontalHeaderLabels([f"t={t}" for t in eps_t_list])
        self._grid.setVerticalHeaderLabels([f"s={s}" for s in eps_s_list])

        for r, eps_s in enumerate(eps_s_list):
            for c, eps_t in enumerate(eps_t_list):
                key = (eps_s, eps_t)
                cell = _ComboCell(eps_s, eps_t, on_toggled=self._refresh_plot)
                self._cells[key] = cell
                self._grid.setCellWidget(r, c, cell)

        self._progress_bar.setRange(0, max(1, len(eps_s_list) * len(eps_t_list) + 1))
        self._progress_bar.setValue(0)

    def _set_running(self, running: bool) -> None:
        self._centroid_btn.setEnabled(not running)
        self._stop_btn.setEnabled(running)
        self._file_input.setEnabled(not running)
        self._eps_s_input.setEnabled(not running)
        self._eps_t_input.setEnabled(not running)
        self._centroid_btn.setText("Centroiding..." if running else "Centroid")

    # ------------------------------------------------------------------
    # Worker signal handlers
    # ------------------------------------------------------------------

    @Slot(object)
    def _on_progress(self, event) -> None:
        """A run is starting (begin event)."""
        self._progress_bar.setMaximum(max(1, event.total))
        self._status_label.setText(
            f"[{event.index}/{event.total}] running {event.label}  |  ETA {_format_eta(event.eta_seconds)}"
        )
        if event.eps_s is not None and event.eps_t is not None:
            cell = self._cells.get((event.eps_s, event.eps_t))
            if cell is not None:
                cell.set_running()

    @Slot(object)
    def _on_combo_finished(self, event) -> None:
        """A run finished (status set)."""
        self._progress_bar.setMaximum(max(1, event.total))
        self._progress_bar.setValue(event.index)
        self._status_label.setText(
            f"[{event.index}/{event.total}] {event.label}: {event.status}  |  ETA {_format_eta(event.eta_seconds)}"
        )

        is_baseline = event.eps_t is None or event.eps_s is None

        if not is_baseline:
            cell = self._cells.get((event.eps_s, event.eps_t))
            if cell is not None:
                cell.set_status(event.status, event.wall_seconds)

        if event.status in ("ok", "skipped") and event.h5_path is not None:
            self._load_curve(event, is_baseline)

    @Slot(object)
    def _on_sweep_finished(self, result) -> None:
        self._set_running(False)
        n_ok = sum(1 for r in result.results if r.status == "ok")
        n_failed = sum(1 for r in result.results if r.status == "failed")
        n_skipped = sum(1 for r in result.results if r.status == "skipped")
        n_cancelled = sum(1 for r in result.results if r.status == "cancelled")
        was_cancelled = n_cancelled > 0 or (
            self._worker is not None and self._worker._cancel_event.is_set()
        )
        if was_cancelled:
            msg = f"Stopped. {n_ok} ok, {n_skipped} cached, {n_failed} failed, {n_cancelled} cancelled."
        else:
            self._progress_bar.setValue(self._progress_bar.maximum())
            msg = f"Done. {n_ok} ok, {n_skipped} cached, {n_failed} failed. Output: {result.run_dir}"
        self._status_label.setText(msg)
        self._refresh_plot()

    @Slot(str)
    def _on_error(self, message: str) -> None:
        self._set_running(False)
        self._status_label.setText(f"Error: {message}")
        QMessageBox.critical(self, "Centroider error", message)

    # ------------------------------------------------------------------
    # Plot management
    # ------------------------------------------------------------------

    def _load_curve(self, event, is_baseline: bool) -> None:
        try:
            api = import_centroider_api()
            xs, counts, label = api.load_histogram(event.h5_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load histogram for %s: %s", event.h5_path, exc)
            return

        xs = np.asarray(xs, dtype=float)
        counts = np.asarray(counts, dtype=float)

        if is_baseline:
            item = self._plot.plot([], [], name="PixelHits (before)", pen=pg.mkPen(color=theme.TEXT_SECONDARY, width=2))
            self._baseline = {"xs": xs, "counts": counts, "item": item, "label": "PixelHits (before)"}
        else:
            key = (event.eps_s, event.eps_t)
            color = _CURVE_COLORS[len(self._combo_order) % len(_CURVE_COLORS)]
            item = self._plot.plot([], [], name=label, pen=pg.mkPen(color=color, width=1))
            self._combo_curves[key] = {"xs": xs, "counts": counts, "item": item, "label": label}
            self._combo_order.append(key)

        self._refresh_plot()

    def _visible_curves(self) -> List[dict]:
        """Curves currently selected for display, baseline first."""
        visible: List[dict] = []
        if self._baseline is not None:
            visible.append(self._baseline)
        for key in self._combo_order:
            curve = self._combo_curves[key]
            cell = self._cells.get(key)
            if cell is not None and cell.is_checked():
                visible.append(curve)
        return visible

    def _refresh_plot(self) -> None:
        visible = self._visible_curves()
        visible_items = {id(c["item"]) for c in visible}

        # Hide everything not currently selected.
        for curve in self._all_curves():
            if id(curve["item"]) not in visible_items:
                curve["item"].setVisible(False)

        # Compute the per-curve waterfall offset step from the visible range.
        step = 0.0
        if self._waterfall_btn.isChecked() and visible:
            gmax = max(float(np.max(c["counts"])) for c in visible if c["counts"].size)
            gmin = min(float(np.min(c["counts"])) for c in visible if c["counts"].size)
            step = (gmax - gmin) / _WATERFALL_DIVISIONS
            if step <= 0:
                step = 1.0

        for i, curve in enumerate(visible):
            offset = i * step
            curve["item"].setData(curve["xs"], curve["counts"] + offset)
            curve["item"].setVisible(True)

    def _all_curves(self) -> List[dict]:
        curves: List[dict] = []
        if self._baseline is not None:
            curves.append(self._baseline)
        for key in self._combo_order:
            curves.append(self._combo_curves[key])
        return curves

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Stop the worker thread and any in-flight tpx3dump process (called on app close)."""
        if self._worker is not None and self._worker.isRunning():
            self._worker.stop()
            # Give tpx3dump up to 5 s to die (SIGTERM + SIGKILL already handled in runner).
            self._worker.wait(5000)
