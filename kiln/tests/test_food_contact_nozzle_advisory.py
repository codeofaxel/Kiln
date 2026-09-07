"""A food-safe filament is not a food-safe part.

Kiln free already reasons about the polymer: it will tell you PETG is a
food-safe family and that PLA is conditional because FDM layer lines
harbour bacteria.  It said nothing about the nozzle the part is extruded
through, and a standard nozzle is usually a leaded brass alloy that wears
trace lead into the print over its life.

The paid tier owns the verdict — whether a given nozzle and material clear
a hard food-contact bar, and how far through its life that nozzle is.
Knowing the nozzle is part of the question at all is not something to
learn after eating off the part, so the advisory is free and rides on
every food-contact recommendation.
"""

from __future__ import annotations

from kiln.material_safety import (
    FOOD_CONTACT_NOZZLE_ADVISORY,
    food_contact_nozzle_advisory,
)


class TestTheAdvisoryItself:
    def test_names_the_hazard_and_the_substitution(self) -> None:
        text = food_contact_nozzle_advisory().lower()
        # The hazard, in words a non-engineer reads.
        assert "lead" in text
        assert "brass" in text
        # And what to do instead — a warning with no action is decoration.
        assert "hardened steel" in text or "stainless" in text

    def test_is_honest_about_what_kiln_does_not_know(self) -> None:
        """Lead-free brass exists and Kiln free cannot see the fitted
        nozzle, so the claim is hedged and hands the check back."""
        text = food_contact_nozzle_advisory().lower()
        assert "usually" in text, "an unhedged claim about every brass nozzle"
        assert "check" in text

    def test_one_source(self) -> None:
        assert food_contact_nozzle_advisory() is FOOD_CONTACT_NOZZLE_ADVISORY


class TestItReachesAFreeUser:
    def _recommend(self, description: str):
        from kiln.design_intelligence import recommend_material_for_design

        return recommend_material_for_design(description)

    def _warnings(self, description: str) -> str:
        """The recommendation's OWN warnings — not anything an alternative
        or a nested profile happens to carry.  An earlier version of this
        test walked the whole result and passed while the recommendation a
        user actually reads was still empty."""
        return " ".join(self._recommend(description).warnings).lower()

    def test_a_food_contact_ask_carries_the_nozzle_advisory(self) -> None:
        """The defect: 'a bowl to eat cereal from' got a food-safe polymer
        and no mention of what it would be extruded through."""
        text = self._warnings("a bowl to eat cereal from")
        assert "nozzle" in text, "food-contact answer never mentions the nozzle"
        assert "lead" in text, "food-contact answer never mentions lead"

    def test_a_non_food_ask_is_not_nagged(self) -> None:
        """It fires on food-contact intent, not on every recommendation —
        a warning shown everywhere is a warning nobody reads."""
        text = self._warnings("a bracket to mount a shelf")
        assert "trace lead" not in text


class TestTheOtherDoor:
    """Naming your own material skips the recommender entirely, and
    "a cereal bowl in PETG" is exactly the ask that most needs telling."""

    def _warnings(self, description: str) -> str:
        from kiln.design_intelligence import get_design_constraints

        brief = get_design_constraints(description)
        return " ".join(brief.recommended_material.warnings).lower()

    def test_a_user_named_material_still_hears_about_the_nozzle(self) -> None:
        text = self._warnings("a cereal bowl printed in PETG")
        assert "nozzle" in text
        assert "lead" in text

    def test_the_recommender_door_carries_it_through_the_brief_too(self) -> None:
        assert "nozzle" in self._warnings("a bowl to eat cereal from")

    def test_and_a_non_food_brief_stays_quiet(self) -> None:
        assert "trace lead" not in self._warnings("a shelf bracket")

    def test_it_is_said_once_not_twice(self) -> None:
        """Both doors guard on the same advisory, so a request that passes
        through both must not stutter."""
        from kiln.design_intelligence import get_design_constraints

        warnings = get_design_constraints("a bowl to eat cereal from").recommended_material.warnings
        nozzle_lines = [w for w in warnings if "nozzle" in w.lower()]
        assert len(nozzle_lines) == 1, nozzle_lines
