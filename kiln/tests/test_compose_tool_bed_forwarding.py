"""The TOOL layer forwards the printer's bed to the 3MF composers.

``test_painted_3mf_bed_placement.py`` proves the ENGINE centres an
off-plate model on whichever plate it is told about.  This file proves the
doors users actually call TELL it — the half a green engine suite cannot
see.  Five doors reach the composers:

* ``compose_multicolor_3mf`` (the tool) — took no printer at all
* ``auto_color_by_height`` / ``auto_color_by_region`` — no printer at all
* ``multi_material_print`` / ``multi_color_copies`` — arranged FOR the
  printer's bed, then composed against the 256 default, so a layout packed
  for a different bed was re-centred onto a plate the machine does not have

Placement is measured from the emitted 3MF's own build transforms via
``compute_3mf_geometry_bbox`` — the number the slicer reads — with a
no-printer control pinning the 256 default, so 90 proves the printer
reached the composer and is not a coincidence.
"""

from __future__ import annotations

import os
import struct
import tempfile
from typing import Any
from unittest.mock import patch

import pytest

from kiln.printers.bed_fit import compute_3mf_geometry_bbox

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_stl(triangles: list) -> str:
    """Binary STL from [(v0, v1, v2), ...]."""
    fd, path = tempfile.mkstemp(suffix=".stl")
    with os.fdopen(fd, "wb") as fh:
        fh.write(b"\x00" * 80 + struct.pack("<I", len(triangles)))
        for tri in triangles:
            fh.write(struct.pack("<3f", 0.0, 0.0, 0.0))
            for v in tri:
                fh.write(struct.pack("<3f", *v))
            fh.write(struct.pack("<H", 0))
    return path


def _off_plate_box(w: float = 20.0) -> list:
    """A closed box spanning x/y -w-10..-10 — off a corner-origin bed."""
    x0, x1, y0, y1, z0, z1 = -w - 10, -10, -w - 10, -10, 0, 8
    v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
         (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    quads = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),
             (2, 3, 7, 6), (1, 2, 6, 5), (3, 0, 4, 7)]
    return [tri for a, b, c, d in quads
            for tri in ((v[a], v[b], v[c]), (v[a], v[c], v[d]))]


def _no_auth(_scope: str) -> None:
    return None


def _centre_xy(threemf_path: str) -> tuple[float, float]:
    bbox = compute_3mf_geometry_bbox(threemf_path)
    assert bbox is not None, "composed 3MF carries no placeable geometry"
    return (
        (bbox["x_min"] + bbox["x_max"]) / 2,
        (bbox["y_min"] + bbox["y_max"]) / 2,
    )


def _color_tool(name: str):
    """Register the color plugin on a fake MCP and return one tool."""
    from kiln.plugins.color_tools import _ColorToolsPlugin

    tools: dict[str, Any] = {}

    class _FakeMcp:
        def tool(self):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

    _ColorToolsPlugin().register(_FakeMcp())
    return tools[name]


# ---------------------------------------------------------------------------
# compose_multicolor_3mf — the direct door
# ---------------------------------------------------------------------------


