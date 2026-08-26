"""Regression pins for the 2026-08-25 template printability sweep.

Eighteen design templates graded D or F at their own defaults because
the model was drawn in use orientation (a closed case, a wall-mounted
hook, a spool with a floating top flange) or was simply broken (a
measuring scoop whose bowl was erased by its own cuts, pegboard pegs
that never touched their bar, a grommet slot that cut the part in
half).  Each was rebuilt in print orientation or split into flat
parts, following the threaded_jar precedent.

These tests pin the load-bearing properties of each rebuild at the
source level, mirroring TestThreadedJarTemplate in
test_printability_micro_features.py.  They are cheap (no OpenSCAD),
so they run everywhere; the geometry itself was verified by
compiling every template at default, min, and max parameters and
grading the meshes (all A at defaults; see the sweep commits).
"""

import json
from pathlib import Path

import pytest

DATA = Path(__file__).parent.parent / "src" / "kiln" / "data"


@pytest.fixture(scope="module")
def templates():
    return json.loads((DATA / "design_templates.json").read_text())


def scad(templates, name):
    return templates[name]["scad_template"]


class TestFlatRebuilds:
    """Templates rebuilt to print flat / in print orientation."""

    def test_bag_clip_prints_flat_with_slider(self, templates):
        """The old clip tilted one jaw 10 degrees through the other and
        had nothing to hold it shut.  Now: coplanar jaws, a living
        hinge, and a slider ring — no rotated jaw at all."""
        s = scad(templates, "bag_clip")
        assert "rotate" not in s
        assert "module slider()" in s
        assert "hinge_t" in s

    def test_pegboard_pegs_root_in_the_bar(self, templates):
        """The old pegs floated 9 mm from the connecting bar (three
        loose pieces) and every rod lay as a round cylinder on the
        bed.  Rods now carry a 45-degree flat and overlap the bar."""
        s = scad(templates, "tool_holder_pegboard")
        assert "flat_bottom_rod" in s
        assert "0.707" in s

    def test_measuring_scoop_has_a_real_bowl(self, templates):
        """The old sphere-minus-boxes construction erased the entire
        bowl, leaving a floating disc and a loose handle.  The cup is
        now a cylinder whose fill volume is volume_ml by construction."""
        s = scad(templates, "measuring_scoop")
        assert "sphere" not in s
        assert "pow(vol * 1000 / 3.14159, 1/3)" in s

    def test_grommet_slot_stops_at_center(self, templates):
        """The old cable slot ran the full diameter and cut the part
        into two half-shells.  It now runs edge-to-center only."""
        s = scad(templates, "cable_grommet")
        assert "hd / 2 + flange + 1" in s

    def test_stands_incline_at_the_requested_angle(self, templates):
        """rotate([90-angle]) gave a 25-degree recline on a '65-degree'
        phone stand — wrong function and a giant overhang.  The slab
        now rises at the named angle and is lifted so its rotated
        thickness cannot dip below the bed."""
        assert "rotate([angle - 90, 0, 0])" in scad(templates, "phone_stand")
        assert "thickness * cos(angle)" in scad(templates, "phone_stand")
        assert "rotate([ang - 90, 0, 0])" in scad(templates, "tablet_stand")
        assert "t * cos(ang)" in scad(templates, "tablet_stand")

    def test_tablet_stand_gussets_are_real(self, templates):
        """The old 'side triangles' were a zero-area polygon (all four
        vertices on one line)."""
        s = scad(templates, "tablet_stand")
        assert "[0, t], [0, bd], [0, lip]" not in s
        assert "polygon([[0, 0], [gusset, 0], [0, gusset]])" in s

    def test_riser_prints_shelf_down(self, templates):
        """The shelf slab used to hover at full height over its legs."""
        s = scad(templates, "monitor_riser_shelf")
        assert "cube([w, d, t]);" in s
        assert "upside down" in s.lower()

    def test_spice_rack_tiers_reach_the_bed(self, templates):
        """Tiers 2 and 3 used to float in mid-air (translate by
        r * step_height with nothing beneath)."""
        s = scad(templates, "spice_jar_rack")
        assert "translate([0, r * row_d, 0])" in s
        assert "r * sh])" not in s

    def test_birdhouse_prints_on_its_back(self, templates):
        """The panel stood upright with a horizontal perch dowel that
        was not even attached (0.15 mm clearance ring).  It now lies
        flat with the perch as a fused vertical post."""
        s = scad(templates, "birdhouse_panel")
        assert "linear_extrude(t)" in s
        assert "pd + 0.3" not in s  # the old floating-dowel clearance

    def test_clamp_pad_prints_face_down(self, templates):
        s = scad(templates, "clamp_pad")
        assert "cube([jw, jh, t]);" in s


