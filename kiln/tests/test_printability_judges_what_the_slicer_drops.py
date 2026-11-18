"""Kiln stops a print only for what its own slice drops.

A thin reading is judged by what a layer holds there.  A wall too narrow
for one line, a round pin and a speck too short to lay a line along stop the
print; a flat sheet thinner than a layer is a warning; anything else prints
as one thin line or layer and is named as fragile.  A crack narrower than the
slicer closes is read through, and named in a note.

Where a wall is too narrow for a line and a pin too thin to print are Kiln's
own figures, asked of Kiln's servers (``kiln.material_limits.sliced_floors``).
The server is stood in here with figures of its own (``WALL_LINE``,
``PIN_LINE``), so these tests pin that the check stops at the figures it is
served, whatever they are.  Without them nothing is a stop: what the same
reading finds against the general minimum wall is a warning that it may be
left out, with one line saying why and how to get Kiln's exact check.
"""
from __future__ import annotations

import errno
import sys

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")  # trimesh's boolean backend builds the parts

from kiln import printability as P  # noqa: E402

NOZZLE = 0.4
#: The stand-in server's narrowest wall the slice lays a line on, and its
#: thinnest round pin, at this nozzle.  Figures of this file's own.
WALL_LINE = 0.17
PIN_LINE = 0.7
LINE = WALL_LINE
#: The general minimum wall at this nozzle (kiln.generic_limits).
GENERAL = 0.8


def _forget() -> None:
    from kiln import _pro_material_limits_bridge as bridge
    from kiln import material_limits as helper

    helper._answers.clear()
    helper._said.clear()
    bridge._service_down_until, bridge._service_down_miss = 0.0, None


def _lines(nozzle_mm: float) -> dict:
    return {
        "success": True,
        "status": "ok",
        "known": False,
        "material_id": None,
        "scenario": {"nozzle_mm": nozzle_mm},
        "limits": {
            "sliced_wall_min_mm": {"value": WALL_LINE, "unit": "mm", "basis": "general"},
            "sliced_pin_min_mm": {"value": PIN_LINE, "unit": "mm", "basis": "general"},
        },
        "note": "",
    }


@pytest.fixture(autouse=True)
def public_floor(monkeypatch):
    """Public Kiln's own floor answers: every ``kiln_pro`` import fails."""
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    _forget()
    yield
    _forget()


@pytest.fixture(autouse=True)
def asked(monkeypatch):
    """What the stand-in server is asked; it answers with this file's lines.
    A test that asks for ``signed_out`` or ``offline`` replaces it."""
    import kiln.server as srv

    calls: list[tuple[str, dict]] = []

    def serve(tool_name, _timeout=None, _asked_by_user=True, _feature=None, **kwargs):
        calls.append((tool_name, {"_asked_by_user": _asked_by_user, **kwargs}))
        return _lines(kwargs.get("nozzle_mm", NOZZLE))

    monkeypatch.setattr(srv, "_pro_api_call", serve)
    return calls


@pytest.fixture
def signed_out(monkeypatch):
    import kiln.server as srv

    def refuse(tool_name, _timeout=None, _asked_by_user=True, **kwargs):
        return {"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "not signed in", "why": "signed_out"}

    monkeypatch.setattr(srv, "_pro_api_call", refuse)


@pytest.fixture
def offline(monkeypatch):
    import kiln.server as srv

    def unreachable(tool_name, _timeout=None, _asked_by_user=True, **kwargs):
        raise OSError(errno.ENETUNREACH, "Network is unreachable")

    monkeypatch.setattr(srv, "_pro_api_call", unreachable)


def _union(*parts):
    mesh = trimesh.boolean.union(list(parts), engine="manifold")
    assert mesh.is_volume, "fixture must be one closed solid"
    return mesh


def _box(lo, hi):
    mesh = trimesh.creation.box(extents=np.subtract(hi, lo))
    mesh.apply_translation(np.add(lo, hi) / 2.0)
    return mesh


def _on_a_plate(*features):
    return _union(_box((0, 0, 0), (12, 14, 1)), *features)


def _wall(thick: float):
    """An upright wall 10 mm long and 6 mm tall."""
    return _box((6 - thick / 2, 2, 0.5), (6 + thick / 2, 12, 7))


def _pin(diameter: float, height: float):
    """A round pin standing on the plate."""
    pin = trimesh.creation.cylinder(radius=diameter / 2, height=height, sections=24)
    pin.apply_translation((6, 7, 0.5 + height / 2))
    return pin


def _between_two_blocks(joint):
    return _union(_box((0, 0, 0), (5, 5, 5)), _box((8, 0, 0), (13, 5, 5)), joint)


def _rod():
    rod = trimesh.creation.cylinder(radius=0.15, height=3.2, sections=24)
    rod.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, (0, 1, 0)))
    rod.apply_translation((6.5, 2.5, 2.5))
    return rod