class TestComposeToolForwardsTheBed:
    def test_printer_id_reaches_the_composer(self):
        stl = _write_stl(_off_plate_box())
        try:
            with patch("kiln.server._check_auth", side_effect=_no_auth):
                from kiln.server import compose_multicolor_3mf

                result = compose_multicolor_3mf(
                    parts=[{"stl_path": stl, "extruder": 1}],
                    printer_id="bambu_a1_mini",
                )
        finally:
            os.unlink(stl)
        assert result.get("success") is True, result.get("error")
        cx, cy = _centre_xy(result["output_path"])
        assert cx == pytest.approx(90.0, abs=0.5), (
            f"centred at x={cx:.1f} — 90 is the middle of the A1 mini's "
            "180mm bed, 128 means the printer never reached the composer"
        )
        assert cy == pytest.approx(90.0, abs=0.5)

    def test_without_a_printer_the_default_plate_stands(self):
        """The control: 90 above is the printer, not a coincidence."""
        stl = _write_stl(_off_plate_box())
        try:
            with patch("kiln.server._check_auth", side_effect=_no_auth):
                from kiln.server import compose_multicolor_3mf

                result = compose_multicolor_3mf(
                    parts=[{"stl_path": stl, "extruder": 1}],
                )
        finally:
            os.unlink(stl)
        assert result.get("success") is True, result.get("error")
        cx, _cy = _centre_xy(result["output_path"])
        assert cx == pytest.approx(128.0, abs=0.5)

    def test_explicit_plate_dims_set_the_centre(self):
        stl = _write_stl(_off_plate_box())
        try:
            with patch("kiln.server._check_auth", side_effect=_no_auth):
                from kiln.server import compose_multicolor_3mf

                result = compose_multicolor_3mf(
                    parts=[{"stl_path": stl, "extruder": 1}],
                    plate_width=300.0,
                    plate_depth=200.0,
                )
        finally:
            os.unlink(stl)
        assert result.get("success") is True, result.get("error")
        cx, cy = _centre_xy(result["output_path"])
        assert cx == pytest.approx(150.0, abs=0.5)
        assert cy == pytest.approx(100.0, abs=0.5)


# ---------------------------------------------------------------------------
# auto_color_by_height / auto_color_by_region — the color doors
# ---------------------------------------------------------------------------


class TestColorToolsForwardTheBed:
    def test_by_height_composes_onto_the_named_bed(self):
        stl = _write_stl(_off_plate_box())
        try:
            result = _color_tool("auto_color_by_height")(
                input_path=stl, num_colors=2, printer_id="bambu_a1_mini",
            )
        finally:
            os.unlink(stl)
        assert result["success"] is True, result.get("error")
        assert result.get("multicolor_3mf"), result.get("compose_3mf_error")
        cx, cy = _centre_xy(result["multicolor_3mf"])
        assert cx == pytest.approx(90.0, abs=0.5)
        assert cy == pytest.approx(90.0, abs=0.5)

    def test_by_height_without_a_printer_keeps_the_default(self):
        stl = _write_stl(_off_plate_box())
        try:
            result = _color_tool("auto_color_by_height")(
                input_path=stl, num_colors=2,
            )
        finally:
            os.unlink(stl)
        assert result["success"] is True, result.get("error")
        assert result.get("multicolor_3mf"), result.get("compose_3mf_error")
        cx, _cy = _centre_xy(result["multicolor_3mf"])
        assert cx == pytest.approx(128.0, abs=0.5)

    def test_by_region_zone_path_composes_onto_the_named_bed(self):
        """method="z_height" takes _try_compose_3mf from its OWN call site,
        which can lose the forward independently of by_height's."""
        stl = _write_stl(_off_plate_box())
        try:
            result = _color_tool("auto_color_by_region")(
                input_path=stl, num_colors=2, method="z_height",
                printer_id="bambu_a1_mini",
            )
        finally:
            os.unlink(stl)
        assert result["success"] is True, result.get("error")
        assert result.get("multicolor_3mf"), result.get("compose_3mf_error")
        cx, cy = _centre_xy(result["multicolor_3mf"])
        assert cx == pytest.approx(90.0, abs=0.5)
        assert cy == pytest.approx(90.0, abs=0.5)

    def test_by_region_painted_path_composes_onto_the_named_bed(self):
        """method="normal" takes the OTHER composer — compose_painted_3mf."""
        stl = _write_stl(_off_plate_box())
        try:
            result = _color_tool("auto_color_by_region")(
                input_path=stl, num_colors=2, method="normal",
                printer_id="bambu_a1_mini",
            )
        finally:
            os.unlink(stl)
        assert result["success"] is True, result.get("error")
        assert result.get("multicolor_3mf"), result.get("compose_3mf_error")
        cx, cy = _centre_xy(result["multicolor_3mf"])
        assert cx == pytest.approx(90.0, abs=0.5)
        assert cy == pytest.approx(90.0, abs=0.5)


