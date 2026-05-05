"""Public-Kiln-side coverage for ``_maybe_overlay_calibration``.

Pins three invariants of the slicer ↔ kiln-pro integration:

    1. Free tier (kiln-pro not importable) → silent no-op: returns
       the input overrides unchanged + cal_used=None.

    2. Pro tier (kiln-pro patched in) without ``input_path`` →
       calibration overlay applied, cal_used populated, NO slice
       event recorded (input_path is the gate for slice recording).

    3. Pro tier WITH ``input_path`` → calibration overlay applied
       AND ``pro_features.record_slice_for_input`` is called
       exactly once.  Failures inside the bridge call must NOT
       propagate (best-effort telemetry; never blocks slicing).

These mirror the kiln-pro-side tests in
``tests/test_slicer_calibration_hook.py`` but exercise the
public-Kiln slicer module directly so a regression in the helper's
parameter wiring fails CI on the public-Kiln branch even when
kiln-pro CI is offline.
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def overlay():
    """Return ``_maybe_overlay_calibration`` from the slicer plugin."""
    from kiln.plugins.slicer_tools import _maybe_overlay_calibration
    return _maybe_overlay_calibration


def test_free_tier_no_kiln_pro_is_silent_no_op(overlay, monkeypatch) -> None:
    """When kiln-pro isn't importable, overlay returns input unchanged."""
    # Force the lazy import inside _maybe_overlay_calibration to fail.
    blocked = "kiln_pro.engineering.calibration_coach"
    monkeypatch.delitem(sys.modules, blocked, raising=False)
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("kiln_pro"):
            raise ImportError(f"simulated: kiln-pro not installed ({name})")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        merged, cal_used = overlay({"x": "1"}, "bambu_a1", material="PLA")

    assert merged == {"x": "1"}
    assert cal_used is None


def test_pro_tier_without_input_path_does_not_record_slice(
    overlay, monkeypatch,
) -> None:
    """Pro overlay applies calibration but skips slice-history recording
    when input_path isn't supplied — that's the back-compat path."""
    fake_cal_used = {
        "printer_id": "bambu_a1", "tier": "high",
        "expected_accuracy_mm": 0.10, "source": "imported",
        "material": "PLA",
        "xy_compensation_mm": 0.0, "flow_rate": 1.0,
        "pressure_advance": 0.0,
    }

    cc_module = _make_fake_calibration_coach(fake_cal_used)
    bridge_calls: list[dict] = []
    bridge_module = _make_fake_bridge(bridge_calls)

    with patch.dict(sys.modules, {
        "kiln_pro.engineering.calibration_coach": cc_module,
        "kiln_pro.bridge": bridge_module,
    }):
        merged, cal_used = overlay({}, "bambu_a1", material="PLA")

    assert cal_used is fake_cal_used
    assert merged == {}  # fake apply returned the dict unchanged
    assert bridge_calls == []  # no slice recorded — input_path was None


def test_pro_tier_with_input_path_records_slice_event_once(
    overlay,
) -> None:
    """input_path supplied + non-None cal_used → bridge invoked exactly once
    with derived material from the cal_used block."""
    fake_cal_used = {
        "printer_id": "bambu_a1", "tier": "high",
        "expected_accuracy_mm": 0.10, "source": "imported",
        "material": "PLA",
        "xy_compensation_mm": 0.0, "flow_rate": 1.0,
        "pressure_advance": 0.0,
    }
    cc_module = _make_fake_calibration_coach(fake_cal_used)
    bridge_calls: list[dict] = []
    bridge_module = _make_fake_bridge(bridge_calls)

    with patch.dict(sys.modules, {
        "kiln_pro.engineering.calibration_coach": cc_module,
        "kiln_pro.bridge": bridge_module,
    }):
        merged, cal_used = overlay(
            {}, "bambu_a1", material="PLA", input_path="/tmp/design.stl",
        )

    assert cal_used is fake_cal_used
    assert len(bridge_calls) == 1
    call = bridge_calls[0]
    assert call["input_path"] == "/tmp/design.stl"
    assert call["printer_id"] == "bambu_a1"
    assert call["material"] == "PLA"


def test_pro_tier_bridge_failure_does_not_break_slicing(overlay) -> None:
    """Best-effort telemetry: a bridge exception must not propagate."""
    fake_cal_used = {
        "printer_id": "bambu_a1", "tier": "high",
        "expected_accuracy_mm": 0.10, "source": "imported",
        "material": "PLA",
        "xy_compensation_mm": 0.0, "flow_rate": 1.0,
        "pressure_advance": 0.0,
    }
    cc_module = _make_fake_calibration_coach(fake_cal_used)

    class _BoomBridge:
        def record_slice_for_input(self, **_: Any) -> None:
            raise RuntimeError("simulated bridge failure")

    bridge_module = types.ModuleType("kiln_pro.bridge")
    bridge_module.pro_features = _BoomBridge()  # type: ignore[attr-defined]

    with patch.dict(sys.modules, {
        "kiln_pro.engineering.calibration_coach": cc_module,
        "kiln_pro.bridge": bridge_module,
    }):
        # Should NOT raise.
        merged, cal_used = overlay(
            {}, "bambu_a1", material="PLA", input_path="/tmp/design.stl",
        )

    assert cal_used is fake_cal_used  # overlay still succeeded


# ---------------------------------------------------------------------------
# Test helpers — fake kiln-pro modules
# ---------------------------------------------------------------------------


def _make_fake_calibration_coach(cal_used: dict) -> types.ModuleType:
    """Synthesize a kiln_pro.engineering.calibration_coach stand-in."""
    module = types.ModuleType("kiln_pro.engineering.calibration_coach")

    class _FakeConfidence:
        HIGH = "high"

    class _FakeVerdict:
        tier = _FakeConfidence.HIGH
        expected_accuracy_mm = 0.10
        profile = None

    module.apply_calibration_to_slicer_args = (  # type: ignore[attr-defined]
        lambda args, _printer, _material: dict(args or {})
    )
    module.calibration_for = (  # type: ignore[attr-defined]
        lambda _printer, _material: _FakeVerdict()
    )
    module.calibration_used_block = (  # type: ignore[attr-defined]
        lambda _verdict, *, printer_id: cal_used
    )
    module.CalibrationConfidence = _FakeConfidence  # type: ignore[attr-defined]
    return module


def _make_fake_bridge(call_log: list[dict]) -> types.ModuleType:
    """Synthesize a kiln_pro.bridge stand-in that records every call."""
    module = types.ModuleType("kiln_pro.bridge")

    class _ProFeatures:
        def record_slice_for_input(self, **kwargs: Any) -> dict:
            call_log.append(kwargs)
            return {"slice_id": "fake-slice-id", "applied_offset_count": 0}

    module.pro_features = _ProFeatures()  # type: ignore[attr-defined]
    return module
