"""Tests for the operator-sidebar preferences module.

Covers the round-trip / malformed-JSON / clamp / atomic-write behavior
described in the plan. These tests exercise ``preferences.py`` directly
and do **not** instantiate Qt widgets — Qt is only loaded when a real
``OperatorTab`` is constructed in ``main.py``, and pulling it in here
would force PySide6 onto the test runner.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from splash_timepix.ui import preferences


def _full_prefs_payload() -> dict:
    """A complete, in-range preferences dict that round-trips identically.

    Must include every key that ``validate_and_clamp`` emits (operator subset
    *and* alignment subset); otherwise round-trip equality fails because the
    sanitized on-disk file fills missing keys with their defaults.
    """
    return {
        "preferences_version": preferences.PREFERENCES_VERSION,
        "tdc_frequency": 2500.5,
        "tdc_channel_text": "1",
        "tdc_edge_text": "Falling",
        "callback_batch_size": 5_000,
        "n_bins": 12_000,
        "duration": 120,
        "output_dir": "/tmp/splash_timepix_test_output",
        "alignment_rate_hz": 15,
        "alignment_auto_range": False,
        "alignment_manual_min": 5.0,
        "alignment_manual_max": 250.0,
        "alignment_log": True,
        "alignment_binarize": False,
        "alignment_show_integrated": True,
        "alignment_show_crosshair": False,
        "alignment_simulator": False,
    }


def test_default_preferences_self_consistent():
    """Defaults are themselves in-range; saving them yields no warnings."""
    defaults = preferences.default_preferences()
    sanitized = preferences.validate_and_clamp(defaults)
    assert sanitized == defaults


def test_round_trip_full_payload(tmp_path: Path):
    """A fully-populated payload survives save → load unchanged."""
    path = tmp_path / "operator_prefs.json"
    payload = _full_prefs_payload()
    preferences.save_operator_preferences(payload, path)
    loaded = preferences.load_operator_preferences(path)
    assert loaded == payload


def test_combo_text_persisted_verbatim(tmp_path: Path):
    """The combo text fields must be the exact display strings (case-sensitive).

    The OperatorTab restores combos via ``QComboBox.setCurrentText`` which
    matches displayed item text *exactly*; if we ever silently lowercase
    or normalize them on save, the restoration would silently no-op.
    """
    path = tmp_path / "operator_prefs.json"
    payload = _full_prefs_payload()
    payload["tdc_channel_text"] = "Both"
    payload["tdc_edge_text"] = "Rising"
    preferences.save_operator_preferences(payload, path)
    loaded = preferences.load_operator_preferences(path)
    assert loaded["tdc_channel_text"] == "Both"
    assert loaded["tdc_edge_text"] == "Rising"


def test_load_missing_file_returns_defaults(tmp_path: Path):
    """A missing file is not an error; defaults are returned."""
    path = tmp_path / "does_not_exist.json"
    loaded = preferences.load_operator_preferences(path)
    assert loaded == preferences.default_preferences()


def test_load_malformed_json_returns_defaults(tmp_path: Path):
    """A corrupt file does not raise into the UI; defaults are returned."""
    path = tmp_path / "operator_prefs.json"
    path.write_text("{this is not valid json")
    loaded = preferences.load_operator_preferences(path)
    assert loaded == preferences.default_preferences()


def test_load_non_dict_top_level_returns_defaults(tmp_path: Path):
    """A JSON top-level that isn't a dict (e.g. a list) collapses to defaults."""
    path = tmp_path / "operator_prefs.json"
    path.write_text(json.dumps([1, 2, 3]))
    loaded = preferences.load_operator_preferences(path)
    assert loaded == preferences.default_preferences()


def test_missing_keys_filled_from_defaults(tmp_path: Path):
    """Partial payloads keep specified values and fill the rest from defaults."""
    path = tmp_path / "operator_prefs.json"
    path.write_text(json.dumps({"tdc_frequency": 250.0, "n_bins": 750}))
    loaded = preferences.load_operator_preferences(path)
    assert loaded["tdc_frequency"] == 250.0
    assert loaded["n_bins"] == 750
    defaults = preferences.default_preferences()
    assert loaded["duration"] == defaults["duration"]
    assert loaded["tdc_channel_text"] == defaults["tdc_channel_text"]


