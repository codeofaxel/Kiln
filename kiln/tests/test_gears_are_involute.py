"""Every gear Kiln draws is an involute gear that meshes, and says its play.

Three doors draw a gear: the SCAD library the image-to-code provider builds
on, the ``gear_spur`` template, and the bundled BOSL2 the component catalog
points at.  The first two used to draw teeth that were not involute (round
bumps, straight flanks), ignored the pressure angle, hid the backlash, and the
herringbone gear's bore was never cut.

So these tests measure the built geometry: the library's tooth thickness
against the bundled BOSL2 gear, a pair set at the stated centre distance (no tooth cuts
into its mate, and turning one gear past the play does), the rack against a
gear, and the template against the library.  The data half holds the floors
and the cautions a person is told.  The geometry half needs OpenSCAD.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import pytest

trimesh = pytest.importorskip("trimesh", reason="needs trimesh to measure the gears")
np = pytest.importorskip("numpy")

import kiln  # noqa: E402
from kiln.generation.scad_library import get_library_source  # noqa: E402

_PKG = Path(kiln.__file__).resolve().parent
_DATA = _PKG / "data"
_GEARS_SCAD = (_PKG / "generation" / "scad_library" / "gears.scad").read_text(encoding="utf-8")
_BOSL2 = "include <BOSL2/std.scad>\ninclude <BOSL2/gears.scad>\n"
#: A unit cube far from the origin, so a build is never empty; its volume is
#: taken back off every measurement.
_MARKER = "translate([900, 900, 900]) cube(1);\n"


def _openscad_available() -> bool:
    try:
        from kiln.generation.openscad import _find_openscad

        _find_openscad()
        return True
    except Exception:  # noqa: BLE001 - any failure to find it means "not here"
        return False


needs_openscad = pytest.mark.skipif(not _openscad_available(), reason="OpenSCAD not installed on this machine")


def _volume(tmp_path: Path, code: str) -> float:
    """Volume of what *code* builds, in mm3, built through Kiln's compile path."""
    from kiln.parametric import compile_scad_code

    stl = tmp_path / f"part_{abs(hash(code))}.stl"
    compile_scad_code(code + _MARKER, output_path=str(stl), timeout=300, as_written=True)
    return abs(trimesh.load(str(stl)).volume) - 1.0


def _library(body: str) -> str:
    return get_library_source() + "\n" + body


def _pair(kind: str, teeth1: int, teeth2: int, mod: float, backlash: float, turned_deg: float = 0.0) -> str:
    """The overlap of two meshing gears, the second with a gap facing the first."""
    mate = "mirror([1, 0, 0]) " if kind == "herringbone_gear" else ""
    args = f"mod={mod}, thickness=6, backlash={backlash}"
    return _library(
        f"d = gear_center_distance({teeth1}, {teeth2}, {mod});\n"
        "intersection() {\n"
        f"  {kind}(teeth={teeth1}, {args});\n"
        f"  translate([d, 0, 0]) rotate([0, 0, 180 + 180 / {teeth2} + {turned_deg}])\n"
        f"    {mate}{kind}(teeth={teeth2}, {args});\n"
        "}\n"
    )


def _half_play_deg(teeth: int, mod: float, backlash: float) -> float:
    """How far a gear turns from the middle of its play to contact, in degrees."""
    return math.degrees((backlash / 2) / (teeth * mod / 2))


# --- the library's teeth ----------------------------------------------------


def _section(tmp_path: Path, code: str):
    """The outline of what *code* builds, cut at z = 2.5: an (n, 2, 2) array of segments."""
    from trimesh.intersections import mesh_plane

    from kiln.parametric import compile_scad_code

    stl = tmp_path / f"section_{abs(hash(code))}.stl"
    compile_scad_code(code, output_path=str(stl), timeout=300, as_written=True)
    segments = mesh_plane(trimesh.load(str(stl)), plane_normal=[0, 0, 1], plane_origin=[0, 0, 2.5])
    return np.asarray(segments)[:, :, :2]


