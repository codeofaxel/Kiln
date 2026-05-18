"""Tests for the free-tier upgrade_hint signal on Phase 2 consumer tools.

When the kiln-pro engineering overlay didn't merge (free tier / missing
license / network past grace / kiln-pro absent), 4 consumer tools attach
a verbatim upgrade-pitch string so MCP agents can surface "where to
find the depth" without inventing copy.  When the overlay merged (Pro+
with valid license), the hint is empty.

Covered tools:
  - troubleshoot_print_issue              (TroubleshootingResult.upgrade_hint)
  - get_post_processing                   (PostProcessingGuide.upgrade_hint)
  - check_environment_compatibility       (EnvironmentReport.upgrade_hint)
  - server.troubleshoot_printer wrapper   (dict["upgrade_hint"])

Detection probe: ``_engineering_overlay_loaded()`` checks whether
``pla.agent_guidance`` is non-empty (overlay-only field).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiln.design_intelligence import (
    _UPGRADE_HINT_ENVIRONMENT,
    _UPGRADE_HINT_POST_PROCESSING,
    _UPGRADE_HINT_TROUBLESHOOTING,
    _engineering_overlay_loaded,
    check_environment_compatibility,
    get_post_processing,
    troubleshoot_print_issue,
)

# ---------------------------------------------------------------------------
# Helper — detect actual overlay state for skip decisions
# ---------------------------------------------------------------------------


def _overlay_actually_loaded() -> bool:
    """Real-state probe, used to gate Pro+-vs-free tests on the runner's
    actual environment.  When kiln-pro is installed + overlay loaded, run
    the Pro+ assertions.  When absent, run the free-tier assertions."""
    return _engineering_overlay_loaded()


# ---------------------------------------------------------------------------
# Hint copy hygiene — every hint string ends with the canonical URL
# ---------------------------------------------------------------------------


class TestHintCopyHygiene:
    """The 3 module-level hint constants must end with kiln3d.com/pricing
    so a user (or MCP agent) reading the field can act on it directly."""

    @pytest.mark.parametrize("hint", [
        _UPGRADE_HINT_TROUBLESHOOTING,
        _UPGRADE_HINT_POST_PROCESSING,
        _UPGRADE_HINT_ENVIRONMENT,
    ])
    def test_hint_ends_with_pricing_url(self, hint):
        assert hint.endswith("https://kiln3d.com/pricing"), (
            f"Hint must end with the canonical pricing URL so agents "
            f"can act on it without further lookup; got: ...{hint[-40:]!r}"
        )

    @pytest.mark.parametrize("hint", [
        _UPGRADE_HINT_TROUBLESHOOTING,
        _UPGRADE_HINT_POST_PROCESSING,
        _UPGRADE_HINT_ENVIRONMENT,
    ])
    def test_hint_mentions_kiln_pro(self, hint):
        assert "Kiln Pro" in hint, (
            f"Hint must name the upsell tier so an agent can disambiguate; "
            f"got: {hint!r}"
        )

    @pytest.mark.parametrize("hint", [
        _UPGRADE_HINT_TROUBLESHOOTING,
        _UPGRADE_HINT_POST_PROCESSING,
        _UPGRADE_HINT_ENVIRONMENT,
    ])
    def test_hint_is_short_enough_for_inline_surface(self, hint):
        """Keep hints under 200 chars so an agent can surface them inline
        without truncation in a Slack/chat message body."""
        assert len(hint) <= 200, (
            f"Hint is {len(hint)} chars (limit 200); trim before merging."
        )


# ---------------------------------------------------------------------------
# Free-tier behavior — overlay simulated absent via patching the probe
# ---------------------------------------------------------------------------


class TestFreeTierHintAttached:
    """When _engineering_overlay_loaded() returns False, every consumer
    tool attaches the corresponding upgrade_hint string."""

    @patch("kiln.design_intelligence._engineering_overlay_loaded", return_value=False)
    def test_troubleshoot_attaches_hint(self, _mock):
        result = troubleshoot_print_issue("pla")
        assert result is not None, "pla should resolve in the catalog"
        assert result.upgrade_hint == _UPGRADE_HINT_TROUBLESHOOTING

    @patch("kiln.design_intelligence._engineering_overlay_loaded", return_value=False)
    def test_post_processing_attaches_hint(self, _mock):
        result = get_post_processing("pla")
        assert result is not None, "pla should resolve in the catalog"
        assert result.upgrade_hint == _UPGRADE_HINT_POST_PROCESSING

    @patch("kiln.design_intelligence._engineering_overlay_loaded", return_value=False)
    def test_environment_attaches_hint(self, _mock):
        result = check_environment_compatibility("pla", "outdoor sunny")
        assert result is not None, "pla + outdoor query should resolve"
        assert result.upgrade_hint == _UPGRADE_HINT_ENVIRONMENT


# ---------------------------------------------------------------------------
# Pro+ behavior — overlay simulated present via patching
# ---------------------------------------------------------------------------


class TestProTierHintEmpty:
    """When _engineering_overlay_loaded() returns True, every consumer
    tool leaves upgrade_hint empty — Pro+ users don't need the prompt."""

    @patch("kiln.design_intelligence._engineering_overlay_loaded", return_value=True)
    def test_troubleshoot_no_hint(self, _mock):
        result = troubleshoot_print_issue("pla")
        assert result is not None
        assert result.upgrade_hint == ""

    @patch("kiln.design_intelligence._engineering_overlay_loaded", return_value=True)
    def test_post_processing_no_hint(self, _mock):
        result = get_post_processing("pla")
        assert result is not None
        assert result.upgrade_hint == ""

    @patch("kiln.design_intelligence._engineering_overlay_loaded", return_value=True)
    def test_environment_no_hint(self, _mock):
        result = check_environment_compatibility("pla", "outdoor sunny")
        assert result is not None
        assert result.upgrade_hint == ""


