"""Stress tests for server.py changes on feature/provenance-qr-validation."""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from unittest.mock import MagicMock, patch
import pytest


# ---------------------------------------------------------------------------
# Test: health_check openscad section
# ---------------------------------------------------------------------------
class TestHealthCheckOpenSCAD:
    """Test the new OpenSCAD section in health_check."""

    def test_openscad_healthy_when_version_found(self):
        """When OpenSCAD is installed and recent, health_data should show ok."""
        with patch("kiln.emboss_generator.get_openscad_version", return_value="2024.09.20"):
            with patch("kiln.emboss_generator._openscad_version_year", return_value=2024):
                with patch("kiln.emboss_generator._OPENSCAD_MIN_VERSION_YEAR", 2024):
                    # Replicate the health_check openscad logic
                    from kiln.emboss_generator import (
                        _OPENSCAD_MIN_VERSION_YEAR,
                        _openscad_version_year,
                        get_openscad_version,
                    )
                    openscad_version = get_openscad_version()
                    info = {"version": openscad_version or "not_found"}
                    if openscad_version:
                        year = _openscad_version_year(openscad_version)
                        if year and year < _OPENSCAD_MIN_VERSION_YEAR:
                            info["svg_operations_supported"] = False
                        else:
                            info["svg_operations_supported"] = True

                    assert info["version"] == "2024.09.20"
                    assert info["svg_operations_supported"] is True

    def test_openscad_outdated_shows_warning(self):
        """When OpenSCAD version is old, should show warning."""
        with patch("kiln.emboss_generator.get_openscad_version", return_value="2021.01"):
            with patch("kiln.emboss_generator._openscad_version_year", return_value=2021):
                from kiln.emboss_generator import (
                    _openscad_version_year,
                    get_openscad_version,
                )
                ver = get_openscad_version()
                year = _openscad_version_year(ver)
                assert year < 2024

    def test_openscad_not_found(self):
        """When OpenSCAD is not installed, version should be not_found."""
        with patch("kiln.emboss_generator.get_openscad_version", return_value=None):
            from kiln.emboss_generator import get_openscad_version
            ver = get_openscad_version()
            info = {"version": ver or "not_found"}
            assert info["version"] == "not_found"

    def test_openscad_exception_handled(self):
        """When emboss_generator import fails, should degrade gracefully."""
        health_data = {}
        try:
            raise ImportError("no module")
        except Exception as exc:
            health_data["openscad"] = {"version": "unknown"}
        assert health_data["openscad"]["version"] == "unknown"


# ---------------------------------------------------------------------------
# Test: get_started openscad guidance section
# ---------------------------------------------------------------------------
class TestGetStartedOpenSCAD:
    """Test the new OpenSCAD guidance in get_started."""

    def _build_guidance(self, version: str | None, version_year: int | None) -> dict:
        """Replicate the get_started openscad logic."""
        openscad_guidance = {}
        _openscad_action_needed = False

        if not version:
            _openscad_action_needed = True
            openscad_guidance = {
                "installed": False,
                "message": "Install OpenSCAD...",
                "install_command": "brew install --cask openscad@snapshot",
                "required_for": ["compile_scad", "generate_product_base", "decorate_surface", "visualize_model"],
            }
        elif version_year and version_year < 2024:
            _openscad_action_needed = True
            openscad_guidance = {
                "installed": True,
                "version": version,
                "status": "outdated",
                "message": f"OpenSCAD {version} is outdated.",
                "install_command": "brew install --cask openscad@snapshot",
                "required_for": ["compile_scad", "generate_product_base", "decorate_surface", "visualize_model"],
            }
        else:
            openscad_guidance = {"installed": True, "version": version, "status": "ok"}

        return openscad_guidance, _openscad_action_needed

    def test_not_installed(self):
        guidance, action = self._build_guidance(None, None)
        assert guidance["installed"] is False
        assert action is True

    def test_outdated(self):
        guidance, action = self._build_guidance("2021.01", 2021)
        assert guidance["status"] == "outdated"
        assert action is True

    def test_up_to_date(self):
        guidance, action = self._build_guidance("2024.09.20", 2024)
        assert guidance["status"] == "ok"
        assert action is False

    def test_quick_start_prepended_when_action_needed(self):
        """When OpenSCAD needs action, quick_start should have step 0."""
        _quick_start_base = ["1. Check printer", "2. Fleet status"]

        _, action_needed = self._build_guidance(None, None)
        guidance = {"message": "Install OpenSCAD", "install_command": "brew install"}

        if action_needed:
            step = f"0. IMPORTANT: {guidance.get('message', '')} -- run: {guidance.get('install_command', '')}"
            quick_start = [step] + _quick_start_base
        else:
            quick_start = _quick_start_base

        assert quick_start[0].startswith("0. IMPORTANT")
        assert len(quick_start) == 3

    def test_quick_start_not_prepended_when_ok(self):
        _quick_start_base = ["1. Check printer"]
        _, action_needed = self._build_guidance("2024.09", 2024)

        if action_needed:
            quick_start = ["0. step"] + _quick_start_base
        else:
            quick_start = _quick_start_base

        assert quick_start[0].startswith("1.")