def _tooth_thickness(segments, radius: float, teeth: int) -> float:
    """Arc length of one tooth at *radius*: the share of that circle inside the outline."""
    angles = np.linspace(0, 2 * np.pi, 36000, endpoint=False)
    px, py = radius * np.cos(angles), radius * np.sin(angles)
    x1, y1, x2, y2 = (segments[:, 0, 0], segments[:, 0, 1], segments[:, 1, 0], segments[:, 1, 1])
    inside = np.zeros(len(angles), dtype=bool)
    for start in range(0, len(angles), 2000):  # even-odd crossing count, in chunks
        qx, qy = px[start : start + 2000, None], py[start : start + 2000, None]
        straddles = (y1 > qy) != (y2 > qy)
        with np.errstate(divide="ignore", invalid="ignore"):
            at_x = x1 + (qy - y1) * (x2 - x1) / (y2 - y1)
        inside[start : start + 2000] = (np.count_nonzero(straddles & (at_x > qx), axis=1) % 2) == 1
    return float(inside.mean() * 2 * np.pi * radius / teeth)


@needs_openscad
@pytest.mark.parametrize(
    ("teeth", "mod", "pressure_angle"),
    [(20, 2, 20), (12, 1.5, 20), (17, 1.5, 20), (24, 2, 25), (8, 2, 20), (40, 1.5, 14.5)],
)
def test_the_library_gear_is_the_reference_involute(tmp_path, teeth, mod, pressure_angle):
    """Same tooth thickness as the bundled BOSL2 gear on the flank, and the same tip."""
    mine = _section(
        tmp_path,
        _library(f"spur_gear(teeth={teeth}, mod={mod}, pressure_angle={pressure_angle}, thickness=5, backlash=0);"),
    )
    reference = _section(
        tmp_path,
        _BOSL2 + f"spur_gear(mod={mod}, teeth={teeth}, pressure_angle={pressure_angle}, thickness=5, anchor=BOTTOM);\n",
    )
    tip = float(np.linalg.norm(reference.reshape(-1, 2), axis=1).max())
    my_tip = float(np.linalg.norm(mine.reshape(-1, 2), axis=1).max())
    if teeth >= 10:
        assert my_tip == pytest.approx(tip, abs=0.02)
    else:  # a tooth this shifted comes to a point; Kiln's is cut short where it is still printable
        assert tip - 0.25 * mod < my_tip <= tip + 0.02
        assert _tooth_thickness(mine, my_tip - 0.01, teeth) >= 0.15 * mod
    # Three heights on the working flank: just under the tip down to mid tooth.
    for below_tip in (0.3, 0.9, 1.5):
        radius = tip - below_tip * mod
        assert _tooth_thickness(mine, radius, teeth) == pytest.approx(
            _tooth_thickness(reference, radius, teeth), abs=0.03
        ), f"tooth thickness differs {below_tip} modules below the tip"


@needs_openscad
@pytest.mark.parametrize(("teeth1", "teeth2", "mod"), [(20, 20, 2), (12, 24, 1.5), (17, 40, 1.5), (8, 30, 2)])
def test_a_pair_meshes_at_the_stated_distance_with_the_stated_play(tmp_path, teeth1, teeth2, mod):
    backlash = 0.3
    half_play = _half_play_deg(teeth2, mod, backlash)
    centred = _volume(tmp_path, _pair("spur_gear", teeth1, teeth2, mod, backlash))
    inside = _volume(tmp_path, _pair("spur_gear", teeth1, teeth2, mod, backlash, 0.8 * half_play))
    past = _volume(tmp_path, _pair("spur_gear", teeth1, teeth2, mod, backlash, 1.6 * half_play))
    assert centred < 0.01, f"teeth cut into each other at the stated distance: {centred:.3f} mm3"
    assert inside < 0.01, f"teeth touch inside the stated play: {inside:.3f} mm3"
    assert past > 0.1, "turning one gear past the play met nothing: these teeth do not mesh"


