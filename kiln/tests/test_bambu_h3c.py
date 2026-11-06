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
