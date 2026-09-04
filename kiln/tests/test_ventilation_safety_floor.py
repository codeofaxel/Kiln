"""Fume-producing materials carry a ventilation warning, free.

The exotics had one and the mainstream materials did not.  ``abs_esd`` said
"Styrene-family material printed hot in a closed chamber. Ventilate the
working area."  Plain ``abs`` -- same styrene, same closed chamber, far more
users -- said nothing, and neither did ASA, polycarbonate, PC-ABS or the
nylon family.  Ten of thirty-eight materials carried a warning; the ten were
the ESD grades and the 350C+ engineering plastics.

Found while writing PAID chamber guidance for four new enclosed printers,
which is what makes it worth a test rather than a fix.  The 2026-07 incident
was a safety line ending up behind a paywall because it shared a data block
with paid content; the inverse is a hazard nobody wrote down at all because
the people describing the chamber were being paid to describe the chamber.

These fields live in the PUBLIC file on purpose.  ``hosted_intelligence``
gates the materials OVERLAY and records why that is safe: the public file
keeps "thermal ceilings, chemical/food flags and the process limits a free
caller needs in order not to get hurt".  A warning added here is therefore
free by construction -- and this test fails if one is ever moved out.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

MATERIALS = (
    Path(__file__).resolve().parents[1]
    / "src" / "kiln" / "data" / "design_knowledge" / "materials.json"
)

# Materials whose own record says they are printed hot enough, or are chemically
# such, that the working area needs ventilating.  Every one of these emits
# either styrene, a polycarbonate fraction, or caprolactam.
NEEDS_VENTILATION = (
    "abs", "abs_esd", "asa",
    "polycarbonate", "pc_abs", "pc_esd",
    "nylon", "cf_nylon", "pa6_gf", "pa612_esd",
    "peek", "pekk", "pekk_esd", "pei_1010", "pei_9085", "pei_esd", "pps", "ppsu",
)

# Filled materials: the print is one hazard, machining the finished part is
# another, and the second one is not obvious from the first.
RESPIRABLE_DUST = ("cf_nylon", "pa6_gf")


@pytest.fixture(scope="module")
def materials() -> dict:
    return json.loads(MATERIALS.read_text(encoding="utf-8"))


@pytest.mark.parametrize("material", NEEDS_VENTILATION)
def test_fume_producing_materials_warn_about_ventilation(materials, material: str):
    entry = materials[material]["chemical"]
    assert entry.get("ventilation_required") is True, (
        f"{material} produces fume and carries no ventilation flag"
    )
    warning = entry.get("handling_warning") or ""
    assert len(warning) > 40, f"{material} has no usable handling warning"
    assert "ventilat" in warning.lower(), (
        f"{material}'s warning never tells the reader to ventilate: {warning!r}"
    )


@pytest.mark.parametrize("material", RESPIRABLE_DUST)
def test_filled_materials_warn_about_machining_the_finished_part(
    materials, material: str
):
    warning = materials[material]["chemical"]["handling_warning"].lower()
    assert any(w in warning for w in ("sanding", "drilling", "machining")), (
        f"{material} is fibre-filled but its warning covers only the print"
    )
    assert "dust" in warning or "respirator" in warning


def test_the_enclosure_is_not_offered_as_a_substitute_for_ventilating(materials):
    """An enclosure concentrates fume; it must never read as the mitigation.

    A closed chamber plus a carbon filter is exactly the setup that makes a
    user believe the problem is handled, which is why the styrene warnings say
    so explicitly rather than leaving it to be inferred.
    """
    for material in ("abs", "asa"):
        warning = materials[material]["chemical"]["handling_warning"].lower()
        assert "enclosure" in warning or "chamber" in warning, (
            f"{material}'s warning does not address the enclosure at all"
        )


def test_no_fume_warning_hides_in_the_paid_overlay(materials):
    """Every warning asserted above must be in the PUBLIC file, not an overlay."""
    for material in NEEDS_VENTILATION:
        assert "handling_warning" in materials[material]["chemical"], (
            f"{material}'s warning is not in the public file, so a free caller "
            "who most needs it is the one who cannot see it"
        )