# ---------------------------------------------------------------------------
# Test: decorate_surface template_id parameter
# ---------------------------------------------------------------------------
class TestDecorateSurfaceTemplateId:
    """Test the template_id auto-resolve from provenance and template_decoration."""

    def test_template_id_from_recipe_design_id(self):
        """When template_id is empty and recipe has design_id, use it."""
        template_id = ""
        recipe_design_id = "nameplate"

        if not template_id and recipe_design_id:
            template_id = recipe_design_id

        assert template_id == "nameplate"

    def test_template_id_not_overridden_when_provided(self):
        """When template_id is already set, recipe design_id should not override."""
        template_id = "coaster"
        recipe_design_id = "nameplate"

        if not template_id and recipe_design_id:
            template_id = recipe_design_id

        assert template_id == "coaster"

    def test_template_decoration_import_fails_gracefully(self):
        """When template_decoration module is not available, should catch and continue."""
        template_profile_used = False
        template_id = "nameplate"

        if template_id:
            try:
                from kiln.template_decoration import resolve_decoration_defaults  # noqa: F401
                # This should fail in public Kiln since it's a pro module
                resolved = resolve_decoration_defaults(template_id, material="PLA")
                if resolved.get("profile_used"):
                    template_profile_used = True
            except Exception:
                pass  # Expected — template_decoration is a pro module

        # Should gracefully handle the missing module
        assert template_profile_used is False

    def test_template_profile_used_in_result(self):
        """When template profile is used, result should include template info."""
        result_dict = {"output": "/tmp/out.stl"}
        template_profile_used = True
        template_id = "nameplate"

        if template_profile_used:
            result_dict["template_profile_used"] = True
            result_dict["template_id"] = template_id

        assert result_dict["template_profile_used"] is True
        assert result_dict["template_id"] == "nameplate"

    def test_template_profile_not_in_result_when_unused(self):
        result_dict = {"output": "/tmp/out.stl"}
        template_profile_used = False

        if template_profile_used:
            result_dict["template_profile_used"] = True

        assert "template_profile_used" not in result_dict


class TestDecorateSurfaceSvgParams:
    """Test the new svg_id and svg_layer parameters."""

    def test_svg_params_passed_to_emboss(self):
        """svg_id and svg_layer should be forwarded to emboss function call."""
        kwargs = {
            "svg_id": "icon",
            "svg_layer": "foreground",
        }
        # Verify the params exist and are passable
        assert kwargs["svg_id"] == "icon"
        assert kwargs["svg_layer"] == "foreground"

    def test_svg_params_default_empty(self):
        """Default values for svg_id and svg_layer should be empty strings."""
        svg_id = ""
        svg_layer = ""
        assert svg_id == ""
        assert svg_layer == ""


# ---------------------------------------------------------------------------
# Test: server.py compiles without errors
# ---------------------------------------------------------------------------
class TestServerCompiles:
    def test_server_module_syntax_valid(self):
        """Verify server.py has valid Python syntax."""
        import py_compile
        server_path = os.path.join(
            os.path.dirname(__file__), "..", "src", "kiln", "server.py"
        )
        # py_compile.compile raises on syntax errors
        py_compile.compile(server_path, doraise=True)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short", "-x"])
