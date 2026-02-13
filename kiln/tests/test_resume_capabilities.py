"""Tests for resume capability tracking.

Coverage:
- ResumeCapability dataclass and to_dict()
- ResumeCapabilityRegistry.get_capabilities() for all 5 adapters
- ResumeCapabilityRegistry.get_recovery_plan() for known and unknown failures
- ResumeCapabilityRegistry.supports_unattended() logic
- Unknown adapter handling
- Recovery method ordering
"""

from __future__ import annotations

import pytest

from kiln.resume_capabilities import ResumeCapability, ResumeCapabilityRegistry

_ALL_ADAPTERS = ("octoprint", "moonraker", "bambu", "prusa_connect", "serial")

_ALL_FAILURE_TYPES = (
    "filament_runout",
    "power_loss",
    "network_disconnect",
    "thermal_runaway",
    "print_detachment",
)


class TestResumeCapabilityDataclass:
    """ResumeCapability dataclass construction and serialisation."""

    def test_basic_construction(self):
        cap = ResumeCapability(
            adapter_type="test",
            supports_pause_resume=True,
            supports_firmware_recovery=False,
            supports_z_offset_resume=True,
            supports_layer_resume=False,
            supports_filament_change=True,
            recovery_methods=["pause_resume"],
            limitations=["test limitation"],
        )
        assert cap.adapter_type == "test"
        assert cap.supports_pause_resume is True
        assert cap.supports_firmware_recovery is False
        assert cap.recovery_methods == ["pause_resume"]
        assert cap.limitations == ["test limitation"]

    def test_default_lists(self):
        cap = ResumeCapability(
            adapter_type="test",
            supports_pause_resume=True,
            supports_firmware_recovery=False,
            supports_z_offset_resume=False,
            supports_layer_resume=False,
            supports_filament_change=False,
        )
        assert cap.recovery_methods == []
        assert cap.limitations == []

    def test_to_dict(self):
        cap = ResumeCapability(
            adapter_type="octoprint",
            supports_pause_resume=True,
            supports_firmware_recovery=True,
            supports_z_offset_resume=True,
            supports_layer_resume=False,
            supports_filament_change=True,
            recovery_methods=["pause_resume", "m413_power_loss"],
            limitations=["No layer resume"],
        )
        d = cap.to_dict()
        assert d["adapter_type"] == "octoprint"
        assert d["supports_pause_resume"] is True
        assert d["supports_layer_resume"] is False
        assert d["recovery_methods"] == ["pause_resume", "m413_power_loss"]
        assert d["limitations"] == ["No layer resume"]

    def test_to_dict_returns_new_lists(self):
        """to_dict() should return copies, not references to internal lists."""
        cap = ResumeCapability(
            adapter_type="test",
            supports_pause_resume=True,
            supports_firmware_recovery=False,
            supports_z_offset_resume=False,
            supports_layer_resume=False,
            supports_filament_change=False,
            recovery_methods=["a"],
        )
        d = cap.to_dict()
        d["recovery_methods"].append("mutated")
        assert "mutated" not in cap.recovery_methods


class TestGetCapabilities:
    """ResumeCapabilityRegistry.get_capabilities() for all adapters."""

    @pytest.fixture()
    def registry(self):
        return ResumeCapabilityRegistry()

    @pytest.mark.parametrize("adapter", _ALL_ADAPTERS)
    def test_all_adapters_return_capabilities(self, registry, adapter):
        cap = registry.get_capabilities(adapter)
        assert cap is not None
        assert cap.adapter_type == adapter

    @pytest.mark.parametrize("adapter", _ALL_ADAPTERS)
    def test_all_adapters_support_pause_resume(self, registry, adapter):
        cap = registry.get_capabilities(adapter)
        assert cap is not None
        assert cap.supports_pause_resume is True

    @pytest.mark.parametrize("adapter", _ALL_ADAPTERS)
    def test_all_adapters_have_recovery_methods(self, registry, adapter):
        cap = registry.get_capabilities(adapter)
        assert cap is not None
        assert len(cap.recovery_methods) > 0

    @pytest.mark.parametrize("adapter", _ALL_ADAPTERS)
    def test_all_adapters_have_limitations(self, registry, adapter):
        cap = registry.get_capabilities(adapter)
        assert cap is not None
        assert len(cap.limitations) > 0

    def test_unknown_adapter_returns_none(self, registry):
        assert registry.get_capabilities("unknown_adapter") is None

    def test_empty_string_returns_none(self, registry):
        assert registry.get_capabilities("") is None

    def test_octoprint_specific_capabilities(self, registry):
        cap = registry.get_capabilities("octoprint")
        assert cap is not None
        assert cap.supports_firmware_recovery is True
        assert cap.supports_z_offset_resume is True
        assert cap.supports_layer_resume is False
        assert cap.supports_filament_change is True

    def test_bambu_specific_capabilities(self, registry):
        cap = registry.get_capabilities("bambu")
        assert cap is not None
        assert cap.supports_firmware_recovery is True
        assert cap.supports_z_offset_resume is False
        assert cap.supports_layer_resume is True
        assert cap.supports_filament_change is True

    def test_moonraker_firmware_recovery_false(self, registry):
        """Moonraker's firmware recovery depends on underlying firmware."""
        cap = registry.get_capabilities("moonraker")
        assert cap is not None
        assert cap.supports_firmware_recovery is False

    def test_serial_limitations_include_no_autonomous(self, registry):
        cap = registry.get_capabilities("serial")
        assert cap is not None
        assert any("autonomous" in lim.lower() for lim in cap.limitations)