# ---------------------------------------------------------------------------
# multi_material_print / multi_color_copies — the arrange-then-compose doors
# ---------------------------------------------------------------------------
#
# Both tools slice and print after composing, so they are tested at the
# seam: the real arrange runs, a recording composer captures what the door
# passes, and the flow is stopped right there by the recorded failure.
# What is asserted is the door's half of the contract — the same
# printer_id the arrangement was packed for reaches the composer.


class _RecordingComposer:
    def __init__(self):
        self.kwargs: dict | None = None

    def __call__(self, parts, output_path=None, **kwargs):
        self.kwargs = kwargs
        return {"success": False, "error": "recorded — stop here"}


class TestArrangeThenComposeDoors:
    def test_multi_color_copies_composes_for_the_arranged_bed(self):
        stl = _write_stl(_off_plate_box())
        recorder = _RecordingComposer()
        try:
            with patch("kiln.server._check_auth", side_effect=_no_auth), \
                    patch("kiln.multicolor_3mf.compose_multicolor_3mf", recorder):
                from kiln.server import multi_color_copies

                multi_color_copies(
                    model_path=stl,
                    copies=2,
                    ams_slots=[0, 1],
                    colors=["#ff0000", "#00ff00"],
                    printer_id="bambu_a1_mini",
                )
        finally:
            os.unlink(stl)
        assert recorder.kwargs is not None, "the door never reached the composer"
        assert recorder.kwargs.get("printer_id") == "bambu_a1_mini", (
            "arranged for the A1 mini's bed but composed without it — the "
            "composer would re-centre the layout on its 256 default"
        )

    def test_multi_material_print_composes_for_the_arranged_bed(self):
        import json

        stl = _write_stl(_off_plate_box())
        recorder = _RecordingComposer()
        try:
            with patch("kiln.server._check_auth", side_effect=_no_auth), \
                    patch("kiln.multicolor_3mf.compose_multicolor_3mf", recorder):
                from kiln.server import multi_material_print

                multi_material_print(
                    objects_json=json.dumps([
                        {"file_path": stl, "material_id": "pla"},
                        {"file_path": stl, "material_id": "pla"},
                    ]),
                    printer_id="bambu_a1_mini",
                )
        finally:
            os.unlink(stl)
        assert recorder.kwargs is not None, "the door never reached the composer"
        assert recorder.kwargs.get("printer_id") == "bambu_a1_mini"


class TestComposeSaysWhetherColoursAreLoaded:
    """compose_multicolor_3mf chooses part colours, so it carries the advisory."""

    def _compose(self, monkeypatch, reply):
        from kiln import server
        from kiln.server import compose_multicolor_3mf

        seen: dict[str, Any] = {}

        def fake(colours, *, printer_name=None, adapter=None):
            seen["colours"] = list(colours)
            seen["printer_name"] = printer_name
            return reply

        monkeypatch.setattr(server, "_spool_advisory", fake)
        body = _write_stl(_off_plate_box(20.0))
        mark = _write_stl(_off_plate_box(8.0))
        try:
            with patch("kiln.server._check_auth", side_effect=_no_auth):
                result = compose_multicolor_3mf(
                    parts=[
                        {"stl_path": body, "extruder": 1, "color": "#FFFFFF"},
                        {"stl_path": mark, "extruder": 2, "color": "#F72323"},
                    ],
                    printer_id="bambu_a1",
                )
        finally:
            os.unlink(body)
            os.unlink(mark)
        return result, seen

    def test_part_colours_are_checked(self, monkeypatch):
        result, seen = self._compose(monkeypatch, {"verdict": "mismatch", "message": "No red loaded."})
        assert result["success"] is True, result.get("error")
        assert result["ams_advisory"]["verdict"] == "mismatch"
        assert seen["colours"] == ["#FFFFFF", "#F72323"]
        assert seen["printer_name"] == "bambu_a1"

    def test_silence_when_there_is_nothing_to_say(self, monkeypatch):
        result, _ = self._compose(monkeypatch, None)
        assert result["success"] is True
        assert "ams_advisory" not in result
