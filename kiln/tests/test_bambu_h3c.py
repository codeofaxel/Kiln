"""The Bambu Lab H3C is catalogued, on every roster and by every identifier.

The H3C is a nine-tool changer.  Kiln reaches it through the existing Bambu
connection and sends and tracks single-material jobs; it does not route work
across the tools, so it must never read as a changer Kiln drives.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiln.cli.printer_model_prompt import suggest_bambu_model
from kiln.motion_facts import motion_facts_for
from kiln.multi_material import KILN_DRIVEN_CHANGERS
from kiln.printer_profile_ids import map_printer_hint_to_profile_id
from kiln.printers.bambu import _BAMBU_MODEL_FAMILIES

DATA = Path(__file__).resolve().parents[1] / "src" / "kiln" / "data"

ROSTER_FILES = {
    "printer_profiles": DATA / "design_knowledge" / "printer_profiles.json",
    "safety_profiles": DATA / "safety_profiles.json",
    "printer_intelligence": DATA / "printer_intelligence.json",
    "slicer_profiles": DATA / "slicer_profiles.json",
}


def _body(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw.get("profiles", raw.get("printers", raw))


@pytest.mark.parametrize("roster", sorted(ROSTER_FILES))
def test_every_roster_file_carries_the_h3c(roster: str):
    assert "bambu_h3c" in _body(ROSTER_FILES[roster])
    assert _body(ROSTER_FILES[roster])["bambu_h3c"]["display_name"] == "Bambu Lab H3C"


@pytest.mark.parametrize(
    "hint",
    ["h3c", "H3C", "bambu h3c", "Bambu Lab H3C", "h3c combo"],
)
def test_the_h3c_names_resolve_to_its_row(hint: str):
    assert map_printer_hint_to_profile_id(hint) == "bambu_h3c"


def test_the_h3c_is_not_mistaken_for_its_siblings():
    assert map_printer_hint_to_profile_id("h2c") == "bambu_h2c"
    assert map_printer_hint_to_profile_id("a2l") == "bambu_a2l"


def test_the_h3c_is_found_by_serial_prefix_model_id_and_name():
    assert suggest_bambu_model("32X00A000000001") == "bambu_h3c"
    assert _BAMBU_MODEL_FAMILIES["32X"] == "h3c"
    assert _BAMBU_MODEL_FAMILIES["N10"] == "h3c"
    assert _BAMBU_MODEL_FAMILIES["Bambu Lab H3C"] == "h3c"


def test_the_h3c_motion_block_leaves_the_unstated_cells_unknown():
    facts = motion_facts_for("bambu_h3c")
    assert facts is not None
    assert facts.z_carrier == "bed" and facts.xy_layout == "corexy"
    assert facts.firmware_family == "bambu"
    assert facts.park_position_mm is None
    assert facts.purge_wipe_station is None
    assert facts.z_travel_limit_mm is None


def test_the_h3c_rated_bed_is_the_slicers_printable_area():
    row = _body(ROSTER_FILES["printer_intelligence"])["bambu_h3c"]
    assert row["rated_build_volume_mm"] == [330, 320, 325]


def test_kiln_does_not_drive_the_tool_changer():
    assert "tool_changer" not in KILN_DRIVEN_CHANGERS


class TestTheH3csOwnSequences:
    """The H3C starts and ends on its own maker's sequences, never a sibling's."""

    def test_it_has_its_own_warm_up_and_end(self):
        from kiln.printers.bambu_3mf import _select_end_gcode, _select_start_gcode

        assert _select_start_gcode("bambu_h3c", 0.4)[1] == "bambu_h3c"
        assert _select_end_gcode("bambu_h3c")[1] == "bambu_h3c"

    @pytest.mark.parametrize(("height", "fixture"), [(20.0, "z20"), (200.0, "z200")])
    def test_its_end_expands_to_the_makers_own_block(self, height, fixture):
        """At 20 mm the lift fits under the top; at 200 mm it does not, and the
        template's second branch holds the bed at the top instead."""
        from pathlib import Path

        from kiln.printers.bambu_3mf import _end_template_variables, _expand_end_template, _select_end_gcode

        template, _ = _select_end_gcode("bambu_h3c")
        body = template[template.index(";===== H3C endgcode"):]
        expanded = _expand_end_template(body, _end_template_variables(height, "bambu_h3c"))
        truth = (Path(__file__).parent / "data" / f"bambu_h3c_end_bambustudio_{fixture}.gcode").read_text()
        assert expanded.rstrip("\n").split("\n") == truth.rstrip("\n").split("\n")

    def test_its_end_values_never_move_the_head(self):
        import re

        from kiln.printers.bambu_3mf import _MODEL_END_VALUES, _select_end_gcode

        template, _ = _select_end_gcode("bambu_h3c")
        motion = [line for line in template.split("\n") if re.match(r"^\s*G[0-3] [XYZ]", line)]
        for name in _MODEL_END_VALUES["bambu_h3c"]:
            assert not any(name in line for line in motion), name

    def test_a_multi_colour_file_is_refused(self):
        """No purge point is on record for the H3C, so Kiln writes it no
        colour change; the first-tool end values hold only while that is so."""
        from kiln.printers.bambu_3mf import _MODEL_END_VALUES, flush_station_for

        assert flush_station_for("bambu_h3c") is None
        assert _MODEL_END_VALUES["bambu_h3c"]["current_extruder"] == 0


class TestAnElsifChain:
    """At most one branch of an if / elsif / else chain is written."""

    @pytest.mark.parametrize(("z", "kept"), [(1, "A"), (5, "B"), (9, "C")])
    def test_one_branch_runs(self, z, kept):
        from kiln.printers.bambu_3mf import _expand_end_template

        template = "{if z < 3}\nA\n{elsif z < 7}\nB\n{else}\nC\n{endif}"
        lines = [line for line in _expand_end_template(template, {"z": z}).split("\n") if line]
        assert lines == [kept]

    def test_an_elsif_with_no_if_is_refused(self):
        from kiln.printers.bambu_3mf import _expand_end_template

        with pytest.raises(ValueError, match="elsif"):
            _expand_end_template("{elsif z < 7}\nB\n{endif}", {"z": 1})