def _skin_over_a_slot(gap: float):
    """A 10 x 10 x 5 block with a slot *gap* wide cut in from one side,
    0.066 mm under its top: a skin over a crack in one body."""
    block = _box((0, 0, 0), (10, 10, 5))
    slot = _box((-1, 2, 5 - 0.066 - gap), (7, 8, 5 - 0.066))
    mesh = trimesh.boolean.difference([block, slot], engine="manifold")
    assert mesh.is_volume
    return mesh.subdivide().subdivide()


def _walls(mesh, *, floor=None):
    return P._analyze_thin_walls(
        [tuple(map(tuple, t)) for t in np.asarray(mesh.triangles).tolist()],
        [tuple(v) for v in np.asarray(mesh.vertices).tolist()],
        nozzle_diameter=NOZZLE, thin_below_mm=floor,
    )


def _report(tmp_path, name, mesh, **kwargs):
    path = tmp_path / f"{name}.stl"
    mesh.export(path)
    kwargs.setdefault("nozzle_diameter", NOZZLE)
    return P.analyze_printability(str(path), **kwargs)


class TestWhatTheSlicerPrintsIsNotAStop:
    def test_a_wall_one_line_can_hold_is_fragile_not_a_stop(self, tmp_path):
        report = _report(tmp_path, "wall", _on_a_plate(_wall(0.3)))
        walls = report.thin_walls
        assert P.stops(report) == []
        assert P.verdict(report)[0] != "wont_print"
        assert walls.dropped_regions == []
        assert walls.thin_line_count > 0 and walls.thin_line_kind == "wall"
        assert walls.judged_for == "Kiln's slicer settings (Arachne walls)"
        said = " ".join(report.recommendations)
        assert "single thin line" in said and "fragile" in said
        assert "Bambu Studio" in said and "Arachne" in said

    def test_a_wall_just_past_the_line_prints(self):
        walls = _walls(_on_a_plate(_wall(round(LINE + 0.03, 2))))
        assert walls.dropped_regions == []
        assert walls.thin_line_count > 0
        assert walls.slicer_check == "exact" and walls.may_be_left_out_count == 0

    def test_a_thin_bar_joining_two_blocks_prints_as_one_layer(self):
        walls = _walls(_between_two_blocks(_box((4.9, 2.0, 2.35), (8.1, 3.0, 2.65))))
        assert walls.dropped_regions == []
        assert walls.thin_line_count > 0 and walls.thin_line_kind == "sheet"

    def test_a_thin_rod_joining_two_blocks_prints(self):
        walls = _walls(_between_two_blocks(_rod()))
        assert walls.dropped_regions == []
        assert walls.thin_line_count > 0

    def test_a_short_pointed_tip_on_a_block_stays_fine_detail(self, tmp_path):
        stub = trimesh.creation.cylinder(radius=0.3, height=1.2, sections=24)
        stub.apply_translation((5, 5, 5.59))
        cap = trimesh.creation.cone(radius=0.3, height=0.6, sections=24)
        cap.apply_translation((5, 5, 6.19))
        report = _report(tmp_path, "tip", _union(_box((0, 0, 0), (10, 10, 5)), stub, cap))
        assert P.stops(report) == []
        assert report.thin_walls.fine_detail_count > 0, "the tip was not even seen"
        assert any(r.startswith("Surface detail") for r in report.recommendations)

    @pytest.mark.parametrize("name", ["wall", "long pin", "needle"])
    def test_the_answer_is_the_same_on_every_tier(self, name):
        """Kiln Pro measures thin walls against a material's minimum; what the
        slicer drops is the nozzle's and the layer's question on both."""
        mesh = {
            "wall": _on_a_plate(_wall(0.3)),
            "long pin": _on_a_plate(_pin(0.6, 8.0)),
            "needle": _on_a_plate(_pin(0.3, 6.5)),
        }[name]
        free, pro = _walls(mesh), _walls(mesh, floor=1.2)
        assert free.dropped_regions == pro.dropped_regions
        assert (free.thin_line_count, free.fine_detail_count) == (pro.thin_line_count, pro.fine_detail_count)


