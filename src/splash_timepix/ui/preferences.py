"""Persistent operator-sidebar preferences (JSON, atomic write, clamp on load).

The acquisition-settings sidebar in :class:`splash_timepix.ui.operator_tab.OperatorTab`
is restored from ``operator_prefs.json`` at startup and written back on a clean
quit. This module owns the on-disk format, the path resolution, and the
validate/clamp logic; the tab itself just calls :func:`load_operator_preferences`
and :func:`save_operator_preferences`.

Saves are atomic (write to ``.tmp``, then ``os.replace``) so a crash during
write cannot corrupt the existing file. Loads tolerate a missing file,
malformed JSON, and out-of-range / unknown values: the sanitized result is
always a complete, in-range preferences dict, never raising into the UI layer.

Crash-time saves are intentionally lost — JSON only updates on a clean exit.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Union

logger = logging.getLogger(__name__)

# Bumped from 1 → 2 when alignment_* keys were added. Purely informational —
# validate_and_clamp tolerates missing/unknown keys, so v1 files load cleanly.
PREFERENCES_VERSION = 2

# Combo *display text* values. These must match the items added to the combo
# boxes in OperatorTab._setup_ui, because restoration uses
# QComboBox.setCurrentText(...) which only matches displayed item text exactly.
TDC_CHANNEL_VALUES = ("Both", "1", "2")
TDC_EDGE_VALUES = ("Rising", "Falling")

# Numeric ranges, mirrored from the QSpinBox / QDoubleSpinBox setRange(...)
# calls in OperatorTab._setup_ui. Keep these in sync with the widget config.
TDC_FREQUENCY_RANGE = (0.1, 1e9)
CALLBACK_BATCH_SIZE_RANGE = (1, 10_000_000)
N_BINS_RANGE = (500, 50_000)
DURATION_RANGE = (1, 19_008_000)

# Alignment-tab numeric ranges, mirrored from AlignmentTab._setup_ui.
ALIGNMENT_RATE_HZ_RANGE = (1, 30)
# Manual min/max levels span the practical range of uint32 alignment counts;
# these are sanity bounds, not perceptually-tuned defaults.
ALIGNMENT_LEVEL_RANGE = (0, 2**31 - 1)

_PREFS_FILENAME = "operator_prefs.json"
_TMP_SUFFIX = ".tmp"


def default_preferences() -> Dict[str, Any]:
    """Return a fresh preferences dict matching the widgets' built-in defaults."""
    return {
        "preferences_version": PREFERENCES_VERSION,
        "tdc_frequency": 1000.0,
        "tdc_channel_text": "Both",
        "tdc_edge_text": "Rising",
        "callback_batch_size": 10_000,
        "n_bins": 10_000,
        "duration": 60,
        "output_dir": str(Path.home() / "Desktop" / "data"),
        # Alignment-tab defaults. UI starts in auto-range with crosshair on,
        # 30 Hz update, latest-only display, linear (non-log) intensity.
        "alignment_rate_hz": 30,
        "alignment_auto_range": True,
        # manual_min/max are floats so they can express log-space levels too
        # (typical max ~5 in log10 space). They're only consulted in Manual
        # range mode; toggling Log forces Auto so the image stays visible.
        "alignment_manual_min": 0.0,
        "alignment_manual_max": 100.0,
        "alignment_log": False,
        # Binarize defaults ON: collapses any pixel > 0 to the brightest LUT
        # color so even single hits are unambiguously visible. Operators
        # routinely have very low count rates during initial alignment, where
        # the linear LUT renders nearly all pixels as near-black.
        "alignment_binarize": True,
        "alignment_show_integrated": False,
        "alignment_show_crosshair": True,
        # Alignment-only local simulator (synthetic flushes; no Serval/live-cli).
        "alignment_simulator": False,
    }


