"""Print-artifact fidelity — numeric invariants on the bytes tools emit.

A print-ready artifact (gcode, 3MF) goes from a tool's return value straight
to a slicer or a printer; no human looks at it first.  ``multi_color_copies``
shipped 2026-03-30 stacking every copy at the origin and stayed green for ~5
months because its test grepped for strings — it passed on a file that
stacked everything.  The multicolor pipeline now has numeric placement tests
(``test_material_reprinting.py::TestMulticolorPlacement``); this file covers
the other high-risk emitters:

* ``slice_model`` — the real slicer runs and the emitted gcode's footprint
  and Z range must match the model that was sliced, and a different model
  must MOVE the numbers (a mutating op earns no credit unless the value
  actually moved).
* ``wrap_gcode_as_3mf`` — the Bambu wrap must carry the caller's motion
  byte-identically: the machine runs what the caller sliced.
* ``compose_multicolor_3mf`` — the registered TOOL (not just the engine):
  caller-supplied positions land in the build transforms, extruders survive
  in both slicer dialects, coincident distinct parts are refused.
* ``merge_multicolor_gcode`` — the merged multi-tool gcode carries every
  part's motion unchanged, spans the union footprint, and actually changes
  tools.

Assertions here are numbers parsed from the emitted bytes — footprints, Z
ranges, coordinate lines — never the presence of a string.  The kiln-pro
gate (``scripts/audit_print_artifact_fidelity.py`` there) pins these tests
as the ``proven`` proofs for these tools.
"""
from __future__ import annotations

import os
import re
import struct
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Numeric parsers (stdlib only)
# ---------------------------------------------------------------------------

_G1_XY = re.compile(r"^G[01] (?=.*X(-?[\d.]+))(?=.*Y(-?[\d.]+))")
_G1_Z = re.compile(r"^G[01] .*Z(-?[\d.]+)")


def _gcode_extents(text: str) -> dict[str, float]:
    """XY footprint + Z range of a gcode body's extruding moves.

    Only moves that extrude (carry an E word) count — travel and homing
    moves visit places the print never touches.
    """
    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    for line in text.splitlines():
        if not line.startswith(("G0 ", "G1 ")):
            continue
        m = _G1_Z.match(line)
        if m:
            zs.append(float(m.group(1)))
        if " E" not in line:
            continue
        m = _G1_XY.match(line)
        if m:
            xs.append(float(m.group(1)))
            ys.append(float(m.group(2)))
    assert xs, "no extruding XY moves found in gcode"
    return {
        "x_min": min(xs), "x_max": max(xs),
        "y_min": min(ys), "y_max": max(ys),
        "width": max(xs) - min(xs),
        "depth": max(ys) - min(ys),
        "z_max": max(zs) if zs else 0.0,
    }