class TestWhatTheSlicerDropsStillStops:
    def test_a_wall_too_narrow_for_one_line_stops(self, tmp_path):
        report = _report(tmp_path, "narrow", _on_a_plate(_wall(round(LINE - 0.04, 2))))
        found = P.stops(report)
        assert found and "Kiln's slicer drops it" in found[0]
        assert {r["why"] for r in report.thin_walls.dropped_regions} == {"wall"}

    def test_a_long_pin_thinner_than_the_pin_line_stops(self, tmp_path):
        """An antenna, a whisker, a long peg: past the nozzle, and still lost."""
        report = _report(tmp_path, "pin", _on_a_plate(_pin(0.6, 8.0)))
        found = P.stops(report)
        assert found and found[0].startswith("A pin 0.5") and "mm across" in found[0], found
        assert f"Thicken it to at least {GENERAL:g} mm" in found[0] and "smaller nozzle" in found[0]
        assert {r["why"] for r in report.thin_walls.dropped_regions} == {"pin"}

    def test_a_free_standing_needle_stops_as_a_pin(self):
        walls = _walls(_on_a_plate(_pin(0.3, 6.5)))
        assert {r["why"] for r in walls.dropped_regions} == {"pin"}

    def test_a_pin_wider_than_the_pin_line_prints(self):
        walls = _walls(_on_a_plate(_pin(PIN_LINE + 0.15, 8.0)))
        assert walls.dropped_regions == []

    def test_a_speck_too_short_for_a_line_stops(self):
        """Three quarters of a nozzle wide and half a nozzle long: no line fits."""
        speck = _box((6 - 0.15, 7 - 0.1, 0.5), (6 + 0.15, 7 + 0.1, 6.0))
        walls = _walls(_on_a_plate(speck))
        assert {r["why"] for r in walls.dropped_regions} == {"island"}

    def test_a_thin_sheet_sloped_just_off_flat_prints(self):
        """Across its layer the sheet is wider than the probes look; that is
        no reason to stop the print."""
        sheet = _box((0, 0, -0.15), (6, 4, 0.15))
        sheet.apply_transform(trimesh.transformations.rotation_matrix(np.radians(9.5), (0, 1, 0)))
        sheet.apply_translation((7, 3, 4))
        walls = _walls(_union(_box((0, 0, 0), (5, 10, 8)), _box((9, 0, 0), (14, 10, 8)), sheet))
        assert walls.dropped_regions == []
        assert walls.thin_line_count > 0

    def test_a_reading_the_probe_could_not_afford_stays_a_stop(self):
        def refused(o, d, c):
            return np.full(len(o), np.inf), np.full(len(o), np.inf)

        at, along, comps = np.zeros((1, 3)), np.array([[1.0, 0.0, 0.0]]), np.zeros(1, dtype=int)
        lines = {"wall_line": LINE, "pin_line": PIN_LINE}
        thin = P._slice_fate(at, along, np.array([0.3]), comps, refused, nozzle=NOZZLE, layer_height=0.2, **lines)
        assert thin.tolist() == [P._FATE_UNREAD] and P._FATE_UNREAD in P._FATE_DROPS
        # Past the nozzle nothing stopped before, and a read never taken stops nothing now.
        past = P._slice_fate(
            at, along, np.array([0.6]), comps, refused, nozzle=NOZZLE, layer_height=0.2, pins_only=True, **lines,
        )
        assert past.tolist() == [P._FATE_PRINTS]


