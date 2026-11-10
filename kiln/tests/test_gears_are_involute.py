"""Every gear Kiln draws is an involute gear that meshes, and says its play.

Three doors draw a gear: the SCAD library the image-to-code provider builds
on, the ``gear_spur`` template, and the bundled BOSL2 the component catalog
points at.  Each must draw involute teeth at the stated pressure angle, carry
its backlash, and cut every bore it declares.

These tests measure the built geometry against the bundled BOSL2 gear, mesh
pairs and a rack at the stated distances, and check what each door says.
The geometry half needs OpenSCAD.
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


def _section(tmp_path: Path, code: str, z: float = 2.5):
    """The outline of what *code* builds, cut at height *z*: an (n, 2, 2) array of segments."""
    from trimesh.intersections import mesh_plane

    from kiln.parametric import compile_scad_code

    stl = tmp_path / f"section_{abs(hash(code))}.stl"
    compile_scad_code(code, output_path=str(stl), timeout=300, as_written=True)
    segments = mesh_plane(trimesh.load(str(stl)), plane_normal=[0, 0, 1], plane_origin=[0, 0, z])
    return np.asarray(segments)[:, :, :2]


def _inside(segments, radius: float):
    """Which of 36000 points round the circle of *radius* lie inside the outline."""
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
    return inside


def _tooth_thickness(segments, radius: float, teeth: int) -> float:
    """Arc length of one tooth at *radius*: the share of that circle inside the outline."""
    return float(_inside(segments, radius).mean() * 2 * np.pi * radius / teeth)


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
    assert my_tip == pytest.approx(tip, abs=0.02)
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
@pytest.mark.parametrize(("teeth", "module_mm"), [(20, 2), (12, 2.5)])
def test_the_template_builds_the_gear_the_library_builds(tmp_path, teeth, module_mm):
    from kiln.parametric import compile_scad_code, render_template_scad

    params = {"teeth": teeth, "module_mm": module_mm, "face_width": 8, "bore_dia": 5, "pressure_angle": 20, "backlash": 0.3, "nozzle_mm": 0.4}
    stl = tmp_path / "template.stl"
    compile_scad_code(render_template_scad(_template(), params), output_path=str(stl))
    template = abs(trimesh.load(str(stl)).volume)
    library = _volume(
        tmp_path, _library(f"spur_gear(teeth={teeth}, mod={module_mm}, thickness=8, bore=5, backlash=0.3);")
    )
    assert template == pytest.approx(library, rel=0.01)


def _build_template(tmp_path: Path, **overrides) -> str | None:
    """Build the template with *overrides*; the refusal text, or None when it built."""
    from kiln.parametric import compile_scad_code, render_template_scad

    template = _template()
    params = {name: spec["default"] for name, spec in template["parameters"].items()} | overrides
    try:
        compile_scad_code(render_template_scad(template, params), output_path=str(tmp_path / "g.stl"))
    except ValueError as exc:
        return str(exc)
    return None


@needs_openscad
@pytest.mark.parametrize(
    ("overrides", "suggested", "clean"),
    [
        ({"teeth": 17, "module_mm": 0.6}, "0.7", "1.4"),
        ({"teeth": 12, "module_mm": 1.0}, "1.1", "2.1"),  # a shifted tip is narrower
        ({"teeth": 17, "module_mm": 0.6, "nozzle_mm": 0.6}, "1.1", "2.1"),
        ({"teeth": 17, "module_mm": 0.3, "nozzle_mm": 0.2}, "0.4", "0.7"),
    ],
)
def test_the_template_refuses_tips_under_one_printed_line(tmp_path, overrides, suggested, clean):
    refusal = _build_template(tmp_path, **overrides)
    assert refusal is not None, f"{overrides} built tips narrower than one printed line"
    assert f"Use module {suggested} or larger ({clean} for clean tips)" in refusal


def _build_template_notes(**overrides) -> tuple[bool, list[str]]:
    from kiln import server

    out = server.generate_from_template("gear_spur", {"nozzle_mm": 0.4, **overrides})
    return bool(out.get("success")), out.get("notes", [])


@needs_openscad
@pytest.mark.parametrize(
    ("overrides", "clean"),
    [
        ({"teeth": 17, "module_mm": 1.0}, "1.4"),  # a replacement for a bought module-1 gear
        ({"teeth": 12, "module_mm": 2.0}, "2.1"),
        ({"teeth": 17, "module_mm": 1.0, "pressure_angle": 25}, "1.9"),
    ],
)
def test_tips_between_one_and_two_lines_build_with_a_warning(overrides, clean):
    built, notes = _build_template_notes(**overrides)
    assert built, overrides
    assert len(notes) == 1 and "print rounded" in notes[0]
    assert f"Module {clean} or larger prints them clean" in notes[0]


@needs_openscad
def test_a_clean_gear_carries_no_warning():
    built, notes = _build_template_notes(teeth=20, module_mm=2)
    assert built and notes == []


@needs_openscad
@pytest.mark.parametrize(
    "overrides",
    [
        {},  # the defaults
        {"teeth": 17, "module_mm": 1.4},
        {"teeth": 12, "module_mm": 2.1},
        {"teeth": 17, "module_mm": 0.7, "nozzle_mm": 0.2},
    ],
)
def test_the_template_builds_the_module_it_suggests(tmp_path, overrides):
    assert _build_template(tmp_path, **overrides) is None


def test_the_template_states_its_backlash_and_nozzle():
    parameters = _template()["parameters"]
    assert parameters["backlash"]["default"] > 0
    assert parameters["nozzle_mm"]["default"] == 0.4
    assert "${backlash}" in _template()["scad_template"]
    assert "${nozzle_mm}" in _template()["scad_template"]


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


def test_the_rack_and_bevel_entries_say_backlash_defaults_to_zero():
    catalog = _catalog()
    for component in ("rack", "bevel_gear"):
        assert "backlash" in catalog[component]["agent_guidance"].lower(), component
    assert "backlash=" in catalog["rack"]["example_call"]
    assert "gear_dist(" in catalog["rack"]["agent_guidance"]


# --- the review's cases: what an earlier version of these tests let through ---


@needs_openscad
@pytest.mark.parametrize(("teeth", "mod"), [(20, 2), (12, 2), (8, 2), (40, 1.5)])
def test_the_root_is_rounded_and_at_least_as_thick_as_the_reference(tmp_path, teeth, mod):
    """A sharp root is where a printed tooth cracks; the reference rounds it."""
    mine = _section(tmp_path, _library(f"spur_gear(teeth={teeth}, mod={mod}, thickness=5, backlash=0);"))
    reference = _section(tmp_path, _BOSL2 + f"spur_gear(mod={mod}, teeth={teeth}, thickness=5, anchor=BOTTOM);\n")
    root = float(np.linalg.norm(reference.reshape(-1, 2), axis=1).min())
    assert float(np.linalg.norm(mine.reshape(-1, 2), axis=1).min()) == pytest.approx(root, abs=0.02)
    for above in (0.1, 0.25):
        radius = root + above * mod
        assert _tooth_thickness(mine, radius, teeth) >= _tooth_thickness(reference, radius, teeth) - 0.02


@needs_openscad
@pytest.mark.parametrize(("teeth1", "teeth2"), [(20, 15), (21, 20), (7, 7)])
def test_the_stated_turn_meshes_odd_and_even_mates(tmp_path, teeth1, teeth2):
    assert _volume(tmp_path, _pair("spur_gear", teeth1, teeth2, 2, 0.3)) < 0.01
    unturned = _library(
        f"intersection() {{ spur_gear(teeth={teeth1}, mod=2, thickness=6);\n"
        f"  translate([gear_center_distance({teeth1}, {teeth2}, 2), 0, 0]) spur_gear(teeth={teeth2}, mod=2, thickness=6); }}\n"
    )
    if teeth2 % 2 == 0:  # an even mate placed unturned puts a tooth on a tooth
        assert _volume(tmp_path, unturned) > 1.0


@needs_openscad
def test_an_unequal_herringbone_pair_meshes_and_each_gear_is_whole_at_mid_height(tmp_path):
    assert _volume(tmp_path, _pair("herringbone_gear", 12, 30, 2, 0.3)) < 0.01
    gear = _library("herringbone_gear(teeth=12, mod=2, thickness=10, backlash=0);")
    below, above = _section(tmp_path, gear, z=4.95), _section(tmp_path, gear, z=5.05)
    pitch_r = 12 * 2 / 2
    mismatch = float(np.mean(_inside(below, pitch_r) != _inside(above, pitch_r)))
    assert mismatch < 0.01, f"the two halves do not meet: {mismatch:.1%} of the pitch circle differs"


@needs_openscad
def test_the_rack_carries_its_share_of_the_play(tmp_path):
    """Gear and rack each take half the backlash: the pinion turns inside it and stops past it."""
    mod, height, length, teeth, backlash = 2, 10, 60, 20, 0.3
    half_play = _half_play_deg(teeth, mod, backlash)

    def overlap(turned: float) -> float:
        return _volume(
            tmp_path,
            _library(
                "intersection() {\n"
                f"  rack_gear(length={length}, mod={mod}, height={height}, thickness=6, backlash={backlash});\n"
                f"  translate([gear_rack_tooth_x({length}, {mod}, 3) + PI * {mod} / 2, 6, "
                f"{height - mod} + gear_rack_distance({teeth}, {mod})])\n"
                f"    rotate([90, 0, 0]) rotate([0, 0, -90 + {turned}]) "
                f"spur_gear(teeth={teeth}, mod={mod}, thickness=6, backlash={backlash});\n"
                "}\n"
            ),
        )

    assert overlap(0.8 * half_play) < 0.01
    assert overlap(-0.8 * half_play) < 0.01
    assert overlap(1.6 * half_play) > 0.1


@needs_openscad
@pytest.mark.parametrize("backlash", [0.3, 0.6])
def test_the_template_thins_each_gear_by_half_the_pair_backlash(tmp_path, backlash):
    from kiln.parametric import render_template_scad

    params = {"teeth": 20, "module_mm": 2, "face_width": 8, "bore_dia": 5, "pressure_angle": 20,
              "backlash": backlash, "nozzle_mm": 0.4}
    template = _section(tmp_path, render_template_scad(_template(), params), z=4)
    library = _section(tmp_path, _library(f"spur_gear(teeth=20, mod=2, thickness=8, backlash={backlash});"), z=4)
    pitch_r = 20 * 2 / 2
    expected = math.pi * 2 / 2 - backlash / 2  # half the circular pitch, less this gear's share
    assert _tooth_thickness(template, pitch_r, 20) == pytest.approx(expected, abs=0.02)
    assert _tooth_thickness(library, pitch_r, 20) == pytest.approx(expected, abs=0.02)


@needs_openscad
def test_two_template_gears_mesh_as_the_description_says(tmp_path):
    """Each built gear has a tooth along +X; the second, turned 180 + 180 / teeth, meshes."""
    from kiln.parametric import compile_scad_code, render_template_scad

    paths = {}
    for teeth in (20, 15):
        params = {"teeth": teeth, "module_mm": 2, "face_width": 6, "bore_dia": 5, "pressure_angle": 20,
                  "backlash": 0.3, "nozzle_mm": 0.4}
        paths[teeth] = tmp_path / f"t{teeth}.stl"
        compile_scad_code(render_template_scad(_template(), params), output_path=str(paths[teeth]))
    assert "180 + 180 / its teeth" in _template()["description"]
    meshed = _volume(
        tmp_path,
        _BOSL2
        + f'intersection() {{ import("{paths[20]}");\n'
        f'  translate([gear_dist(teeth1=20, teeth2=15, mod=2), 0, 0]) rotate([0, 0, 180 + 180 / 15]) import("{paths[15]}"); }}\n',
    )
    assert meshed < 0.01


@needs_openscad
def test_the_catalog_pairing_recipe_meshes(tmp_path):
    guidance = _catalog()["spur_gear"]["agent_guidance"]
    assert "gear_spin=-90" in guidance and "gear_spin=90+180/teeth2" in guidance
    for teeth1, teeth2 in ((20, 15), (21, 20), (13, 27)):
        meshed = _volume(
            tmp_path,
            _BOSL2
            + f"intersection() {{ spur_gear(mod=2, teeth={teeth1}, thickness=4, gear_spin=-90);\n"
            f"  translate([gear_dist(teeth1={teeth1}, teeth2={teeth2}, mod=2), 0, 0])"
            f" spur_gear(mod=2, teeth={teeth2}, thickness=4, gear_spin=90+180/{teeth2}); }}\n",
        )
        assert meshed < 0.01, (teeth1, teeth2)


@needs_openscad
@pytest.mark.parametrize(
    ("body", "said"),
    [
        ("echo(gear_center_distance(6, 7, 2)); cube(1);", "14 or more teeth between them"),
        ("spur_gear(teeth=12, mod=1, bore=12);", "leaves no ring under the teeth"),
        ("rack_gear(length=60, mod=0.5, height=10, pressure_angle=40);", "pressure_angle must be"),
        ("herringbone_gear(helix_angle=80);", "helix_angle must be"),
    ],
)
def test_the_library_refuses_what_cannot_work(tmp_path, body, said):
    with pytest.raises(ValueError, match=said):
        _volume(tmp_path, _library(body))


@needs_openscad
@pytest.mark.parametrize(
    ("overrides", "said"),
    [
        ({"teeth": 6, "module_mm": 2}, "come to a point: use more teeth"),
        ({"teeth": 8, "module_mm": 2}, "too few for printable tips"),
        ({"backlash": 5}, "Backlash 5 mm is outside"),
    ],
)
def test_the_template_names_the_right_way_out(tmp_path, overrides, said):
    refusal = _build_template(tmp_path, **overrides)
    assert refusal is not None and said in refusal


@needs_openscad
def test_a_refusal_reaches_the_caller_as_a_sentence(monkeypatch):
    from kiln import server

    monkeypatch.setattr("kiln.assumed_nozzle.assumed_nozzle", _fixed_nozzle(0.4))
    out = server.generate_from_template("gear_spur", {"teeth": 17, "module_mm": 0.6})
    assert out["error"]["code"] == "PARAMETERS_REFUSED"
    message = out["error"]["message"]
    assert message.startswith("These teeth are too small for a 0.4 mm nozzle")
    assert "TRACE" not in message and "Assertion" not in message


def _fixed_nozzle(size: float):
    from kiln.assumed_nozzle import AssumedNozzle

    return lambda *a, **k: AssumedNozzle(size, "record", "the_printer")


@needs_openscad
def test_the_nozzle_on_record_sets_the_floor(monkeypatch):
    """No nozzle given: the one on record is used, and the reply says so."""
    from kiln import server

    monkeypatch.setattr("kiln.assumed_nozzle.assumed_nozzle", _fixed_nozzle(0.6))
    out = server.generate_from_template("gear_spur", {"teeth": 17, "module_mm": 1.5})
    assert out["success"] and out["parameters_used"]["nozzle_mm"] == 0.6
    assert "0.6 mm nozzle" in out["nozzle"]["note"]
    assert "Module 2.1 or larger prints them clean" in out["notes"][0]


def test_range_trying_doors_hold_backlash_and_nozzle():
    from kiln.parametric import sweepable_parameters

    sweepable = sweepable_parameters(_template())
    assert "backlash" not in sweepable and "nozzle_mm" not in sweepable
    assert {"teeth", "module_mm", "face_width"} <= set(sweepable)


def test_the_optimizer_never_tries_zero_backlash(monkeypatch, tmp_path):
    """Every variant it builds carries the template's backlash and the nozzle on record."""
    from kiln import design_reasoning

    written: list[str] = []

    class _Failed:
        returncode = 1
        stdout = stderr = ""

    def fake_run(cmd, *a, **k):
        written.extend(Path(arg).read_text(encoding="utf-8") for arg in cmd if str(arg).endswith(".scad"))
        return _Failed()

    monkeypatch.setattr("kiln.openscad_runner.run_openscad", fake_run)
    monkeypatch.setattr("kiln.assumed_nozzle.assumed_nozzle", _fixed_nozzle(0.6))
    monkeypatch.setattr("kiln.generation.openscad._find_openscad", lambda *a, **k: "openscad")
    try:
        design_reasoning.optimize_template_params("gear_spur", output_dir=str(tmp_path), max_variants=9)
    except ValueError:
        pass  # every variant "failed" here on purpose
    assert written, "the optimizer built nothing"
    assert all("bl = 0.3;" in code and "nz = 0.6;" in code for code in written)


