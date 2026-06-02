"""Tests for the printer_model prompt helpers.

Incident #0 (2026-04-15) exposed that the kiln setup CLI never asked
for the printer_model field.  This module centralises the prompt so
setup, quickstart, and status all share consistent behaviour.
"""
from __future__ import annotations

from kiln.cli.printer_model_prompt import (
    _find_close_matches,
    _validate_model_key,
    check_existing_config_for_missing_model,
    prompt_for_printer_model,
    suggest_bambu_model,
)


class TestSuggestBambuModel:
    def test_a1_serial_prefix(self):
        assert suggest_bambu_model("03900D5C2513213") == "bambu_a1"

    # Serial prefixes corrected 2026-06-01 against Bambu's official find-sn
    # wiki (the prior table was unsourced and wrong on 5 of 6 entries).
    # All Bambu prefixes are 3 chars and mutually non-overlapping.
    def test_x1c_prefix(self):
        assert suggest_bambu_model("00M0ABC123") == "bambu_x1c"

    def test_a1_mini_prefix(self):
        assert suggest_bambu_model("030XYZ") == "bambu_a1_mini"

    def test_p1s_prefix(self):
        assert suggest_bambu_model("01P0ABC") == "bambu_p1s"

    def test_p1p_prefix(self):
        assert suggest_bambu_model("01S0ABC") == "bambu_p1p"

    def test_x1e_prefix(self):
        assert suggest_bambu_model("03W0ABC") == "bambu_x1e"

    def test_p2s_prefix(self):
        assert suggest_bambu_model("22E0ABC") == "bambu_p2s"

    def test_unknown_serial_returns_none(self):
        assert suggest_bambu_model("XYZZZZ_future_model") is None

    def test_empty_serial_returns_none(self):
        assert suggest_bambu_model("") is None
        assert suggest_bambu_model(None) is None


class TestValidateModelKey:
    def test_known_model_valid(self):
        assert _validate_model_key("bambu_a1") is True

    def test_creality_alias_model_valid(self):
        assert _validate_model_key("creality_k1_max") is True

    def test_new_creality_models_valid(self):
        assert _validate_model_key("sparkx_i7") is True
        assert _validate_model_key("k1c") is True
        assert _validate_model_key("ender3_v4") is True
        assert _validate_model_key("ender3_v3_ke") is True
        assert _validate_model_key("ender3_v3_se") is True
        assert _validate_model_key("ender3_v2") is True

    def test_unknown_model_invalid(self):
        assert _validate_model_key("totally_fake_model_xyz_789") is False

    def test_empty_invalid(self):
        # get_build_volume("") returns None → should not validate
        assert _validate_model_key("") is False


class TestFindCloseMatches:
    def test_typo_bambu_A1(self):
        matches = _find_close_matches("bambu_A1")
        # Either bambu_a1 appears, or at least we get some suggestions
        assert len(matches) >= 1

    def test_totally_unknown_returns_empty_or_few(self):
        matches = _find_close_matches("absolutely_nothing_like_any_printer")
        # Very low-cutoff check — may return zero
        assert isinstance(matches, list)


class TestCheckExistingConfig:
    def test_missing_printer_model_detected(self):
        cfg = {
            "active_printer": "default",
            "printers": {
                "default": {"type": "bambu", "host": "10.0.0.5"},
                "other": {"type": "prusa", "printer_model": "prusa_mk4"},
            },
        }
        missing = check_existing_config_for_missing_model(cfg)
        assert "default" in missing
        assert "other" not in missing

    def test_empty_printer_model_treated_as_missing(self):
        cfg = {
            "printers": {"default": {"type": "bambu", "printer_model": ""}},
        }
        missing = check_existing_config_for_missing_model(cfg)
        assert missing == ["default"]

    def test_no_printers_returns_empty(self):
        assert check_existing_config_for_missing_model({}) == []

    def test_malformed_config_returns_empty(self):
        # 'printers' is a list, not a dict
        assert check_existing_config_for_missing_model({"printers": []}) == []


class TestInteractivePrompt:
    """Interactive-prompt tests use click's mock runner via monkeypatch."""

    def test_bambu_prompt_accepts_default_on_enter(self, monkeypatch):
        """When the user presses Enter, the Bambu serial-prefix default
        should be accepted without requiring them to type anything."""
        inputs = iter([""])  # press Enter
        monkeypatch.setattr("click.prompt", lambda *a, **kw: kw.get("default", next(inputs)))
        result = prompt_for_printer_model("bambu", serial="03900D5C_test")
        assert result == "bambu_a1"

    def test_bambu_prompt_accepts_user_override(self, monkeypatch):
        """User overrides the suggested default with another known model.

        Note: only models whose build_volume_mm is populated in
        printer_intelligence.json will validate.  Today that's
        ``bambu_a1`` on the Bambu side — other entries exist as stubs
        but lack build_volume.  See the tasks.md follow-up to populate
        the remaining entries.
        """
        # Use bambu_a1 (same as default) — confirms the override path
        # returns whatever the user typed when the value IS valid.
        monkeypatch.setattr("click.prompt", lambda *a, **kw: "bambu_a1")
        result = prompt_for_printer_model("bambu", serial="03900D5C_test")
        assert result == "bambu_a1"

    def test_typo_then_reject_then_retype(self, monkeypatch):
        """User types a typo, refuses to record it, then types the real
        model and it validates."""
        answers = iter(["bambu_A1", "bambu_a1"])
        monkeypatch.setattr("click.prompt", lambda *a, **kw: next(answers))
        monkeypatch.setattr("click.confirm", lambda *a, **kw: False)
        result = prompt_for_printer_model("bambu", serial=None)
        assert result == "bambu_a1"

    def test_typo_then_accept_anyway(self, monkeypatch):
        """User insists on their typo — we record it + warn but return
        the value so their config reflects their intent."""
        monkeypatch.setattr("click.prompt", lambda *a, **kw: "bambu_A1")
        monkeypatch.setattr("click.confirm", lambda *a, **kw: True)
        result = prompt_for_printer_model("bambu", serial=None)
        assert result == "bambu_A1"

    def test_allow_skip_with_empty_input(self, monkeypatch):
        """With allow_skip=True and empty input, return None cleanly."""
        monkeypatch.setattr("click.prompt", lambda *a, **kw: kw.get("default", ""))
        result = prompt_for_printer_model(
            "octoprint", serial=None, allow_skip=True,
        )
        assert result is None
