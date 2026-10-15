"""Shared set-up for the UI tests.

Some modules in this package build widgets (``test_save_on_stop.py``), others only need
``QCoreApplication`` (``test_log_manager.py``). Qt allows exactly one application object per
process and a ``QCoreApplication`` cannot be upgraded to a ``QApplication`` afterwards, so the
first module to run would decide for everybody — and a widget built on a bare
``QCoreApplication`` aborts the whole pytest process. This conftest is imported before any
test module in the package is collected, so it creates the widget-capable ``QApplication``
(offscreen, headless-safe) once; the modules' ``QCoreApplication.instance() or ...`` guards then
find and reuse it. Without PySide6 it does nothing; each module skips itself.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # ui extra not installed: the test modules importorskip themselves
    QApplication = None

if QApplication is not None:
    _qapp = QApplication.instance() or QApplication(sys.argv[:1])