class TestAFlatSheetIsAWarning:
    def test_a_sheet_thinner_than_a_layer_may_be_left_out(self, tmp_path):
        flange = _box((9.9, 2, 2.5), (15, 8, 2.58))
        report = _report(tmp_path, "flange", _union(_box((0, 0, 0), (10, 10, 5)), flange).subdivide())
        walls = report.thin_walls
        assert P.stops(report) == []
        assert walls.thin_sheet_count > 0 and walls.thin_sheet_min_mm == pytest.approx(0.08, abs=0.005)
        said = [r for r in report.recommendations if r.startswith("A flat piece")]
        assert said and "thinner than one 0.2 mm layer" in said[0] and "at least 0.2 mm" in said[0]

    def test_the_layer_is_kilns_own_for_the_nozzle_unless_named(self, tmp_path):
        assert [P._fitted_layer_height(n) for n in (0.2, 0.4, 0.6)] == [0.15, 0.2, 0.2]
        flange = _union(_box((0, 0, 0), (10, 10, 5)), _box((9.9, 2, 2.5), (15, 8, 2.58)))
        fine = _report(tmp_path, "fine", flange, nozzle_diameter=0.2)
        named = _report(tmp_path, "named", flange, nozzle_diameter=0.2, layer_height=0.12)
        assert (fine.thin_walls.layer_height_mm, named.thin_walls.layer_height_mm) == (0.15, 0.12)


class TestHairlineCracks:
    def test_a_crack_the_slicer_closes_is_read_through(self, tmp_path):
        report = _report(tmp_path, "crack", _skin_over_a_slot(0.06))
        walls = report.thin_walls
        assert P.stops(report) == []
        assert (walls.thin_line_count, walls.thin_sheet_count) == (0, 0)
        assert walls.sealed_crack_count >= 1
        assert walls.sealed_crack_widest_mm == pytest.approx(0.06, abs=0.005)
        note = [r for r in report.recommendations if "hairline crack" in r]
        assert note and "widen it past 0.10 mm" in note[0]

    def test_a_reading_that_starts_inside_the_crack_is_read_through_too(self):
        """The skin's underside faces into the crack; read from there, the
        skin carries on behind it through the closed crack."""
        walls = _walls(_skin_over_a_slot(0.06))
        assert walls.min_wall_thickness_mm >= 1.0, walls.min_wall_thickness_mm

    def test_a_wider_gap_stays_open(self):
        walls = _walls(_skin_over_a_slot(0.2))
        assert walls.sealed_crack_count == 0
        assert walls.thin_sheet_min_mm == pytest.approx(0.066, abs=0.002)

    def test_another_body_is_never_reached(self):
        """Two plates 0.06 mm apart are two bodies: fusion_check owns that gap."""
        plates = trimesh.util.concatenate([_box((0, 0, 0), (10, 10, 0.3)), _box((0, 0, 0.36), (10, 10, 0.66))])
        walls = _walls(plates.subdivide())
        assert walls.sealed_crack_count == 0
        assert walls.min_wall_thickness_mm == pytest.approx(0.3, abs=0.01)


def _void_beside_a_skin(width: float):
    """A 12 x 12 x 8 block with a void *width* wide sealed inside it, 0.1 mm
    under one face: the void is a piece of mesh of its own."""
    mesh = trimesh.boolean.difference(
        [_box((0, 0, 0), (12, 12, 8)), _box((0.1, 3, 1.5), (0.1 + width, 9, 6.5))], engine="manifold",
    )
    assert mesh.is_volume and len(mesh.split(only_watertight=False)) == 2
    return mesh