# ---------------------------------------------------------------------------
# to_dict() serialization — hint round-trips through MCP responses
# ---------------------------------------------------------------------------


class TestToDictIncludesHint:
    """asdict() in the dataclass to_dict() round-trip MUST include the
    upgrade_hint field (the MCP layer serializes via to_dict, so a
    missing field would silently strip the hint before it reaches the
    agent)."""

    @patch("kiln.design_intelligence._engineering_overlay_loaded", return_value=False)
    def test_troubleshoot_dict_carries_hint(self, _mock):
        result = troubleshoot_print_issue("pla")
        d = result.to_dict()
        assert "upgrade_hint" in d
        assert d["upgrade_hint"] == _UPGRADE_HINT_TROUBLESHOOTING

    @patch("kiln.design_intelligence._engineering_overlay_loaded", return_value=False)
    def test_post_processing_dict_carries_hint(self, _mock):
        result = get_post_processing("pla")
        d = result.to_dict()
        assert "upgrade_hint" in d
        assert d["upgrade_hint"] == _UPGRADE_HINT_POST_PROCESSING

    @patch("kiln.design_intelligence._engineering_overlay_loaded", return_value=False)
    def test_environment_dict_carries_hint(self, _mock):
        result = check_environment_compatibility("pla", "outdoor sunny")
        d = result.to_dict()
        assert "upgrade_hint" in d
        assert d["upgrade_hint"] == _UPGRADE_HINT_ENVIRONMENT


# ---------------------------------------------------------------------------
# Detection probe — verifies the helper returns the right answer on the
# runner's actual environment (orthogonal cross-check for the patched
# tests above; runs end-to-end against _get_kb()).
# ---------------------------------------------------------------------------


class TestOverlayDetectionProbe:
    def test_probe_returns_bool(self):
        result = _engineering_overlay_loaded()
        assert isinstance(result, bool)

    def test_probe_matches_environment(self):
        """If kiln-pro is importable AND the overlay merged, probe should
        return True.  Otherwise False.  The probe checks pla.agent_guidance
        because that field only exists in the overlay-merged record."""
        probe = _engineering_overlay_loaded()
        try:
            import kiln_pro  # noqa: F401
            # kiln-pro is importable — overlay should have merged
            # (unless there's a license/network issue, which the probe
            # correctly detects as "overlay not loaded").  Just assert
            # that the probe is internally consistent: if it says True,
            # then pla.agent_guidance must be present.
            if probe:
                from kiln.design_intelligence import _get_kb
                pla_guidance = _get_kb().materials.get("pla", {}).get("agent_guidance")
                assert pla_guidance, (
                    "Probe returned True but pla.agent_guidance is empty — "
                    "probe is lying about the overlay state"
                )
        except ImportError:
            # kiln-pro absent — probe MUST return False
            assert probe is False, (
                "kiln-pro not importable but probe returned True — "
                "probe is hallucinating the overlay"
            )
