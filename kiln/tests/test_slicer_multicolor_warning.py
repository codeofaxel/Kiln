"""Multicolor-flatten advisory on the slicer tool surface.

A multicolor 3MF — per-object extruder assignments (compose_multicolor_3mf)
or a painted single object (compose_painted_3mf) — sliced through a
single-filament configuration prints ENTIRELY in one filament: measured
with PrusaSlicer's default single-extruder config on a two-color file,
the output had 0 tool changes and 0.00 mm of second filament.  Before the
advisory, ``slice_model`` returned a bare green success and the user found
out at the printer.

These tests cover the detection helpers and the advisory chokepoint in
``kiln.plugins.slicer_tools``, plus the ``slice_model`` tool wiring (real
tool, mocked slicer — the house idiom).  The advisory warns and never
blocks; any detection failure reads as "not multicolor".
"""

from __future__ import annotations

import ast
import struct
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from kiln.multicolor_3mf import (
    ColorPart,
    compose_multicolor_3mf,
    compose_painted_3mf,
)
from kiln.plugins.slicer_tools import (
    _count_gcode_tools,
    _detect_3mf_multicolor,
    _multicolor_flatten_advisory,
    _profile_filament_slots,
)

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _write_tri_stl(path: Path, z: float = 0.0) -> str:
    """Minimal binary STL: one triangle (enough for the composer)."""
    with open(path, "wb") as f:
        f.write(b"\x00" * 80)
        f.write(struct.pack("<I", 1))
        f.write(struct.pack("<3f", 0, 0, 1))
        f.write(struct.pack("<3f", 0, 0, z))
        f.write(struct.pack("<3f", 10, 0, z))
        f.write(struct.pack("<3f", 5, 10, z))
        f.write(struct.pack("<H", 0))
    return str(path)


_TETRA_TRIS = [
    ((0, 0, 0), (10, 0, 0), (5, 10, 0)),
    ((0, 0, 0), (5, 10, 0), (5, 5, 8)),
    ((10, 0, 0), (5, 5, 8), (5, 10, 0)),
    ((0, 0, 0), (5, 5, 8), (10, 0, 0)),
]


def _two_color_3mf(tmp_path: Path) -> str:
    """Multi-object form: two parts on two different extruders."""
    a = _write_tri_stl(tmp_path / "a.stl")
    b = _write_tri_stl(tmp_path / "b.stl", z=1.0)
    result = compose_multicolor_3mf(
        [
            ColorPart(a, extruder=1, name="body", color="#AAAAAA"),
            ColorPart(b, extruder=2, name="accent", color="#111111"),
        ],
        output_path=str(tmp_path / "two_color.3mf"),
    )
    assert result["success"], result
    return result["output_path"]


def _plain_3mf(tmp_path: Path) -> str:
    """Single part, single extruder, no color hints — not multicolor."""
    a = _write_tri_stl(tmp_path / "solo.stl")
    result = compose_multicolor_3mf(
        [ColorPart(a, extruder=1, name="solo")],
        output_path=str(tmp_path / "plain.3mf"),
    )
    assert result["success"], result
    return result["output_path"]


def _painted_3mf(tmp_path: Path, colors: list[str | None]) -> str:
    result = compose_painted_3mf(
        _TETRA_TRIS,
        colors,
        output_path=str(tmp_path / "painted.3mf"),
        name="painted",
    )
    assert result["success"], result
    return result["output_path"]


def _single_slot_ini(tmp_path: Path) -> str:
    ini = tmp_path / "single.ini"
    ini.write_text("layer_height = 0.2\nnozzle_diameter = 0.4\n")
    return str(ini)


def _toolless_gcode(tmp_path: Path) -> str:
    g = tmp_path / "out.gcode"
    # M104 T0 is heater targeting, not a tool change — must not count.
    g.write_text("; header\nG28\nM104 T0 S200\nG1 X0 Y0\nG1 X10 Y10 E0.5\n")
    return str(g)


# ---------------------------------------------------------------------------
# _detect_3mf_multicolor
# ---------------------------------------------------------------------------