class TestAVoidSealedInsideThePart:
    def test_the_skin_between_a_void_and_the_outside_is_measured(self, tmp_path):
        report = _report(tmp_path, "void", _void_beside_a_skin(0.2))
        found = P.stops(report)
        assert found and found[0].startswith("A wall measures 0.10 mm"), found

    def test_a_void_no_wider_than_a_crack_is_read_through(self):
        walls = _walls(_void_beside_a_skin(0.06))
        assert walls.dropped_regions == []
        assert walls.sealed_crack_count >= 1



class TestAWallThinnerThanTheOldFloor:
    """A chord under 50 µm left uncounted lets a real wall that thin read as
    grade A while every slicer drops it.  The floor is rounding (5 µm); a
    flake standing on solid still reads as detail."""

    def test_a_005mm_wall_every_slicer_drops_stops(self, tmp_path):
        report = _report(tmp_path, "wall_005", _on_a_plate(_wall(0.05)))
        found = P.stops(report)
        assert found and found[0].startswith("A wall measures 0.05 mm"), found

    def test_a_microscopic_flake_on_solid_is_fine_detail_not_a_stop(self, tmp_path):
        # 15 µm thin, 0.3 mm proud of a block, 3 mm long: the kind of flake
        # a thread's start leaves.  The print loses it, not the part.
        flake = _box((3.5, 5 - 0.0075, 4.5), (6.5, 5 + 0.0075, 5.3))
        report = _report(tmp_path, "flake", _union(_box((0, 0, 0), (10, 10, 5)), flake))
        assert P.stops(report) == [], P.stops(report)
        assert report.thin_walls.fine_detail_count > 0, "the flake was not even seen"


# --- every door judges the same layer ---


class TestEveryDoorJudgesTheSameLayer:
    def test_the_prepare_step_judges_at_kilns_own_layer_for_the_nozzle(self, tmp_path):
        """The analysis tool judges a flat sheet against the layer Kiln slices
        the nozzle at (0.15 mm on a 0.2 mm nozzle); if the prepare step,
        the design audit and the generation doors defaulted to 0.2 mm, one
        part could warn through one door and not another.  At the 0.4 mm
        nozzle both answers are 0.2, so this runs at 0.2 mm."""
        from kiln.mesh_validation_pipeline import run_validation_pipeline

        path = tmp_path / "plate.stl"
        _on_a_plate(_wall(0.5)).export(path)
        direct = P.analyze_printability(str(path), nozzle_diameter=0.2)
        prepared = run_validation_pipeline(str(path), nozzle_diameter=0.2)
        assert direct.thin_walls.layer_height_mm == 0.15
        assert prepared.printability_details["thin_walls"]["layer_height_mm"] == 0.15


class TestAShortPieceSaysHowToFixIt:
    def test_the_short_piece_stop_names_length_as_the_fix(self):
        """The stop for a piece too short to print said 'Thicken it', the
        wrong fix for a piece whose problem is its length, and its first
        clause read '0.30 mm thin'.  No test read the sentence, only the
        stop's presence (fails at 8f220df6)."""
        said = P._dropped_sentence({"thickness_mm": 0.3, "why": "island"}, NOZZLE)
        assert "Make it longer" in said and "0.30 mm thick" in said, said

    @pytest.mark.parametrize("why", ["wall", "pin", "island"])
    def test_every_stop_names_the_general_minimum_wall_as_enough(self, why):
        """The fix a stop names is a figure every install already has, the
        general minimum wall, never the line the slice drops at."""
        for nozzle, enough in ((0.2, 0.8), (0.4, 0.8), (0.6, 1.2)):
            said = P._dropped_sentence({"thickness_mm": 0.05, "why": why}, nozzle)
            assert f"at least {enough:g} mm" in said, said


# --- Kiln's own figures: asked of Kiln's servers ---


