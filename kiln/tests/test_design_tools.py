"""Tests for design intelligence MCP tools in the design-tools plugin.

Covers:
- check_material_environment — happy path, unknown material, exception
- check_multi_material_pairing — happy path, exception
- estimate_print_cost_from_mesh — happy path, file not found, bad input, exception
- estimate_structural_load — happy path, unknown material, exception
- find_design_templates — happy path, empty results, exception
- get_design_template_info — happy path, unknown pattern, exception
- get_material_design_profile — happy path, unknown material, exception
- get_post_processing_guide — happy path, unknown material, exception
- list_design_materials — happy path, exception
- list_design_templates_catalog — happy path, exception
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
        agent_guidance=["Great for prototyping"],
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
        assert result["count"] == 1
        assert len(result["templates"]) == 1
        mock_fn.assert_called_once_with("battery cover")

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
        assert "agent_guidance" not in result
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
        assert result["count"] == 2
        assert result["templates"][0]["template_id"] == "snap_fit_cantilever"

    @patch("kiln.design_intelligence.list_public_design_templates")
    def test_summary_fields_present(self, mock_fn, registered_tools):
        mock_fn.return_value = [_fake_public_template()]

        result = registered_tools["list_design_templates_catalog"]()
        pat = result["templates"][0]

        assert "template_id" in pat
        assert "display_name" in pat
        assert "description" in pat
        assert "use_cases" in pat
        assert "best_materials" in pat

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
