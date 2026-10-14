"""Crash-safe file writes shared by the UI preferences, the parquet sink and the acquisition script.

Every writer here follows the same recipe: write the full content to a sibling
temporary file, flush and ``fsync`` it, then ``os.replace`` it onto the final
name. A reader can therefore never observe a half-written file — it sees either
the previous version or the new one. ``fsync`` failures are tolerated (tmpfs in
containers), because ``os.replace`` is atomic on POSIX regardless.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Union

PathLike = Union[str, Path]

_TMP_SUFFIX = ".tmp"


def fsync_file(path: PathLike) -> None:
    """``fsync`` a file by path (opens it read-only; works for files another handle wrote)."""
    fd = os.open(os.fspath(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def fsync_dir(path: PathLike) -> None:
    """``fsync`` a directory so a rename inside it is durable. Best effort."""
    try:
        fd = os.open(os.fspath(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: PathLike, data: bytes, *, sync_dir: bool = True) -> Path:
    """Write ``data`` to ``path`` atomically. Creates the parent directory. Returns the path."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + _TMP_SUFFIX)
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass
    os.replace(tmp, p)
    if sync_dir:
        fsync_dir(p.parent)
    return p


def atomic_write_text(path: PathLike, text: str, *, encoding: str = "utf-8", sync_dir: bool = True) -> Path:
    """Text variant of :func:`atomic_write_bytes`."""
    return atomic_write_bytes(path, text.encode(encoding), sync_dir=sync_dir)


def atomic_write_json(path: PathLike, obj: Any, *, indent: Optional[int] = 2, sort_keys: bool = True) -> Path:
    """Serialise ``obj`` as JSON (trailing newline) and write it atomically."""
    payload = json.dumps(obj, indent=indent, sort_keys=sort_keys, default=_json_default) + "\n"
    return atomic_write_text(path, payload)


def read_json(path: PathLike, default: Any = None) -> Any:
    """Load JSON from ``path``; return ``default`` if the file is missing or not valid JSON."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return default


def merge_json_file(path: PathLike, updates: Dict[str, Any]) -> Dict[str, Any]:
    """Read-merge-write: shallow-merge ``updates`` into the JSON object stored at ``path``.

    A missing or unreadable file counts as ``{}``. The merged object is written
    atomically and returned. Used by two processes that each own one top-level
    key of the same file (UI → ``user``, ``acq.py`` → ``detector``); they never
    run concurrently, so no cross-process lock is taken.
    """
    current = read_json(path, default={})
    if not isinstance(current, dict):
        current = {}
    merged: Dict[str, Any] = {**current, **updates}
    atomic_write_json(path, merged)
    return merged


def _json_default(obj: Any) -> Any:
    """Fallback encoder: paths and numpy scalars become plain JSON types."""
    if isinstance(obj, Path):
        return str(obj)
    for attr in ("item", "tolist"):  # numpy scalar / array
        fn = getattr(obj, attr, None)
        if callable(fn):
            return fn()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
