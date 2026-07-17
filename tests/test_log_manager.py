"""Tests for LogManager — disk persistence, rollover, dispatch, and formatting.

These tests are pure Python; they do not instantiate Qt widgets or start a
QApplication, keeping the test suite fast and headless-friendly.

``LogManager`` is a QObject, so a minimal QCoreApplication is created once for
the module.  All signal emissions in the tests use direct (synchronous)
connections so we can inspect results without an event-loop spin.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
from datetime import datetime
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Minimal QCoreApplication — must be created before any QObject
# ---------------------------------------------------------------------------
import pytest

pytest.importorskip(
    "PySide6",
    reason="PySide6 (ui extra) not installed; LogManager tests need QtCore",
)

from PySide6.QtCore import QCoreApplication  # noqa: E402

_qapp = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])

from splash_timepix.ui.log_manager import LogManager, _format_line, _QtLoggingHandler  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{4} \[.+?\] .+$")


def _collect_emissions(manager: LogManager) -> list[tuple[str, str]]:
    """Return a list of (source, formatted_line) received via line_emitted."""
    received: list[tuple[str, str]] = []
    manager.line_emitted.connect(lambda src, line: received.append((src, line)))
    return received


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


class TestFormatLine:
    def test_iso_format_matches_regex(self):
        line = _format_line("streaming", "Server started")
        assert _ISO_RE.match(line), f"Format mismatch: {line!r}"

    def test_source_tag_present(self):
        line = _format_line("serval", "chip temps: 28.1")
        assert "[serval]" in line

    def test_message_present(self):
        msg = "some unique message 99"
        line = _format_line("system", msg)
        assert msg in line

    def test_millisecond_precision(self):
        line = _format_line("acquisition", "done")
        # After the time part there should be a dot followed by exactly 3 digits
        assert re.search(r"T\d{2}:\d{2}:\d{2}\.\d{3}", line)

    def test_timezone_offset_present(self):
        line = _format_line("zmq-backend", "flush #1")
        # ends with something like +0000 or -0700 before the space
        assert re.search(r"[+-]\d{4} ", line)


# ---------------------------------------------------------------------------
# append() — per-source file dispatch
# ---------------------------------------------------------------------------


class TestAppendDispatch:
    def test_streaming_writes_streaming_log(self, tmp_path):
        with patch("splash_timepix.ui.log_manager._LOG_ROOT_CANDIDATE", tmp_path):
            mgr = LogManager()
            mgr.append("streaming", "hello streaming")
            mgr.close()

        today = datetime.now().strftime("%Y-%m-%d")
        log_file = tmp_path / today / "streaming.log"
        assert log_file.exists(), "streaming.log not created"
        content = log_file.read_text()
        assert "hello streaming" in content

    def test_streaming_does_not_write_serval_log(self, tmp_path):
        with patch("splash_timepix.ui.log_manager._LOG_ROOT_CANDIDATE", tmp_path):
            mgr = LogManager()
            mgr.append("streaming", "exclusive line")
            mgr.close()

        today = datetime.now().strftime("%Y-%m-%d")
        serval_file = tmp_path / today / "serval.log"
        if serval_file.exists():
            assert "exclusive line" not in serval_file.read_text()

    def test_all_log_receives_every_source(self, tmp_path):
        with patch("splash_timepix.ui.log_manager._LOG_ROOT_CANDIDATE", tmp_path):
            mgr = LogManager()
            mgr.append("serval", "serval line")
            mgr.append("streaming", "streaming line")
            mgr.append("zmq-backend", "zmq line")
            mgr.close()

        today = datetime.now().strftime("%Y-%m-%d")
        all_log = tmp_path / today / "all.log"
        assert all_log.exists()
        content = all_log.read_text()
        assert "serval line" in content
        assert "streaming line" in content
        assert "zmq line" in content

    def test_simulator_maps_to_live_cli_log(self, tmp_path):
        with patch("splash_timepix.ui.log_manager._LOG_ROOT_CANDIDATE", tmp_path):
            mgr = LogManager()
            mgr.append("simulator", "sim output")
            mgr.close()

        today = datetime.now().strftime("%Y-%m-%d")
        live_file = tmp_path / today / "live-cli.log"
        assert live_file.exists()
        assert "sim output" in live_file.read_text()

    def test_multiline_text_each_line_formatted(self, tmp_path):
        with patch("splash_timepix.ui.log_manager._LOG_ROOT_CANDIDATE", tmp_path):
            mgr = LogManager()
            mgr.append("system", "line one\nline two\nline three")
            mgr.close()

        today = datetime.now().strftime("%Y-%m-%d")
        sys_file = tmp_path / today / "system.log"
        lines = [ln for ln in sys_file.read_text().splitlines() if ln.strip()]
        assert len(lines) == 3, f"Expected 3 lines, got {lines}"
        for line in lines:
            assert _ISO_RE.match(line), f"Bad format: {line!r}"

    def test_blank_text_produces_no_output(self, tmp_path):
        with patch("splash_timepix.ui.log_manager._LOG_ROOT_CANDIDATE", tmp_path):
            mgr = LogManager()
            mgr.append("system", "   \n\n")
            mgr.close()

        today = datetime.now().strftime("%Y-%m-%d")
        sys_file = tmp_path / today / "system.log"
        if sys_file.exists():
            assert sys_file.read_text().strip() == ""


# ---------------------------------------------------------------------------
# line_emitted signal
# ---------------------------------------------------------------------------


class TestLineEmitted:
    def test_signal_emitted_for_each_line(self, tmp_path):
        with patch("splash_timepix.ui.log_manager._LOG_ROOT_CANDIDATE", tmp_path):
            mgr = LogManager()
            received = _collect_emissions(mgr)
            mgr.append("serval", "alpha\nbeta")
            mgr.close()

        # Filter out any system records emitted by the Python logging bridge
        serval_lines = [line for src, line in received if src == "serval"]
        assert len(serval_lines) == 2

    def test_signal_source_matches_input(self, tmp_path):
        with patch("splash_timepix.ui.log_manager._LOG_ROOT_CANDIDATE", tmp_path):
            mgr = LogManager()
            received = _collect_emissions(mgr)
            mgr.append("acquisition", "done")
            mgr.close()

        acq_lines = [line for src, line in received if src == "acquisition"]
        assert acq_lines, "No acquisition lines received"

    def test_signal_formatted_line_is_iso(self, tmp_path):
        with patch("splash_timepix.ui.log_manager._LOG_ROOT_CANDIDATE", tmp_path):
            mgr = LogManager()
            received = _collect_emissions(mgr)
            mgr.append("streaming", "check format")
            mgr.close()

        for src, line in received:
            if src == "streaming":
                assert _ISO_RE.match(line), f"Bad format in signal: {line!r}"


# ---------------------------------------------------------------------------
# Midnight rollover
# ---------------------------------------------------------------------------


class TestRollover:
    def test_rollover_creates_new_day_folder(self, tmp_path):
        day1 = "2026-01-01"
        day2 = "2026-01-02"

        with patch("splash_timepix.ui.log_manager._LOG_ROOT_CANDIDATE", tmp_path):
            mgr = LogManager()

            fake_day1 = datetime(2026, 1, 1, 12, 0, 0).astimezone()
            fake_day2 = datetime(2026, 1, 2, 0, 0, 1).astimezone()

            with patch("splash_timepix.ui.log_manager.datetime") as mock_dt:
                mock_dt.now.return_value = fake_day1
                mgr.append("system", "day 1 message")

                mock_dt.now.return_value = fake_day2
                mgr.append("system", "day 2 message")

            mgr.close()

        assert (tmp_path / day1).is_dir(), f"{day1}/ not created"
        assert (tmp_path / day2).is_dir(), f"{day2}/ not created"

    def test_rollover_content_is_correct_day(self, tmp_path):
        day1 = "2026-02-28"
        day2 = "2026-03-01"

        with patch("splash_timepix.ui.log_manager._LOG_ROOT_CANDIDATE", tmp_path):
            mgr = LogManager()

            fake_day1 = datetime(2026, 2, 28, 23, 59, 59).astimezone()
            fake_day2 = datetime(2026, 3, 1, 0, 0, 0).astimezone()

            with patch("splash_timepix.ui.log_manager.datetime") as mock_dt:
                mock_dt.now.return_value = fake_day1
                mgr.append("streaming", "before midnight")

                mock_dt.now.return_value = fake_day2
                mgr.append("streaming", "after midnight")

            mgr.close()

        content_day1 = (tmp_path / day1 / "streaming.log").read_text()
        content_day2 = (tmp_path / day2 / "streaming.log").read_text()
        assert "before midnight" in content_day1
        assert "after midnight" not in content_day1
        assert "after midnight" in content_day2
        assert "before midnight" not in content_day2


# ---------------------------------------------------------------------------
# Thread safety — no interleaved bytes within a single line
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_concurrent_appends_produce_complete_lines(self, tmp_path):
        """Two threads writing simultaneously must not produce split lines."""
        n_lines = 50

        with patch("splash_timepix.ui.log_manager._LOG_ROOT_CANDIDATE", tmp_path):
            mgr = LogManager()

            def write_source(source: str):
                for i in range(n_lines):
                    mgr.append(source, f"{source}-msg-{i}")

            t1 = threading.Thread(target=write_source, args=("serval",))
            t2 = threading.Thread(target=write_source, args=("streaming",))
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            mgr.close()

        today = datetime.now().strftime("%Y-%m-%d")
        all_log = tmp_path / today / "all.log"
        for raw_line in all_log.read_text().splitlines():
            line = raw_line.strip()
            if not line:
                continue
            assert _ISO_RE.match(line), f"Partial/corrupt line: {line!r}"


# ---------------------------------------------------------------------------
# session_marker
# ---------------------------------------------------------------------------


class TestSessionMarker:
    def test_marker_written_to_all_open_files(self, tmp_path):
        with patch("splash_timepix.ui.log_manager._LOG_ROOT_CANDIDATE", tmp_path):
            mgr = LogManager()
            mgr.append("serval", "before clear")
            mgr.session_marker("--- cleared by user ---")
            mgr.close()

        today = datetime.now().strftime("%Y-%m-%d")
        all_content = (tmp_path / today / "all.log").read_text()
        assert "cleared by user" in all_content

    def test_marker_emitted_as_system_signal(self, tmp_path):
        with patch("splash_timepix.ui.log_manager._LOG_ROOT_CANDIDATE", tmp_path):
            mgr = LogManager()
            received = _collect_emissions(mgr)
            mgr.session_marker("test marker")
            mgr.close()

        system_lines = [line for src, line in received if src == "system"]
        assert any("test marker" in ln for ln in system_lines)


# ---------------------------------------------------------------------------
# Python logging bridge
# ---------------------------------------------------------------------------


class TestLoggingBridge:
    def test_python_logger_reaches_system_log(self, tmp_path):
        with patch("splash_timepix.ui.log_manager._LOG_ROOT_CANDIDATE", tmp_path):
            mgr = LogManager()
            test_logger = logging.getLogger("test.bridge")
            test_logger.warning("bridge-test-warning-message")
            mgr.close()

        today = datetime.now().strftime("%Y-%m-%d")
        sys_file = tmp_path / today / "system.log"
        assert sys_file.exists()
        assert "bridge-test-warning-message" in sys_file.read_text()

    def test_no_duplicate_handlers_on_reinstantiation(self, tmp_path):
        """Creating two LogManagers must leave exactly one _QtLoggingHandler."""
        with patch("splash_timepix.ui.log_manager._LOG_ROOT_CANDIDATE", tmp_path):
            mgr1 = LogManager()
            mgr2 = LogManager()
            mgr1.close()
            mgr2.close()

        root = logging.getLogger()
        count = sum(1 for h in root.handlers if isinstance(h, _QtLoggingHandler))
        assert count == 1, f"Expected exactly one _QtLoggingHandler on the root logger, found {count}"
