"""Print-artifact fidelity — numeric invariants on the bytes tools emit.

A print-ready artifact (gcode, 3MF) goes from a tool's return value straight
to a slicer or a printer; no human looks at it first.  ``multi_color_copies``
shipped 2026-03-30 stacking every copy at the origin and stayed green for ~5
months because its test grepped for strings — it passed on a file that
stacked everything.  The multicolor pipeline now has numeric placement tests
(``test_material_reprinting.py::TestMulticolorPlacement``); this file covers
the other two high-risk emitters:

* ``slice_model`` — the real slicer runs and the emitted gcode's footprint
  and Z range must match the model that was sliced, and a different model
  must MOVE the numbers (a mutating op earns no credit unless the value
  actually moved).
* ``wrap_gcode_as_3mf`` — the Bambu wrap must carry the caller's motion
  byte-identically: the machine runs what the caller sliced.

Assertions here are numbers parsed from the emitted bytes — footprints, Z
ranges, coordinate lines — never the presence of a string.  The kiln-pro
gate (``scripts/audit_print_artifact_fidelity.py`` there) pins these tests
as the ``proven`` proofs for both tools.
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

    def test_wrap_refuses_missing_gcode(self, tmp_path: Path):
        from kiln.printers.bambu import BambuAdapter

        adapter = BambuAdapter("192.0.2.1", "code", "serial")
        with patch("kiln.server._check_auth", side_effect=_no_auth), \
                patch("kiln.server._check_rate_limit", return_value=None), \
                patch("kiln.server._get_adapter", return_value=adapter):
            from kiln.server import wrap_gcode_as_3mf

            result = wrap_gcode_as_3mf(gcode_path=str(tmp_path / "ghost.gcode"))
        assert result.get("success") is not True