class TestProfileExtrusions:
    """Clips and clamps re-expressed as 2D profiles extruded upward,
    so the layer lines wrap the spring features."""

    def test_raceway_clip_is_an_extruded_profile(self, templates):
        s = scad(templates, "cable_raceway_clip")
        assert "linear_extrude(mw)" in s

    def test_rail_clamp_bolt_crosses_the_split(self, templates):
        """The old clamping bolt ran parallel to the split plane at
        the ears' outer face — tightening it could not close the
        ring."""
        s = scad(templates, "rail_clamp")
        assert "linear_extrude(cw)" in s
        assert "rotate([90, 0, 0])" in s  # bolt axis crosses the slit


class TestConeTransitions:
    """Overhanging ledges replaced with 45-degree cones."""

    def test_strainer_cone_and_bounded_slots(self, templates):
        s = scad(templates, "drain_strainer")
        assert "d1 = dd - 1, d2 = od" in s
        # Slots stay inside the disc and cannot merge at the center.
        assert "slot_r0" in s

    def test_pulley_flange_clears_the_barrel(self, templates):
        """At 60 teeth the old 30 mm flange was smaller than the
        38.7 mm tooth barrel, so the barrel overhung it."""
        s = scad(templates, "pulley_gt2")
        assert "fde = max(fd, (or + 1.5) * 2)" in s
        assert "r1 = or, r2 = fde / 2" in s

    def test_bench_dog_prints_head_down(self, templates):
        s = scad(templates, "bench_dog")
        assert "d1 = head_d, d2 = head" in s


class TestSplitParts:
    """Templates split into flat parts, threaded_jar-style."""

    def test_pi_case_lid_prints_open_face_up(self, templates):
        """The old lid printed roof-up over a board-sized unsupported
        ceiling, and the standoffs used a hole span no Pi has."""
        s = scad(templates, "raspberry_pi_case")
        assert "hole_span_y = 58" in s
        assert "hole_inset = 3.5" in s
        # The lid's cavity must open through its top face.
        assert "bd + clear * 2, oh / 2]" in s

    def test_cable_spool_top_flange_is_separate(self, templates):
        """The one-piece spool's top flange hung 20 mm past the hub.
        It is now a second flat part located by a hub boss and clamped
        by the mounting screw."""
        s = scad(templates, "cable_wrap_guide")
        assert "module spool_body()" in s
        assert "module top_flange()" in s
        assert "boss_h" in s


class TestStackableBin:
    def test_label_pocket_replaces_through_window(self, templates):
        """The old 'label slot' was a hole in the wall — it could not
        hold a label, and its top edge was a 56 mm bridge."""
        s = scad(templates, "stackable_bin")
        assert "card_w" in s
        # the old through-cut: a label-sized hole in the front wall
        assert "h * 0.35]" not in s

    def test_stacking_groove_clears_and_keeps_a_floor(self, templates):
        """The old groove left a 0.2 mm floor above it and had zero
        clearance on the lip's inner face, so stacked bins jammed."""
        s = scad(templates, "stackable_bin")
        assert "wall + lip + 0.2])" in s  # cavity floor above the groove
        assert "wall + 0.6" in s          # groove inner clearance
