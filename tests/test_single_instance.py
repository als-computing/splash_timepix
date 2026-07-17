"""Tests for the Linux-only per-user single-instance lock.

These tests cover:

- Successful acquire / release round-trip.
- ``O_CLOEXEC`` is set on the held FD (so spawned children — Serval,
  streaming-server, live-cli, simulator — don't inherit the flock).
- The PID written into ``ui.lock`` is the holder's PID.
- ``flock`` contention raises :class:`AlreadyRunning` with the holder's PID.
- ``psutil`` cmdline / uid checks reject unrelated processes.

Pure-Python tests where possible — flock contention is simulated with a
second FD opened from the same process (per ``man 2 flock``: "An attempt
to lock the file using one of these file descriptors may be denied by a
lock that the calling process has already placed via another file
descriptor.").
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Linux-only by design — flock semantics on macOS/Windows differ enough
# that the production code does not enforce there. Skipping early avoids
# accidental import-time failures of fcntl on hostile platforms.
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="single-instance is Linux-only")

import fcntl  # noqa: E402

from splash_timepix.ui import single_instance  # noqa: E402


def _can_signal_subprocess() -> bool:
    """Return True if this process can SIGTERM a subprocess it spawned.

    Some sandboxed environments restrict ptrace/signal delivery even to
    child processes owned by the same UID.  The four tests that spawn a
    ``sleep`` helper and then SIGTERM it are meaningless in those
    environments and should be skipped rather than fail noisily.
    """
    try:
        proc = subprocess.Popen(["sleep", "5"])
        time.sleep(0.05)
        os.kill(proc.pid, signal.SIGTERM)
        proc.wait(timeout=2)
        return True
    except PermissionError:
        return False
    except Exception:
        return False


_HAS_SIGNAL_PERMISSION = _can_signal_subprocess()
_skip_no_signal = pytest.mark.skipif(
    not _HAS_SIGNAL_PERMISSION,
    reason="cannot SIGTERM own subprocess in this environment (sandbox restriction)",
)


@pytest.fixture(autouse=True)
def _release_held_lock_after_test():
    """Release any module-held FD so tests don't leak state across cases."""
    yield
    single_instance.release_lock()


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    return tmp_path / "ui.lock"


def test_acquire_returns_fd(lock_path: Path):
    fd = single_instance.acquire_lock(lock_path)
    assert isinstance(fd, int)
    assert fd >= 0


def test_acquire_idempotent_within_process(lock_path: Path):
    """A second call from the same process returns the already-held FD."""
    fd1 = single_instance.acquire_lock(lock_path)
    fd2 = single_instance.acquire_lock(lock_path)
    assert fd1 == fd2


def test_acquire_release_acquire_round_trip(lock_path: Path):
    """After release, the next acquire succeeds (simulates clean restart)."""
    single_instance.acquire_lock(lock_path)
    single_instance.release_lock()
    fd = single_instance.acquire_lock(lock_path)
    assert isinstance(fd, int)


def test_lock_fd_has_o_cloexec(lock_path: Path):
    """The held FD must not be inherited by spawned children.

    Without ``O_CLOEXEC`` (or ``set_inheritable(False)``), children like
    Serval / streaming-server would keep the lock alive past the UI's
    death — a fresh launch would then see the lock as busy until those
    children also exit.
    """
    fd = single_instance.acquire_lock(lock_path)
    flags = fcntl.fcntl(fd, fcntl.F_GETFD)
    assert flags & fcntl.FD_CLOEXEC, "lock FD must have FD_CLOEXEC set"


def test_pid_written_to_file_is_holders_pid(lock_path: Path):
    """The lock file contains our PID after acquisition (read via separate FD)."""
    single_instance.acquire_lock(lock_path)
    pid_from_file = single_instance.other_instance_pid(lock_path)
    assert pid_from_file == os.getpid()


