"""Tests for the safety-gap warning helper.

Judges' verdict on placement: wire into the 4 canonical entry points
(kiln_health, printer_status, preflight_check, get_started).  Skip
downstream tools (upload_file, slice_*, send_gcode) — they'd be
redundant once the agent has seen the warning once.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from kiln.safety_gap_warning import (
    attach_safety_warning,
    safety_gap_warning,
)


class TestWarningPresence:
    def test_returns_dict_when_model_missing(self):
        with patch(
            "kiln.printer_model_resolver.resolve_printer_model",
            return_value=None,
        ):
            warn = safety_gap_warning()
        assert warn is not None
        assert warn["code"] == "PRINTER_MODEL_NOT_SET"
        assert warn["severity"] == "warning"
        assert "unsafe" in warn["message"]
        assert "printer_model" in warn["remediation"]

    def test_returns_none_when_model_set(self):
        with patch(
            "kiln.printer_model_resolver.resolve_printer_model",
            return_value="bambu_a1",
        ):
            assert safety_gap_warning() is None

    def test_returns_none_on_internal_error(self):
        """Never block a legitimate tool response because the check
        itself failed."""
        with patch(
            "kiln.printer_model_resolver.resolve_printer_model",
            side_effect=RuntimeError("boom"),
        ):
            assert safety_gap_warning() is None


class TestBambuSuggestion:
    def test_surfaces_suggestion_for_known_bambu_serial(self):
        with patch(
            "kiln.printer_model_resolver.resolve_printer_model",
            return_value=None,
        ), patch("kiln.server._PRINTER_TYPE", "bambu"), patch(
            "kiln.server._PRINTER_SERIAL", "03900D5C2513213",
        ):
            warn = safety_gap_warning()
        assert warn is not None
        assert warn.get("suggestion") == "bambu_a1"

    def test_no_suggestion_for_unknown_bambu_serial(self):
        with patch(
            "kiln.printer_model_resolver.resolve_printer_model",
            return_value=None,
        ), patch("kiln.server._PRINTER_TYPE", "bambu"), patch(
            "kiln.server._PRINTER_SERIAL", "XYZ_future_model",
        ):
            warn = safety_gap_warning()
        assert warn is not None
        assert "suggestion" not in warn

    def test_no_suggestion_for_non_bambu(self):
        with patch(
            "kiln.printer_model_resolver.resolve_printer_model",
            return_value=None,
        ), patch("kiln.server._PRINTER_TYPE", "prusa"), patch(
            "kiln.server._PRINTER_SERIAL", "some_prusa_serial",
        ):
            warn = safety_gap_warning()
        assert warn is not None
        assert "suggestion" not in warn


class TestAttachSafetyWarning:
    def test_attaches_when_gap_present(self):
        with patch(
            "kiln.printer_model_resolver.resolve_printer_model",
            return_value=None,
        ):
            result = attach_safety_warning({"success": True})
        assert "safety_warning" in result
        assert result["success"] is True

    def test_does_not_attach_when_all_clear(self):
        with patch(
            "kiln.printer_model_resolver.resolve_printer_model",
            return_value="bambu_a1",
        ):
            result = attach_safety_warning({"success": True})
        assert "safety_warning" not in result

    def test_handles_non_dict_response(self):
        # Defensive — callers shouldn't pass non-dicts, but don't crash
        assert attach_safety_warning(None) is None
        assert attach_safety_warning("not a dict") == "not a dict"

    def test_preserves_existing_response_fields(self):
        with patch(
            "kiln.printer_model_resolver.resolve_printer_model",
            return_value=None,
        ):
            result = attach_safety_warning({
                "success": True,
                "printer": {"state": "idle"},
                "job": {"completion": 0.5},
            })
        assert result["success"] is True
        assert result["printer"] == {"state": "idle"}
        assert result["job"] == {"completion": 0.5}
        assert "safety_warning" in result