def test_a_hyphenated_request_finds_the_worm_and_its_caution():
    from kiln.components import match_components

    for request in ("self-locking gear", "a self-locking gear to hold a lift"):
        ids = [m.component.component_id for m in match_components(request)]
        assert "worm_gear" in ids, request
    spur = _catalog()["spur_gear"]["agent_guidance"].lower()
    assert "never the thing that holds a load up" in spur


@pytest.mark.parametrize(
    ("material", "heat", "grease"),
    [("PLA", True, False), ("Tough PLA", True, False), ("PETG", False, False), ("ABS", False, True), ("PC/ABS", False, True)],
)
def test_the_public_clearance_answer_says_the_gear_cautions(material, heat, grease):
    from kiln.assembly import get_clearance_recommendation
    from kiln.gear_safety import GEAR_LOAD_HOLDING_CAUTION, GREASE_ON_STYRENICS_CAUTION, PLA_GEAR_HEAT_CAUTION

    warnings = get_clearance_recommendation("clearance_fit", material, material, mating="gear_flank")["warnings"]
    assert GEAR_LOAD_HOLDING_CAUTION in warnings
    assert (PLA_GEAR_HEAT_CAUTION in warnings) is heat
    assert (GREASE_ON_STYRENICS_CAUTION in warnings) is grease
    pin = get_clearance_recommendation("clearance_fit", material, material, mating="pin_in_bore")["warnings"]
    assert GEAR_LOAD_HOLDING_CAUTION not in pin