def test_contention_raises_already_running_with_pid(lock_path: Path):
    """A second acquire while another FD holds the flock raises with the PID.

    Simulated within one process: open + flock a sentinel FD that is *not*
    the one ``acquire_lock`` will use, then write a fake PID into the file
    (mirroring what a real first instance would have done). The next
    ``acquire_lock`` call must see the contention and surface that PID.
    """
    sentinel_pid = 424242

    holder_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(holder_fd, str(sentinel_pid).encode("ascii"))

        with pytest.raises(single_instance.AlreadyRunning) as exc:
            single_instance.acquire_lock(lock_path)
        assert exc.value.pid == sentinel_pid
    finally:
        try:
            fcntl.flock(holder_fd, fcntl.LOCK_UN)
        finally:
            os.close(holder_fd)


def test_contention_with_empty_lock_file_yields_pid_none(lock_path: Path):
    """Lock held mid-startup (file empty) → AlreadyRunning(pid=None).

    The plan calls out this race window explicitly: the holder grabs flock
    *before* writing its PID, so a second instance arriving in that gap
    sees the file empty. The Kill UX must be disabled in that case
    (``main._show_already_running_dialog`` does the right thing).
    """
    holder_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Deliberately do not write a PID.

        with pytest.raises(single_instance.AlreadyRunning) as exc:
            single_instance.acquire_lock(lock_path)
        assert exc.value.pid is None
    finally:
        try:
            fcntl.flock(holder_fd, fcntl.LOCK_UN)
        finally:
            os.close(holder_fd)


def test_other_instance_pid_returns_none_for_missing_file(tmp_path: Path):
    assert single_instance.other_instance_pid(tmp_path / "absent.lock") is None


def test_other_instance_pid_returns_none_for_unparseable(lock_path: Path):
    lock_path.write_text("not a pid\n")
    assert single_instance.other_instance_pid(lock_path) is None


def test_is_other_instance_alive_rejects_dead_pid():
    """A PID that has never existed (very large) is not a live instance."""
    # PID_MAX on Linux defaults to 2^22; pick something well above any real PID.
    bogus_pid = 4_194_303
    assert single_instance.is_other_instance_alive(bogus_pid) is False


@_skip_no_signal
def test_is_other_instance_alive_rejects_unrelated_cmdline():
    """A live process whose cmdline lacks our markers is rejected.

    This is the PID-reuse mitigation: even if the lock file's PID happens
    to match an unrelated process, the cmdline check ensures we never
    SIGTERM something that isn't ours.
    """
    proc = subprocess.Popen(["sleep", "10"])
    try:
        # Give the kernel a moment to set up cmdline.
        time.sleep(0.05)
        assert single_instance.is_other_instance_alive(proc.pid) is False
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@_skip_no_signal
def test_is_other_instance_alive_accepts_matching_marker():
    """When markers match the cmdline, a live same-uid process is accepted."""
    proc = subprocess.Popen(["sleep", "10"])
    try:
        time.sleep(0.05)
        assert single_instance.is_other_instance_alive(proc.pid, app_markers=("sleep",)) is True
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@_skip_no_signal
def test_terminate_other_instance_refuses_unrelated_process():
    """Refuse SIGTERM if cmdline doesn't match our markers (no signal sent)."""
    proc = subprocess.Popen(["sleep", "10"])
    try:
        time.sleep(0.05)
        terminated = single_instance.terminate_other_instance(proc.pid, timeout_s=0.2)
        assert terminated is False
        # Process must still be alive — we refused to signal it.
        assert proc.poll() is None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@_skip_no_signal
def test_terminate_other_instance_signals_matching_process():
    """When markers match, SIGTERM is delivered and the process exits."""
    proc = subprocess.Popen(["sleep", "10"])
    try:
        time.sleep(0.05)
        terminated = single_instance.terminate_other_instance(proc.pid, timeout_s=3.0, app_markers=("sleep",))
        assert terminated is True
        # Reap the now-dead process so it doesn't leak as a zombie.
        proc.wait(timeout=1)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_lock_path_under_xdg_config_home(monkeypatch, tmp_path: Path):
    """``lock_path()`` honors XDG_CONFIG_HOME (shared with preferences)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert single_instance.lock_path() == tmp_path / "splash_timepix" / "ui.lock"
