"""Linux-only per-user single-instance enforcement for the TimePix UI.

Acquires a non-blocking exclusive ``fcntl.flock`` on
``<config_dir>/ui.lock`` opened with ``O_CLOEXEC`` so the lock FD is not
inherited by child processes spawned by :class:`ProcessManager` (Serval,
streaming-server, live-cli, simulator). The kernel drops the flock on
process exit — clean, segfault, or SIGKILL — so a crashed previous run
never bricks the next launch.

The PID of the current holder is written into the lock file *only after*
acquiring the lock. The PID is consumed by the second-instance "Kill" UX
in ``main.py``; the authoritative "someone is running" signal is the
flock failure itself, not the file contents.

Non-Linux platforms (e.g. macOS dev boxes) are intentionally not
enforced: :func:`acquire_lock` returns ``None`` and :func:`other_instance_pid`
returns ``None``. The plan calls out Linux-only as a hard scope decision.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Iterable, Optional, Tuple, Union

import psutil

from .preferences import config_dir as _default_config_dir

logger = logging.getLogger(__name__)

LOCK_FILENAME = "ui.lock"

# Markers used to confirm the lock-file PID actually points at a TimePix UI
# process before signaling. Either the entry-point script name or the
# module path is sufficient — both are stable across the supported launch
# methods (``tpx-ui`` console-script and ``python -m splash_timepix.ui.main``).
_DEFAULT_APP_MARKERS: Tuple[str, ...] = ("tpx-ui", "splash_timepix.ui.main")

# Module-level reference keeps the held FD alive for the entire process
# lifetime. Closing the FD (via __del__, atexit, or accidental gc of a
# wrapper) would drop the lock prematurely. The kernel will drop it for us
# at exit; that is the desired behavior.
_held_fd: Optional[int] = None


class AlreadyRunning(RuntimeError):
    """Another process holds the singleton lock.

    ``pid`` is the PID written by the holder, or ``None`` if the file is
    empty / unparseable (which can happen during the holder's startup
    window between flock and write). Callers should treat ``None`` as
    "lock is held but PID unknown" and skip the Kill path.
    """

    def __init__(self, pid: Optional[int]):
        super().__init__(f"another instance is running (pid={pid})")
        self.pid = pid


def lock_path(base: Optional[Union[Path, str]] = None) -> Path:
    """Path to ``ui.lock`` under the config dir (or override)."""
    base_path = Path(base) if base is not None else _default_config_dir()
    return base_path / LOCK_FILENAME


def _is_linux() -> bool:
    return sys.platform == "linux"


def acquire_lock(path: Optional[Union[Path, str]] = None) -> Optional[int]:
    """Acquire the singleton lock; return the held FD or ``None`` (non-Linux).

    On Linux: opens ``path`` with ``O_RDWR | O_CREAT | O_CLOEXEC``,
    attempts ``fcntl.flock(LOCK_EX | LOCK_NB)``, writes the current PID
    on success, and stashes the FD in a module-global so it stays alive
    for the process lifetime. Subsequent calls in the same process are
    idempotent (returns the already-held FD).

    Raises :class:`AlreadyRunning` if another process holds the lock.

    On non-Linux platforms: logs a warning and returns ``None`` without
    enforcing anything (matches the plan's explicit Linux-only scope).
    """
    global _held_fd
    if not _is_linux():
        logger.warning("Single-instance lock not enforced on platform %r", sys.platform)
        return None

    if _held_fd is not None:
        return _held_fd

    import fcntl

    p = Path(path) if path is not None else lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(p, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        # Read the holder's PID from a fresh FD (no flock) so we never
        # block. The holder may be mid-startup with an empty file; we
        # tolerate that by returning pid=None.
        holder_pid = _read_pid(p)
        raise AlreadyRunning(holder_pid)
    except OSError:
        os.close(fd)
        raise

    try:
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))
        try:
            os.fsync(fd)
        except OSError:
            pass
    except OSError:
        # Releasing the lock on the way out is fine here: we never returned
        # success, so no caller is depending on it being held.
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        raise

    _held_fd = fd
    logger.info("Acquired single-instance lock at %s (pid=%d)", p, os.getpid())
    return fd


def release_lock() -> None:
    """Release the held lock if any (test-only; production relies on kernel).

    Production code should *not* call this — the kernel drops the lock at
    process exit, and an early release window would let a second instance
    start before child processes (Serval, live-cli) finish shutting down.
    Tests use this to simulate process death.
    """
    global _held_fd
    if _held_fd is None:
        return
    try:
        import fcntl

        fcntl.flock(_held_fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(_held_fd)
    except OSError:
        pass
    _held_fd = None


def _read_pid(path: Path) -> Optional[int]:
    try:
        with open(path, "r", encoding="ascii") as fh:
            text = fh.read().strip()
    except (FileNotFoundError, OSError):
        return None
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def other_instance_pid(path: Optional[Union[Path, str]] = None) -> Optional[int]:
    """Return the PID written in the lock file, or ``None`` if unavailable."""
    if not _is_linux():
        return None
    return _read_pid(Path(path) if path is not None else lock_path())


def _proc_is_live(proc: psutil.Process) -> bool:
    """Return True iff ``proc`` is running and not a zombie.

    ``psutil.Process.is_running()`` returns True for zombie processes
    (terminated but not yet reaped by the parent), which in our context
    means the process cannot accept further signals and is effectively
    gone. Treat that the same as "not running".
    """
    try:
        if not proc.is_running():
            return False
        return proc.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False


def is_other_instance_alive(
    pid: int,
    *,
    app_markers: Iterable[str] = _DEFAULT_APP_MARKERS,
) -> bool:
    """Return True iff ``pid`` is a live TimePix UI owned by the current user.

    Verifies (in order):
      1. ``psutil.Process(pid)`` resolves, is running, and is not a zombie.
      2. Real uid matches ``os.getuid()`` (no cross-user signaling).
      3. ``cmdline()`` contains one of ``app_markers`` (mitigates PID
         reuse: a recycled PID belonging to an unrelated process is
         vanishingly unlikely to match any of our entry-point names).

    Any psutil exception (NoSuchProcess, AccessDenied, ZombieProcess) is
    treated as "not our process" and returns False.
    """
    try:
        proc = psutil.Process(pid)
        if not _proc_is_live(proc):
            return False
        if proc.uids().real != os.getuid():
            return False
        cmdline = " ".join(proc.cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    return any(marker in cmdline for marker in app_markers)


def terminate_other_instance(
    pid: int,
    *,
    timeout_s: float = 5.0,
    poll_interval_s: float = 0.1,
    app_markers: Iterable[str] = _DEFAULT_APP_MARKERS,
) -> bool:
    """Send SIGTERM to ``pid`` if it looks like our app; wait up to ``timeout_s``.

    Returns ``True`` iff the target process is no longer running by the
    time we return. Refuses to signal anything that fails the
    :func:`is_other_instance_alive` checks — the caller can present that
    refusal to the user as "could not verify the other instance".

    SIGKILL escalation is **not** done here; that decision belongs to the
    UI layer (gated behind a confirmation prompt because the first
    instance may legitimately be inside Serval/live-cli teardown).
    """
    if not is_other_instance_alive(pid, app_markers=app_markers):
        logger.warning("Refusing to signal pid=%d: not a live TimePix UI process", pid)
        return False

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        logger.warning("Permission denied sending SIGTERM to pid=%d", pid)
        return False

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if not _proc_is_live(psutil.Process(pid)):
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(poll_interval_s)

    try:
        return not _proc_is_live(psutil.Process(pid))
    except psutil.NoSuchProcess:
        return True


def force_kill_other_instance(pid: int) -> bool:
    """Send SIGKILL to ``pid`` (use only after :func:`terminate_other_instance`).

    Same uid + cmdline guard as the SIGTERM path. Returns True if the
    process is gone after the signal.
    """
    if not is_other_instance_alive(pid):
        return True  # already gone

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        logger.warning("Permission denied sending SIGKILL to pid=%d", pid)
        return False

    # SIGKILL is uncatchable; the process is gone by the time the syscall
    # returns. A short poll handles zombie reaping race-windows.
    for _ in range(10):
        try:
            if not _proc_is_live(psutil.Process(pid)):
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.05)
    return False
