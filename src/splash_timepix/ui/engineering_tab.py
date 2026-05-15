"""Engineering tab - process monitoring and debugging interface."""

import logging

from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from . import theme
from .widgets import TerminalWidget

logger = logging.getLogger(__name__)

# Map QProcess / LogManager source keys → terminal widget keys.
# simulator shares the source panel with live-cli (mirrors the merged UI panel).
_SOURCE_TO_TERMINAL = {
    "simulator": "live-cli",
    "zmq-backend": "zmq-backend",
    "system": "system",
    "serval": "serval",
    "streaming": "streaming",
    "live-cli": "live-cli",
    "acquisition": "acquisition",
}


class EngineeringTab(QWidget):
    """Logs tab — 2×3 grid of per-subsystem terminal panels.

    Layout:
        [Serval]      [Streaming]   [live-cli / simulator]
        [Acquisition] [ZMQ Backend] [System Logs]

    The merged "All Logs (Integrated)" view lives in the separate Timeline tab.
    """

    kill_all_requested = Signal()
    clear_logs_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._terminals: dict[str, TerminalWidget] = {}
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # Toolbar
        toolbar = QHBoxLayout()

        kill_all_btn = QPushButton("Kill All Processes")
        kill_all_btn.setStyleSheet(theme.button_style(theme.BUTTON_STOP))
        kill_all_btn.clicked.connect(self.kill_all_requested.emit)
        toolbar.addWidget(kill_all_btn)

        clear_btn = QPushButton("Clear Logs")
        clear_btn.setStyleSheet(theme.secondary_button_style())
        clear_btn.clicked.connect(self._clear_all_logs)
        toolbar.addWidget(clear_btn)

        toolbar.addStretch()

        self._status_label = QLabel("No processes running")
        self._status_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        toolbar.addWidget(self._status_label)

        layout.addLayout(toolbar)

        # Terminal grid (2x3)
        grid = QGridLayout()
        grid.setSpacing(6)

        # Row 1
        self._terminals["serval"] = TerminalWidget("Serval Server")
        self._terminals["streaming"] = TerminalWidget("Streaming Server (app.py)")
        self._terminals["live-cli"] = TerminalWidget("live-cli / simulator")

        grid.addWidget(self._terminals["serval"], 0, 0)
        grid.addWidget(self._terminals["streaming"], 0, 1)
        grid.addWidget(self._terminals["live-cli"], 0, 2)

        # Row 2
        self._terminals["acquisition"] = TerminalWidget("Acquisition (acq.py)")
        self._terminals["zmq-backend"] = TerminalWidget("ZMQ Backend")
        self._terminals["system"] = TerminalWidget("System Logs")

        grid.addWidget(self._terminals["acquisition"], 1, 0)
        grid.addWidget(self._terminals["zmq-backend"], 1, 1)
        grid.addWidget(self._terminals["system"], 1, 2)

        self._terminals["system"].set_status("logs", False)

        for i in range(3):
            grid.setColumnStretch(i, 1)
        for i in range(2):
            grid.setRowStretch(i, 1)

        layout.addLayout(grid)

    def _clear_all_logs(self):
        for terminal in self._terminals.values():
            terminal.clear()
        self.clear_logs_requested.emit()

    def _terminal_key(self, source: str) -> str:
        return _SOURCE_TO_TERMINAL.get(source, source)

    @Slot(str, str)
    def on_log_line(self, source: str, formatted_line: str) -> None:
        """Receive a pre-formatted line from LogManager and route it.

        ``formatted_line`` already contains the ISO timestamp and source tag —
        it is written verbatim to the per-source terminal widget and to the
        integrated "All Logs" view.  No additional timestamp prefix is added
        by TerminalWidget (see widgets.py).
        """
        key = self._terminal_key(source)
        target = self._terminals.get(key, self._terminals["system"])
        target.append_text(formatted_line)

    @Slot(str, bool)
    def set_process_status(self, process_name: str, running: bool):
        key = self._terminal_key(process_name)
        if key in self._terminals:
            status = "running" if running else "stopped"
            self._terminals[key].set_status(status, running)
        self._update_status_summary()

    def set_zmq_thread_status(self, state: str) -> None:
        """ZMQ subscriber thread lifecycle: not running | running | stopped."""
        term = self._terminals["zmq-backend"]
        term.set_status(state, state == "running")
        self._update_status_summary()

    def _update_status_summary(self):
        running = []
        for name, terminal in self._terminals.items():
            text = terminal._status_label.text().lower()
            if text == "running":
                running.append(name)

        if running:
            self._status_label.setText(f"Running: {', '.join(running)}")
            self._status_label.setStyleSheet(f"color: {theme.STATUS_OK};")
        else:
            self._status_label.setText("No processes running")
            self._status_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")

    def clear_terminal(self, source: str):
        if source in self._terminals:
            self._terminals[source].clear()