class TestDetect3mfMulticolor:
    def test_multi_object_two_extruders_detected(self, tmp_path):
        evidence = _detect_3mf_multicolor(_two_color_3mf(tmp_path))
        assert evidence is not None
        assert evidence["extruders"] == [1, 2]

    def test_plain_single_part_not_detected(self, tmp_path):
        assert _detect_3mf_multicolor(_plain_3mf(tmp_path)) is None

    def test_two_parts_same_extruder_same_color_not_detected(self, tmp_path):
        a = _write_tri_stl(tmp_path / "a.stl")
        b = _write_tri_stl(tmp_path / "b.stl", z=1.0)
        result = compose_multicolor_3mf(
            [
                ColorPart(a, extruder=1, name="a", color="#AAAAAA"),
                ColorPart(b, extruder=1, name="b", color="#AAAAAA"),
            ],
            output_path=str(tmp_path / "one_filament.3mf"),
        )
        assert result["success"], result
        assert _detect_3mf_multicolor(result["output_path"]) is None

    def test_painted_two_colors_detected(self, tmp_path):
        path = _painted_3mf(
            tmp_path, ["#FF0000", "#FF0000", "#0000FF", None],
        )
        evidence = _detect_3mf_multicolor(path)
        assert evidence is not None
        assert evidence["palette_colors"] == 2

    def test_painted_single_color_not_detected(self, tmp_path):
        path = _painted_3mf(tmp_path, ["#FF0000"] * 4)
        assert _detect_3mf_multicolor(path) is None

    @pytest.mark.parametrize(
        "attribute",
        ["paint_color", "slic3rpe:mmu_segmentation"],
    )
    def test_native_paint_attribute_detected(self, tmp_path, attribute):
        """Slicer-native painted models (BambuStudio paint_color /
        PrusaSlicer mmu_segmentation) are detected when they carry two-plus
        DISTINCT paint states — a painting that is one state everywhere is
        one filament, and flattening it loses nothing."""
        path = tmp_path / "native_paint.3mf"
        model = (
            '<?xml version="1.0"?><model><resources><object id="1">'
            f"<mesh><triangles><triangle v1=\"0\" v2=\"1\" v3=\"2\" {attribute}=\"4\"/>"
            f"<triangle v1=\"0\" v2=\"2\" v3=\"3\" {attribute}=\"8\"/>"
            "</triangles></mesh></object></resources></model>"
        )
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("3D/3dmodel.model", model)
        evidence = _detect_3mf_multicolor(str(path))
        assert evidence is not None
        assert evidence["paint_attribute"] in (
            "paint_color", "mmu_segmentation",
        )

    def test_corrupt_zip_reads_as_not_multicolor(self, tmp_path):
        corrupt = tmp_path / "corrupt.3mf"
        corrupt.write_bytes(b"this is not a zip archive")
        assert _detect_3mf_multicolor(str(corrupt)) is None

    def test_missing_file_reads_as_not_multicolor(self, tmp_path):
        assert _detect_3mf_multicolor(str(tmp_path / "nope.3mf")) is None


# ---------------------------------------------------------------------------
# _profile_filament_slots
# ---------------------------------------------------------------------------


class TestProfileFilamentSlots:
    def test_no_profile_is_single_slot(self):
        assert _profile_filament_slots(None) == 1

    def test_single_value_nozzle_diameter(self, tmp_path):
        assert _profile_filament_slots(_single_slot_ini(tmp_path)) == 1

    def test_two_value_nozzle_diameter(self, tmp_path):
        ini = tmp_path / "dual.ini"
        ini.write_text("nozzle_diameter = 0.4,0.4\n")
        assert _profile_filament_slots(str(ini)) == 2

    def test_missing_key_is_single_slot(self, tmp_path):
        ini = tmp_path / "bare.ini"
        ini.write_text("layer_height = 0.2\n")
        assert _profile_filament_slots(str(ini)) == 1

    def test_unreadable_profile_is_single_slot(self, tmp_path):
        assert _profile_filament_slots(str(tmp_path / "gone.ini")) == 1

    def test_bundled_profile_is_single_slot(self):
        """Pin the premise: Kiln's bundled profiles are single-extruder
        today, which is exactly the configuration the measured flatten
        happened under."""
        from kiln.slicer_profiles import resolve_slicer_profile

        assert _profile_filament_slots(resolve_slicer_profile("ender3")) == 1