@pytest.mark.parametrize(
    "key,bad_value,expected",
    [
        ("tdc_frequency", 0.0, preferences.TDC_FREQUENCY_RANGE[0]),
        ("tdc_frequency", 1e12, preferences.TDC_FREQUENCY_RANGE[1]),
        ("callback_batch_size", 0, preferences.CALLBACK_BATCH_SIZE_RANGE[0]),
        ("callback_batch_size", 99_999_999, preferences.CALLBACK_BATCH_SIZE_RANGE[1]),
        ("n_bins", 10, preferences.N_BINS_RANGE[0]),
        ("n_bins", 100_000, preferences.N_BINS_RANGE[1]),
        ("duration", -1, preferences.DURATION_RANGE[0]),
        ("duration", 100_000_000, preferences.DURATION_RANGE[1]),
    ],
)
def test_out_of_range_values_clamped(key: str, bad_value, expected, tmp_path: Path):
    """Out-of-range numeric values are clamped to the nearest in-range bound."""
    payload = _full_prefs_payload()
    payload[key] = bad_value
    path = tmp_path / "operator_prefs.json"
    path.write_text(json.dumps(payload))
    loaded = preferences.load_operator_preferences(path)
    assert loaded[key] == expected


@pytest.mark.parametrize(
    "key,bad_value",
    [
        ("tdc_channel_text", "Three"),
        ("tdc_channel_text", "BOTH"),
        ("tdc_channel_text", 1),
        ("tdc_edge_text", "rising"),
        ("tdc_edge_text", None),
    ],
)
def test_unknown_combo_value_falls_back_to_default(key: str, bad_value, tmp_path: Path):
    """Unknown / wrong-type combo values revert to defaults (case-sensitive)."""
    payload = _full_prefs_payload()
    payload[key] = bad_value
    path = tmp_path / "operator_prefs.json"
    path.write_text(json.dumps(payload))
    loaded = preferences.load_operator_preferences(path)
    assert loaded[key] == preferences.default_preferences()[key]


def test_non_numeric_value_falls_back_to_default(tmp_path: Path):
    """Strings where numbers are expected revert to default with a warning."""
    payload = _full_prefs_payload()
    payload["tdc_frequency"] = "fast"
    payload["n_bins"] = "many"
    path = tmp_path / "operator_prefs.json"
    path.write_text(json.dumps(payload))
    loaded = preferences.load_operator_preferences(path)
    defaults = preferences.default_preferences()
    assert loaded["tdc_frequency"] == defaults["tdc_frequency"]
    assert loaded["n_bins"] == defaults["n_bins"]


def test_unknown_keys_dropped(tmp_path: Path):
    """Extra keys in the JSON file are ignored (forward-compat with older readers)."""
    payload = _full_prefs_payload()
    payload["future_field"] = {"some": "blob"}
    payload["another_future_field"] = 42
    path = tmp_path / "operator_prefs.json"
    path.write_text(json.dumps(payload))
    loaded = preferences.load_operator_preferences(path)
    assert "future_field" not in loaded
    assert "another_future_field" not in loaded


def test_atomic_write_no_tmp_file_left(tmp_path: Path):
    """A successful save leaves no ``.tmp`` artefact behind."""
    path = tmp_path / "operator_prefs.json"
    preferences.save_operator_preferences(_full_prefs_payload(), path)
    leftover = path.with_name(path.name + ".tmp")
    assert path.exists()
    assert not leftover.exists()


def test_atomic_write_creates_parent_dir(tmp_path: Path):
    """Save creates the config directory if it does not yet exist."""
    path = tmp_path / "nested" / "deeper" / "operator_prefs.json"
    assert not path.parent.exists()
    preferences.save_operator_preferences(_full_prefs_payload(), path)
    assert path.exists()


def test_save_overwrites_existing_file(tmp_path: Path):
    """A second save replaces the file contents (replace semantics)."""
    path = tmp_path / "operator_prefs.json"
    a = _full_prefs_payload()
    b = _full_prefs_payload()
    b["duration"] = 999
    preferences.save_operator_preferences(a, path)
    preferences.save_operator_preferences(b, path)
    loaded = preferences.load_operator_preferences(path)
    assert loaded["duration"] == 999


def test_save_sanitizes_input_before_writing(tmp_path: Path):
    """save() writes through validate_and_clamp; bad inputs are not persisted."""
    path = tmp_path / "operator_prefs.json"
    payload = _full_prefs_payload()
    payload["duration"] = -1
    payload["tdc_channel_text"] = "Three"
    preferences.save_operator_preferences(payload, path)
    on_disk = json.loads(path.read_text())
    assert on_disk["duration"] == preferences.DURATION_RANGE[0]
    assert on_disk["tdc_channel_text"] == preferences.default_preferences()["tdc_channel_text"]


def test_config_dir_honors_xdg_config_home(monkeypatch, tmp_path: Path):
    """``config_dir()`` uses ``XDG_CONFIG_HOME`` when set."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert preferences.config_dir() == tmp_path / "splash_timepix"


def test_config_dir_falls_back_to_home_dot_config(monkeypatch, tmp_path: Path):
    """Without XDG_CONFIG_HOME, falls back to ``~/.config/splash_timepix``."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert preferences.config_dir() == tmp_path / ".config" / "splash_timepix"
