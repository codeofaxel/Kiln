"""The H2-family Bambu machines are catalogued, on every roster and by
every identifier.

The H2D, H2D Pro, H2C and X2D were missing from every roster file while
kiln-pro's detection intelligence already modelled three of them, so a user
could ask a detection question about an H2D and get an answer for a machine
that had no printer profile at all.

The sharp half of this work is the build volume.  Every one of these
machines publishes SEVERAL envelopes -- a single-nozzle figure, a smaller
dual-nozzle figure, and a "total volume for two nozzles" that is a
tool-travel span rather than a printable box -- and the catalogue figure is
a crash guard: a part that fits the single-nozzle envelope but not the dual
one MUST be refused.  Since 2026-10-12 that figure, the hotend ceilings and
the per-material recipes are served per printer from Kiln's catalogue, so
the conservative-envelope refusal, the X2D's lower ceiling and the recipe
checks are pinned beside the records, in kiln-pro's
``tests/test_printer_catalogue_numbers.py``.  What stays here is what the
public tree still owns: the five rosters, the serial prefixes, the hint
table and the BambuStudio model ids.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiln.cli.printer_model_prompt import suggest_bambu_model
from kiln.printer_profile_ids import map_printer_hint_to_profile_id

DATA = Path(__file__).resolve().parents[1] / "src" / "kiln" / "data"

NEW_MODELS = ("bambu_h2d", "bambu_h2d_pro", "bambu_h2c", "bambu_x2d")

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


# --- BambuStudio model_id detection ---------------------------------------
#
# Per the maker's own machine profiles, the same source the map's own comment
# cites.


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