# ---------------------------------------------------------------------------
# _count_gcode_tools
# ---------------------------------------------------------------------------


class TestCountGcodeTools:
    def test_toolless_gcode(self, tmp_path):
        assert _count_gcode_tools(_toolless_gcode(tmp_path)) == (0, 0)

    def test_two_tools_counted(self, tmp_path):
        g = tmp_path / "multi.gcode"
        g.write_text("T0\nG1 X0\nT1\nG1 X1\nT0\nG1 X2\n")
        assert _count_gcode_tools(str(g)) == (2, 2)

    def test_repeated_same_tool_is_not_a_change(self, tmp_path):
        g = tmp_path / "same.gcode"
        g.write_text("T0\nG1 X0\nT0\nG1 X1\n")
        assert _count_gcode_tools(str(g)) == (0, 1)

    def test_unreadable_file_returns_none(self, tmp_path):
        assert _count_gcode_tools(str(tmp_path / "gone.gcode")) is None

    def test_none_path_returns_none(self):
        assert _count_gcode_tools(None) is None


# ---------------------------------------------------------------------------
# _multicolor_flatten_advisory
# ---------------------------------------------------------------------------


class TestMulticolorFlattenAdvisory:
    def test_warns_on_multi_object_single_slot(self, tmp_path):
        block, warning = _multicolor_flatten_advisory(
            _two_color_3mf(tmp_path),
            _single_slot_ini(tmp_path),
            _toolless_gcode(tmp_path),
        )
        assert block is not None
        assert block["colors_flattened"] is True
        assert block["extruders"] == [1, 2]
        assert block["tool_changes"] == 0
        assert warning is not None
        assert "ONE filament" in warning
        assert "0 tool changes" in warning
        assert "multi_material_print" in warning

    def test_warns_on_painted_single_slot(self, tmp_path):
        """Painted files now carry native paint attributes, so the
        stronger painted-on evidence (distinct paint states) outranks the
        palette count in the warning wording."""
        block, warning = _multicolor_flatten_advisory(
            _painted_3mf(tmp_path, ["#FF0000", "#FF0000", "#0000FF", None]),
            _single_slot_ini(tmp_path),
            _toolless_gcode(tmp_path),
        )
        assert block is not None
        assert warning is not None
        assert "painted-on colors" in warning
        assert block["paint_states"] == 2

    def test_warns_without_gcode_measurement(self, tmp_path):
        """No readable G-code: the structural evidence still warns, just
        without the measured tool-change claim."""
        _block, warning = _multicolor_flatten_advisory(
            _two_color_3mf(tmp_path), _single_slot_ini(tmp_path), None,
        )
        assert warning is not None
        assert "measured" not in warning

    def test_no_warning_for_stl_input(self, tmp_path):
        stl = _write_tri_stl(tmp_path / "part.stl")
        assert _multicolor_flatten_advisory(
            stl, _single_slot_ini(tmp_path), _toolless_gcode(tmp_path),
        ) == (None, None)

    def test_no_warning_for_plain_3mf(self, tmp_path):
        assert _multicolor_flatten_advisory(
            _plain_3mf(tmp_path),
            _single_slot_ini(tmp_path),
            _toolless_gcode(tmp_path),
        ) == (None, None)

    def test_no_warning_when_profile_has_two_slots(self, tmp_path):
        ini = tmp_path / "dual.ini"
        ini.write_text("nozzle_diameter = 0.4,0.4\n")
        assert _multicolor_flatten_advisory(
            _two_color_3mf(tmp_path), str(ini), _toolless_gcode(tmp_path),
        ) == (None, None)

    def test_no_warning_when_gcode_measures_two_tools(self, tmp_path):
        """The measured output outranks the profile read: if two tools
        actually ran, the colors survived — don't cry wolf."""
        g = tmp_path / "multi.gcode"
        g.write_text("T0\nG1 X0\nT1\nG1 X1\n")
        assert _multicolor_flatten_advisory(
            _two_color_3mf(tmp_path), _single_slot_ini(tmp_path), str(g),
        ) == (None, None)

    def test_corrupt_zip_no_warning_no_raise(self, tmp_path):
        corrupt = tmp_path / "corrupt.3mf"
        corrupt.write_bytes(b"not a zip")
        assert _multicolor_flatten_advisory(
            str(corrupt), _single_slot_ini(tmp_path), _toolless_gcode(tmp_path),
        ) == (None, None)


