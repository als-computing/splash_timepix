"""Unified log manager — captures all subsystem output to dated files on disk.

All six subsystems (Serval, Streaming, live-cli/simulator, Acquisition,
ZMQ Backend, System) funnel through a single ``LogManager`` instance.

On-disk layout::

    <repo>/logs/
    └── YYYY-MM-DD/
        ├── all.log            # merged, time-ordered, [source]-tagged
        ├── serval.log
        ├── streaming.log
        ├── live-cli.log
        ├── acquisition.log
        ├── zmq-backend.log
        └── system.log

Every line (on disk and via the ``line_emitted`` signal) uses:

    2026-05-15T10:07:42.318-0700 [streaming] Server started on tcp://*:5657

Midnight rollover is automatic — ``append()`` detects a date change and
opens the new day's folder without requiring a restart.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Repo root resolution — same anchor used by ProcessManager (workers.py:313)
# ---------------------------------------------------------------------------
_MODULE_DIR = Path(__file__).parent  # …/ui/
_REPO_ROOT = _MODULE_DIR.parent.parent.parent.parent  # …/splash_timepix_dev/
_LOG_ROOT_CANDIDATE = _REPO_ROOT / "logs"

# Fallback when package is installed as a wheel (no writeable in-tree dir).
_LOG_ROOT_FALLBACK = Path.home() / ".local" / "state" / "splash_timepix" / "logs"

# Timestamp format for each line.
_TS_FMT = "%Y-%m-%dT%H:%M:%S"

# Known source → file-stem mapping (anything not listed maps to its own name).
_SOURCE_TO_FILE: dict[str, str] = {
    "serval": "serval",
    "streaming": "streaming",
    "live-cli": "live-cli",
    "simulator": "live-cli",  # mirrors the merged UI panel
    "acquisition": "acquisition",
    "zmq-backend": "zmq-backend",
    "system": "system",
}


def _log_root() -> Path:
    """Return the root logs directory, falling back to XDG state home."""
    candidate = _LOG_ROOT_CANDIDATE
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        # Verify we can actually write there.
        probe = candidate / ".write_probe"
        probe.touch()
        probe.unlink()
        return candidate
    except OSError:
        return _LOG_ROOT_FALLBACK


def _format_line(source: str, line: str) -> str:
    """Return ``line`` wrapped in an ISO-8601 timestamp + source tag."""
    now = datetime.now().astimezone()
    ms = f"{now.microsecond // 1000:03d}"
    tz = now.strftime("%z")
    return f"{now.strftime(_TS_FMT)}.{ms}{tz} [{source}] {line}"


# ---------------------------------------------------------------------------
# LogManager
# ---------------------------------------------------------------------------


class LogManager(QObject):
    """Central routing point for all subsystem log output.

    Signals
    -------
    line_emitted(source, formatted_line)
        Emitted for every non-empty line that passes through ``append()``.
        ``source`` is the raw source key (e.g. ``"streaming"``).
        ``formatted_line`` already includes the ISO timestamp + source tag
        and is identical to what is written to disk.
    """

    line_emitted = Signal(str, str)  # (source, formatted_line)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)

        self._lock = threading.Lock()
        self._log_root: Optional[Path] = None
        self._current_date: str = ""  # "YYYY-MM-DD"
        self._handles: dict[str, object] = {}  # stem → file handle
        self._all_handle: Optional[object] = None

        self._using_fallback = False
        self._install_python_logging_bridge()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append(self, source: str, text: str) -> None:
        """Append ``text`` (possibly multi-line) under ``source``.

        Thread-safe — may be called from QProcess callbacks or background
        threads.  Each call holds the lock only for the duration of the
        disk writes for the lines within ``text``.
        """
        stripped = text.rstrip("\n")
        if not stripped:
            return

        lines = stripped.split("\n")
        stem = _SOURCE_TO_FILE.get(source, source)

        with self._lock:
            self._ensure_today_open()
            for line in lines:
                if not line.strip():  # skip blank / whitespace-only lines
                    continue
                formatted = _format_line(source, line)
                self._write_line(stem, formatted)
                # Emit outside the lock would be safer for Qt but the signal
                # emission is non-blocking here (direct connection from main
                # thread triggers queued delivery on UI thread).  Emitting
                # inside the lock ensures ordering matches disk ordering.
                self.line_emitted.emit(source, formatted)

    def session_marker(self, message: str = "--- session marker ---") -> None:
        """Write a separator line to all open files (called on Clear Logs)."""
        with self._lock:
            self._ensure_today_open()
            formatted = _format_line("system", message)
            for stem in list(self._handles.keys()):
                self._write_line(stem, formatted)
            self.line_emitted.emit("system", formatted)

    def close(self) -> None:
        """Flush and close all open file handles (call from closeEvent)."""
        with self._lock:
            self._close_handles()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_today_open(self) -> None:
        """Open (or reopen after midnight) today's log folder/files."""
        today = datetime.now().strftime("%Y-%m-%d")
        if today == self._current_date and self._handles:
            return

        self._close_handles()
        self._current_date = today

        # Resolve log root once; cache it.
        if self._log_root is None:
            self._log_root = _log_root()
            using_fallback = self._log_root != _LOG_ROOT_CANDIDATE
            if using_fallback and not self._using_fallback:
                self._using_fallback = True
                logger.warning(
                    "logs/ not writeable at %s; falling back to %s",
                    _LOG_ROOT_CANDIDATE,
                    self._log_root,
                )

        day_dir = self._log_root / today
        try:
            day_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("Cannot create log dir %s: %s", day_dir, exc)
            return

        # Open per-source files (append mode so multiple sessions per day
        # accumulate in the same file rather than overwriting).
        stems = set(_SOURCE_TO_FILE.values())
        for stem in stems:
            path = day_dir / f"{stem}.log"
            try:
                self._handles[stem] = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
            except OSError as exc:
                logger.error("Cannot open log file %s: %s", path, exc)

        # The merged "all.log" is handled separately so we can keep it open
        # alongside the per-source handles.
        all_path = day_dir / "all.log"
        try:
            self._all_handle = open(all_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        except OSError as exc:
            logger.error("Cannot open all.log at %s: %s", all_path, exc)
            self._all_handle = None

    def _write_line(self, stem: str, formatted: str) -> None:
        """Write *formatted* to the per-source file and to all.log."""
        line_nl = formatted + "\n"
        fh = self._handles.get(stem)
        if fh is not None:
            try:
                fh.write(line_nl)
            except OSError as exc:
                logger.error("Log write failed (%s): %s", stem, exc)

        if self._all_handle is not None:
            try:
                self._all_handle.write(line_nl)
            except OSError as exc:
                logger.error("Log write failed (all.log): %s", exc)

    def _close_handles(self) -> None:
        for fh in self._handles.values():
            try:
                fh.flush()
                fh.close()
            except OSError:
                pass
        self._handles.clear()

        if self._all_handle is not None:
            try:
                self._all_handle.flush()
                self._all_handle.close()
            except OSError:
                pass
            self._all_handle = None

    # ------------------------------------------------------------------
    # Python logging bridge
    # ------------------------------------------------------------------

    def _install_python_logging_bridge(self) -> None:
        """Attach a Handler to the root logger that feeds into this manager."""
        handler = _QtLoggingHandler(self)
        handler.setLevel(logging.DEBUG)
        # Avoid adding duplicates if LogManager is ever re-instantiated.
        root = logging.getLogger()
        for existing in list(root.handlers):
            if isinstance(existing, _QtLoggingHandler):
                root.removeHandler(existing)
        root.addHandler(handler)


# ---------------------------------------------------------------------------
# Python logging → LogManager bridge
# ---------------------------------------------------------------------------


class _QtLoggingHandler(logging.Handler):
    """Logging handler that routes Python log records into a ``LogManager``.

    Records from UI-internal loggers (``main``, ``workers``, ``operator_tab``,
    etc.) are written to ``system.log`` so they appear in the System Logs
    terminal and in ``all.log``.
    """

    # Avoid re-entrancy: if LogManager.append itself triggers a log record
    # (e.g. an OSError caught with logger.error) we must not recurse.
    _in_emit = threading.local()

    def __init__(self, manager: LogManager) -> None:
        super().__init__()
        self._manager = manager
        fmt = logging.Formatter("%(name)s - %(levelname)s - %(message)s")
        self.setFormatter(fmt)

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._in_emit, "active", False):
            return
        self._in_emit.active = True
        try:
            msg = self.format(record)
            self._manager.append("system", msg)
        except Exception:  # noqa: BLE001
            self.handleError(record)
        finally:
            self._in_emit.active = False
