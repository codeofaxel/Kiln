"""Tests for mesh manipulation MCP tools in plugins/mesh_tools.py.

Covers:
- add_mesh_chamfer — happy path, auth failure, exception
- add_mesh_fillet — happy path, auth failure, exception
- center_model_on_bed — happy path, auth failure, exception
- compose_models — happy path, auth failure, exception
- compose_part_from_primitives — happy path, ValueError, auth failure, exception
- export_model_3mf — happy path, auth failure, exception
- hollow_mesh_model — happy path, auth failure, exception
- merge_mesh_files — happy path, auth failure, exception
- mirror_mesh_model — happy path, auth failure, exception
- remove_mesh_floating_regions — happy path, auth failure, exception
- repair_mesh — happy path, auth failure, exception
- repair_mesh_advanced — happy path, auth failure, exception
- rescale_model — happy path, ValueError, auth failure, exception
- simplify_mesh_model — happy path, auth failure, exception
- splice_mesh_at_z — happy path, auth failure, exception
- split_mesh_by_component — happy path, auth failure, exception
- thicken_mesh_walls — happy path, auth failure, exception
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth_error() -> dict:
    """Simulate the dict _check_auth returns when auth fails."""
    return {
        "success": False,
        "error": {
            "code": "AUTH_ERROR",
            "message": "Authentication failed.",
            "retryable": False,
        },
    }


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
def mesh_tools(mock_mcp):
    """Register mesh_tools plugin and return captured tools dict."""
    mcp, tools = mock_mcp
    from kiln.plugins.mesh_tools import plugin

    plugin.register(mcp)
    return tools


# ---------------------------------------------------------------------------
# TestPluginMeta
# ---------------------------------------------------------------------------


class TestMeshToolsPluginMeta:
    """Plugin identity and registration."""

    def test_plugin_name(self) -> None:
        from kiln.plugins.mesh_tools import plugin

        assert plugin.name == "mesh_tools"

    def test_plugin_description(self) -> None:
        from kiln.plugins.mesh_tools import plugin

        assert "mesh" in plugin.description.lower()

    def test_registers_expected_tools(self, mesh_tools) -> None:
        expected = {
            "validate_generated_mesh",
            "rescale_model",
            "analyze_mesh_geometry",
            "detect_mesh_pockets",
            "analyze_non_manifold_edges",
            "cross_section_view",
            "mesh_quality_scorecard",
            "compare_mesh_versions",
            "repair_mesh",
            "repair_mesh_advanced",
            "splice_mesh_at_z",
            "mirror_mesh_model",
            "hollow_mesh_model",
            "thicken_mesh_walls",
            "add_mesh_fillet",
            "add_mesh_chamfer",
            "scale_mesh_to_fit",
            "center_model_on_bed",
            "compose_models",
            "merge_mesh_files",
            "boolean_mesh_op",
            "compose_part_from_primitives",
            "split_mesh_by_component",
            "remove_mesh_floating_regions",
            "simplify_mesh_model",
            "export_model_3mf",
            "extract_model_from_3mf",
            "estimate_mesh_weight",
            "estimate_mesh_print_time",
        }
        assert expected == set(mesh_tools.keys())


# ---------------------------------------------------------------------------
# TestRepairMesh
# ---------------------------------------------------------------------------


class TestRepairMesh:
    """Tests for the repair_mesh tool."""

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.repair_stl")
    def test_happy_path(self, mock_repair, _auth, mesh_tools) -> None:
        mock_repair.return_value = {"repaired_triangles": 5, "output_path": "/tmp/out.stl"}
        result = mesh_tools["repair_mesh"](file_path="/tmp/model.stl")

        assert result["success"] is True
        assert result["repaired_triangles"] == 5
        mock_repair.assert_called_once_with(
            "/tmp/model.stl", output_path=None, weld_tolerance=None,
        )

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.repair_stl")
    def test_with_output_path(self, mock_repair, _auth, mesh_tools) -> None:
        mock_repair.return_value = {"output_path": "/tmp/fixed.stl"}
        result = mesh_tools["repair_mesh"](file_path="/tmp/in.stl", output_path="/tmp/fixed.stl")

        assert result["success"] is True
        mock_repair.assert_called_once_with(
            "/tmp/in.stl", output_path="/tmp/fixed.stl", weld_tolerance=None,
        )

    def test_auth_failure(self, mesh_tools) -> None:
        with patch("kiln.server._check_auth", return_value=_auth_error()):
            result = mesh_tools["repair_mesh"](file_path="/tmp/model.stl")

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.repair_stl", side_effect=RuntimeError("corrupt"))
    def test_exception_returns_error(self, _repair, _auth, mesh_tools) -> None:
        result = mesh_tools["repair_mesh"](file_path="/tmp/model.stl")

        assert result["success"] is False
        assert result["error"]["code"] == "REPAIR_ERROR"
        assert "corrupt" in result["error"]["message"]

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.repair_stl_advanced")
    def test_close_holes_routes_to_deep_engine(
        self, mock_advanced, _auth, mesh_tools
    ) -> None:
        """repair_mesh(close_holes=True) runs the boundary-aware engine —
        the merged replacement for the old repair_mesh_advanced tool."""
        mock_advanced.return_value = {"holes_closed": 2, "path": "/tmp/out.stl"}
        result = mesh_tools["repair_mesh"](
            file_path="/tmp/model.stl", close_holes=True,
        )

        assert result["success"] is True
        assert result["holes_closed"] == 2
        mock_advanced.assert_called_once_with(
            "/tmp/model.stl", output_path=None, close_holes=True,
            weld_tolerance=None,
        )


# ---------------------------------------------------------------------------
# TestRepairMeshAdvanced
# ---------------------------------------------------------------------------


class TestRepairMeshAdvanced:
    """Tests for the repair_mesh_advanced tool."""

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.repair_stl_advanced")
    def test_happy_path(self, mock_repair, _auth, mesh_tools) -> None:
        mock_repair.return_value = {"holes_closed": 3, "output_path": "/tmp/out.stl"}
        result = mesh_tools["repair_mesh_advanced"](file_path="/tmp/model.stl")

        assert result["success"] is True
        assert result["holes_closed"] == 3
        mock_repair.assert_called_once_with(
            "/tmp/model.stl", output_path=None, close_holes=True,
            weld_tolerance=None,
        )

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.repair_stl_advanced")
    def test_close_holes_false(self, mock_repair, _auth, mesh_tools) -> None:
        mock_repair.return_value = {"output_path": "/tmp/out.stl"}
        mesh_tools["repair_mesh_advanced"](
            file_path="/tmp/model.stl", close_holes=False,
        )

        mock_repair.assert_called_once_with(
            "/tmp/model.stl", output_path=None, close_holes=False,
            weld_tolerance=None,
        )

    def test_auth_failure(self, mesh_tools) -> None:
        with patch("kiln.server._check_auth", return_value=_auth_error()):
            result = mesh_tools["repair_mesh_advanced"](file_path="/tmp/model.stl")

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"

    @patch("kiln.server._check_auth", return_value=None)
    @patch(
        "kiln.generation.validation.repair_stl_advanced",
        side_effect=RuntimeError("failed"),
    )
    def test_exception_returns_error(self, _repair, _auth, mesh_tools) -> None:
        result = mesh_tools["repair_mesh_advanced"](file_path="/tmp/model.stl")

        assert result["success"] is False
        assert result["error"]["code"] == "REPAIR_ERROR"
        assert "failed" in result["error"]["message"]


# ---------------------------------------------------------------------------
# TestRescaleModel
# ---------------------------------------------------------------------------


class TestRescaleModel:
    """Tests for the rescale_model tool."""

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.rescale_stl")
    def test_happy_path_target_height(self, mock_rescale, _auth, mesh_tools) -> None:
        mock_rescale.return_value = {
            "scale_applied": 2.0,
            "new_dimensions_mm": {"x": 20, "y": 20, "z": 40},
        }
        result = mesh_tools["rescale_model"](
            file_path="/tmp/model.stl", target_height_mm=40.0,
        )

        assert result["success"] is True
        assert "rescaled" in result["message"].lower()
        mock_rescale.assert_called_once_with(
            "/tmp/model.stl",
            target_height_mm=40.0,
            scale_factor=None,
            max_dimension_mm=None,
            scale_x=None,
            scale_y=None,
            scale_z=None,
        )

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.rescale_stl")
    def test_per_axis_scale(self, mock_rescale, _auth, mesh_tools) -> None:
        mock_rescale.return_value = {
            "scale_applied": {"x": 2.0, "y": 1.0, "z": 0.5},
            "new_dimensions_mm": {"x": 40, "y": 20, "z": 10},
        }
        result = mesh_tools["rescale_model"](
            file_path="/tmp/model.stl", scale_x=2.0, scale_y=1.0, scale_z=0.5,
        )

        assert result["success"] is True
        assert "x=2.0" in result["message"]

    @patch("kiln.server._check_auth", return_value=None)
    @patch(
        "kiln.generation.validation.rescale_stl",
        side_effect=ValueError("cannot combine uniform and per-axis"),
    )
    def test_value_error_returns_invalid_input(self, _rescale, _auth, mesh_tools) -> None:
        result = mesh_tools["rescale_model"](
            file_path="/tmp/model.stl", target_height_mm=10.0, scale_x=2.0,
        )

        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_INPUT"

    def test_auth_failure(self, mesh_tools) -> None:
        with patch("kiln.server._check_auth", return_value=_auth_error()):
            result = mesh_tools["rescale_model"](file_path="/tmp/model.stl")

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"

    @patch("kiln.server._check_auth", return_value=None)
    @patch(
        "kiln.generation.validation.rescale_stl",
        side_effect=RuntimeError("IO error"),
    )
    def test_unexpected_exception(self, _rescale, _auth, mesh_tools) -> None:
        result = mesh_tools["rescale_model"](file_path="/tmp/model.stl")

        assert result["success"] is False
        assert result["error"]["code"] == "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# TestCenterModelOnBed
# ---------------------------------------------------------------------------


class TestCenterModelOnBed:
    """Tests for the center_model_on_bed tool."""

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.center_on_bed")
    def test_happy_path(self, mock_center, _auth, mesh_tools) -> None:
        mock_center.return_value = {
            "translation_mm": {"x": 128.0, "y": 128.0, "z": 0.0},
            "output_path": "/tmp/model.stl",
        }
        result = mesh_tools["center_model_on_bed"](file_path="/tmp/model.stl")

        assert result["success"] is True
        assert result["translation_mm"]["x"] == 128.0
        mock_center.assert_called_once_with(
            "/tmp/model.stl", bed_x_mm=256.0, bed_y_mm=256.0, output_path=None,
        )

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.center_on_bed")
    def test_custom_bed_size(self, mock_center, _auth, mesh_tools) -> None:
        mock_center.return_value = {"translation_mm": {"x": 100, "y": 100, "z": 0}}
        mesh_tools["center_model_on_bed"](
            file_path="/tmp/m.stl", bed_x_mm=200.0, bed_y_mm=200.0,
        )

        mock_center.assert_called_once_with(
            "/tmp/m.stl", bed_x_mm=200.0, bed_y_mm=200.0, output_path=None,
        )

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.center_on_bed")
    def test_printer_id_resolves_bed_size(self, mock_center, _auth, mesh_tools) -> None:
        mock_center.return_value = {"translation_mm": {"x": 150, "y": 150, "z": 0}}
        result = mesh_tools["center_model_on_bed"](
            file_path="/tmp/m.stl", printer_id="Creality K1 Max",
        )

        assert result["success"] is True
        assert result["bed_size_model_id"] == "k1_max"
        assert result["bed_dims_mm"] == [300.0, 300.0]
        mock_center.assert_called_once_with(
            "/tmp/m.stl", bed_x_mm=300.0, bed_y_mm=300.0, output_path=None,
        )

    def test_auth_failure(self, mesh_tools) -> None:
        with patch("kiln.server._check_auth", return_value=_auth_error()):
            result = mesh_tools["center_model_on_bed"](file_path="/tmp/m.stl")

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"

    @patch("kiln.server._check_auth", return_value=None)
    @patch(
        "kiln.generation.validation.center_on_bed",
        side_effect=RuntimeError("bad mesh"),
    )
    def test_exception(self, _center, _auth, mesh_tools) -> None:
        result = mesh_tools["center_model_on_bed"](file_path="/tmp/m.stl")

        assert result["success"] is False
        assert "bad mesh" in result["error"]["message"]


# ---------------------------------------------------------------------------
# TestComposeModels
# ---------------------------------------------------------------------------


class TestComposeModels:
    """Tests for the compose_models tool."""

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.compose_stls")
    def test_happy_path(self, mock_compose, _auth, mesh_tools) -> None:
        mock_compose.return_value = {
            "output_path": "/tmp/merged.stl",
            "total_triangles": 5000,
        }
        paths = ["/tmp/a.stl", "/tmp/b.stl"]
        result = mesh_tools["compose_models"](
            file_paths=paths, output_path="/tmp/merged.stl",
        )

        assert result["success"] is True
        assert result["total_triangles"] == 5000
        mock_compose.assert_called_once_with(paths, "/tmp/merged.stl")

    def test_auth_failure(self, mesh_tools) -> None:
        with patch("kiln.server._check_auth", return_value=_auth_error()):
            result = mesh_tools["compose_models"](
                file_paths=["/tmp/a.stl"], output_path="/tmp/out.stl",
            )

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"

    @patch("kiln.server._check_auth", return_value=None)
    @patch(
        "kiln.generation.validation.compose_stls",
        side_effect=FileNotFoundError("not found"),
    )
    def test_exception(self, _compose, _auth, mesh_tools) -> None:
        result = mesh_tools["compose_models"](
            file_paths=["/tmp/a.stl"], output_path="/tmp/out.stl",
        )

        assert result["success"] is False
        assert result["error"]["code"] == "COMPOSE_ERROR"


# ---------------------------------------------------------------------------
# TestComposePartFromPrimitives
# ---------------------------------------------------------------------------


class TestComposePartFromPrimitives:
    """Tests for the compose_part_from_primitives tool."""

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.openscad.compose_from_primitives")
    def test_happy_path(self, mock_compose, _auth, mesh_tools) -> None:
        mock_compose.return_value = {
            "output_path": "/tmp/part.stl",
            "scad_code": "cube([10,10,10]);",
            "triangle_count": 12,
        }
        ops = [{"type": "primitive", "shape": "cube", "params": {"size": [10, 10, 10]}}]
        result = mesh_tools["compose_part_from_primitives"](operations=ops)

        assert result["success"] is True
        assert result["triangle_count"] == 12
        mock_compose.assert_called_once_with(ops, output_path=None)

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.openscad.compose_from_primitives")
    def test_with_output_path(self, mock_compose, _auth, mesh_tools) -> None:
        mock_compose.return_value = {"output_path": "/tmp/out.stl"}
        mesh_tools["compose_part_from_primitives"](
            operations=[], output_path="/tmp/out.stl",
        )

        mock_compose.assert_called_once_with([], output_path="/tmp/out.stl")

    @patch("kiln.server._check_auth", return_value=None)
    @patch(
        "kiln.generation.openscad.compose_from_primitives",
        side_effect=ValueError("invalid shape"),
    )
    def test_value_error(self, _compose, _auth, mesh_tools) -> None:
        result = mesh_tools["compose_part_from_primitives"](operations=[])

        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_ARGS"
        assert "invalid shape" in result["error"]["message"]

    def test_auth_failure(self, mesh_tools) -> None:
        with patch("kiln.server._check_auth", return_value=_auth_error()):
            result = mesh_tools["compose_part_from_primitives"](operations=[])

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"

    @patch("kiln.server._check_auth", return_value=None)
    @patch(
        "kiln.generation.openscad.compose_from_primitives",
        side_effect=RuntimeError("openscad not found"),
    )
    def test_unexpected_exception(self, _compose, _auth, mesh_tools) -> None:
        result = mesh_tools["compose_part_from_primitives"](operations=[])

        assert result["success"] is False
        assert "openscad not found" in result["error"]["message"]


# ---------------------------------------------------------------------------
# TestExportModel3mf
# ---------------------------------------------------------------------------


class TestExportModel3mf:
    """Tests for the export_model_3mf tool."""

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.export_3mf")
    @patch("os.path.getsize", return_value=12345)
    def test_happy_path(self, _getsize, mock_export, _auth, mesh_tools) -> None:
        mock_export.return_value = "/tmp/model.3mf"
        result = mesh_tools["export_model_3mf"](file_path="/tmp/model.stl")

        assert result["success"] is True
        assert result["path"] == "/tmp/model.3mf"
        assert result["file_size_bytes"] == 12345
        assert "12345" in result["message"]
        mock_export.assert_called_once_with("/tmp/model.stl", output_path=None)

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.export_3mf")
    @patch("os.path.getsize", return_value=100)
    def test_with_output_path(self, _getsize, mock_export, _auth, mesh_tools) -> None:
        mock_export.return_value = "/tmp/out.3mf"
        mesh_tools["export_model_3mf"](
            file_path="/tmp/model.stl", output_path="/tmp/out.3mf",
        )

        mock_export.assert_called_once_with("/tmp/model.stl", output_path="/tmp/out.3mf")

    def test_auth_failure(self, mesh_tools) -> None:
        with patch("kiln.server._check_auth", return_value=_auth_error()):
            result = mesh_tools["export_model_3mf"](file_path="/tmp/model.stl")

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"

    @patch("kiln.server._check_auth", return_value=None)
    @patch(
        "kiln.generation.validation.export_3mf",
        side_effect=RuntimeError("trimesh error"),
    )
    def test_exception(self, _export, _auth, mesh_tools) -> None:
        result = mesh_tools["export_model_3mf"](file_path="/tmp/model.stl")

        assert result["success"] is False
        assert result["error"]["code"] == "EXPORT_ERROR"


# ---------------------------------------------------------------------------
# TestHollowMeshModel
# ---------------------------------------------------------------------------


class TestHollowMeshModel:
    """Tests for the hollow_mesh_model tool."""

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.hollow_mesh")
    def test_happy_path(self, mock_hollow, _auth, mesh_tools) -> None:
        mock_hollow.return_value = {
            "output_path": "/tmp/model_hollow.stl",
            "material_savings_pct": 45.0,
        }
        result = mesh_tools["hollow_mesh_model"](file_path="/tmp/model.stl")

        assert result["success"] is True
        assert result["material_savings_pct"] == 45.0
        mock_hollow.assert_called_once_with(
            "/tmp/model.stl", wall_thickness_mm=2.0, output_path=None,
        )

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.hollow_mesh")
    def test_custom_wall_thickness(self, mock_hollow, _auth, mesh_tools) -> None:
        mock_hollow.return_value = {"output_path": "/tmp/out.stl"}
        mesh_tools["hollow_mesh_model"](
            file_path="/tmp/m.stl", wall_thickness_mm=3.5,
        )

        mock_hollow.assert_called_once_with(
            "/tmp/m.stl", wall_thickness_mm=3.5, output_path=None,
        )

    def test_auth_failure(self, mesh_tools) -> None:
        with patch("kiln.server._check_auth", return_value=_auth_error()):
            result = mesh_tools["hollow_mesh_model"](file_path="/tmp/m.stl")

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"

    @patch("kiln.server._check_auth", return_value=None)
    @patch(
        "kiln.generation.validation.hollow_mesh",
        side_effect=RuntimeError("non-manifold"),
    )
    def test_exception(self, _hollow, _auth, mesh_tools) -> None:
        result = mesh_tools["hollow_mesh_model"](file_path="/tmp/m.stl")

        assert result["success"] is False
        assert "non-manifold" in result["error"]["message"]


# ---------------------------------------------------------------------------
# TestMergeMeshFiles
# ---------------------------------------------------------------------------


class TestMergeMeshFiles:
    """Tests for the merge_mesh_files tool."""

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.merge_stl_files")
    def test_happy_path(self, mock_merge, _auth, mesh_tools) -> None:
        mock_merge.return_value = {
            "output_path": "/tmp/merged.stl",
            "files_merged": 3,
        }
        paths = ["/tmp/a.stl", "/tmp/b.stl", "/tmp/c.stl"]
        result = mesh_tools["merge_mesh_files"](
            file_paths=paths, output_path="/tmp/merged.stl",
        )

        assert result["success"] is True
        assert result["files_merged"] == 3
        mock_merge.assert_called_once_with(paths, output_path="/tmp/merged.stl")

    def test_auth_failure(self, mesh_tools) -> None:
        with patch("kiln.server._check_auth", return_value=_auth_error()):
            result = mesh_tools["merge_mesh_files"](
                file_paths=[], output_path="/tmp/out.stl",
            )

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"

    @patch("kiln.server._check_auth", return_value=None)
    @patch(
        "kiln.generation.validation.merge_stl_files",
        side_effect=RuntimeError("empty list"),
    )
    def test_exception(self, _merge, _auth, mesh_tools) -> None:
        result = mesh_tools["merge_mesh_files"](
            file_paths=[], output_path="/tmp/out.stl",
        )

        assert result["success"] is False
        assert "empty list" in result["error"]["message"]


# ---------------------------------------------------------------------------
# TestMirrorMeshModel
# ---------------------------------------------------------------------------


class TestMirrorMeshModel:
    """Tests for the mirror_mesh_model tool."""

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.mirror_mesh")
    def test_happy_path(self, mock_mirror, _auth, mesh_tools) -> None:
        mock_mirror.return_value = {
            "axis": "x",
            "output_path": "/tmp/model.stl",
        }
        result = mesh_tools["mirror_mesh_model"](file_path="/tmp/model.stl")

        assert result["success"] is True
        assert result["axis"] == "x"
        mock_mirror.assert_called_once_with(
            "/tmp/model.stl", axis="x", output_path=None,
        )

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.mirror_mesh")
    def test_y_axis(self, mock_mirror, _auth, mesh_tools) -> None:
        mock_mirror.return_value = {"axis": "y", "output_path": "/tmp/model.stl"}
        result = mesh_tools["mirror_mesh_model"](
            file_path="/tmp/model.stl", axis="y",
        )

        assert result["success"] is True
        mock_mirror.assert_called_once_with(
            "/tmp/model.stl", axis="y", output_path=None,
        )

    def test_auth_failure(self, mesh_tools) -> None:
        with patch("kiln.server._check_auth", return_value=_auth_error()):
            result = mesh_tools["mirror_mesh_model"](file_path="/tmp/m.stl")

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"

    @patch("kiln.server._check_auth", return_value=None)
    @patch(
        "kiln.generation.validation.mirror_mesh",
        side_effect=RuntimeError("axis invalid"),
    )
    def test_exception(self, _mirror, _auth, mesh_tools) -> None:
        result = mesh_tools["mirror_mesh_model"](file_path="/tmp/m.stl")

        assert result["success"] is False
        assert "axis invalid" in result["error"]["message"]


# ---------------------------------------------------------------------------
# TestRemoveMeshFloatingRegions
# ---------------------------------------------------------------------------


class TestRemoveMeshFloatingRegions:
    """Tests for the remove_mesh_floating_regions tool."""

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.remove_floating_regions")
    def test_happy_path(self, mock_remove, _auth, mesh_tools) -> None:
        mock_remove.return_value = {
            "components_found": 4,
            "components_removed": 3,
            "output_path": "/tmp/model.stl",
        }
        result = mesh_tools["remove_mesh_floating_regions"](
            file_path="/tmp/model.stl",
        )

        assert result["success"] is True
        assert result["components_removed"] == 3
        mock_remove.assert_called_once_with(
            "/tmp/model.stl",
            output_path=None,
            keep_largest=True,
            min_triangle_pct=1.0,
        )

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.remove_floating_regions")
    def test_keep_largest_false(self, mock_remove, _auth, mesh_tools) -> None:
        mock_remove.return_value = {"output_path": "/tmp/model.stl"}
        mesh_tools["remove_mesh_floating_regions"](
            file_path="/tmp/m.stl", keep_largest=False, min_triangle_pct=5.0,
        )

        mock_remove.assert_called_once_with(
            "/tmp/m.stl",
            output_path=None,
            keep_largest=False,
            min_triangle_pct=5.0,
        )

    def test_auth_failure(self, mesh_tools) -> None:
        with patch("kiln.server._check_auth", return_value=_auth_error()):
            result = mesh_tools["remove_mesh_floating_regions"](
                file_path="/tmp/m.stl",
            )

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"

    @patch("kiln.server._check_auth", return_value=None)
    @patch(
        "kiln.generation.validation.remove_floating_regions",
        side_effect=RuntimeError("empty mesh"),
    )
    def test_exception(self, _remove, _auth, mesh_tools) -> None:
        result = mesh_tools["remove_mesh_floating_regions"](
            file_path="/tmp/m.stl",
        )

        assert result["success"] is False
        assert "empty mesh" in result["error"]["message"]


# ---------------------------------------------------------------------------
# TestSimplifyMeshModel
# ---------------------------------------------------------------------------


class TestSimplifyMeshModel:
    """Tests for the simplify_mesh_model tool."""

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.simplify_mesh")
    def test_happy_path(self, mock_simplify, _auth, mesh_tools) -> None:
        mock_simplify.return_value = {
            "original_triangles": 10000,
            "simplified_triangles": 5000,
            "reduction_pct": 50.0,
            "output_path": "/tmp/model_simplified.stl",
        }
        result = mesh_tools["simplify_mesh_model"](file_path="/tmp/model.stl")

        assert result["success"] is True
        assert result["reduction_pct"] == 50.0
        mock_simplify.assert_called_once_with(
            "/tmp/model.stl", target_ratio=0.5, output_path=None,
        )

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.simplify_mesh")
    def test_custom_ratio(self, mock_simplify, _auth, mesh_tools) -> None:
        mock_simplify.return_value = {"output_path": "/tmp/out.stl"}
        mesh_tools["simplify_mesh_model"](
            file_path="/tmp/m.stl", target_ratio=0.25,
        )

        mock_simplify.assert_called_once_with(
            "/tmp/m.stl", target_ratio=0.25, output_path=None,
        )

    def test_auth_failure(self, mesh_tools) -> None:
        with patch("kiln.server._check_auth", return_value=_auth_error()):
            result = mesh_tools["simplify_mesh_model"](file_path="/tmp/m.stl")

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"

    @patch("kiln.server._check_auth", return_value=None)
    @patch(
        "kiln.generation.validation.simplify_mesh",
        side_effect=RuntimeError("decimation failed"),
    )
    def test_exception(self, _simplify, _auth, mesh_tools) -> None:
        result = mesh_tools["simplify_mesh_model"](file_path="/tmp/m.stl")

        assert result["success"] is False
        assert "decimation failed" in result["error"]["message"]


# ---------------------------------------------------------------------------
# TestSpliceMeshAtZ
# ---------------------------------------------------------------------------


class TestSpliceMeshAtZ:
    """Tests for the splice_mesh_at_z tool."""

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.splice_mesh_at_z")
    def test_happy_path(self, mock_splice, _auth, mesh_tools) -> None:
        mock_splice.return_value = {
            "output_path": "/tmp/spliced.stl",
            "z_plane": 5.0,
            "top_triangles": 100,
            "bottom_triangles": 200,
        }
        result = mesh_tools["splice_mesh_at_z"](
            top_path="/tmp/top.stl",
            bottom_path="/tmp/bottom.stl",
            z_plane=5.0,
            output_path="/tmp/spliced.stl",
        )

        assert result["success"] is True
        assert result["z_plane"] == 5.0
        mock_splice.assert_called_once_with(
            "/tmp/top.stl", "/tmp/bottom.stl", 5.0, "/tmp/spliced.stl",
        )

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.splice_mesh_at_z")
    @patch("tempfile.mkstemp", return_value=(99, "/tmp/kiln_splice_abc.stl"))
    @patch("os.close")
    def test_auto_output_path(self, _close, _mkstemp, mock_splice, _auth, mesh_tools) -> None:
        mock_splice.return_value = {"output_path": "/tmp/kiln_splice_abc.stl"}
        result = mesh_tools["splice_mesh_at_z"](
            top_path="/tmp/top.stl",
            bottom_path="/tmp/bottom.stl",
            z_plane=3.0,
        )

        assert result["success"] is True
        mock_splice.assert_called_once_with(
            "/tmp/top.stl", "/tmp/bottom.stl", 3.0, "/tmp/kiln_splice_abc.stl",
        )

    def test_auth_failure(self, mesh_tools) -> None:
        with patch("kiln.server._check_auth", return_value=_auth_error()):
            result = mesh_tools["splice_mesh_at_z"](
                top_path="/tmp/t.stl", bottom_path="/tmp/b.stl", z_plane=1.0,
            )

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"

    @patch("kiln.server._check_auth", return_value=None)
    @patch(
        "kiln.generation.validation.splice_mesh_at_z",
        side_effect=RuntimeError("clip error"),
    )
    def test_exception(self, _splice, _auth, mesh_tools) -> None:
        result = mesh_tools["splice_mesh_at_z"](
            top_path="/tmp/t.stl",
            bottom_path="/tmp/b.stl",
            z_plane=1.0,
            output_path="/tmp/out.stl",
        )

        assert result["success"] is False
        assert result["error"]["code"] == "SPLICE_ERROR"
        assert "clip error" in result["error"]["message"]


# ---------------------------------------------------------------------------
# TestSplitMeshByComponent
# ---------------------------------------------------------------------------


class TestSplitMeshByComponent:
    """Tests for the split_mesh_by_component tool."""

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.split_by_component")
    def test_happy_path(self, mock_split, _auth, mesh_tools) -> None:
        mock_split.return_value = {
            "component_count": 3,
            "files": ["/tmp/c1.stl", "/tmp/c2.stl", "/tmp/c3.stl"],
        }
        result = mesh_tools["split_mesh_by_component"](file_path="/tmp/model.stl")

        assert result["success"] is True
        assert result["component_count"] == 3
        mock_split.assert_called_once_with("/tmp/model.stl", output_dir=None)

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.split_by_component")
    def test_custom_output_dir(self, mock_split, _auth, mesh_tools) -> None:
        mock_split.return_value = {"component_count": 1, "files": ["/out/c1.stl"]}
        mesh_tools["split_mesh_by_component"](
            file_path="/tmp/model.stl", output_dir="/out",
        )

        mock_split.assert_called_once_with("/tmp/model.stl", output_dir="/out")

    def test_auth_failure(self, mesh_tools) -> None:
        with patch("kiln.server._check_auth", return_value=_auth_error()):
            result = mesh_tools["split_mesh_by_component"](
                file_path="/tmp/model.stl",
            )

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"

    @patch("kiln.server._check_auth", return_value=None)
    @patch(
        "kiln.generation.validation.split_by_component",
        side_effect=RuntimeError("single component"),
    )
    def test_exception(self, _split, _auth, mesh_tools) -> None:
        result = mesh_tools["split_mesh_by_component"](
            file_path="/tmp/model.stl",
        )

        assert result["success"] is False
        assert "single component" in result["error"]["message"]


# ---------------------------------------------------------------------------
# TestThickenMeshWalls
# ---------------------------------------------------------------------------


class TestThickenMeshWalls:
    """Tests for the thicken_mesh_walls tool."""

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.thicken_walls")
    def test_happy_path(self, mock_thicken, _auth, mesh_tools) -> None:
        mock_thicken.return_value = {
            "vertices_modified": 42,
            "amount_mm": 0.5,
            "output_path": "/tmp/model_thickened.stl",
        }
        result = mesh_tools["thicken_mesh_walls"](file_path="/tmp/model.stl")

        assert result["success"] is True
        assert result["vertices_modified"] == 42
        mock_thicken.assert_called_once_with(
            "/tmp/model.stl", amount_mm=0.5, output_path=None,
        )

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.thicken_walls")
    def test_custom_amount(self, mock_thicken, _auth, mesh_tools) -> None:
        mock_thicken.return_value = {"output_path": "/tmp/out.stl"}
        mesh_tools["thicken_mesh_walls"](
            file_path="/tmp/m.stl", amount_mm=1.0,
        )

        mock_thicken.assert_called_once_with(
            "/tmp/m.stl", amount_mm=1.0, output_path=None,
        )

    def test_auth_failure(self, mesh_tools) -> None:
        with patch("kiln.server._check_auth", return_value=_auth_error()):
            result = mesh_tools["thicken_mesh_walls"](file_path="/tmp/m.stl")

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"

    @patch("kiln.server._check_auth", return_value=None)
    @patch(
        "kiln.generation.validation.thicken_walls",
        side_effect=RuntimeError("degenerate mesh"),
    )
    def test_exception(self, _thicken, _auth, mesh_tools) -> None:
        result = mesh_tools["thicken_mesh_walls"](file_path="/tmp/m.stl")

        assert result["success"] is False
        assert "degenerate mesh" in result["error"]["message"]


# ---------------------------------------------------------------------------
# TestAddMeshFillet
# ---------------------------------------------------------------------------


class TestAddMeshFillet:
    """Tests for the add_mesh_fillet tool."""

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.add_fillet")
    def test_happy_path(self, mock_fillet, _auth, mesh_tools) -> None:
        mock_fillet.return_value = {
            "sharp_edges": 24,
            "triangles_added": 96,
            "output_path": "/tmp/model_filleted.stl",
        }
        result = mesh_tools["add_mesh_fillet"](file_path="/tmp/model.stl")

        assert result["success"] is True
        assert result["sharp_edges"] == 24
        mock_fillet.assert_called_once_with(
            "/tmp/model.stl",
            radius_mm=1.0,
            angle_threshold_deg=60.0,
            output_path=None,
        )

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.add_fillet")
    def test_custom_params(self, mock_fillet, _auth, mesh_tools) -> None:
        mock_fillet.return_value = {"output_path": "/tmp/out.stl"}
        mesh_tools["add_mesh_fillet"](
            file_path="/tmp/m.stl",
            radius_mm=2.5,
            angle_threshold_deg=45.0,
            output_path="/tmp/out.stl",
        )

        mock_fillet.assert_called_once_with(
            "/tmp/m.stl",
            radius_mm=2.5,
            angle_threshold_deg=45.0,
            output_path="/tmp/out.stl",
        )

    def test_auth_failure(self, mesh_tools) -> None:
        with patch("kiln.server._check_auth", return_value=_auth_error()):
            result = mesh_tools["add_mesh_fillet"](file_path="/tmp/m.stl")

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"

    @patch("kiln.server._check_auth", return_value=None)
    @patch(
        "kiln.generation.validation.add_fillet",
        side_effect=RuntimeError("fillet geometry error"),
    )
    def test_exception(self, _fillet, _auth, mesh_tools) -> None:
        result = mesh_tools["add_mesh_fillet"](file_path="/tmp/m.stl")

        assert result["success"] is False
        assert "fillet geometry error" in result["error"]["message"]


# ---------------------------------------------------------------------------
# TestAddMeshChamfer
# ---------------------------------------------------------------------------


class TestAddMeshChamfer:
    """Tests for the add_mesh_chamfer tool."""

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.add_chamfer")
    def test_happy_path(self, mock_chamfer, _auth, mesh_tools) -> None:
        mock_chamfer.return_value = {
            "sharp_edges": 12,
            "triangles_added": 24,
            "output_path": "/tmp/model_chamfered.stl",
        }
        result = mesh_tools["add_mesh_chamfer"](file_path="/tmp/model.stl")

        assert result["success"] is True
        assert result["sharp_edges"] == 12
        mock_chamfer.assert_called_once_with(
            "/tmp/model.stl",
            distance_mm=0.5,
            angle_threshold_deg=60.0,
            output_path=None,
        )

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.generation.validation.add_chamfer")
    def test_custom_params(self, mock_chamfer, _auth, mesh_tools) -> None:
        mock_chamfer.return_value = {"output_path": "/tmp/out.stl"}
        mesh_tools["add_mesh_chamfer"](
            file_path="/tmp/m.stl",
            distance_mm=1.0,
            angle_threshold_deg=30.0,
            output_path="/tmp/out.stl",
        )

        mock_chamfer.assert_called_once_with(
            "/tmp/m.stl",
            distance_mm=1.0,
            angle_threshold_deg=30.0,
            output_path="/tmp/out.stl",
        )

    def test_auth_failure(self, mesh_tools) -> None:
        with patch("kiln.server._check_auth", return_value=_auth_error()):
            result = mesh_tools["add_mesh_chamfer"](file_path="/tmp/m.stl")

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"

    @patch("kiln.server._check_auth", return_value=None)
    @patch(
        "kiln.generation.validation.add_chamfer",
        side_effect=RuntimeError("chamfer error"),
    )
    def test_exception(self, _chamfer, _auth, mesh_tools) -> None:
        result = mesh_tools["add_mesh_chamfer"](file_path="/tmp/m.stl")

        assert result["success"] is False
        assert "chamfer error" in result["error"]["message"]


# ---------------------------------------------------------------------------
# analyze_mesh_geometry — a read failure is an error, never an analysis
# ---------------------------------------------------------------------------


class TestAnalyzeMeshGeometryHonesty:
    """Found 2026-07-27: any file the analyzer could not read came back as
    success:True with 0 triangles / "not manifold" — a valid input reported
    as an empty, broken mesh.  The engine records the real reason in
    printability_issues; the tool must surface it as an ERROR."""

    def test_garbage_file_is_an_error_not_an_empty_mesh(self, mesh_tools, tmp_path):
        bad = tmp_path / "not_a_mesh.stl"
        bad.write_text("this is not a mesh\n")

        result = mesh_tools["analyze_mesh_geometry"](file_path=str(bad))

        assert result["success"] is False
        assert result["error"]["code"] == "UNREADABLE_INPUT"

    def test_step_input_error_names_the_conversion_path(self, mesh_tools, tmp_path):
        step = tmp_path / "part.step"
        step.write_text("ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n")

        result = mesh_tools["analyze_mesh_geometry"](file_path=str(step))

        assert result["success"] is False
        assert result["error"]["code"] == "UNREADABLE_INPUT"
        assert "import_step_file" in result["error"]["message"]

    def test_real_mesh_still_analyzes(self, mesh_tools, tmp_path):
        stl = tmp_path / "tri.stl"
        stl.write_text(
            "solid tri\n"
            " facet normal 0 0 1\n"
            "  outer loop\n"
            "   vertex 0 0 0\n"
            "   vertex 10 0 0\n"
            "   vertex 0 10 0\n"
            "  endloop\n"
            " endfacet\n"
            "endsolid tri\n"
        )

        result = mesh_tools["analyze_mesh_geometry"](file_path=str(stl))

        assert result["success"] is True
        assert result["triangle_count"] == 1