def config_dir() -> Path:
    """Resolve the per-user config dir, honoring ``XDG_CONFIG_HOME``."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "splash_timepix"


def operator_prefs_path(base: Optional[Path] = None) -> Path:
    """Path to ``operator_prefs.json`` under the config dir (or override)."""
    return (base if base is not None else config_dir()) / _PREFS_FILENAME


def _clamp_float(value: Any, lo: float, hi: float, fallback: float, name: str) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        logger.warning("Pref %s: cannot coerce %r to float; using default %s", name, value, fallback)
        return fallback
    if v != v:  # NaN
        logger.warning("Pref %s: NaN; using default %s", name, fallback)
        return fallback
    if v < lo:
        logger.warning("Pref %s: %s below min %s; clamping", name, v, lo)
        return lo
    if v > hi:
        logger.warning("Pref %s: %s above max %s; clamping", name, v, hi)
        return hi
    return v


def _clamp_int(value: Any, lo: int, hi: int, fallback: int, name: str) -> int:
    try:
        # Reject bools (which are ints in Python) and float-like inputs that
        # would silently truncate.
        if isinstance(value, bool):
            raise TypeError
        v = int(value)
        if isinstance(value, float) and not value.is_integer():
            raise TypeError
    except (TypeError, ValueError):
        logger.warning("Pref %s: cannot coerce %r to int; using default %s", name, value, fallback)
        return fallback
    if v < lo:
        logger.warning("Pref %s: %s below min %s; clamping", name, v, lo)
        return lo
    if v > hi:
        logger.warning("Pref %s: %s above max %s; clamping", name, v, hi)
        return hi
    return v


def _validate_choice(value: Any, choices: tuple, fallback: str, name: str) -> str:
    if isinstance(value, str) and value in choices:
        return value
    logger.warning("Pref %s: %r not in %s; using default %r", name, value, choices, fallback)
    return fallback


def _validate_output_dir(value: Any, fallback: str, name: str) -> str:
    if isinstance(value, str) and value.strip():
        return value
    logger.warning("Pref %s: %r not a non-empty string; using default %r", name, value, fallback)
    return fallback


def _validate_bool(value: Any, fallback: bool, name: str) -> bool:
    if isinstance(value, bool):
        return value
    logger.warning("Pref %s: %r not a bool; using default %r", name, value, fallback)
    return fallback


def validate_and_clamp(raw: Any) -> Dict[str, Any]:
    """Return a complete, in-range preferences dict from arbitrary input.

    Unknown keys are dropped silently; missing keys fall back to defaults;
    out-of-range values are clamped with a warning. Non-dict inputs (e.g. a
    JSON file containing a list) yield the full default set.
    """
    defaults = default_preferences()
    if not isinstance(raw, dict):
        if raw is not None:
            logger.warning("Preferences: top-level not a dict (%r); using defaults", type(raw).__name__)
        return defaults

    out: Dict[str, Any] = {"preferences_version": PREFERENCES_VERSION}

    out["tdc_frequency"] = _clamp_float(
        raw.get("tdc_frequency", defaults["tdc_frequency"]),
        *TDC_FREQUENCY_RANGE,
        fallback=defaults["tdc_frequency"],
        name="tdc_frequency",
    )
    out["tdc_channel_text"] = _validate_choice(
        raw.get("tdc_channel_text", defaults["tdc_channel_text"]),
        TDC_CHANNEL_VALUES,
        fallback=defaults["tdc_channel_text"],
        name="tdc_channel_text",
    )
    out["tdc_edge_text"] = _validate_choice(
        raw.get("tdc_edge_text", defaults["tdc_edge_text"]),
        TDC_EDGE_VALUES,
        fallback=defaults["tdc_edge_text"],
        name="tdc_edge_text",
    )
    out["callback_batch_size"] = _clamp_int(
        raw.get("callback_batch_size", defaults["callback_batch_size"]),
        *CALLBACK_BATCH_SIZE_RANGE,
        fallback=defaults["callback_batch_size"],
        name="callback_batch_size",
    )
    out["n_bins"] = _clamp_int(
        raw.get("n_bins", defaults["n_bins"]),
        *N_BINS_RANGE,
        fallback=defaults["n_bins"],
        name="n_bins",
    )
    out["duration"] = _clamp_int(
        raw.get("duration", defaults["duration"]),
        *DURATION_RANGE,
        fallback=defaults["duration"],
        name="duration",
    )
    out["output_dir"] = _validate_output_dir(
        raw.get("output_dir", defaults["output_dir"]),
        fallback=defaults["output_dir"],
        name="output_dir",
    )

    # Alignment-tab keys. Same flat namespace as operator keys; adding them here
    # rather than in a nested dict keeps the JSON shape and existing read/write
    # codepaths simple.
    out["alignment_rate_hz"] = _clamp_int(
        raw.get("alignment_rate_hz", defaults["alignment_rate_hz"]),
        *ALIGNMENT_RATE_HZ_RANGE,
        fallback=defaults["alignment_rate_hz"],
        name="alignment_rate_hz",
    )
    out["alignment_auto_range"] = _validate_bool(
        raw.get("alignment_auto_range", defaults["alignment_auto_range"]),
        fallback=defaults["alignment_auto_range"],
        name="alignment_auto_range",
    )
    out["alignment_manual_min"] = _clamp_float(
        raw.get("alignment_manual_min", defaults["alignment_manual_min"]),
        float(ALIGNMENT_LEVEL_RANGE[0]),
        float(ALIGNMENT_LEVEL_RANGE[1]),
        fallback=float(defaults["alignment_manual_min"]),
        name="alignment_manual_min",
    )
    out["alignment_manual_max"] = _clamp_float(
        raw.get("alignment_manual_max", defaults["alignment_manual_max"]),
        float(ALIGNMENT_LEVEL_RANGE[0]),
        float(ALIGNMENT_LEVEL_RANGE[1]),
        fallback=float(defaults["alignment_manual_max"]),
        name="alignment_manual_max",
    )
    out["alignment_log"] = _validate_bool(
        raw.get("alignment_log", defaults["alignment_log"]),
        fallback=defaults["alignment_log"],
        name="alignment_log",
    )
    out["alignment_binarize"] = _validate_bool(
        raw.get("alignment_binarize", defaults["alignment_binarize"]),
        fallback=defaults["alignment_binarize"],
        name="alignment_binarize",
    )
    out["alignment_show_integrated"] = _validate_bool(
        raw.get("alignment_show_integrated", defaults["alignment_show_integrated"]),
        fallback=defaults["alignment_show_integrated"],
        name="alignment_show_integrated",
    )
    out["alignment_show_crosshair"] = _validate_bool(
        raw.get("alignment_show_crosshair", defaults["alignment_show_crosshair"]),
        fallback=defaults["alignment_show_crosshair"],
        name="alignment_show_crosshair",
    )
    out["alignment_simulator"] = _validate_bool(
        raw.get("alignment_simulator", defaults["alignment_simulator"]),
        fallback=defaults["alignment_simulator"],
        name="alignment_simulator",
    )
    return out


def load_operator_preferences(path: Optional[Union[Path, str]] = None) -> Dict[str, Any]:
    """Load and sanitize preferences from ``path`` (defaults to the user config).

    Never raises: a missing file, unreadable file, malformed JSON, or
    out-of-range values all collapse to the default-and-clamp path with a
    warning. The returned dict is always complete and in-range.
    """
    p = Path(path) if path is not None else operator_prefs_path()
    try:
        with open(p, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        logger.info("Preferences: %s does not exist; using defaults", p)
        return default_preferences()
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Preferences: failed to read %s (%s); using defaults", p, e)
        return default_preferences()
    result = validate_and_clamp(raw)
    logger.info("Preferences loaded from %s (%d top-level keys)", p, len(result))
    return result


def save_operator_preferences(prefs: Dict[str, Any], path: Optional[Union[Path, str]] = None) -> None:
    """Atomically persist ``prefs`` to ``path`` (defaults to user config).

    The supplied ``prefs`` dict is *merged* with the existing on-disk state
    (current file wins for keys not in ``prefs``, ``prefs`` wins where
    overlapping). This lets callers persist only their subset (e.g. just the
    operator-tab keys, or just the alignment-tab keys) without zeroing out
    keys owned by another part of the UI.

    Validates/clamps on the way out so a misuse in the UI layer cannot write
    a malformed file. Creates the parent directory if missing. Writes to
    ``<path>.tmp`` then ``os.replace`` into the final name so a crash mid-
    write cannot corrupt the existing file.
    """
    p = Path(path) if path is not None else operator_prefs_path()

    # Read current on-disk state (best-effort) so partial saves preserve other
    # tabs' keys. Any read failure collapses to an empty merge — equivalent to
    # the pre-merge behavior.
    existing: Dict[str, Any] = {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            existing = loaded
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        existing = {}

    merged: Dict[str, Any] = {**existing, **prefs}
    sanitized = validate_and_clamp(merged)

    parent = p.parent
    parent.mkdir(parents=True, exist_ok=True)

    tmp_path = p.with_name(p.name + _TMP_SUFFIX)
    payload = json.dumps(sanitized, indent=2, sort_keys=True) + "\n"

    # Write + fsync the tmp file before replacing so the rename target's
    # contents are guaranteed durable (modulo FS quirks). os.replace is
    # atomic on POSIX within a single filesystem.
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            # Some filesystems (tmpfs in containers) don't support fsync;
            # the replace below is still atomic on POSIX.
            pass
    os.replace(tmp_path, p)
    logger.info("Preferences saved to %s", p)