class TestKilnsOwnFiguresAreAsked:
    def test_the_ask_names_the_nozzle_and_no_plastic(self, asked, tmp_path):
        _report(tmp_path, "wall", _on_a_plate(_wall(0.3)))
        sliced = [sent for tool, sent in asked if tool == "material_limits" and "quantities" in sent]
        assert sliced == [{
            "_asked_by_user": False,
            "nozzle_mm": NOZZLE,
            "quantities": ["sliced_wall_min_mm", "sliced_pin_min_mm"],
        }]

    def test_a_session_asks_once_per_nozzle(self, asked):
        for _ in range(3):
            _walls(_on_a_plate(_wall(0.3)))
        assert len([sent for _tool, sent in asked if "quantities" in sent]) == 1

    def test_an_answer_without_both_figures_is_no_answer(self, monkeypatch):
        import kiln.server as srv
        from kiln.material_limits import sliced_floors

        half = _lines(NOZZLE)
        del half["limits"]["sliced_pin_min_mm"]
        monkeypatch.setattr(srv, "_pro_api_call", lambda *_a, **_k: half)
        floors = sliced_floors(nozzle_mm=NOZZLE)
        assert floors.exact is False and floors.miss is not None
        assert (floors.wall_mm, floors.pin_mm) == (GENERAL, GENERAL)


# --- without Kiln's own figures: a warning, never a stop ---


def _why(report) -> list[str]:
    return [r for r in report.recommendations if "Kiln's exact check" in r]


class TestWithoutKilnsOwnFigures:
    """Signed out or offline, Kiln's own figures are not read: nothing the
    slice may leave out stops the print.  What the same reading finds against
    the general minimum wall is one warning, and one line says why and how
    to get Kiln's exact check."""

    def test_a_wall_too_narrow_for_a_line_is_a_warning_when_signed_out(self, signed_out, tmp_path):
        report = _report(tmp_path, "narrow", _on_a_plate(_wall(0.13)))
        walls = report.thin_walls
        assert P.stops(report) == [] and walls.dropped_regions == []
        assert walls.slicer_check == "general" and walls.general_wall_mm == GENERAL
        assert walls.may_be_left_out_count > 0 and {r["why"] for r in walls.may_be_left_out_regions} == {"wall"}
        warned = [r for r in report.recommendations if r.startswith("A wall or pin as thin as")]
        assert len(warned) == 1 and "may be left out when sliced" in warned[0]
        assert f"at least {GENERAL:g} mm" in warned[0]
        why = _why(report)
        assert len(why) == 1 and "Kiln is signed out" in why[0] and "Sign in" in why[0]

    def test_a_long_pin_is_a_warning_when_offline(self, offline, tmp_path):
        report = _report(tmp_path, "pin", _on_a_plate(_pin(0.6, 8.0)))
        walls = report.thin_walls
        assert P.stops(report) == [] and walls.dropped_regions == []
        assert {r["why"] for r in walls.may_be_left_out_regions} == {"pin"}
        why = _why(report)
        assert len(why) == 1 and "offline" in why[0]

    def test_the_general_figure_warns_about_a_pin_kilns_own_would_print(self, signed_out):
        """Between Kiln's own pin line and the general minimum wall, the
        general check warns where Kiln's own would let it through."""
        walls = _walls(_on_a_plate(_pin(0.75, 8.0)))
        assert walls.dropped_regions == [] and walls.may_be_left_out_count > 0

    def test_a_wall_at_least_a_nozzle_wide_is_not_said_to_be_left_out(self, signed_out):
        """A wall at least a nozzle wide always gets a line; the general check
        says nothing new about it."""
        walls = _walls(_on_a_plate(_wall(0.6)))
        assert walls.may_be_left_out_count == 0 and walls.dropped_regions == []

    def test_a_part_with_nothing_thin_says_nothing(self, signed_out, tmp_path):
        report = _report(tmp_path, "block", _box((0, 0, 0), (10, 10, 5)))
        assert report.thin_walls.may_be_left_out_count == 0
        assert _why(report) == []

    def test_the_verdict_is_not_wont_print(self, signed_out, tmp_path):
        report = _report(tmp_path, "narrow", _on_a_plate(_wall(0.13)))
        assert P.verdict(report)[0] != "wont_print"