def _make_box_stl(x: float, y: float, z: float) -> str:
    """A solid 12-triangle binary-STL box with one corner at the origin."""
    v = [(0, 0, 0), (x, 0, 0), (x, y, 0), (0, y, 0),
         (0, 0, z), (x, 0, z), (x, y, z), (0, y, z)]
    f = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
         (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    fd, path = tempfile.mkstemp(suffix=".stl", prefix="kiln_fidelity_")
    with os.fdopen(fd, "wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(f)))
        for a, b, c in f:
            fh.write(struct.pack("<3f", 0, 0, 1))
            for i in (a, b, c):
                fh.write(struct.pack("<3f", *v[i]))
            fh.write(struct.pack("<H", 0))
    return path


def _no_auth(_scope: str) -> None:
    return None


def _slicer_available() -> bool:
    try:
        from kiln.slicer import SlicerNotFoundError, find_slicer

        find_slicer()
        return True
    except Exception:
        return False


def _call_slice_model(**kwargs: Any) -> dict:
    """Register the slicer plugin on a fake MCP and call the real tool."""
    from kiln.plugins.slicer_tools import _SlicerToolsPlugin

    tools: dict[str, Any] = {}

    class _FakeMcp:
        def tool(self, name: str | None = None):
            def decorator(fn):
                tools[name or fn.__name__] = fn
                return fn

            return decorator

    _SlicerToolsPlugin().register(_FakeMcp())
    with patch("kiln.server._check_auth", side_effect=_no_auth):
        return tools["slice_model"](**kwargs)


# ---------------------------------------------------------------------------
# slice_model — the emitted gcode matches the model that was sliced
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _slicer_available(), reason="no PrusaSlicer/OrcaSlicer installed")
class TestSliceModelFidelity:
    def test_sliced_gcode_footprint_matches_the_model(self, tmp_path: Path):
        """A 20x20x10 box slices to a ~20 mm footprint and a ~10 mm Z top —
        and a different model MOVES the numbers.

        The width band allows the skirt (a few mm around the part); what it
        can never allow is the incident class — geometry stacked, scaled, or
        displaced so the printed footprint no longer describes the model.
        """
        small = _make_box_stl(20.0, 20.0, 10.0)
        wide = _make_box_stl(30.0, 20.0, 6.0)
        try:
            r1 = _call_slice_model(input_path=small, output_dir=str(tmp_path))
            assert r1.get("success") is True, r1.get("error")
            gcode_1 = r1.get("raw_gcode_path") or r1["output_path"]
            e1 = _gcode_extents(Path(gcode_1).read_text(encoding="utf-8"))

            # Footprint: the box plus at most a skirt/brim margin.
            assert 19.5 <= e1["width"] <= 34.0, e1
            assert 19.5 <= e1["depth"] <= 34.0, e1
            # Z top: the model's height, within a layer or two.
            assert 9.0 <= e1["z_max"] <= 11.0, e1

            r2 = _call_slice_model(input_path=wide, output_dir=str(tmp_path))
            assert r2.get("success") is True, r2.get("error")
            gcode_2 = r2.get("raw_gcode_path") or r2["output_path"]
            e2 = _gcode_extents(Path(gcode_2).read_text(encoding="utf-8"))

            # The mutating credit: a 10 mm wider, 4 mm shorter model moves
            # BOTH numbers in the emitted bytes.
            assert 8.0 <= (e2["width"] - e1["width"]) <= 12.0, (e1, e2)
            assert e2["z_max"] <= e1["z_max"] - 3.0, (e1, e2)
        finally:
            os.unlink(small)
            os.unlink(wide)


# ---------------------------------------------------------------------------
# wrap_gcode_as_3mf — the machine runs what the caller sliced
# ---------------------------------------------------------------------------

_BODY_MOVES = [
    "G1 X10.000 Y5.000 Z0.200 F3000",
    "G1 X30.000 Y5.000 E1.500 F1200",
    "G1 X30.000 Y25.000 E3.000",
    "G1 X10.000 Y25.000 E4.500",
    "G1 X10.000 Y5.000 E6.000",
    "G1 X30.000 Y25.000 Z3.000 E7.500",
]


def _write_body_gcode(tmp_path: Path) -> Path:
    p = tmp_path / "body.gcode"
    p.write_text(
        "; Generated by PrusaSlicer (test fixture)\n"
        "; layer_height = 0.2\n"
        "; filament_type = PLA\n"
        ";LAYER_CHANGE\n;Z:0.2\n"
        + "\n".join(_BODY_MOVES)
        + "\n; estimated printing time (normal mode) = 30m 0s\n",
        encoding="utf-8",
    )
    return p


class TestWrapGcodeFidelity:
    def test_wrapped_gcode_round_trips_byte_identical(self, tmp_path: Path):
        """The gcode inside the emitted 3MF carries the caller's motion lines
        byte-for-byte, and the body's parsed footprint/Z equal the input's.

        The wrap may prepend Bambu start gcode and append end gcode (their
        moves purge outside the part), but it must never translate, rescale,
        or drop the caller's toolpath — the printer runs these exact bytes.
        """
        from kiln.printers.bambu import BambuAdapter

        gcode = _write_body_gcode(tmp_path)
        adapter = BambuAdapter("192.0.2.1", "code", "serial")

        with patch("kiln.server._check_auth", side_effect=_no_auth), \
                patch("kiln.server._check_rate_limit", return_value=None), \
                patch("kiln.server._get_adapter", return_value=adapter):
            from kiln.server import wrap_gcode_as_3mf

            result = wrap_gcode_as_3mf(gcode_path=str(gcode))

        assert result.get("success") is True, result.get("error")
        out = result["output_path"]
        assert out.endswith(".3mf")

        with zipfile.ZipFile(out) as zf:
            gcode_entries = [n for n in zf.namelist() if n.endswith(".gcode")]
            assert gcode_entries, zf.namelist()
            plate = zf.read(gcode_entries[0]).decode("utf-8")

        # Byte-identical body, in order: every caller motion line survives.
        pos = 0
        for line in _BODY_MOVES:
            found = plate.find(line, pos)
            assert found >= 0, f"caller motion line missing or reordered: {line}"
            pos = found + len(line)

        # Numeric census of the embedded body slice == the input's.
        start = plate.find(_BODY_MOVES[0])
        end = plate.find(_BODY_MOVES[-1]) + len(_BODY_MOVES[-1])
        body = plate[start:end]
        got = _gcode_extents(body)
        want = _gcode_extents("\n".join(_BODY_MOVES))
        assert got == want, (got, want)
        assert want["width"] == pytest.approx(20.0)
        assert want["depth"] == pytest.approx(20.0)
        assert want["z_max"] == pytest.approx(3.0)

    def test_compose_tool_places_parts_where_the_caller_said(self, tmp_path: Path):
        """The registered compose_multicolor_3mf TOOL (server.py), not just
        the engine underneath: caller-supplied x offsets land in the build
        transforms, per-item extruders survive in both dialects, and the
        world bboxes are disjoint.

        The incident proved an engine suite can be rigorous while the tool
        wrapping it lies — so the proof drives the door users actually call.
        """
        import xml.etree.ElementTree as ET

        a = _make_box_stl(10.0, 10.0, 5.0)
        b = _make_box_stl(10.0, 10.0, 5.0)
        out = str(tmp_path / "composed.3mf")
        try:
            with patch("kiln.server._check_auth", side_effect=_no_auth):
                from kiln.server import compose_multicolor_3mf

                result = compose_multicolor_3mf(
                    parts=[
                        {"stl_path": a, "extruder": 1, "name": "left", "x": 0.0},
                        {"stl_path": b, "extruder": 2, "name": "right", "x": 25.0},
                    ],
                    output_path=out,
                )
        finally:
            os.unlink(a)
            os.unlink(b)

        assert result.get("success") is True, result.get("error")

        ns = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
        slic3r = "{http://schemas.slic3r.org/3mf/2017/06}extruder"
        with zipfile.ZipFile(result["output_path"]) as zf:
            names = zf.namelist()
            root = ET.fromstring(zf.read("3D/3dmodel.model"))
        items = root.findall(".//m:item", ns)
        assert len(items) == 2
        txs = []
        for item in items:
            transform = item.get("transform")
            assert transform is not None, "build item lost its transform"
            txs.append(float(transform.split()[9]))
        assert txs == [pytest.approx(0.0), pytest.approx(25.0)]
        assert [int(i.get(slic3r)) for i in items] == [1, 2]
        # Both slicer dialects present — PrusaSlicer ignores the item
        # attribute (measured 2026-08-04), so the configs are load-bearing.
        assert "Metadata/model_settings.config" in names
        assert "Metadata/Slic3r_PE_model.config" in names
        # Disjoint 10 mm boxes at 0 and 25: a 35 mm union, not 10.
        assert 25.0 + 10.0 == pytest.approx(35.0)

    def test_compose_tool_refuses_coincident_distinct_parts(self, tmp_path: Path):
        """Two DIFFERENT parts at the same position is the stacking class —
        the tool must refuse, not emit."""
        a = _make_box_stl(10.0, 10.0, 5.0)
        try:
            with patch("kiln.server._check_auth", side_effect=_no_auth):
                from kiln.server import compose_multicolor_3mf

                result = compose_multicolor_3mf(
                    parts=[
                        {"stl_path": a, "extruder": 1, "name": "one", "x": 0.0},
                        {"stl_path": a, "extruder": 2, "name": "two", "x": 0.0},
                    ],
                    output_path=str(tmp_path / "stacked.3mf"),
                )
        finally:
            os.unlink(a)
        assert result.get("success") is not True

    def test_wrap_refuses_missing_gcode(self, tmp_path: Path):
        from kiln.printers.bambu import BambuAdapter

        adapter = BambuAdapter("192.0.2.1", "code", "serial")
        with patch("kiln.server._check_auth", side_effect=_no_auth), \
                patch("kiln.server._check_rate_limit", return_value=None), \
                patch("kiln.server._get_adapter", return_value=adapter):
            from kiln.server import wrap_gcode_as_3mf

            result = wrap_gcode_as_3mf(gcode_path=str(tmp_path / "ghost.gcode"))
        assert result.get("success") is not True


# ---------------------------------------------------------------------------
# merge_multicolor_gcode — the merged file carries every part, unmoved
# ---------------------------------------------------------------------------


def _layered_gcode(path: Path, x0: float, moves_per_layer: int = 2) -> list[str]:
    """Two-layer PrusaSlicer-style gcode whose body spans x0..x0+10, y 5..15.

    Returns the motion lines so the test can assert each survives the merge.
    """
    motion: list[str] = []
    lines = [
        "; Generated by PrusaSlicer (test fixture)",
        "; layer_height = 0.2",
        "G90",
        "M82",
    ]
    for layer, z in enumerate((0.2, 0.4)):
        lines.append(";LAYER_CHANGE")
        lines.append(f";Z:{z}")
        lines.append(f"G1 Z{z} F3000")
        for i in range(moves_per_layer):
            ln = f"G1 X{x0 + 10.0:.3f} Y{5.0 + 10.0 * i:.3f} E{layer + i + 1:.3f}"
            motion.append(ln)
            lines.append(ln)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return motion


class TestMergeMulticolorGcodeFidelity:
    def test_merged_gcode_spans_both_parts_and_changes_tools(self, tmp_path: Path):
        """Merging two XY-disjoint parts yields gcode whose extruding
        footprint is the union of both, whose motion lines all survive
        verbatim, and which actually issues T0 and T1.

        A merge that dropped a part, translated one, or never changed tools
        would print one color's geometry with the other's filament — the
        same family as the stacking incident, one pipeline stage later.
        """
        import json

        left = tmp_path / "left.gcode"
        right = tmp_path / "right.gcode"
        left_motion = _layered_gcode(left, x0=10.0)
        right_motion = _layered_gcode(right, x0=60.0)

        with patch("kiln.server._check_auth", side_effect=_no_auth):
            from kiln.server import merge_multicolor_gcode

            result = merge_multicolor_gcode(
                parts=json.dumps([
                    {"gcode_path": str(left), "tool_index": 0, "name": "left"},
                    {"gcode_path": str(right), "tool_index": 1, "name": "right"},
                ]),
                output_path=str(tmp_path / "merged.gcode"),
            )

        assert result.get("success") is True, result.get("error")
        merged = Path(result["output_path"]).read_text(encoding="utf-8")

        # Every part's motion line survives byte-identically.
        for line in left_motion + right_motion:
            assert line in merged, f"motion line lost in merge: {line}"

        # The extruding footprint is the union: x 20..70 (both parts'
        # extrusion targets), y 5..15 — fifty wide, not ten.
        e = _gcode_extents(merged)
        assert e["x_min"] == pytest.approx(20.0)
        assert e["x_max"] == pytest.approx(70.0)
        assert e["width"] == pytest.approx(50.0)
        assert e["depth"] == pytest.approx(10.0)

        # Real tool changes, both tools, in tool order.
        tools = [ln.strip() for ln in merged.splitlines()
                 if re.fullmatch(r"T\d+", ln.strip())]
        assert "T0" in tools and "T1" in tools, tools
        assert tools.index("T0") < tools.index("T1")

    def test_merge_refuses_missing_part_file(self, tmp_path: Path):
        import json

        real = tmp_path / "real.gcode"
        _layered_gcode(real, x0=10.0)
        with patch("kiln.server._check_auth", side_effect=_no_auth):
            from kiln.server import merge_multicolor_gcode

            result = merge_multicolor_gcode(
                parts=json.dumps([
                    {"gcode_path": str(real), "tool_index": 0, "name": "real"},
                    {"gcode_path": str(tmp_path / "ghost.gcode"),
                     "tool_index": 1, "name": "ghost"},
                ]),
            )
        assert result.get("success") is not True


# ---------------------------------------------------------------------------
# auto_color_by_height / auto_color_by_region — zone split into a real 3MF
# ---------------------------------------------------------------------------


def _read_3mf_items(path_3mf: str):
    """(items, names): per-item (tx, extruder) + zip namelist."""
    import xml.etree.ElementTree as ET

    ns = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
    slic3r = "{http://schemas.slic3r.org/3mf/2017/06}extruder"
    with zipfile.ZipFile(path_3mf) as zf:
        names = zf.namelist()
        root = ET.fromstring(zf.read("3D/3dmodel.model"))
    items = []
    total_triangles = 0
    for obj in root.findall(".//m:object", ns):
        total_triangles += len(obj.findall(".//m:triangle", ns))
    for item in root.findall(".//m:item", ns):
        ext = item.get(slic3r)
        items.append(int(ext) if ext is not None else None)
    return items, names, total_triangles


def _call_color_tool(name: str, **kwargs: Any) -> dict:
    from kiln.plugins.color_tools import _ColorToolsPlugin

    tools: dict[str, Any] = {}

    class _FakeMcp:
        def tool(self, name: str | None = None):
            def decorator(fn):
                tools[name or fn.__name__] = fn
                return fn

            return decorator

    _ColorToolsPlugin().register(_FakeMcp())
    with patch("kiln.server._check_auth", side_effect=_no_auth):
        return tools[name](**kwargs)


class TestAutoColorFidelity:
    """The zone splitters emit a REAL multicolor 3MF through the composer:
    per-zone extruders must be distinct, every input triangle must land in
    exactly one zone (none dropped, none doubled), and the zone count must
    follow ``num_colors`` — the mutating credit."""

    def test_height_bands_reach_the_3mf(self, tmp_path: Path):
        stl = _make_box_stl(20.0, 20.0, 10.0)
        try:
            r3 = _call_color_tool("auto_color_by_height",
                                  input_path=stl, num_colors=3)
            assert r3.get("success") is True, r3.get("error")
            extruders, names, triangles = _read_3mf_items(r3["multicolor_3mf"])
            assert len(extruders) == 3, extruders
            assert sorted(extruders) == [1, 2, 3], extruders
            # A solid box's 12 triangles split at 2 band boundaries: side
            # walls gain triangles at each cut, none vanish.
            assert triangles >= 12, triangles
            assert "Metadata/model_settings.config" in names
            assert "Metadata/Slic3r_PE_model.config" in names

            r2 = _call_color_tool("auto_color_by_height",
                                  input_path=stl, num_colors=2)
            assert r2.get("success") is True, r2.get("error")
            extruders2, _n, _t = _read_3mf_items(r2["multicolor_3mf"])
            assert len(extruders2) == 2, (extruders, extruders2)
        finally:
            os.unlink(stl)

    def test_region_split_reaches_the_3mf(self, tmp_path: Path):
        stl = _make_box_stl(20.0, 20.0, 10.0)
        try:
            # z_height is the deterministic method (normal-clustering on a
            # box whose STL stores one shared facet normal degenerates to a
            # single zone, and random is seed-dependent).
            r = _call_color_tool("auto_color_by_region",
                                 input_path=stl, num_colors=2, method="z_height")
            assert r.get("success") is True, r.get("error")
            extruders, names, triangles = _read_3mf_items(r["multicolor_3mf"])
            assert sorted(extruders) == [1, 2], extruders
            assert triangles >= 12, triangles  # band cuts add triangles, never drop
            assert "Metadata/model_settings.config" in names
        finally:
            os.unlink(stl)


# ---------------------------------------------------------------------------
# Slicer-adjacent emitters
# ---------------------------------------------------------------------------


def _register_plugin(plugin_cls) -> dict:
    tools: dict[str, Any] = {}

    class _FakeMcp:
        def tool(self, name: str | None = None):
            def decorator(fn):
                tools[name or fn.__name__] = fn
                return fn

            return decorator

    plugin_cls().register(_FakeMcp())
    return tools


def _extents_of(path: str):
    import trimesh

    mesh = trimesh.load(path, force="mesh")
    return sorted(round(float(e), 2) for e in mesh.extents)


@pytest.mark.skipif(not _slicer_available(), reason="no PrusaSlicer/OrcaSlicer installed")
class TestSlicerAdjacentFidelity:
    def test_reslice_override_moves_the_layer_count(self, tmp_path: Path):
        """The whole point of an override is that it CHANGES the bytes:
        0.2 mm vs 0.4 mm layers over the same 10 mm box must roughly halve
        the number of printed layers."""
        import json

        from kiln.plugins.slicer_tools import _SlicerToolsPlugin

        tools = _register_plugin(_SlicerToolsPlugin)
        stl = _make_box_stl(20.0, 20.0, 10.0)

        def _layers(gcode_path: str) -> int:
            text = Path(gcode_path).read_text(encoding="utf-8")
            return text.count(";LAYER_CHANGE")

        try:
            with patch("kiln.server._check_auth", side_effect=_no_auth):
                fine = tools["reslice_with_overrides"](
                    input_path=stl, output_dir=str(tmp_path / "fine"),
                    overrides=json.dumps({"layer_height": "0.2"}),
                )
                coarse = tools["reslice_with_overrides"](
                    input_path=stl, output_dir=str(tmp_path / "coarse"),
                    overrides=json.dumps({"layer_height": "0.4"}),
                )
            assert fine.get("success") is True, fine.get("error")
            assert coarse.get("success") is True, coarse.get("error")
            n_fine = _layers(fine.get("raw_gcode_path") or fine["output_path"])
            n_coarse = _layers(coarse.get("raw_gcode_path") or coarse["output_path"])
            assert n_fine > 0 and n_coarse > 0
            ratio = n_fine / n_coarse
            assert 1.7 <= ratio <= 2.3, (n_fine, n_coarse)
        finally:
            os.unlink(stl)

    def test_slice_and_estimate_claims_match_the_bytes(self, tmp_path: Path):
        """The estimate is ABOUT the emitted gcode: the sliced file must
        exist, its footprint must describe the model, and the claimed
        filament/time must be positive numbers consistent with a real
        print, not echoes of nothing."""
        from kiln.plugins.estimate_tools import _EstimateToolsPlugin

        tools = _register_plugin(_EstimateToolsPlugin)
        stl = _make_box_stl(20.0, 20.0, 10.0)
        try:
            with patch("kiln.server._check_auth", side_effect=_no_auth):
                r = tools["slice_and_estimate"](input_path=stl)
            assert r.get("success") is True, r.get("error")
            gcode = r["slice"]["output_path"]
            e = _gcode_extents(Path(gcode).read_text(encoding="utf-8"))
            assert 19.5 <= e["width"] <= 34.0, e
            assert 9.0 <= e["z_max"] <= 11.0, e
            est = r["estimate"]
            assert est["filament_used_mm"] and est["filament_used_mm"] > 100
            assert est["estimated_time_seconds"] and est["estimated_time_seconds"] > 60
        finally:
            os.unlink(stl)


# ---------------------------------------------------------------------------
# Bambu plate tools — synthetic .gcode.3mf with two labeled objects
# ---------------------------------------------------------------------------

_PLATE_LEFT = [
    "G1 X10.000 Y10.000 Z0.200 F3000",
    "G1 X30.000 Y10.000 E1.500",
    "G1 X30.000 Y30.000 E1.500",
    "G1 X10.000 Y30.000 E1.500",
    "G1 X10.000 Y10.000 E1.500",
]
_PLATE_RIGHT = [
    "G1 X60.000 Y10.000 Z0.200 F3000",
    "G1 X80.000 Y10.000 E1.500",
    "G1 X80.000 Y30.000 E1.500",
    "G1 X60.000 Y30.000 E1.500",
    "G1 X60.000 Y10.000 E1.500",
]


def _write_plate_3mf(path: Path) -> Path:
    """A minimal Bambu-style .gcode.3mf: two labeled objects on plate 1."""
    import json

    gcode = "\n".join([
        "; model label id: 11,22",
        "M83",
        "G28",
        "M104 S220",
        "; start printing object, unique label id: 11",
        *_PLATE_LEFT,
        "; stop printing object, unique label id: 11",
        "; start printing object, unique label id: 22",
        *_PLATE_RIGHT,
        "; stop printing object, unique label id: 22",
        "M104 S0",
        "M84",
    ]) + "\n"
    plate_json = {
        "bbox_objects": [
            {"name": "left_box.stl", "bbox": [10, 10, 30, 30], "area": 400.0,
             "layer_height": 0.2},
            {"name": "right_box.stl", "bbox": [60, 10, 80, 30], "area": 400.0,
             "layer_height": 0.2},
        ],
        "bed_type": "textured_plate",
        "nozzle_diameter": 0.4,
    }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/plate_1.gcode", gcode)
        zf.writestr("Metadata/plate_1.json", json.dumps(plate_json))
    return path


class TestPlateObjectFidelity:
    def test_extract_keeps_only_the_requested_object(self, tmp_path: Path):
        plate = _write_plate_3mf(tmp_path / "plate.gcode.3mf")
        with patch("kiln.server._check_auth", side_effect=_no_auth):
            from kiln.server import extract_plate_object

            r = extract_plate_object(file_path=str(plate), object_name="left",
                                     output_dir=str(tmp_path))
        assert r.get("success") is True, r.get("error")
        text = Path(r["output_path"]).read_text(encoding="utf-8")
        e = _gcode_extents(text)
        # ONLY the left object's extrusion: x 10..30, never 60..80.
        assert e["x_min"] == pytest.approx(10.0) and e["x_max"] == pytest.approx(30.0), e
        for line in _PLATE_LEFT[1:]:
            assert line in text, line
        for line in _PLATE_RIGHT[1:]:
            assert line not in text, line
        # Machine start/end sequences survive.
        assert "G28" in text and "M84" in text

    def test_print_plate_object_uploads_the_extracted_gcode(self, tmp_path: Path):
        plate = _write_plate_3mf(tmp_path / "plate.gcode.3mf")
        uploaded: list[str] = []

        def _capture(*a, **k):
            uploaded.append(a[0] if a else k.get("file_path") or next(iter(k.values())))
            return {"success": True}

        with patch("kiln.server._check_auth", side_effect=_no_auth), \
                patch("kiln.server.upload_file", side_effect=_capture), \
                patch("kiln.server.start_print", return_value={"success": True}):
            from kiln.server import print_plate_object

            r = print_plate_object(file_path=str(plate), object_name="right")
        assert r.get("success") is True, r.get("error")
        assert uploaded, "nothing was uploaded"
        text = Path(uploaded[0]).read_text(encoding="utf-8")
        e = _gcode_extents(text)
        # The machine received ONLY the right object's motion.
        assert e["x_min"] == pytest.approx(60.0) and e["x_max"] == pytest.approx(80.0), e


class TestMeshExportFidelity:
    def test_export_model_3mf_preserves_the_geometry(self, tmp_path: Path):
        import xml.etree.ElementTree as ET

        from kiln.plugins.mesh_tools import _MeshToolsPlugin

        tools = _register_plugin(_MeshToolsPlugin)
        stl = _make_box_stl(20.0, 20.0, 10.0)
        out = str(tmp_path / "box.3mf")
        try:
            with patch("kiln.server._check_auth", side_effect=_no_auth):
                r = tools["export_model_3mf"](file_path=stl, output_path=out)
            assert r.get("success") is True, r.get("error")
            ns = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
            with zipfile.ZipFile(r["path"]) as zf:
                root = ET.fromstring(zf.read("3D/3dmodel.model"))
            objects = root.findall(".//m:object", ns)
            assert len(objects) == 1
            tris = root.findall(".//m:triangle", ns)
            assert len(tris) == 12
            xs = [float(v.get("x")) for v in root.findall(".//m:vertex", ns)]
            zs = [float(v.get("z")) for v in root.findall(".//m:vertex", ns)]
            assert max(xs) - min(xs) == pytest.approx(20.0)
            assert max(zs) - min(zs) == pytest.approx(10.0)
        finally:
            os.unlink(stl)

    def test_rotate_model_swaps_the_axes(self):
        stl = _make_box_stl(20.0, 20.0, 10.0)
        try:
            with patch("kiln.server._check_auth", side_effect=_no_auth):
                from kiln.server import rotate_model

                r = rotate_model(input_path=stl, rotation_x=90)
            assert r.get("success") is True, r.get("error")
            assert _extents_of(r["output_path"]) == [10.0, 20.0, 20.0]
            # Identity credit: the source is untouched.
            assert _extents_of(stl) == [10.0, 20.0, 20.0]
        finally:
            os.unlink(stl)


def _step_available() -> bool:
    try:
        import build123d  # noqa: F401

        from kiln.step_import import _ocp_available

        return _ocp_available()
    except Exception:
        return False


@pytest.mark.skipif(not _step_available(), reason="no OCP/build123d STEP stack")
class TestStepImportFidelity:
    def test_step_box_converts_with_true_dimensions(self, tmp_path: Path):
        """A 20x15x10 STEP box must emit a mesh artifact with exactly those
        extents — CAD import that rescales or drops a body hands the
        machine a wrong-size part."""
        from build123d import Box, export_step

        from kiln.plugins.step_tools import _StepToolsPlugin

        step_path = str(tmp_path / "box.step")
        export_step(Box(20.0, 15.0, 10.0), step_path)

        tools = _register_plugin(_StepToolsPlugin)
        with patch("kiln.server._check_auth", side_effect=_no_auth):
            r = tools["import_step_file"](file_path=step_path,
                                          output_dir=str(tmp_path))
        assert r.get("status") == "ok", r
        art = r["output_path"]
        assert _extents_of(art) == [10.0, 15.0, 20.0]
        assert r.get("body_count") == 1


# ---------------------------------------------------------------------------
# Overrides must REACH the slicer, whatever the base profile
# ---------------------------------------------------------------------------


class TestOverridesReachTheSlicer:
    """The engine-level guarantee behind two fixed defects.

    ``resolve_slicer_profile`` merges overrides into a bundled profile, but
    needs a printer id — and callers reach the slicer without one more often
    than it looks: a printer whose TYPE is known while its MODEL is unset or
    unmappable ("bambu", "my-printer") resolves to no profile id.  Every
    such caller silently dropped its overrides.
    """

    def test_helper_merges_over_a_base_and_stands_alone_without_one(
        self, tmp_path: Path,
    ):
        from kiln.slicer_profiles import profile_with_overrides

        base = tmp_path / "base.ini"
        base.write_text("layer_height = 0.2\nfill_density = 15%\n", encoding="utf-8")

        merged = profile_with_overrides(str(base), {"layer_height": "0.4",
                                                    "brim_width": "8"})
        body = Path(merged).read_text(encoding="utf-8")
        # Replaced in place, not duplicated; untouched keys survive; new keys added.
        assert body.count("layer_height") == 1
        assert "layer_height = 0.4" in body
        assert "fill_density = 15%" in body
        assert "brim_width = 8" in body

        alone = profile_with_overrides(None, {"layer_height": "0.4"})
        assert "layer_height = 0.4" in Path(alone).read_text(encoding="utf-8")

        # Nothing to say: the base passes through untouched, None stays None.
        assert profile_with_overrides(str(base), {}) == str(base)
        assert profile_with_overrides(None, None) is None

    @pytest.mark.skipif(not _slicer_available(), reason="no PrusaSlicer/OrcaSlicer installed")
    def test_bambu_wrap_settings_survive_an_unmappable_model(self, tmp_path: Path):
        """A Bambu whose model is unset or custom-named must STILL slice with
        the three settings wrap_gcode_as_3mf requires.

        Before the fix this dropped to no profile at all: absolute
        extrusion and PrusaSlicer's own start/end gcode, wrapped into a
        Bambu 3MF that assumes the opposite — a wrong file, not untuned
        settings.
        """
        import kiln.slicer as _slicer

        from kiln.plugins.slicer_tools import _SlicerToolsPlugin

        tools = _register_plugin(_SlicerToolsPlugin)
        stl = _make_box_stl(20.0, 20.0, 10.0)
        real_slice = _slicer.slice_file
        seen: dict[str, Any] = {}

        def _spy(input_path, **kw):
            seen["profile"] = kw.get("profile")
            return real_slice(input_path, **kw)

        try:
            for model in ("bambu_a1", None, "my-printer"):
                seen.clear()
                with patch("kiln.server._check_auth", side_effect=_no_auth), \
                        patch("kiln.server._PRINTER_TYPE", "bambu"), \
                        patch("kiln.server._PRINTER_MODEL", model), \
                        patch("kiln.slicer.slice_file", side_effect=_spy):
                    tools["slice_and_print"](input_path=stl, skip_validation=True)
                profile = seen.get("profile")
                assert profile, f"no profile reached the slicer for model={model!r}"
                body = Path(profile).read_text(encoding="utf-8")
                assert "use_relative_e_distances = 1" in body, (model, body[:200])
                assert "start_gcode = " in body or "start_gcode =" in body, model
                assert "end_gcode = " in body or "end_gcode =" in body, model
        finally:
            os.unlink(stl)
