"""Tests for kiln.parametric — OpenSCAD parameter parsing, updating, validation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiln.parametric import (
    ParameterDef,
    ParameterWarning,
    ScadModule,
    analyze_scad_structure,
    compile_scad_code,
    insert_into_scad_module,
    modify_scad_module,
    parse_openscad_parameters,
    tweak_and_compile,
    update_openscad_parameter,
    validate_openscad_parameters,
)

# ---------------------------------------------------------------------------
# parse_openscad_parameters
# ---------------------------------------------------------------------------


class TestParseOpenscadParameters:
    """parse_openscad_parameters extracts variable declarations."""

    def test_basic_variable_with_unit_comment(self):
        code = "wall_thickness = 2.5; // mm"
        params = parse_openscad_parameters(code)
        assert len(params) == 1
        p = params[0]
        assert p.name == "wall_thickness"
        assert p.value == 2.5
        assert p.unit == "mm"

    def test_parse_min_max_from_comment(self):
        code = "width = 75; // mm (min: 55, max: 100)"
        params = parse_openscad_parameters(code)
        assert len(params) == 1
        p = params[0]
        assert p.name == "width"
        assert p.value == 75.0
        assert p.min_value == 55.0
        assert p.max_value == 100.0

    def test_parse_degrees_unit(self):
        code = "angle = 45; // degrees"
        params = parse_openscad_parameters(code)
        assert len(params) == 1
        assert params[0].unit == "degrees"
        assert params[0].value == 45.0

    def test_variable_no_comment_defaults_unit(self):
        code = "height = 100;"
        params = parse_openscad_parameters(code)
        assert len(params) == 1
        assert params[0].name == "height"
        assert params[0].value == 100.0
        assert params[0].unit == "mm"

    def test_integer_value_parsed_as_float(self):
        code = "count = 4;"
        params = parse_openscad_parameters(code)
        assert len(params) == 1
        assert params[0].value == 4.0

    def test_stops_at_module_declaration(self):
        code = (
            "wall = 2; // mm\n"
            "module box() {\n"
            "  inner = 10;\n"
            "}\n"
        )
        params = parse_openscad_parameters(code)
        assert len(params) == 1
        assert params[0].name == "wall"

    def test_stops_at_function_declaration(self):
        code = (
            "wall = 2; // mm\n"
            "function double(x) = x * 2;\n"
            "extra = 99;\n"
        )
        params = parse_openscad_parameters(code)
        assert len(params) == 1

    def test_skips_comment_and_blank_lines(self):
        code = (
            "// This is a header comment\n"
            "\n"
            "// Another comment\n"
            "width = 50; // mm\n"
            "depth = 30; // mm\n"
        )
        params = parse_openscad_parameters(code)
        assert len(params) == 2
        assert params[0].name == "width"
        assert params[1].name == "depth"

    def test_multiple_parameters(self):
        code = (
            "width = 50; // mm\n"
            "depth = 30; // mm\n"
            "height = 20; // mm\n"
        )
        params = parse_openscad_parameters(code)
        assert len(params) == 3
        names = [p.name for p in params]
        assert names == ["width", "depth", "height"]

    def test_negative_value(self):
        code = "offset = -5; // mm"
        params = parse_openscad_parameters(code)
        assert len(params) == 1
        assert params[0].value == -5.0

    def test_stops_at_cube(self):
        code = (
            "size = 10; // mm\n"
            "cube([size, size, size]);\n"
        )
        params = parse_openscad_parameters(code)
        assert len(params) == 1

    def test_stops_at_use_keyword(self):
        code = (
            "wall = 2; // mm\n"
            'use <MCAD/boxes.scad>\n'
        )
        params = parse_openscad_parameters(code)
        assert len(params) == 1

    def test_stops_at_include_keyword(self):
        code = (
            "wall = 2; // mm\n"
            'include <config.scad>\n'
        )
        params = parse_openscad_parameters(code)
        assert len(params) == 1

    def test_parse_min_only(self):
        code = "wall = 2; // mm (min: 1.5)"
        params = parse_openscad_parameters(code)
        assert params[0].min_value == 1.5
        assert params[0].max_value is None

    def test_parse_max_only(self):
        code = "wall = 2; // mm (max: 10)"
        params = parse_openscad_parameters(code)
        assert params[0].max_value == 10.0
        assert params[0].min_value is None

    def test_parse_range_fallback(self):
        code = "width = 50; // mm (30 - 80)"
        params = parse_openscad_parameters(code)
        assert params[0].min_value == 30.0
        assert params[0].max_value == 80.0

    def test_empty_code_returns_empty(self):
        assert parse_openscad_parameters("") == []

    def test_only_comments_returns_empty(self):
        assert parse_openscad_parameters("// just a comment\n// another") == []


# ---------------------------------------------------------------------------
# update_openscad_parameter
# ---------------------------------------------------------------------------


class TestUpdateOpenscadParameter:
    """update_openscad_parameter replaces values in source code."""

    def test_basic_update(self):
        code = "wall = 2; // mm"
        result = update_openscad_parameter(code, "wall", 5)
        assert "wall = 5; // mm" in result

    def test_update_preserves_comment(self):
        code = "wall_thickness = 2; // mm (min: 1, max: 10)"
        result = update_openscad_parameter(code, "wall_thickness", 4)
        assert "wall_thickness = 4; // mm (min: 1, max: 10)" in result

    def test_raises_for_nonexistent_parameter(self):
        code = "wall = 2; // mm"
        with pytest.raises(ValueError, match="not_here"):
            update_openscad_parameter(code, "not_here", 5)

    def test_update_integer_to_float(self):
        code = "count = 4;"
        result = update_openscad_parameter(code, "count", 3.5)
        assert "count = 3.5;" in result

    def test_only_target_changed(self):
        code = "width = 50; // mm\ndepth = 30; // mm\n"
        result = update_openscad_parameter(code, "depth", 40)
        assert "width = 50; // mm" in result
        assert "depth = 40; // mm" in result

    def test_update_float_value(self):
        code = "wall = 2.5; // mm"
        result = update_openscad_parameter(code, "wall", 3.0)
        assert "wall = 3; // mm" in result

    def test_update_to_integer_representation(self):
        """When new value has no fractional part, use int form."""
        code = "size = 10.5; // mm"
        result = update_openscad_parameter(code, "size", 10.0)
        assert "size = 10;" in result


# ---------------------------------------------------------------------------
# validate_openscad_parameters
# ---------------------------------------------------------------------------


class TestValidateOpenscadParameters:
    """validate_openscad_parameters checks values against limits."""

    def test_no_material_returns_empty_when_in_range(self):
        code = "wall = 2; // mm (min: 1, max: 5)"
        warnings = validate_openscad_parameters(code)
        assert warnings == []

    def test_no_material_comment_range_violation_below_min(self):
        code = "wall = 0.5; // mm (min: 1, max: 5)"
        warnings = validate_openscad_parameters(code)
        assert len(warnings) == 1
        assert warnings[0].limit_type == "min"
        assert warnings[0].limit_value == 1.0

    def test_no_material_comment_range_violation_above_max(self):
        code = "wall = 10; // mm (min: 1, max: 5)"
        warnings = validate_openscad_parameters(code)
        assert len(warnings) == 1
        assert warnings[0].limit_type == "max"

    @patch("kiln.design_intelligence.get_material_profile")
    def test_material_wall_thickness_violation(self, mock_profile):
        mock_profile.return_value = SimpleNamespace(
            design_limits={"min_wall_thickness_mm": 1.2},
        )
        code = "wall_thickness = 0.8; // mm"
        warnings = validate_openscad_parameters(code, material="pla")
        assert any(
            w.parameter_name == "wall_thickness" and w.limit_type == "min"
            for w in warnings
        )

    @patch("kiln.design_intelligence.get_material_profile")
    def test_material_hole_diameter_violation(self, mock_profile):
        mock_profile.return_value = SimpleNamespace(
            design_limits={"min_hole_diameter_mm": 2.0},
        )
        code = "hole_size = 1.0; // mm"
        warnings = validate_openscad_parameters(code, material="pla")
        assert any(
            w.parameter_name == "hole_size" and w.limit_type == "min"
            for w in warnings
        )

    @patch("kiln.design_intelligence.get_material_profile")
    def test_parameter_within_limits_no_warning(self, mock_profile):
        mock_profile.return_value = SimpleNamespace(
            design_limits={"min_wall_thickness_mm": 1.0},
        )
        code = "wall_thickness = 2.0; // mm"
        warnings = validate_openscad_parameters(code, material="pla")
        assert warnings == []

    @patch("kiln.design_intelligence.get_material_profile")
    def test_multiple_violations(self, mock_profile):
        mock_profile.return_value = SimpleNamespace(
            design_limits={
                "min_wall_thickness_mm": 1.2,
                "min_hole_diameter_mm": 2.0,
            },
        )
        code = (
            "wall_thickness = 0.5; // mm\n"
            "hole_diameter = 1.0; // mm\n"
        )
        warnings = validate_openscad_parameters(code, material="pla")
        param_names = [w.parameter_name for w in warnings]
        assert "wall_thickness" in param_names
        assert "hole_diameter" in param_names

    def test_comment_min_max_violation_combined(self):
        code = (
            "width = 10; // mm (min: 20, max: 100)\n"
            "height = 200; // mm (min: 5, max: 50)\n"
        )
        warnings = validate_openscad_parameters(code)
        assert len(warnings) == 2
        names = {w.parameter_name for w in warnings}
        assert names == {"width", "height"}

    @patch("kiln.design_intelligence.get_material_profile")
    def test_bridge_span_exceeds_max(self, mock_profile):
        mock_profile.return_value = SimpleNamespace(
            design_limits={"max_bridge_length_mm": 20},
        )
        code = "bridge_span = 30; // mm"
        warnings = validate_openscad_parameters(code, material="pla")
        assert any(w.limit_type == "max" for w in warnings)

    @patch("kiln.design_intelligence.get_material_profile")
    def test_overhang_angle_exceeds_max(self, mock_profile):
        mock_profile.return_value = SimpleNamespace(
            design_limits={"max_unsupported_overhang_deg": 50},
        )
        code = "overhang_angle = 60; // degrees"
        warnings = validate_openscad_parameters(code, material="pla")
        assert any(w.limit_type == "max" for w in warnings)

    def test_no_params_returns_empty(self):
        code = "module box() { cube([10,10,10]); }"
        assert validate_openscad_parameters(code) == []

    @patch("kiln.design_intelligence.get_material_profile")
    def test_recommended_wall_warning(self, mock_profile):
        """Value between min and recommended triggers a recommendation warning."""
        mock_profile.return_value = SimpleNamespace(
            design_limits={
                "min_wall_thickness_mm": 0.8,
                "recommended_wall_thickness_mm": 1.6,
            },
        )
        code = "wall_thickness = 1.0; // mm"
        warnings = validate_openscad_parameters(code, material="pla")
        assert any("recommended" in w.message for w in warnings)

    @patch("kiln.design_intelligence.get_material_profile")
    def test_pin_diameter_violation(self, mock_profile):
        mock_profile.return_value = SimpleNamespace(
            design_limits={"min_pin_diameter_mm": 3.0},
        )
        code = "pin_width = 1.5; // mm"
        warnings = validate_openscad_parameters(code, material="pla")
        assert any(w.parameter_name == "pin_width" for w in warnings)


# ---------------------------------------------------------------------------
# Dataclass to_dict
# ---------------------------------------------------------------------------


class TestDataclassToDict:
    """Dataclass serialization."""

    def test_parameter_def_to_dict(self):
        p = ParameterDef(
            name="width", value=50.0, unit="mm", description="total width",
            min_value=10.0, max_value=100.0,
        )
        d = p.to_dict()
        assert d["name"] == "width"
        assert d["value"] == 50.0
        assert d["unit"] == "mm"
        assert d["description"] == "total width"
        assert d["min_value"] == 10.0
        assert d["max_value"] == 100.0

    def test_parameter_def_to_dict_no_minmax(self):
        p = ParameterDef(name="height", value=20.0)
        d = p.to_dict()
        assert "min_value" not in d
        assert "max_value" not in d

    def test_parameter_warning_to_dict(self):
        w = ParameterWarning(
            parameter_name="wall",
            current_value=0.5,
            limit_value=1.0,
            limit_type="min",
            message="too thin",
        )
        d = w.to_dict()
        assert d["parameter_name"] == "wall"
        assert d["current_value"] == 0.5
        assert d["limit_value"] == 1.0
        assert d["limit_type"] == "min"
        assert d["message"] == "too thin"


# ---------------------------------------------------------------------------
# compile_scad_code
# ---------------------------------------------------------------------------


class TestCompileScadCode:
    """compile_scad_code compiles OpenSCAD to STL."""

    @patch("kiln.generation.openscad.OpenSCADProvider")
    def test_returns_stl_path(self, MockProvider):
        mock_job = SimpleNamespace(
            id="job1",
            status=SimpleNamespace(value="succeeded"),
            error=None,
        )
        mock_result = SimpleNamespace(local_path="/tmp/test.stl")
        instance = MockProvider.return_value
        instance.generate.return_value = mock_job
        instance.download_result.return_value = mock_result

        path = compile_scad_code("cube([10,10,10]);")
        assert path == "/tmp/test.stl"

    @patch("kiln.generation.openscad.OpenSCADProvider")
    def test_raises_on_failed_job(self, MockProvider):
        mock_job = SimpleNamespace(
            id="job1",
            status=SimpleNamespace(value="failed"),
            error="syntax error",
        )
        instance = MockProvider.return_value
        instance.generate.return_value = mock_job

        with pytest.raises(ValueError, match="compilation failed"):
            compile_scad_code("invalid code")

    @patch("shutil.move")
    @patch("kiln.generation.openscad.OpenSCADProvider")
    def test_moves_to_output_path(self, MockProvider, mock_move):
        mock_job = SimpleNamespace(
            id="job1",
            status=SimpleNamespace(value="succeeded"),
            error=None,
        )
        mock_result = SimpleNamespace(local_path="/tmp/original.stl")
        instance = MockProvider.return_value
        instance.generate.return_value = mock_job
        instance.download_result.return_value = mock_result

        path = compile_scad_code("cube([10,10,10]);", output_path="/tmp/custom.stl")
        mock_move.assert_called_once_with("/tmp/original.stl", "/tmp/custom.stl")
        assert path == "/tmp/custom.stl"


# ---------------------------------------------------------------------------
# tweak_and_compile
# ---------------------------------------------------------------------------


class TestTweakAndCompile:
    """tweak_and_compile updates params and compiles."""

    @patch("kiln.generation.openscad.OpenSCADProvider")
    def test_updates_parameter_and_compiles(self, MockProvider):
        mock_job = SimpleNamespace(
            id="job1",
            status=SimpleNamespace(value="succeeded"),
            error=None,
        )
        mock_result = SimpleNamespace(local_path="/tmp/tweaked.stl")
        instance = MockProvider.return_value
        instance.generate.return_value = mock_job
        instance.download_result.return_value = mock_result

        code = "wall = 2; // mm\ncube([wall, wall, wall]);"
        result = tweak_and_compile(code, "wall", 5.0)

        assert result["parameter_name"] == "wall"
        assert result["new_value"] == 5.0
        assert "wall = 5;" in result["updated_code"]
        assert result["stl_path"] == "/tmp/tweaked.stl"
        assert isinstance(result["warnings"], list)

    def test_raises_for_missing_parameter(self):
        code = "wall = 2; // mm\ncube([wall, wall, wall]);"
        with pytest.raises(ValueError, match="not_real"):
            tweak_and_compile(code, "not_real", 5.0)


# ---------------------------------------------------------------------------
# analyze_scad_structure
# ---------------------------------------------------------------------------


class TestAnalyzeScadStructure:
    """analyze_scad_structure parses OpenSCAD code structure."""

    def test_finds_modules(self):
        code = (
            "width = 50; // mm\n"
            "// The base plate\n"
            "module base() {\n"
            "    cube([width, width, 5]);\n"
            "}\n"
            "// Top cover\n"
            "module top() {\n"
            "    cube([width, width, 2]);\n"
            "}\n"
            "base();\n"
            "top();\n"
        )
        structure = analyze_scad_structure(code)
        assert len(structure.modules) == 2
        assert structure.modules[0].name == "base"
        assert structure.modules[0].description == "The base plate"
        assert structure.modules[1].name == "top"
        assert structure.modules[1].description == "Top cover"

    def test_finds_parameters(self):
        code = (
            "width = 50; // mm\n"
            "height = 30; // mm\n"
            "module box() {\n"
            "    cube([width, height, 10]);\n"
            "}\n"
        )
        structure = analyze_scad_structure(code)
        assert len(structure.parameters) == 2

    def test_detects_library_imports(self):
        code = (
            "include <BOSL2/std.scad>\n"
            "use <MCAD/boxes.scad>\n"
            "module test() { cube(10); }\n"
        )
        structure = analyze_scad_structure(code)
        assert structure.has_library_imports is True
        assert "BOSL2" in structure.libraries_used
        assert "MCAD" in structure.libraries_used

    def test_no_imports(self):
        code = "module test() { cube(10); }\n"
        structure = analyze_scad_structure(code)
        assert structure.has_library_imports is False
        assert structure.libraries_used == []

    def test_total_lines(self):
        code = "a = 1;\nb = 2;\nmodule x() { cube(1); }"
        structure = analyze_scad_structure(code)
        assert structure.total_lines == 3

    def test_to_dict(self):
        code = "width = 10; // mm\nmodule box() {\n    cube(width);\n}\n"
        structure = analyze_scad_structure(code)
        d = structure.to_dict()
        assert "parameters" in d
        assert "modules" in d
        assert "total_lines" in d
        assert "has_library_imports" in d
        assert "libraries_used" in d


# ---------------------------------------------------------------------------
# modify_scad_module
# ---------------------------------------------------------------------------


class TestModifyScadModule:
    """modify_scad_module replaces module implementations."""

    def test_replaces_module(self):
        code = (
            "module box() {\n"
            "    cube([10, 10, 10]);\n"
            "}\n"
            "box();\n"
        )
        new_mod = "module box() {\n    sphere(r=5);\n}"
        result = modify_scad_module(code, "box", new_mod)
        assert "sphere(r=5)" in result
        assert "cube([10, 10, 10])" not in result
        assert "box();" in result  # call preserved

    def test_raises_for_missing_module(self):
        code = "module box() { cube(10); }\n"
        with pytest.raises(ValueError, match="not_here"):
            modify_scad_module(code, "not_here", "module not_here() {}")

    def test_error_message_lists_available(self):
        code = "module box() { cube(10); }\nmodule lid() { cube(5); }\n"
        with pytest.raises(ValueError, match="box"):
            modify_scad_module(code, "missing", "module missing() {}")


# ---------------------------------------------------------------------------
# insert_into_scad_module
# ---------------------------------------------------------------------------


class TestInsertIntoScadModule:
    """insert_into_scad_module adds code inside modules."""

    def test_insert_at_end(self):
        code = (
            "module box() {\n"
            "    cube([10, 10, 10]);\n"
            "}\n"
        )
        result = insert_into_scad_module(code, "box", "sphere(r=2);", position="end")
        assert "sphere(r=2);" in result
        assert "cube([10, 10, 10])" in result

    def test_insert_at_start(self):
        code = (
            "module box() {\n"
            "    cube([10, 10, 10]);\n"
            "}\n"
        )
        result = insert_into_scad_module(code, "box", 'color("red")', position="start")
        lines = result.split("\n")
        # The inserted code should appear after the opening brace
        brace_idx = next(i for i, line in enumerate(lines) if "{" in line)
        assert 'color("red")' in lines[brace_idx + 1]

    def test_raises_for_missing_module(self):
        code = "module box() { cube(10); }\n"
        with pytest.raises(ValueError, match="nope"):
            insert_into_scad_module(code, "nope", "sphere(1);")


# ---------------------------------------------------------------------------
# ScadModule.to_dict
# ---------------------------------------------------------------------------


class TestScadModuleToDict:
    """ScadModule serialization."""

    def test_to_dict(self):
        m = ScadModule(
            name="test", line_start=1, line_end=3,
            code="module test() { cube(1); }", description="A test",
        )
        d = m.to_dict()
        assert d["name"] == "test"
        assert d["line_start"] == 1
        assert d["line_end"] == 3
        assert d["description"] == "A test"