class TestGetRecoveryPlan:
    """ResumeCapabilityRegistry.get_recovery_plan() for various scenarios."""

    @pytest.fixture()
    def registry(self):
        return ResumeCapabilityRegistry()

    @pytest.mark.parametrize("adapter", _ALL_ADAPTERS)
    @pytest.mark.parametrize("failure", _ALL_FAILURE_TYPES)
    def test_all_adapter_failure_combinations_return_steps(self, registry, adapter, failure):
        plan = registry.get_recovery_plan(adapter, failure_type=failure)
        assert isinstance(plan, list)
        assert len(plan) > 0

    def test_unknown_adapter_returns_empty(self, registry):
        plan = registry.get_recovery_plan("unknown", failure_type="power_loss")
        assert plan == []

    def test_unknown_failure_returns_empty(self, registry):
        plan = registry.get_recovery_plan("octoprint", failure_type="unknown_failure")
        assert plan == []

    def test_both_unknown_returns_empty(self, registry):
        plan = registry.get_recovery_plan("unknown", failure_type="unknown")
        assert plan == []

    def test_recovery_plan_returns_copy(self, registry):
        """Modifying the returned plan should not affect the registry."""
        plan = registry.get_recovery_plan("octoprint", failure_type="power_loss")
        original_len = len(plan)
        plan.append("mutated step")
        fresh_plan = registry.get_recovery_plan("octoprint", failure_type="power_loss")
        assert len(fresh_plan) == original_len

    def test_filament_runout_octoprint_starts_with_pause(self, registry):
        plan = registry.get_recovery_plan("octoprint", failure_type="filament_runout")
        assert "pause" in plan[0].lower()

    def test_power_loss_bambu_mentions_built_in(self, registry):
        plan = registry.get_recovery_plan("bambu", failure_type="power_loss")
        assert any("built-in" in step.lower() for step in plan)

    def test_thermal_runaway_ends_with_inspection(self, registry):
        """All thermal runaway plans should end requiring physical inspection."""
        for adapter in _ALL_ADAPTERS:
            plan = registry.get_recovery_plan(adapter, failure_type="thermal_runaway")
            assert "inspection" in plan[-1].lower()

    def test_network_disconnect_serial_marks_as_failed(self, registry):
        """Serial network disconnect should acknowledge the print is lost."""
        plan = registry.get_recovery_plan("serial", failure_type="network_disconnect")
        assert any("lost" in step.lower() or "failed" in step.lower() for step in plan)

    def test_recovery_steps_are_ordered(self, registry):
        """Verify plans have a logical order (first step is detection/action, not cleanup)."""
        for adapter in _ALL_ADAPTERS:
            plan = registry.get_recovery_plan(adapter, failure_type="filament_runout")
            # First step should be about pausing or detecting, not resuming
            assert "resume" not in plan[0].lower()


class TestSupportsUnattended:
    """ResumeCapabilityRegistry.supports_unattended() logic."""

    @pytest.fixture()
    def registry(self):
        return ResumeCapabilityRegistry()

    def test_octoprint_supports_unattended(self, registry):
        assert registry.supports_unattended("octoprint") is True

    def test_bambu_supports_unattended(self, registry):
        assert registry.supports_unattended("bambu") is True

    def test_prusa_connect_supports_unattended(self, registry):
        assert registry.supports_unattended("prusa_connect") is True

    def test_serial_supports_unattended(self, registry):
        assert registry.supports_unattended("serial") is True

    def test_moonraker_does_not_support_unattended(self, registry):
        """Moonraker lacks guaranteed firmware recovery (Klipper limitation)."""
        assert registry.supports_unattended("moonraker") is False

    def test_unknown_adapter_not_unattended(self, registry):
        assert registry.supports_unattended("unknown") is False

    def test_empty_string_not_unattended(self, registry):
        assert registry.supports_unattended("") is False