@needs_openscad
def test_a_small_gear_sits_farther_out_than_the_sum_of_pitch_radii(tmp_path):
    """The distance for a shifted pair is BOSL2's, and the naive one makes teeth collide."""
    from kiln.parametric import compile_scad_source

    built = compile_scad_source(
        _BOSL2
        + _GEARS_SCAD.replace("module spur_gear(", "module kiln_spur_gear(")
        + "assert(abs(gear_center_distance(12, 24, 1.5) - gear_dist(teeth1=12, teeth2=24, mod=1.5)) < 1e-6);\n"
        "assert(abs(gear_center_distance(8, 30, 2, 25) - gear_dist(teeth1=8, teeth2=30, mod=2, pressure_angle=25)) < 1e-6);\n"
        "assert(gear_center_distance(20, 40, 2) == 60);\n"
        "assert(gear_center_distance(12, 24, 1.5) > 27.4);\n"
        "cube(1);\n",
        output_path=str(tmp_path / "distances.stl"),
        as_written=True,
    )
    assert Path(built.stl_path).exists()

    naive = _library(
        "intersection() {\n"
        "  spur_gear(teeth=12, mod=1.5, thickness=6, backlash=0);\n"
        "  translate([(12 + 24) * 1.5 / 2, 0, 0]) rotate([0, 0, 180 + 180 / 24])\n"
        "    spur_gear(teeth=24, mod=1.5, thickness=6, backlash=0);\n"
        "}\n"
    )
    assert _volume(tmp_path, naive) > 0.1


@needs_openscad
def test_the_pressure_angle_changes_the_tooth(tmp_path):
    def gear(angle: float) -> float:
        return _volume(tmp_path, _library(f"spur_gear(teeth=20, mod=2, pressure_angle={angle}, backlash=0);"))

    assert abs(gear(14.5) - gear(25)) > 5.0


@needs_openscad
def test_the_herringbone_gear_meshes_with_its_mirror_and_has_its_bore(tmp_path):
    solid = _volume(tmp_path, _library("herringbone_gear(thickness=10, bore=0);"))
    bored = _volume(tmp_path, _library("herringbone_gear(thickness=10, bore=8);"))
    assert bored == pytest.approx(solid - math.pi * 4**2 * 10, rel=0.02)
    assert _volume(tmp_path, _pair("herringbone_gear", 20, 20, 2, 0.3)) < 0.01
    # Unmirrored, the two helices cross.
    crossed = _pair("herringbone_gear", 20, 20, 2, 0.3).replace("mirror([1, 0, 0]) ", "")
    assert _volume(tmp_path, crossed) > 0.1


@needs_openscad
@pytest.mark.parametrize("teeth", [20, 12])
def test_the_rack_meshes_with_a_gear(tmp_path, teeth):
    """A gear tooth sits in a rack gap at the stated height; over a rack tooth it collides."""
    mod, height, length = 2, 10, 60
    pitch = math.pi * mod
    count = math.floor(length / pitch)
    first_tooth = (length - (count - 1) * pitch) / 2

    def overlap(x: float) -> float:
        return _volume(
            tmp_path,
            _library(
                "intersection() {\n"
                f"  rack_gear(length={length}, mod={mod}, height={height}, thickness=6);\n"
                # Axis along Y, one tooth pointing straight down at the rack.
                f"  translate([{x}, 6, {height - mod} + gear_rack_distance({teeth}, {mod})])\n"
                f"    rotate([90, 0, 0]) rotate([0, 0, -90]) spur_gear(teeth={teeth}, mod={mod}, thickness=6);\n"
                "}\n"
            ),
        )

    in_a_gap = first_tooth + 3 * pitch + pitch / 2
    on_a_tooth = first_tooth + 3 * pitch
    assert overlap(in_a_gap) < 0.01
    assert overlap(on_a_tooth) > 1.0


