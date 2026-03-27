"""Engineering tab - process monitoring and debugging interface."""

import logging

from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from . import theme
from .widgets import TerminalWidget

logger = logging.getLogger(__name__)

# Map QProcess names to terminal keys (simulator shares the source panel with live-cli).
_PROCESS_TO_TERMINAL = {
    "simulator": "live-cli",
}


class EngineeringTab(QWidget):
    """Engineering interface tab with 2x3 terminal grid.

    Layout:
        [Serval]      [Streaming]   [live-cli]
        [Acquisition] [ZMQ Backend] [System Logs]
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

    def _terminal_key(self, process_name: str) -> str:
        return _PROCESS_TO_TERMINAL.get(process_name, process_name)

    @Slot(str, str)
    def append_output(self, process_name: str, text: str):
        key = self._terminal_key(process_name)
        if key in self._terminals:
            self._terminals[key].append_text(text)
        else:
            self._terminals["system"].append_text(f"[{process_name}] {text}")

    @Slot(str)
    def append_system_log(self, text: str):
        self._terminals["system"].append_text(text)

    @Slot(str)
    def append_zmq_log(self, text: str):
        self._terminals["zmq-backend"].append_text(text)

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

    def clear_terminal(self, process_name: str):
        if process_name in self._terminals:
            self._terminals[process_name].clear()
