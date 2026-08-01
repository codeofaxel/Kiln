"""A material the hotend cannot melt is not a candidate.

The printer's hotend ceiling was applied as a score PENALTY rather than an
exclusion, with the comment "heavy penalty but don't exclude".  A uniform
deduction stops constraining anything the moment it applies to EVERY
candidate: it cancels out, the remaining preferences decide, and the result
gets WORSE as the printer gets weaker.  Measured on the real catalogue, a
heat requirement resolved to:

    hotend <= 240C -> ASA   (needs 235C — fine)
    hotend <= 220C -> PETG  (needs 220C — fine)
    hotend <= 200C -> PLA   (needs 190C — fine)
    hotend <= 180C -> ASA   (needs 235C — cannot be printed at all)

The 180C machine got the hottest answer in the catalogue, because at that
ceiling PLA was penalised too and the heat preference broke the tie.  A user
following it would heat-soak a hotend against a filament that never melts.
"""
from __future__ import annotations

import pytest

from kiln import design_intelligence as di

HOT = "gets hot, above 50C, like a car dashboard or near heat; must not soften or deform"

# Ceilings spanning "everything prints" down to "nothing prints".
CEILINGS = [300, 260, 250, 240, 230, 220, 210, 200, 190, 185, 180, 150]


def _min_temp(material_id: str) -> int:
    """The lowest temperature this filament will extrude at."""
    data = di._get_kb().materials.get(material_id) or {}
    thermal = data.get("thermal") or {}
    return (thermal.get("print_temp_range_c") or [0, 0])[0]


def _unmet(rec) -> bool:
    """Whether the recommender SAID it could not satisfy the request."""
    return any("satisfied your constraints" in w for w in (rec.warnings or []))


@pytest.mark.parametrize("ceiling", CEILINGS)
def test_never_recommends_a_filament_the_hotend_cannot_melt(ceiling):
    """The invariant, across the whole range: whatever comes back is either
    printable on this machine, or an explicit admission that nothing is."""
    rec = di.recommend_material_for_design(
        HOT, printer_has_enclosure=False, max_hotend_temp_c=ceiling
    )
    if _unmet(rec):
        return
    needs = _min_temp(rec.material.material_id)
    assert needs <= ceiling, (
        f"recommended {rec.material.material_id} (needs {needs}C) to a printer "
        f"capped at {ceiling}C"
    )


def test_a_lower_ceiling_never_produces_a_hotter_pick():
    """The bug's signature, stated directly.  A weaker printer must never get a
    more demanding material than a stronger one."""
    picks: list[tuple[int, int]] = []
    for ceiling in sorted(CEILINGS, reverse=True):
        rec = di.recommend_material_for_design(
            HOT, printer_has_enclosure=False, max_hotend_temp_c=ceiling
        )
        if not _unmet(rec):
            picks.append((ceiling, _min_temp(rec.material.material_id)))
    for (hi_ceiling, hi_needs), (lo_ceiling, lo_needs) in zip(picks, picks[1:]):
        assert lo_needs <= hi_needs, (
            f"dropping the ceiling from {hi_ceiling}C to {lo_ceiling}C made the "
            f"recommendation hotter ({hi_needs}C -> {lo_needs}C)"
        )


def test_the_180c_inversion_specifically():
    """The measured case that started this: a 180C hotend was told to print
    ASA, which needs 235C."""
    rec = di.recommend_material_for_design(
        HOT, printer_has_enclosure=False, max_hotend_temp_c=180
    )
    assert _unmet(rec), f"expected an honest refusal, got {rec.material.material_id}"
    assert rec.material.material_id != "asa"


def test_an_impossible_ceiling_is_said_and_quantified():
    """A refusal has to be actionable — name the ceiling and the coolest thing
    that would clear it, rather than shrugging."""
    rec = di.recommend_material_for_design(
        HOT, printer_has_enclosure=False, max_hotend_temp_c=150
    )
    blob = " ".join(rec.warnings or [])
    assert _unmet(rec), blob
    assert "150C" in blob, blob
    assert "hotend" in blob.lower(), blob


def test_a_reachable_ceiling_still_answers():
    """The exclusion must not swallow the ordinary case: a printer that can run
    the right material still gets told to."""
    rec = di.recommend_material_for_design(
        HOT, printer_has_enclosure=False, max_hotend_temp_c=300
    )
    assert not _unmet(rec)
    assert _min_temp(rec.material.material_id) <= 300


def test_a_material_with_no_recorded_temperature_is_not_excluded(monkeypatch):
    """Missing data must not read as "too hot".  An unknown minimum is unknown,
    and excluding on it would quietly shrink the catalogue every time a new
    material landed without a temperature range."""
    kb = di._get_kb()
    catalogue = {
        "mystery": {
            "display_name": "Mystery Filament",
            "category": "thermoplastic",
            "thermal": {},  # no print_temp_range_c at all
            "chemical": {},
        }
    }
    monkeypatch.setattr(
        type(kb), "materials", property(lambda self: catalogue), raising=False
    )
    rec = di.recommend_material_for_design(
        "a decorative desk ornament",
        printer_has_enclosure=False,
        max_hotend_temp_c=180,
    )
    assert rec.material.material_id == "mystery", (
        "a material with no recorded print temperature was excluded by the "
        f"hotend ceiling: {rec.warnings}"
    )


# ---------------------------------------------------------------------------
# The second door — the overlay path, which scores its own candidates
# ---------------------------------------------------------------------------

def test_the_overlay_path_applies_the_same_ceiling(monkeypatch):
    """``recommend_material_for_design`` has two scoring paths: the safety
    floor (what public Kiln runs) and the fuller one taken when a pro overlay
    has merged a ``mechanical`` block.  Both carried the same soft penalty, so
    fixing only the reachable one would leave the bug live for paying users.
    """
    kb = di._get_kb()
    enriched = {
        mid: {**data, "mechanical": {"tensile_strength_mpa": 50}}
        for mid, data in kb.materials.items()
    }
    monkeypatch.setattr(kb, "_materials", enriched, raising=False)
    monkeypatch.setattr(
        type(kb), "materials", property(lambda self: enriched), raising=False
    )

    rec = di.recommend_material_for_design(
        HOT, printer_has_enclosure=False, max_hotend_temp_c=180
    )
    needs = (enriched.get(rec.material.material_id, {}).get("thermal") or {}).get(
        "print_temp_range_c", [0, 0]
    )[0]
    assert _unmet(rec) or needs <= 180, (
        f"overlay path recommended {rec.material.material_id} (needs {needs}C) "
        "to a printer capped at 180C"
    )
