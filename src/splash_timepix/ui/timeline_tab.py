"""Timeline tab — integrated, time-ordered view of all subsystem logs.

Every line that passes through ``LogManager`` (all six sources) appears here
in arrival order, already carrying the ISO-8601 timestamp + ``[source]`` tag.
This gives a single scrollable transcript that can be used to reconstruct the
exact sequence of events across subsystems without opening individual files.
"""

import logging

from PySide6.QtCore import Slot
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from . import theme
from .widgets import TerminalWidget

logger = logging.getLogger(__name__)


class TimelineTab(QWidget):
    """Full-width integrated log view (All Logs, time-ordered)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(8, 8, 8, 8)

        self._terminal = TerminalWidget("All Logs (Integrated)")
        self._terminal.set_status("live", True)

        # Toolbar
        toolbar = QHBoxLayout()

        info = QLabel("All subsystem logs — time-ordered, ISO-8601 timestamps")
        info.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 11px;")
        toolbar.addWidget(info)

        toolbar.addStretch()

        clear_btn = QPushButton("Clear View")
        clear_btn.setStyleSheet(theme.secondary_button_style())
        clear_btn.clicked.connect(self._terminal.clear)
        toolbar.addWidget(clear_btn)

        layout.addLayout(toolbar)
        layout.addWidget(self._terminal)

    @Slot(str, str)
    def on_log_line(self, source: str, formatted_line: str) -> None:
        """Append a pre-formatted line from LogManager to the integrated view."""
        self._terminal.append_text(formatted_line)

    def clear(self):
        """Clear the terminal widget (called when the Logs tab clears all)."""
        self._terminal.clear()
