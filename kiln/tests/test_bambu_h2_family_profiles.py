"""The H2-family Bambu machines are catalogued, and catalogued CONSERVATIVELY.

The H2D, H2D Pro, H2C and X2D were missing from every roster file while
kiln-pro's detection intelligence already modelled three of them, so a user
could ask a detection question about an H2D and get an answer for a machine
that had no printer profile at all.

The sharp half of this file is the build volume.  Every one of these machines
publishes SEVERAL envelopes -- a single-nozzle figure, a smaller dual-nozzle
figure, and a "total volume for two nozzles" that is a tool-travel span rather
than a printable box.  ``bed_fit`` calls itself the last line of defence
before slicing and before send, so the catalogue figure is a crash guard: a
part that fits the single-nozzle envelope but not the dual one MUST be
refused.  These tests assert the refusal, not the presence of a number.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiln.cli.printer_model_prompt import suggest_bambu_model
from kiln.printer_profile_ids import map_printer_hint_to_profile_id
from kiln.printers.bed_fit import check_bed_fit, resolve_build_volume

DATA = Path(__file__).resolve().parents[1] / "src" / "kiln" / "data"

NEW_MODELS = ("bambu_h2d", "bambu_h2d_pro", "bambu_h2c", "bambu_x2d")

# The envelope each machine must be catalogued at: the smallest SUPPORTED
# printable mode, never the marketing figure.
CONSERVATIVE_VOLUME = {
    "bambu_h2d": (300.0, 320.0, 320.0),
    "bambu_h2d_pro": (300.0, 320.0, 320.0),
    "bambu_h2c": (300.0, 320.0, 320.0),
    "bambu_x2d": (235.0, 256.0, 256.0),
}

ROSTER_FILES = {
    "printer_profiles": DATA / "design_knowledge" / "printer_profiles.json",
    "printer_material_compatibility": (
        DATA / "design_knowledge" / "printer_material_compatibility.json"
    ),
    "safety_profiles": DATA / "safety_profiles.json",
    "printer_intelligence": DATA / "printer_intelligence.json",
    "slicer_profiles": DATA / "slicer_profiles.json",
}


def _ids(path: Path) -> set[str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    body = raw.get("profiles", raw.get("printers", raw))
    return {k for k in body if not k.startswith("_")}


@pytest.mark.parametrize("model", NEW_MODELS)
@pytest.mark.parametrize("roster", sorted(ROSTER_FILES))
def test_every_roster_file_carries_the_new_machines(roster: str, model: str):
    """A machine missing from one of the five files is a half-added machine."""
    assert model in _ids(ROSTER_FILES[roster]), (
        f"{model} missing from {roster}; the five roster files are pinned to "
        "an identical key set"
    )


@pytest.mark.parametrize("model", NEW_MODELS)
def test_catalogued_at_the_conservative_envelope(model: str):
    resolved = resolve_build_volume(model)
    assert resolved is not None, f"{model} has no build volume for the bed-fit guard"
    assert resolved[1] == CONSERVATIVE_VOLUME[model]


@pytest.mark.parametrize(
    "model,over_x,fits_single_nozzle_envelope",
    [
        # 310 clears the H2D/H2C single-nozzle X (325) but not the dual X (300).
        ("bambu_h2d", 310.0, 325.0),
        ("bambu_h2d_pro", 310.0, 325.0),
        ("bambu_h2c", 310.0, 325.0),
        # 250 clears the X2D main-nozzle X (256) but not the auxiliary X (235.5).
        ("bambu_x2d", 250.0, 256.0),
    ],
)
def test_a_part_only_the_wider_mode_could_print_is_refused(
    model: str, over_x: float, fits_single_nozzle_envelope: float
):
    """The crash guard refuses what only the WIDER published mode could print.

    This is the assertion that would have caught taking the marketing figure:
    each ``over_x`` genuinely fits that machine's single-nozzle envelope, so a
    catalogue built on that number would wave the part through and drive the
    toolhead into the parked second nozzle.
    """
    assert over_x < fits_single_nozzle_envelope, "fixture must fit the wider mode"

    volume = resolve_build_volume(model)[1]
    bbox = {
        "x_min": 0.0, "x_max": over_x,
        "y_min": 0.0, "y_max": 100.0,
        "z_min": 0.0, "z_max": 100.0,
    }
    verdict = check_bed_fit(bbox, volume)
    assert verdict["ok"] is False, (
        f"{model} accepted a {over_x} mm part; the catalogue is carrying a "
        "wider envelope than the machine can print in every supported mode"
    )
    assert verdict["error_code"] == "EXCEEDS_BED"


@pytest.mark.parametrize(
    "serial_prefix,expected",
    [
        ("094", "bambu_h2d"),
        ("239", "bambu_h2d_pro"),
        ("31B", "bambu_h2c"),
        ("20P", "bambu_x2d"),
    ],
)
def test_serial_prefix_suggests_the_new_models(serial_prefix: str, expected: str):
    """These four prefixes were documented in the table's own comment but unmapped."""
    assert suggest_bambu_model(f"{serial_prefix}00A000000000") == expected


