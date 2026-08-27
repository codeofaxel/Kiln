"""Tests for design intelligence MCP tools in the design-tools plugin.

Covers:
- check_material_environment — happy path, unknown material, exception
- check_multi_material_pairing — happy path, exception
- estimate_print_cost_from_mesh — happy path, file not found, bad input, exception
- estimate_structural_load — happy path, unknown material, exception
- find_design_templates — merged search across both template libraries,
  ranking, labelling, and the pin that a generatable id really renders
- get_design_template_info — happy path, unknown pattern, exception
- get_material_design_profile — happy path, unknown material, exception
- get_post_processing_guide — happy path, unknown material, exception
- list_design_materials — happy path, exception
- list_design_templates_catalog — both libraries, labelled, with parameters
- match_design_requirements — happy path, empty match, exception
- recommend_design_material — happy path, exception
- broad printer-record helpers remain off the MCP registry
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_mcp():
    """Create a mock MCP server that captures registered tools."""
    tools: dict[str, callable] = {}

    class MockMCP:
        def tool(self):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

    return MockMCP(), tools


@pytest.fixture()
def registered_tools(mock_mcp):
    """Register design_tools plugin and return the captured tool dict."""
    mcp, tools = mock_mcp
    from kiln.plugins.design_tools import plugin

    plugin.register(mcp)
    return tools


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_material_profile(material_id: str = "pla") -> SimpleNamespace:
    return SimpleNamespace(
        material_id=material_id,
        display_name="PLA",
        category="standard",
        mechanical={"tensile_strength_mpa": 37, "impact_resistance": "low", "layer_adhesion": "good"},
        thermal={"max_service_temp_c": 50, "warping_tendency": "low"},
        chemical={"uv_resistance": "poor", "food_safe": False},
        agent_instruction=["Great for prototyping"],
        to_dict=lambda: {"material_id": material_id, "display_name": "PLA"},
    )


def _fake_public_template(
    template_id: str = "snap_fit_cantilever",
) -> dict[str, object]:
    return {
        "template_id": template_id,
        "display_name": "Snap-Fit Cantilever",
        "description": "Flexible arm that snaps into a recess.",
        "use_cases": ["enclosures", "battery covers"],
        "material_compatibility": {"excellent": ["petg", "nylon"]},
        "print_orientation": "arm_in_xy_plane",
    }


# ---------------------------------------------------------------------------
# TestCheckMaterialEnvironment
# ---------------------------------------------------------------------------


class TestCheckMaterialEnvironment:
    """Tests for check_material_environment MCP tool."""

    @patch("kiln.design_intelligence.check_environment_compatibility")
    def test_happy_path(self, mock_fn, registered_tools):
        mock_fn.return_value = SimpleNamespace(
            to_dict=lambda: {"compatible": True, "warnings": []},
        )

        result = registered_tools["check_material_environment"]("petg", "outdoor, UV exposure")

        assert result["success"] is True
        assert result["compatible"] is True
        mock_fn.assert_called_once_with("petg", "outdoor, UV exposure")

    @patch("kiln.design_intelligence.check_environment_compatibility")
    def test_unknown_material(self, mock_fn, registered_tools):
        mock_fn.return_value = None

        result = registered_tools["check_material_environment"]("unobtanium", "indoor")

        assert result["success"] is False
        assert "unobtanium" in result["error"]

    @patch("kiln.design_intelligence.check_environment_compatibility", side_effect=RuntimeError("boom"))
    def test_exception(self, mock_fn, registered_tools):
        result = registered_tools["check_material_environment"]("pla", "indoor")

        assert result["success"] is False
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# TestCheckMultiMaterialPairing
# ---------------------------------------------------------------------------


class TestCheckMultiMaterialPairing:
    """Tests for check_multi_material_pairing MCP tool."""

    @patch("kiln.design_intelligence.check_multi_material_compatibility")
    def test_happy_path(self, mock_fn, registered_tools):
        mock_fn.return_value = SimpleNamespace(
            to_dict=lambda: {"compatible": True, "adhesion": "good"},
        )

        result = registered_tools["check_multi_material_pairing"]("pla", "tpu")

        assert result["success"] is True
        assert result["compatible"] is True
        mock_fn.assert_called_once_with("pla", "tpu")

    @patch("kiln.design_intelligence.check_multi_material_compatibility", side_effect=RuntimeError("fail"))
    def test_exception(self, mock_fn, registered_tools):
        result = registered_tools["check_multi_material_pairing"]("pla", "abs")

        assert result["success"] is False
        assert "fail" in result["error"]


# ---------------------------------------------------------------------------
# TestEstimatePrintCostFromMesh
# ---------------------------------------------------------------------------


class TestEstimatePrintCostFromMesh:
    """Tests for estimate_print_cost_from_mesh MCP tool."""

    @patch("kiln.cost_estimator.CostEstimator")
    def test_happy_path(self, mock_cls, registered_tools):
        mock_estimator = MagicMock()
        mock_estimator.estimate_from_mesh.return_value = SimpleNamespace(
            to_dict=lambda: {"total_cost_usd": 1.50, "material_grams": 25.0},
        )
        mock_cls.return_value = mock_estimator

        result = registered_tools["estimate_print_cost_from_mesh"]("/tmp/model.stl")

        assert result["success"] is True
        assert result["total_cost_usd"] == 1.50
        mock_estimator.estimate_from_mesh.assert_called_once()

    @patch("kiln.cost_estimator.CostEstimator")
    def test_file_not_found(self, mock_cls, registered_tools):
        mock_estimator = MagicMock()
        mock_estimator.estimate_from_mesh.side_effect = FileNotFoundError("no such file")
        mock_cls.return_value = mock_estimator

        result = registered_tools["estimate_print_cost_from_mesh"]("/tmp/missing.stl")

        assert result["success"] is False
        assert "no such file" in result["error"]

    @patch("kiln.cost_estimator.CostEstimator")
    def test_invalid_input(self, mock_cls, registered_tools):
        mock_estimator = MagicMock()
        mock_estimator.estimate_from_mesh.side_effect = ValueError("bad infill")
        mock_cls.return_value = mock_estimator

        result = registered_tools["estimate_print_cost_from_mesh"](
            "/tmp/model.stl", infill_percent=200.0,
        )

        assert result["success"] is False
        assert "bad infill" in result["error"]

    @patch("kiln.cost_estimator.CostEstimator")
    def test_unexpected_exception(self, mock_cls, registered_tools):
        mock_estimator = MagicMock()
        mock_estimator.estimate_from_mesh.side_effect = RuntimeError("crash")
        mock_cls.return_value = mock_estimator

        result = registered_tools["estimate_print_cost_from_mesh"]("/tmp/model.stl")

        assert result["success"] is False
        assert "crash" in result["error"]

    @patch("kiln.cost_estimator.CostEstimator")
    def test_passes_all_kwargs(self, mock_cls, registered_tools):
        mock_estimator = MagicMock()
        mock_estimator.estimate_from_mesh.return_value = SimpleNamespace(
            to_dict=lambda: {"total_cost_usd": 2.00},
        )
        mock_cls.return_value = mock_estimator

        registered_tools["estimate_print_cost_from_mesh"](
            "/tmp/model.stl",
            material="petg",
            infill_percent=30.0,
            wall_layers=4,
            layer_height_mm=0.15,
            nozzle_mm=0.6,
            include_supports=True,
            support_density=20.0,
            adhesion_type="brim",
            electricity_rate=0.15,
            printer_wattage=350.0,
        )

        call_kwargs = mock_estimator.estimate_from_mesh.call_args
        assert call_kwargs[1]["material"] == "petg"
        assert call_kwargs[1]["infill_percent"] == 30.0
        assert call_kwargs[1]["include_supports"] is True
        assert call_kwargs[1]["adhesion_type"] == "brim"


# ---------------------------------------------------------------------------
# TestEstimateStructuralLoad
# ---------------------------------------------------------------------------


class TestEstimateStructuralLoad:
    """Tests for estimate_structural_load MCP tool."""

    @patch("kiln.design_intelligence.estimate_load_capacity")
    def test_happy_path(self, mock_fn, registered_tools):
        mock_fn.return_value = SimpleNamespace(
            to_dict=lambda: {"safe_load_kg": 5.2, "safety_factor": 3.0},
        )

        result = registered_tools["estimate_structural_load"](
            "petg", 50.0, 100.0, load_across_layers=True,
        )

        assert result["success"] is True
        assert result["safe_load_kg"] == 5.2
        mock_fn.assert_called_once_with("petg", 50.0, 100.0, load_across_layers=True)

    @patch("kiln.design_intelligence.estimate_load_capacity")
    def test_unknown_material(self, mock_fn, registered_tools):
        mock_fn.return_value = None

        result = registered_tools["estimate_structural_load"]("unobtanium", 10.0, 50.0)

        assert result["success"] is False
        assert "unobtanium" in result["error"]

    @patch("kiln.design_intelligence.estimate_load_capacity", side_effect=RuntimeError("boom"))
    def test_exception(self, mock_fn, registered_tools):
        result = registered_tools["estimate_structural_load"]("pla", 10.0, 50.0)

        assert result["success"] is False
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# TestFindDesignTemplates
# ---------------------------------------------------------------------------


class TestFindDesignTemplates:
    """Tests for find_design_templates MCP tool."""

    @patch("kiln.design_intelligence.find_public_design_templates")
    def test_happy_path(self, mock_fn, registered_tools):
        mock_fn.return_value = [_fake_public_template("snap_fit_cantilever")]

        result = registered_tools["find_design_templates"]("battery cover")

        assert result["success"] is True
        # Both libraries: the mocked pattern plus whatever parametric
        # parts really match.  The pattern is still there and still last.
        assert result["pattern_count"] == 1
        assert result["generatable_count"] >= 1
        assert result["count"] == (
            result["generatable_count"] + result["pattern_count"]
        )
        assert result["templates"][-1]["template_id"] == "snap_fit_cantilever"
        mock_fn.assert_called_once_with("battery cover")

    @patch("kiln.design_intelligence.find_public_design_templates")
    def test_generatable_parts_rank_before_patterns(
        self, mock_fn, registered_tools,
    ):
        """A renderable part is the more useful answer, so it comes first."""
        mock_fn.return_value = [_fake_public_template("snap_fit_cantilever")]

        result = registered_tools["find_design_templates"]("battery")
        flags = [t["generatable"] for t in result["templates"]]

        assert flags, "expected at least one match"
        assert flags == sorted(flags, reverse=True)

    def test_every_result_says_whether_it_can_be_generated(
        self, registered_tools,
    ):
        result = registered_tools["find_design_templates"]("bracket")

        assert result["templates"]
        for template in result["templates"]:
            assert "generatable" in template
            assert "next_step" in template

    def test_generatable_ids_are_actually_renderable(self, registered_tools):
        """The regression this whole change exists for.

        Discovery read only the design-PATTERN library, so every one of
        find_design_templates' own docstring examples returned an id
        that generate_from_template rejects with NOT_FOUND.  Any id
        offered as generatable must be one the generator accepts.
        """
        import json
        from pathlib import Path

        import kiln.server as _srv

        library = json.loads(
            (Path(_srv.__file__).parent / "data" / "design_templates.json")
            .read_text(encoding="utf-8")
        )
        renderable = {k for k in library if not k.startswith("_")}

        for use_case in (
            "enclosure", "gear train", "battery cover", "storage bin",
            "cable clip", "shelf bracket", "hose adapter", "garden stake",
        ):
            result = registered_tools["find_design_templates"](use_case)
            for template in result["templates"]:
                if template["generatable"]:
                    assert template["template_id"] in renderable, (
                        f"{use_case!r} offered "
                        f"{template['template_id']!r} as generatable, but "
                        f"generate_from_template cannot render it"
                    )

    def test_whole_parametric_library_is_reachable(self, registered_tools):
        """No part may be unreachable from every search — that is the bug.

        Searching each part's own display name must find it.  A template
        nobody can search for is invisible however good its geometry is.
        """
        catalog = registered_tools["list_design_templates_catalog"]()
        parts = [t for t in catalog["templates"] if t["generatable"]]
        assert len(parts) >= 65

        for part in parts:
            found = registered_tools["find_design_templates"](
                part["display_name"]
            )
            ids = {t["template_id"] for t in found["templates"]}
            assert part["template_id"] in ids, (
                f"{part['template_id']} cannot be found by its own name"
            )

    @patch("kiln.design_intelligence.find_public_design_templates")
    def test_empty_results(self, mock_fn, registered_tools):
        mock_fn.return_value = []

        result = registered_tools["find_design_templates"]("alien spaceship")

        assert result["success"] is True
        assert result["count"] == 0
        assert result["templates"] == []

    @patch(
        "kiln.design_intelligence.find_public_design_templates",
        side_effect=RuntimeError("boom"),
    )
    def test_exception(self, mock_fn, registered_tools):
        result = registered_tools["find_design_templates"]("enclosure")

        assert result["success"] is False
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# TestGenerateFromTemplateRejection
# ---------------------------------------------------------------------------


class TestGenerateFromTemplateRejection:
    """A rejected id has to say WHICH library it came from.

    Both libraries are called "design templates" and one search returns
    them side by side, so a design PATTERN reaching the generator is the
    predictable miss.  Listing 65 ids and leaving the caller to diff them
    by eye is not an answer.
    """

    @patch("kiln.server._check_auth", return_value=None)
    def test_pattern_id_is_named_as_a_pattern(self, _auth):
        from kiln.server import generate_from_template

        result = generate_from_template("snap_fit_cantilever")

        assert result["success"] is False
        assert result["error"]["code"] == "NOT_FOUND"
        message = result["error"]["message"]
        assert "design PATTERN" in message
        assert "get_design_template_info" in message
        assert "compile_scad" in message

    @patch("kiln.server._check_auth", return_value=None)
    def test_unknown_id_still_lists_the_parts(self, _auth):
        from kiln.server import generate_from_template

        result = generate_from_template("definitely_not_a_template")

        assert result["success"] is False
        message = result["error"]["message"]
        assert "design PATTERN" not in message
        assert "shelf_bracket" in message


# ---------------------------------------------------------------------------
# TestTemplateUsageIsRecorded
# ---------------------------------------------------------------------------


class TestTemplateUsageIsRecorded:
    """The generator has to phone the counter, or the wire measures nothing.

    A recorder nobody calls is the same blind spot with extra steps, so
    the call is pinned at the tool, not just unit-tested in isolation.
    """

    @staticmethod
    def _succeeded_provider(tmp_path):
        job = MagicMock()
        job.status.value = "succeeded"
        job.error = None
        job.to_dict.return_value = {"id": "j1", "status": "succeeded"}

        stl = tmp_path / "out.stl"
        stl.write_text("solid x\nendsolid x\n", encoding="utf-8")

        dl = MagicMock()
        dl.local_path = str(stl)
        dl.to_dict.return_value = {"local_path": str(stl)}

        provider = MagicMock()
        provider.generate.return_value = job
        provider.download_result.return_value = dl
        return provider

    @patch("kiln.server._check_auth", return_value=None)
    def test_a_successful_build_is_counted_by_template_id(
        self, _auth, tmp_path,
    ):
        import kiln.daily_stats as stats
        import kiln.server as srv

        with patch.object(
            srv, "_get_generation_provider",
            return_value=self._succeeded_provider(tmp_path),
        ), patch.object(srv, "validate_mesh") as mock_validate, patch.object(
            stats, "record_template_use"
        ) as mock_record:
            mock_validate.return_value = SimpleNamespace(
                to_dict=lambda: {"valid": True}, bounding_box=None,
            )
            srv.generate_from_template("shelf_bracket", {"arm_length": 200})

        mock_record.assert_called_once_with("shelf_bracket")

    @patch("kiln.server._check_auth", return_value=None)
    def test_a_rejected_template_is_not_counted(self, _auth):
        import kiln.daily_stats as stats
        import kiln.server as srv

        with patch.object(stats, "record_template_use") as mock_record:
            srv.generate_from_template("snap_fit_cantilever")

        mock_record.assert_not_called()

    @patch("kiln.server._check_auth", return_value=None)
    def test_a_counter_failure_never_breaks_the_build(self, _auth, tmp_path):
        """Telemetry is silent by contract, including when it is broken."""
        import kiln.daily_stats as stats
        import kiln.server as srv

        with patch.object(
            srv, "_get_generation_provider",
            return_value=self._succeeded_provider(tmp_path),
        ), patch.object(srv, "validate_mesh") as mock_validate, patch.object(
            stats, "record_template_use", side_effect=RuntimeError("boom")
        ):
            mock_validate.return_value = SimpleNamespace(
                to_dict=lambda: {"valid": True}, bounding_box=None,
            )
            result = srv.generate_from_template("shelf_bracket")

        assert result["success"] is True


# ---------------------------------------------------------------------------
# TestGetDesignTemplateInfo
# ---------------------------------------------------------------------------


class TestGetDesignTemplateInfo:
    """Tests for get_design_template_info MCP tool."""

    @patch("kiln.design_intelligence.get_public_design_template")
    def test_happy_path(self, mock_fn, registered_tools):
        mock_fn.return_value = _fake_public_template()

        result = registered_tools["get_design_template_info"]("snap_fit_cantilever")

        assert result["success"] is True
        assert result["template_id"] == "snap_fit_cantilever"
        assert "design_rules" not in result
        assert "agent_instruction" not in result
        mock_fn.assert_called_once_with("snap_fit_cantilever")

    @patch("kiln.design_intelligence.list_public_design_templates")
    @patch("kiln.design_intelligence.get_public_design_template")
    def test_unknown_pattern(self, mock_get, mock_list, registered_tools):
        mock_get.return_value = None
        mock_list.return_value = [_fake_public_template("snap_fit_cantilever")]

        result = registered_tools["get_design_template_info"]("nonexistent")

        assert result["success"] is False
        assert "nonexistent" in result["error"]
        assert "snap_fit_cantilever" in result["error"]

    @patch(
        "kiln.design_intelligence.get_public_design_template",
        side_effect=RuntimeError("boom"),
    )
    def test_exception(self, mock_fn, registered_tools):
        result = registered_tools["get_design_template_info"]("snap_fit_cantilever")

        assert result["success"] is False
        assert "boom" in result["error"]

    def test_resolves_a_parametric_part(self, registered_tools):
        result = registered_tools["get_design_template_info"]("shelf_bracket")

        assert result["success"] is True
        assert result["generatable"] is True
        assert result["parameters"]["arm_length"]["unit"] == "mm"

    def test_pattern_is_labelled_not_generatable(self, registered_tools):
        result = registered_tools["get_design_template_info"](
            "snap_fit_cantilever"
        )

        assert result["success"] is True
        assert result["generatable"] is False
        # The leak guard still holds: private fields stay private.
        assert "design_rules" not in result
        assert "agent_instruction" not in result


# ---------------------------------------------------------------------------
# TestGetMaterialDesignProfile
# ---------------------------------------------------------------------------


class TestGetMaterialDesignProfile:
    """Tests for get_material_design_profile MCP tool."""

    @patch("kiln.design_intelligence.get_public_material_profile")
    def test_happy_path(self, mock_fn, registered_tools):
        mock_fn.return_value = _fake_material_profile()

        result = registered_tools["get_material_design_profile"]("pla")

        assert result["success"] is True
        assert set(result) == {"material_id", "display_name", "success"}
        assert result["material_id"] == "pla"
        mock_fn.assert_called_once_with("pla")

    @patch("kiln.design_intelligence.get_public_material_profile")
    def test_unknown_material(self, mock_fn, registered_tools):
        mock_fn.return_value = None

        result = registered_tools["get_material_design_profile"]("unobtanium")

        assert result["success"] is False
        assert "unobtanium" in result["error"]

    @patch(
        "kiln.design_intelligence.get_public_material_profile",
        side_effect=RuntimeError("boom"),
    )
    def test_exception(self, mock_fn, registered_tools):
        result = registered_tools["get_material_design_profile"]("pla")

        assert result["success"] is False
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# TestGetPostProcessingGuide
# ---------------------------------------------------------------------------


class TestGetPostProcessingGuide:
    """Tests for get_post_processing_guide MCP tool."""

    @patch("kiln.design_intelligence.get_public_post_processing")
    def test_happy_path(self, mock_fn, registered_tools):
        mock_fn.return_value = SimpleNamespace(
            material="pla",
            techniques=[
                {
                    "name": "Sanding",
                    "difficulty": "easy",
                    "tools_needed": ["sandpaper"],
                },
            ],
            paintability={"primer_needed": True},
            strengthening=[{"method": "Annealing", "applicable": True}],
            upgrade_hint="Ask one focused question for more detail.",
        )

        result = registered_tools["get_post_processing_guide"]("pla")

        assert result["success"] is True
        assert set(result) == {
            "success",
            "material",
            "available_goals",
            "techniques",
            "paintability",
            "strengthening",
            "upgrade_hint",
        }
        assert result["material"] == "pla"
        assert result["available_goals"] == [
            "surface_finish",
            "paint",
            "strengthen",
        ]
        assert result["techniques"] == [
            {"name": "Sanding", "difficulty": "easy"},
        ]
        mock_fn.assert_called_once_with("pla")

    @patch("kiln.design_intelligence.get_public_post_processing")
    def test_goal_returns_only_requested_band(self, mock_fn, registered_tools):
        mock_fn.return_value = SimpleNamespace(
            material="pla",
            techniques=[{"name": "Sanding", "difficulty": "easy"}],
            paintability={"primer_needed": True, "paint_types": ["acrylic"]},
            strengthening=[{"method": "Annealing", "applicable": True}],
            upgrade_hint="",
        )

        result = registered_tools["get_post_processing_guide"]("pla", "paint")

        assert result["success"] is True
        assert set(result) == {
            "success",
            "material",
            "goal",
            "answer",
            "upgrade_hint",
        }
        assert result["goal"] == "paint"
        assert result["answer"] == {
            "paintability": {
                "primer_needed": True,
                "paint_types": ["acrylic"],
            },
        }
        assert "techniques" not in result
        assert "strengthening" not in result

    @patch("kiln.design_intelligence.get_public_post_processing")
    def test_unknown_material(self, mock_fn, registered_tools):
        mock_fn.return_value = None

        result = registered_tools["get_post_processing_guide"]("unobtanium")

        assert result["success"] is False
        assert "unobtanium" in result["error"]

    @patch(
        "kiln.design_intelligence.get_public_post_processing",
        side_effect=RuntimeError("boom"),
    )
    def test_exception(self, mock_fn, registered_tools):
        result = registered_tools["get_post_processing_guide"]("pla")

        assert result["success"] is False
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# TestRetiredPrinterRecordTools
# ---------------------------------------------------------------------------


class TestRetiredPrinterRecordTools:
    def test_whole_record_tools_are_not_registered(self, registered_tools):
        retired = {
            "get_brand_filament_profile",
            "get_printer_design_capabilities",
            "list_brand_filament_profiles",
            "list_printer_design_profiles",
        }
        assert retired.isdisjoint(registered_tools)


class TestQuestionScopedKnowledgeTools:
    @patch("kiln.design_intelligence.troubleshoot_print_issue")
    def test_troubleshooting_requires_a_real_symptom(
        self,
        mock_fn,
        registered_tools,
    ):
        result = registered_tools["troubleshoot_print_issue"]("pla", "  ")

        assert result["success"] is False
        assert "symptom is required" in result["error"]
        mock_fn.assert_not_called()

    @patch("kiln.design_intelligence.troubleshoot_print_issue")
    def test_troubleshooting_passes_the_named_symptom_only(
        self,
        mock_fn,
        registered_tools,
    ):
        mock_fn.return_value = SimpleNamespace(
            matched_issues=[{"symptom": "stringing"}],
            to_dict=lambda: {
                "material": "pla",
                "symptom": "stringing",
                "matched_issues": [{"symptom": "stringing"}],
            },
        )

        result = registered_tools["troubleshoot_print_issue"](
            "pla",
            " stringing ",
        )

        assert result["success"] is True
        assert result["match_count"] == 1
        mock_fn.assert_called_once_with("pla", "stringing")

    @patch("kiln.design_intelligence.check_printer_material_compatibility")
    def test_printer_compatibility_requires_a_real_material(
        self,
        mock_fn,
        registered_tools,
    ):
        result = registered_tools["check_printer_material_compatibility"](
            "ender3",
            " ",
        )

        assert result["success"] is False
        assert "material is required" in result["error"]
        mock_fn.assert_not_called()

    @patch("kiln.design_intelligence.check_printer_material_compatibility")
    def test_printer_compatibility_returns_only_the_named_material(
        self,
        mock_fn,
        registered_tools,
    ):
        mock_fn.return_value = SimpleNamespace(
            materials={"pla": {"status": "compatible"}},
            to_dict=lambda: {
                "printer_id": "ender3",
                "materials": {"pla": {"status": "compatible"}},
            },
        )

        result = registered_tools["check_printer_material_compatibility"](
            "ender3",
            " pla ",
        )

        assert result == {
            "success": True,
            "printer_id": "ender3",
            "materials": {"pla": {"status": "compatible"}},
        }
        mock_fn.assert_called_once_with("ender3", "pla")

    @patch("kiln.design_intelligence.get_print_diagnostic")
    def test_print_diagnostic_requires_a_real_symptom(
        self,
        mock_fn,
        registered_tools,
    ):
        result = registered_tools["get_print_diagnostic"]("pla", "")

        assert result["success"] is False
        assert "symptom is required" in result["error"]
        mock_fn.assert_not_called()


class TestResolveFilamentProfileBoundary:
    @patch("kiln.design_intelligence.resolve_filament")
    def test_projects_operational_settings_not_the_private_record(
        self,
        mock_fn,
        registered_tools,
    ):
        mock_fn.return_value = SimpleNamespace(
            material_id="pla",
            display_name="Example PLA",
            is_brand_specific=True,
            nozzle_temp_optimal_c=220,
            nozzle_temp_range_c=[210, 230],
            bed_temp_optimal_c=60,
            bed_temp_range_c=[50, 70],
            max_volumetric_speed_mm3s=18.0,
            max_print_speed_mms=250,
            drying_temp_c=55,
            drying_time_hours=6,
            enclosure_required=False,
            hardened_nozzle_required=False,
            ams_compatible=True,
            warnings=[],
            brand_profile_id="private_profile_key",
            density_g_per_cm3=1.24,
            cost_per_kg_usd=99.0,
            filament_diameter_mm=1.75,
        )

        result = registered_tools["resolve_filament_profile"](
            "example_pla",
            "bambu_a1",
        )

        assert set(result) == {
            "success",
            "material",
            "display_name",
            "is_brand_specific",
            "print_settings",
            "preparation",
            "warnings",
        }
        assert result["print_settings"]["nozzle_temp_c"] == {
            "target": 220,
            "range": [210, 230],
        }
        assert "brand_profile_id" not in result
        assert "density_g_per_cm3" not in result
        assert "cost_per_kg_usd" not in result
        assert "filament_diameter_mm" not in result
        mock_fn.assert_called_once_with(
            "example_pla",
            printer_id="bambu_a1",
        )


# ---------------------------------------------------------------------------
# TestListDesignMaterials
# ---------------------------------------------------------------------------


class TestListDesignMaterials:
    """Tests for list_design_materials MCP tool."""

    @patch("kiln.design_intelligence.list_public_material_profiles")
    def test_happy_path(self, mock_fn, registered_tools):
        mock_fn.return_value = [_fake_material_profile("pla"), _fake_material_profile("petg")]

        result = registered_tools["list_design_materials"]()

        assert result["success"] is True
        assert set(result) == {"success", "materials", "count"}
        assert result["count"] == 2
        assert len(result["materials"]) == 2
        assert result["materials"][0]["material_id"] == "pla"

    @patch("kiln.design_intelligence.list_public_material_profiles")
    def test_summary_fields_present(self, mock_fn, registered_tools):
        mock_fn.return_value = [_fake_material_profile()]

        result = registered_tools["list_design_materials"]()
        mat = result["materials"][0]

        assert set(mat) == {
            "material_id",
            "display_name",
            "category",
            "max_service_temp_c",
            "food_safe",
            "ease_of_print",
        }
        assert "max_service_temp_c" in mat
        assert "food_safe" in mat
        assert "tensile_strength_mpa" not in mat
        assert "top_guidance" not in mat

    @patch(
        "kiln.design_intelligence.list_public_material_profiles",
        side_effect=RuntimeError("boom"),
    )
    def test_exception(self, mock_fn, registered_tools):
        result = registered_tools["list_design_materials"]()

        assert result["success"] is False
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# TestListDesignTemplatesCatalog
# ---------------------------------------------------------------------------


class TestListDesignTemplatesCatalog:
    """Tests for list_design_templates_catalog MCP tool."""

    @patch("kiln.design_intelligence.list_public_design_templates")
    def test_happy_path(self, mock_fn, registered_tools):
        mock_fn.return_value = [
            _fake_public_template("snap_fit_cantilever"),
            _fake_public_template("press_fit"),
        ]

        result = registered_tools["list_design_templates_catalog"]()

        assert result["success"] is True
        assert result["pattern_count"] == 2
        # The parametric parts are the larger library and lead the list.
        assert result["generatable_count"] >= 65
        assert result["count"] == (
            result["generatable_count"] + result["pattern_count"]
        )
        assert result["templates"][0]["generatable"] is True
        assert result["templates"][-1]["template_id"] == "press_fit"

    @patch("kiln.design_intelligence.list_public_design_templates")
    def test_pattern_summary_fields_present(self, mock_fn, registered_tools):
        mock_fn.return_value = [_fake_public_template()]

        result = registered_tools["list_design_templates_catalog"]()
        pat = result["templates"][-1]

        assert "template_id" in pat
        assert "display_name" in pat
        assert "description" in pat
        assert "use_cases" in pat
        assert "best_materials" in pat
        assert pat["generatable"] is False

    def test_part_summary_carries_its_parameters(self, registered_tools):
        """The dimensions a user brings are the reason to offer a part."""
        result = registered_tools["list_design_templates_catalog"]()
        part = next(t for t in result["templates"] if t["generatable"])

        assert part["parameters"]
        spec = next(iter(part["parameters"].values()))
        assert "default" in spec
        assert "unit" in spec

    @patch(
        "kiln.design_intelligence.list_public_design_templates",
        side_effect=RuntimeError("boom"),
    )
    def test_exception(self, mock_fn, registered_tools):
        result = registered_tools["list_design_templates_catalog"]()

        assert result["success"] is False
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# TestMatchDesignRequirements
# ---------------------------------------------------------------------------


class TestMatchDesignRequirements:
    """Tests for match_design_requirements MCP tool."""

    @patch("kiln.design_intelligence.match_requirements")
    def test_happy_path(self, mock_fn, registered_tools):
        mock_fn.return_value = [
            SimpleNamespace(to_dict=lambda: {"requirement": "load_bearing", "rules": []}),
        ]

        result = registered_tools["match_design_requirements"]("shelf bracket holding 10 lbs")

        assert result["success"] is True
        assert result["count"] == 1
        assert result["matched_requirements"][0]["requirement"] == "load_bearing"
        mock_fn.assert_called_once_with("shelf bracket holding 10 lbs")

    @patch("kiln.design_intelligence.match_requirements")
    def test_no_matches(self, mock_fn, registered_tools):
        mock_fn.return_value = []

        result = registered_tools["match_design_requirements"]("decorative figurine")

        assert result["success"] is True
        assert result["count"] == 0
        assert result["matched_requirements"] == []

    @patch("kiln.design_intelligence.match_requirements", side_effect=RuntimeError("boom"))
    def test_exception(self, mock_fn, registered_tools):
        result = registered_tools["match_design_requirements"]("anything")

        assert result["success"] is False
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# TestRecommendDesignMaterial
# ---------------------------------------------------------------------------


class TestRecommendDesignMaterial:
    """Tests for recommend_design_material MCP tool."""

    @patch("kiln.design_intelligence.recommend_material_for_design")
    def test_happy_path(self, mock_fn, registered_tools):
        mock_fn.return_value = SimpleNamespace(
            to_dict=lambda: {"material": "petg", "reasoning": "Best for outdoor use"},
        )

        result = registered_tools["recommend_design_material"]("outdoor shelf bracket")

        assert result["success"] is True
        assert result["material"] == "petg"
        mock_fn.assert_called_once_with(
            "outdoor shelf bracket",
            printer_has_enclosure=False,
            printer_has_direct_drive=True,
            max_hotend_temp_c=300,
        )

    @patch("kiln.design_intelligence.recommend_material_for_design")
    def test_passes_printer_constraints(self, mock_fn, registered_tools):
        mock_fn.return_value = SimpleNamespace(
            to_dict=lambda: {"material": "nylon"},
        )

        registered_tools["recommend_design_material"](
            "load-bearing bracket",
            printer_has_enclosure=True,
            printer_has_direct_drive=False,
            max_hotend_temp_c=280,
        )

        mock_fn.assert_called_once_with(
            "load-bearing bracket",
            printer_has_enclosure=True,
            printer_has_direct_drive=False,
            max_hotend_temp_c=280,
        )

    @patch("kiln.design_intelligence.recommend_material_for_design", side_effect=RuntimeError("boom"))
    def test_exception(self, mock_fn, registered_tools):
        result = registered_tools["recommend_design_material"]("anything")

        assert result["success"] is False
        assert "boom" in result["error"]

    @patch("kiln.design_intelligence.recommend_material_for_design")
    def test_skin_contact_floor_surfaced_for_wearable(self, mock_rec, registered_tools):
        # A worn-against-skin request surfaces the free skin-contact floor +
        # a warning inline — the caution reaches the user without asking.
        mock_rec.return_value = SimpleNamespace(to_dict=lambda: {"material": "pla"})

        result = registered_tools["recommend_design_material"]("a ring worn daily")

        assert result["success"] is True
        sc = result.get("skin_contact")
        assert sc is not None, "wearable intent must surface the skin-contact floor"
        assert sc["never_skin_safe"] is True
        assert sc["refer_to_medical"] and sc["honesty_note"]
        assert any("SKIN CONTACT" in w for w in result.get("warnings", []))

    @patch("kiln.design_intelligence.recommend_material_for_design")
    def test_no_skin_contact_block_for_non_wearable(self, mock_rec, registered_tools):
        mock_rec.return_value = SimpleNamespace(to_dict=lambda: {"material": "petg"})

        result = registered_tools["recommend_design_material"]("an outdoor shelf bracket")

        assert "skin_contact" not in result

    @patch("kiln.design_intelligence.get_material_profile")
    @patch("kiln.design_intelligence.recommend_material_for_design")
    def test_bonding_caveat_surfaced(self, mock_rec, mock_profile, registered_tools):
        # Recommender picks PP; its (Pro-overlay) bonding block is very_hard,
        # so the tool surfaces a structured bonding block + a top-of-list
        # warning and points at recommend_adhesive.
        mock_rec.return_value = SimpleNamespace(
            to_dict=lambda: {"material": "pp", "reasoning": "stiff + chemical"},
        )
        mock_profile.return_value = SimpleNamespace(
            bonding={
                "bonding_difficulty": "very_hard",
                "primer_required": True,
                "recommended_primer": "synthetic test primer",
                "bonding_note": "synthetic test note",
            },
            bonding_caveat=lambda: "synthetic caveat mentioning hard to bond",
        )

        result = registered_tools["recommend_design_material"]("chemical tank fitting")

        assert result["success"] is True
        assert result["bonding"]["material"] == "pp"
        assert result["bonding"]["difficulty"] == "very_hard"
        assert result["bonding"]["primer_required"] is True
        assert result["bonding"]["for_details_use"] == "recommend_adhesive"
        assert any("hard to bond" in w for w in result.get("warnings", []))

    @patch("kiln.design_intelligence.get_material_profile")
    @patch("kiln.design_intelligence.recommend_material_for_design")
    def test_bonding_absent_for_free_tier(self, mock_rec, mock_profile, registered_tools):
        # Free tier: overlay absent -> profile.bonding empty -> no bonding key,
        # no bonding warning, recommendation otherwise unaffected.
        mock_rec.return_value = SimpleNamespace(to_dict=lambda: {"material": "petg"})
        mock_profile.return_value = SimpleNamespace(bonding={}, bonding_caveat=lambda: "")

        result = registered_tools["recommend_design_material"]("outdoor bracket")

        assert result["success"] is True
        assert "bonding" not in result

    @patch("kiln.design_intelligence.get_material_profile")
    @patch("kiln.design_intelligence.recommend_material_for_design")
    def test_bonding_floor_nudge_free_tier(self, mock_rec, mock_profile, registered_tools):
        # Free tier with the public common-knowledge floor flag (no overlay):
        # a minimal bonding block (hard_to_bond + upgrade_url, no Pro fields)
        # and the upgrade nudge as a top-of-list warning.
        mock_rec.return_value = SimpleNamespace(to_dict=lambda: {"material": "pp"})
        mock_profile.return_value = SimpleNamespace(
            bonding={"hard_to_bond": True},
            bonding_caveat=lambda: (
                "Polypropylene bonds poorly with common glues. ... "
                "See https://kiln3d.com/pricing"
            ),
        )

        result = registered_tools["recommend_design_material"]("chemical tank fitting")

        assert result["success"] is True
        assert result["bonding"]["hard_to_bond"] is True
        assert result["bonding"]["upgrade_url"] == "https://kiln3d.com/pricing"
        assert "difficulty" not in result["bonding"]  # no Pro verdict leaked
        assert "for_details_use" not in result["bonding"]
        assert any("kiln3d.com/pricing" in w for w in result.get("warnings", []))


# ---------------------------------------------------------------------------
# TestFastenerAdviceOnTemplateBuilds
# ---------------------------------------------------------------------------


def _succeeded_template_provider(tmp_path):
    """A generation provider that reports one successful STL build."""
    job = MagicMock()
    job.status.value = "succeeded"
    job.error = None
    job.to_dict.return_value = {"id": "j1", "status": "succeeded"}

    stl = tmp_path / "out.stl"
    stl.write_text("solid x\nendsolid x\n", encoding="utf-8")

    dl = MagicMock()
    dl.local_path = str(stl)
    dl.to_dict.return_value = {"local_path": str(stl)}

    provider = MagicMock()
    provider.generate.return_value = job
    provider.download_result.return_value = dl
    return provider


def _build(tmp_path, template_id, parameters=None):
    """Run generate_from_template with the compile stood in for."""
    import kiln.server as srv

    with patch.object(
        srv, "_get_generation_provider",
        return_value=_succeeded_template_provider(tmp_path),
    ), patch.object(srv, "validate_mesh") as mock_validate:
        mock_validate.return_value = SimpleNamespace(
            to_dict=lambda: {"valid": True}, bounding_box=None,
        )
        return srv.generate_from_template(template_id, parameters)


class TestFastenerAdviceOnTemplateBuilds:
    """A caller-supplied screw/bolt/bore/dowel/pin/magnet dimension is used
    exactly as given, and the result says so once.

    The trigger is DECLARED, never inferred: ``"fastener": true`` on the
    parameter in ``design_templates.json``.  A name-matching rule would fire
    on ``nozzle_dia`` (a nozzle) and miss ``screw_hole`` (a screw), and
    nobody would ever see the rule that decided it.
    """

    @patch("kiln.server._check_auth", return_value=None)
    def test_a_caller_supplied_fastener_dimension_gets_the_advisory(
        self, _auth, tmp_path,
    ):
        result = _build(tmp_path, "shelf_bracket", {"hole_dia": 5})

        advice = result["fastener_advice"]
        assert advice["parameters"] == ["hole_dia"]
        assert advice["pro_depth_applied"] is False
        assert "hole_dia" in advice["note"]
        assert advice["agent_instruction"]

    @patch("kiln.server._check_auth", return_value=None)
    def test_taking_the_defaults_says_nothing(self, _auth, tmp_path):
        """Not on every build — only when the caller stated the dimension."""
        assert "fastener_advice" not in _build(tmp_path, "shelf_bracket")
        assert "fastener_advice" not in _build(tmp_path, "shelf_bracket", {})

    @patch("kiln.server._check_auth", return_value=None)
    def test_a_non_fastener_override_says_nothing(self, _auth, tmp_path):
        """arm_length is a dimension; it is not a hole for hardware."""
        result = _build(tmp_path, "shelf_bracket", {"arm_length": 200})
        assert "fastener_advice" not in result

    @patch("kiln.server._check_auth", return_value=None)
    def test_a_slot_or_nozzle_dimension_is_not_a_fastener(
        self, _auth, tmp_path,
    ):
        """The judgement calls, pinned: these size a nozzle and a cable."""
        assert "fastener_advice" not in _build(
            tmp_path, "nozzle_rack", {"nozzle_dia": 9},
        )
        assert "fastener_advice" not in _build(
            tmp_path, "cable_grommet", {"cable_slot_width": 12},
        )

    @patch("kiln.server._check_auth", return_value=None)
    def test_the_free_block_carries_no_size_no_fit_and_no_mechanics(
        self, _auth, tmp_path,
    ):
        """The moat rule, as a test.

        Sizing IS the paid capability.  A free result that names a
        recommended number, a clearance, a tolerance, a fit class, or the
        checklist the paid analysis runs hands the answer over for nothing.
        """
        advice = _build(
            tmp_path, "fridge_magnet", {"magnet_diameter": 12},
        )["fastener_advice"]
        blob = f"{advice['note']} {advice['agent_instruction']}".lower()

        import re

        assert not any(ch.isdigit() for ch in blob), blob
        for banned in (
            r"clearance", r"tolerance", r"press fit", r"close fit",
            r"loose fit", r"shrink", r"compensat", r"\bmm\b",
            r"wall thickness", r"edge distance", r"engagement", r"torque",
            r"chamfer",
        ):
            assert not re.search(banned, blob), (
                f"free copy leaks {banned!r}: {blob}"
            )

    @patch("kiln.server._check_auth", return_value=None)
    def test_the_free_block_survives_kiln_pro_being_absent(
        self, _auth, tmp_path, monkeypatch,
    ):
        """The public install path: the import fails and nothing else does."""
        import builtins

        real_import = builtins.__import__

        def _no_kiln_pro(name, *args, **kwargs):
            if name.startswith("kiln_pro"):
                raise ImportError("No module named 'kiln_pro'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_kiln_pro)
        result = _build(tmp_path, "hook", {"screw_hole_dia": 4})

        assert result["success"] is True
        assert result["fastener_advice"]["pro_depth_applied"] is False

    @patch("kiln.server._check_auth", return_value=None)
    def test_an_entitled_kiln_pro_replaces_the_same_field(
        self, _auth, tmp_path, monkeypatch,
    ):
        """The surface deepens, it does not change shape: same field name."""
        import kiln.server as srv

        deep = {"pro_depth_applied": True, "holes": [{"parameter": "hole_dia"}]}
        fake = SimpleNamespace(
            is_available=lambda feature: feature == "fastener_hole_advice",
            fastener_hole_advice=SimpleNamespace(
                advise_template_holes=lambda *a, **k: deep,
            ),
        )
        monkeypatch.setitem(
            __import__("sys").modules, "kiln_pro.bridge",
            SimpleNamespace(pro_features=fake),
        )
        result = _build(tmp_path, "shelf_bracket", {"hole_dia": 5})

        assert result["fastener_advice"] == deep
        assert srv  # the tool under test, not a stand-in

    @patch("kiln.server._check_auth", return_value=None)
    def test_an_advisory_fault_never_breaks_the_build(self, _auth, tmp_path):
        """The part is the product; the advisory is not worth losing it for."""
        import kiln.server as srv

        with patch.object(
            srv, "_fastener_advice_free", side_effect=RuntimeError("boom"),
        ):
            result = _build(tmp_path, "shelf_bracket", {"hole_dia": 5})

        assert result["success"] is True
        assert "fastener_advice" not in result


class TestFastenerFlagsAreWellFormed:
    """The flag is data, so the data is what gets checked."""

    @staticmethod
    def _flagged():
        import json
        from pathlib import Path

        import kiln.server as srv

        path = Path(srv.__file__).parent / "data" / "design_templates.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return [
            (tid, pname, pspec)
            for tid, tpl in data.items()
            if not tid.startswith("_")
            for pname, pspec in (tpl.get("parameters") or {}).items()
            if pspec.get("fastener")
        ]

    def test_every_flagged_parameter_is_a_millimetre_dimension(self):
        """A count is not a hole.  ``rail_clamp.mount_holes`` is a count and
        ``switch_panel.mount_holes`` is a diameter — which is exactly why the
        flag is per-parameter data rather than a rule about names."""
        for tid, pname, pspec in self._flagged():
            assert pspec.get("unit") == "mm", f"{tid}.{pname} is not in mm"
            assert isinstance(pspec.get("default"), (int, float)), (
                f"{tid}.{pname} has no numeric default"
            )

    def test_the_flag_is_a_bare_boolean(self):
        """No sizes, no tables, no curated text rides in on this flag."""
        for tid, pname, pspec in self._flagged():
            assert pspec["fastener"] is True, f"{tid}.{pname}"

    def test_the_library_actually_carries_flags(self):
        assert len(self._flagged()) >= 15
