"""Tests for camera_resolution + camera_quality settings.

Pins the contract for the two new settings introduced after the
field session that exposed how restrictive the hardcoded defaults
were (640×480 captures resized to ≈256×192, JPEG q=70 always). The
settings are read on every encode, so changes take effect on the
next camera_capture call without restart (resolution requires a
camera reset to re-open with the new size).

We don't drive a real camera here — just verify the helpers + the
preset table + that config.get() returns the right shapes.
"""

from __future__ import annotations

import importlib
import sys

import pytest


@pytest.fixture
def fresh(qwe_temp_data_dir):
    """Reload tools + config against a fresh QWE_DATA_DIR."""
    if "tools" in sys.modules:
        importlib.reload(sys.modules["tools"])
    import tools as t
    import config as c
    return t, c


def test_default_camera_resolution_is_auto(fresh):
    _, c = fresh
    assert c.get("camera_resolution") == "auto"


def test_default_camera_quality_is_70(fresh):
    _, c = fresh
    assert c.get("camera_quality") == 70


def test_presets_table_has_expected_entries(fresh):
    t, _ = fresh
    p = t._CAMERA_PRESETS
    assert set(p.keys()) == {"auto", "480p", "720p", "1080p"}
    # auto/480p use the legacy 49K cap for back-compat
    assert p["auto"] == (None, None, 49152)
    assert p["480p"] == (640, 480, 49152)
    # Higher tiers get progressively bigger caps so users picking
    # 1080p don't end up with the 256×192 default
    assert p["720p"] == (1280, 720, 196608)
    assert p["1080p"] == (1920, 1080, 786432)


def test_presets_max_area_grows_with_resolution(fresh):
    t, _ = fresh
    caps = [t._CAMERA_PRESETS[k][2] for k in ("auto", "480p", "720p", "1080p")]
    # Monotonically non-decreasing
    assert caps == sorted(caps)


def test_apply_camera_resolution_returns_preset_for_unknown_value(fresh, monkeypatch):
    """Bogus settings.camera_resolution should fall back to auto, not crash."""
    t, c = fresh
    monkeypatch.setattr(c, "get", lambda key, default=None: "WAT" if key == "camera_resolution" else default)

    # Build a stub VideoCapture that records .set() calls
    calls = []

    class _StubCap:
        def set(self, prop, val): calls.append((prop, val))

    w, h, cap_max = t._apply_camera_resolution(_StubCap())
    # Unknown → auto preset → no width/height, legacy cap
    assert (w, h) == (None, None)
    assert cap_max == 49152
    assert calls == [], "auto preset should not call cap.set"


def test_apply_camera_resolution_sets_width_height_for_720p(fresh, monkeypatch):
    """720p preset must call cap.set with WIDTH=1280, HEIGHT=720."""
    t, c = fresh
    monkeypatch.setattr(c, "get", lambda key, default=None: "720p" if key == "camera_resolution" else default)

    import cv2
    calls = []

    class _StubCap:
        def set(self, prop, val): calls.append((prop, val))

    w, h, cap_max = t._apply_camera_resolution(_StubCap())
    assert (w, h, cap_max) == (1280, 720, 196608)
    # Both width and height should have been set
    props_set = {prop: val for prop, val in calls}
    assert props_set.get(cv2.CAP_PROP_FRAME_WIDTH) == 1280
    assert props_set.get(cv2.CAP_PROP_FRAME_HEIGHT) == 720


def test_apply_camera_resolution_swallows_cap_set_errors(fresh, monkeypatch):
    """Some camera backends throw on cap.set — must not propagate up."""
    t, c = fresh
    monkeypatch.setattr(c, "get", lambda key, default=None: "1080p" if key == "camera_resolution" else default)

    class _BrokenCap:
        def set(self, prop, val):
            raise RuntimeError("backend doesn't support set()")

    # Must not raise — the exception is swallowed inside the helper
    w, h, cap_max = t._apply_camera_resolution(_BrokenCap())
    # And the preset values are still returned for the encoder
    assert (w, h, cap_max) == (1920, 1080, 786432)


def test_camera_quality_setting_is_clamped_to_1_100(fresh):
    """The encoder clamps the value to [1, 100]. Pin via config range
    metadata since the runtime clamping happens inside the OpenCV
    branch of camera_capture which we can't easily exercise without a
    real camera. config.EDITABLE_SETTINGS['camera_quality'] declares
    the bounds; verify they're correct so set_setting() rejects
    out-of-range writes via its own validator."""
    _, c = fresh
    spec = c.EDITABLE_SETTINGS["camera_quality"]
    # tuple shape: (kv_key, type, default, desc, min, max)
    assert spec[1] is int
    assert spec[2] == 70
    assert spec[4] == 1
    assert spec[5] == 100


def test_camera_resolution_setting_metadata(fresh):
    _, c = fresh
    spec = c.EDITABLE_SETTINGS["camera_resolution"]
    assert spec[1] is str
    assert spec[2] == "auto"