# ---------------------------------------------------------------------------
# slice_model tool wiring (real tool, mocked slicer — house idiom)
# ---------------------------------------------------------------------------


def _register_slicer_tools() -> dict:
    """Register the plugin on a fake MCP and return captured tool fns."""
    from kiln.plugins.slicer_tools import plugin

    tools: dict = {}

    class FakeMCP:
        def tool(self_mcp, name: str | None = None):
            def decorator(fn):
                tools[name or fn.__name__] = fn
                return fn
            return decorator

    plugin.register(FakeMCP())
    return tools


@pytest.fixture(scope="module")
def slicer_tools():
    return _register_slicer_tools()


class TestSliceModelToolWiring:
    def _slice(self, slicer_tools, tmp_path, input_path: str) -> dict:
        """Run the real slice_model tool with the slicer binary mocked
        out (the mock writes toolless gcode, as a single-extruder slice
        of a multicolor file measurably produces)."""
        from kiln.slicer import SliceResult

        gcode = _toolless_gcode(tmp_path)
        ini = _single_slot_ini(tmp_path)

        def fake_slice_file(path, **kwargs):
            return SliceResult(
                success=True, output_path=gcode,
                slicer="prusa-slicer", message="ok",
            )

        import kiln.server as _srv

        with patch.object(_srv, "_check_auth", return_value=None), \
                patch.object(
                    _srv, "_resolve_slice_profile_context",
                    return_value=(None, ini),
                ), \
                patch.object(_srv, "_PRINTER_MODEL", None), \
                patch("kiln.slicer.slice_file", side_effect=fake_slice_file):
            return slicer_tools["slice_model"](input_path=input_path)

    def test_two_color_3mf_gets_flatten_warning(self, slicer_tools, tmp_path):
        response = self._slice(
            slicer_tools, tmp_path, _two_color_3mf(tmp_path),
        )
        assert response["success"] is True
        assert response["multicolor_flattened"]["colors_flattened"] is True
        assert response["multicolor_flattened"]["tool_changes"] == 0
        assert any("ONE filament" in w for w in response["warnings"])

    def test_corrupt_3mf_slices_clean_without_warning(
        self, slicer_tools, tmp_path,
    ):
        """Detection failure must not break (or noise up) slicing."""
        corrupt = tmp_path / "corrupt.3mf"
        corrupt.write_bytes(b"not a zip")
        response = self._slice(slicer_tools, tmp_path, str(corrupt))
        assert response["success"] is True
        assert "multicolor_flattened" not in response
        assert "warnings" not in response

    def test_stl_slices_clean_without_warning(self, slicer_tools, tmp_path):
        stl = _write_tri_stl(tmp_path / "part.stl")
        response = self._slice(slicer_tools, tmp_path, stl)
        assert response["success"] is True
        assert "multicolor_flattened" not in response
        assert "warnings" not in response


class TestAdvisoryWiredIntoEveryDoor:
    """All three slicing tools route through the one advisory helper —
    a per-door reimplementation is how doors drift apart."""

    def test_all_three_tools_call_the_advisory(self):
        import kiln.plugins.slicer_tools as mod

        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        callers: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id == "_multicolor_flatten_advisory"
                ):
                    callers.add(node.name)
        assert {
            "slice_model", "reslice_with_overrides", "slice_and_print",
        } <= callers