@pytest.mark.parametrize(
    "hint,expected",
    [
        ("h2d", "bambu_h2d"),
        ("H2D Pro", "bambu_h2d_pro"),
        ("h2d-pro", "bambu_h2d_pro"),
        ("Bambu Lab H2D Pro", "bambu_h2d_pro"),
        ("h2c", "bambu_h2c"),
        ("x2d", "bambu_x2d"),
    ],
)
def test_hint_resolution_tests_the_pro_before_the_base_model(hint: str, expected: str):
    """"h2d_pro" contains "h2d", so order decides whether the Pro exists at all."""
    assert map_printer_hint_to_profile_id(hint) == expected


def test_x2d_keeps_the_lower_hotend_ceiling_of_its_own_class():
    """The X2D is 300C, not the 350C the rest of the H2 family reaches.

    Its material verdicts are therefore derived from the 300C X1C, and the
    difference has to survive into the shipped data: PPS needs a hotend the
    X2D does not have.
    """
    intel = json.loads(ROSTER_FILES["printer_intelligence"].read_text(encoding="utf-8"))
    assert intel["bambu_x2d"]["max_hotend_temp"] == 300
    for hot_family in ("bambu_h2d", "bambu_h2d_pro", "bambu_h2c"):
        assert intel[hot_family]["max_hotend_temp"] == 350

    compat = json.loads(
        ROSTER_FILES["printer_material_compatibility"].read_text(encoding="utf-8")
    )
    compat = compat.get("printers", compat)
    assert compat["bambu_x2d"]["pps"]["status"] == "not_compatible"
    assert "high_temp_hotend" in compat["bambu_x2d"]["pps"]["upgrades_needed"]
    assert compat["bambu_h2d"]["pps"]["status"] == "needs_upgrade"


# --- BambuStudio model_id detection ---------------------------------------
#
# Read from the installed BambuStudio's own machine profiles
# (Contents/Resources/profiles/BBL/machine/*.json, field "model_id"), which is
# the same source the map's own comment cites.


@pytest.mark.parametrize(
    "model_id,family",
    [
        ("O1D", "h2d"),
        ("O1E", "h2d_pro"),
        # The H2C is O1C2.  An O1C also exists in that directory and is a
        # different machine, so this row is the reason to read the profile
        # rather than infer the code from the model name.
        ("O1C2", "h2c"),
        ("N6", "x2d"),
    ],
)
def test_new_machines_are_detected_by_their_bambustudio_model_id(
    model_id: str, family: str
):
    from kiln.printers.bambu import _BAMBU_MODEL_FAMILIES

    assert _BAMBU_MODEL_FAMILIES.get(model_id) == family


def test_bl_p001_is_the_x1_carbon_not_the_p1s():
    """A 3MF sliced for an X1 Carbon must not announce itself as a P1S.

    BambuStudio's own profile gives the X1 Carbon ``model_id`` BL-P001 and the
    P1S C12.  The map had BL-P001 pointing at the P1S, so the mismatch check
    compared a real X1C against the wrong family -- the same class of error
    the "01S" serial row above it was already fixed for.
    """
    from kiln.printers.bambu import _BAMBU_MODEL_FAMILIES

    assert _BAMBU_MODEL_FAMILIES["BL-P001"] == "x1c"
    assert _BAMBU_MODEL_FAMILIES["C12"] == "p1s"
    assert _BAMBU_MODEL_FAMILIES["C11"] == "p1p"


@pytest.mark.parametrize("model", NEW_MODELS)
def test_every_new_machine_is_reachable_by_all_three_identifier_kinds(model: str):
    """product_name, serial prefix and model_id must all land on the machine.

    Detection self-corrects on connect via MQTT, but a 3MF carries only the
    model_id, so a machine known by two of the three still answers wrongly for
    the file the user is about to print.
    """
    from kiln.printers.bambu import _BAMBU_MODEL_FAMILIES

    family = model.removeprefix("bambu_")
    resolved = {v for v in _BAMBU_MODEL_FAMILIES.values()}
    assert family in resolved, f"{model} unreachable by any identifier"

    kinds = {
        "product_name": any(
            k.startswith("Bambu Lab") and v == family
            for k, v in _BAMBU_MODEL_FAMILIES.items()
        ),
        "serial_prefix": any(
            len(k) == 3 and k.isalnum() and not k.startswith("Bambu") and v == family
            for k, v in _BAMBU_MODEL_FAMILIES.items()
        ),
        "model_id": any(
            k in {"O1D", "O1E", "O1C2", "N6"} and v == family
            for k, v in _BAMBU_MODEL_FAMILIES.items()
        ),
    }
    assert all(kinds.values()), f"{model} missing identifier kinds: {kinds}"