# --- the template -----------------------------------------------------------


def _template() -> dict:
    return json.loads((_DATA / "design_templates.json").read_text(encoding="utf-8"))["gear_spur"]


@needs_openscad
@pytest.mark.parametrize(("teeth", "module_mm"), [(20, 2), (12, 2)])
def test_the_template_builds_the_gear_the_library_builds(tmp_path, teeth, module_mm):
    from kiln.parametric import compile_scad_code, render_template_scad

    params = {"teeth": teeth, "module_mm": module_mm, "face_width": 8, "bore_dia": 5, "pressure_angle": 20, "backlash": 0.3}
    stl = tmp_path / "template.stl"
    compile_scad_code(render_template_scad(_template(), params), output_path=str(stl))
    template = abs(trimesh.load(str(stl)).volume)
    library = _volume(
        tmp_path, _library(f"spur_gear(teeth={teeth}, mod={module_mm}, thickness=8, bore=5, backlash=0.3);")
    )
    assert template == pytest.approx(library, rel=0.01)


def test_the_template_refuses_teeth_and_modules_that_do_not_print_or_mesh():
    parameters = _template()["parameters"]
    assert parameters["teeth"]["min"] >= 12
    assert parameters["module_mm"]["min"] >= 1.5
    assert parameters["backlash"]["default"] > 0
    assert "${backlash}" in _template()["scad_template"]


# --- what a person is told --------------------------------------------------


def _catalog() -> dict:
    return json.loads((_DATA / "component_catalog.json").read_text(encoding="utf-8"))


def test_a_worm_is_never_offered_as_a_brake():
    worm = _catalog()["worm_gear"]
    said = worm["agent_guidance"].lower()
    assert "self-locking" not in said
    assert "never rely" in said and "brake" in said
    # The request still finds the worm, which is where the caution is.
    assert "self-locking gear" in worm["user_intents"]


def test_the_catalog_gear_states_its_backlash_and_tooth_floor():
    gear = _catalog()["spur_gear"]
    assert "backlash" in gear["key_params"]
    assert "backlash=" in gear["example_call"]
    assert gear["key_params"]["teeth"]["min"] >= 12
    assert "gear_dist" in gear["agent_guidance"]


def test_gear_materials_follow_what_was_measured():
    pattern = json.loads((_DATA / "design_knowledge" / "design_templates.json").read_text(encoding="utf-8"))
    gear = pattern["patterns"]["gear"] if "patterns" in pattern else pattern["gear"]
    rated = gear["material_compatibility"]
    order = ["excellent", "good", "poor", "avoid"]
    rank = {material: order.index(band) for band in order for material in rated.get(band, [])}
    assert rank["petg"] == 0 and rank["nylon"] == 0
    assert rank["pla"] <= rank["abs"]
    assert rank["pla"] < rank["polycarbonate"]
    assert "heat" in gear["description"].lower()


def test_the_provider_is_told_the_gears_the_library_has():
    """The prompt's signatures are the library's own, so the two cannot drift."""
    from kiln.generation import gemini

    for name in ("spur_gear", "herringbone_gear", "gear_profile_2d", "rack_gear"):
        declared = re.search(rf"^module {name}\(([^)]*)\)", _GEARS_SCAD, re.MULTILINE | re.DOTALL)
        assert declared, name
        parameters = [p.split("=")[0].strip() for p in declared.group(1).split(",")]
        advertised = re.search(rf"^  {name}\(([^)]*)\)", gemini._SYSTEM_PROMPT, re.MULTILINE)
        assert advertised, name
        told = [p.split("=")[0].strip() for p in advertised.group(1).split(",")]
        assert set(told) <= set(parameters), (name, told, parameters)
        assert "backlash" in told, name
    assert "gear_center_distance(" in gemini._SYSTEM_PROMPT
