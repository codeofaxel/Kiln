"""Requirement triggers must match whole words, not fragments of other words.

``match_requirements`` and ``use_case_implies_skin_contact`` read the user's
own sentence against the trigger vocabularies in
``data/design_knowledge/functional_requirements.json``.  Matched as bare
substrings those triggers fired from inside ordinary words, and several of the
collisions were with this project's most common vocabulary:

    "art"    inside *part*          -> a part for my printer was decorative
    "mate"   inside *material*      -> the material list was a precision fit
    "eat"    inside *heat*          -> a heat-resistant bracket was food contact
    "soft"   inside *software*      -> a software enclosure needed TPU
    "hang"   inside *change*        -> change the color was load bearing
    "hot"    inside *photo*         -> a photo frame was a hot environment
    "oven"   inside *proven*        -> a proven recipe was an oven
    "engine" inside *engineering*   -> an engineering bracket was an engine bay
    "ring"   inside *engineering*   -> and it was worn against skin

These reach every install: the constraint sets steer material choice, and the
skin caution is prepended to ``recommend_material``'s reasoning.
"""
from __future__ import annotations

import pytest

from kiln import design_intelligence as di


def _ids(text: str) -> set[str]:
    return {s.requirement_id for s in di.match_requirements(text)}


# ---------------------------------------------------------------------------
# The matcher itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,token,expected", [
    ("a part for my printer", "art", False),
    ("the material list", "mate", False),
    ("a heat resistant bracket", "eat", False),
    ("a software enclosure", "soft", False),
    ("change the color", "hang", False),
    ("a photo frame", "hot", False),
    ("a proven recipe", "oven", False),
    ("an engineering bracket", "engine", False),
    ("a wedding ring", "ring", True),
    ("two wedding rings", "ring", True),        # plural
    ("three watches", "watch", True),           # -es plural
    ("a cookie cutter shape", "cookie cutter", True),
    ("a cookie  cutter shape", "cookie cutter", True),   # whitespace run
    ("a cookie\ncutter shape", "cookie cutter", True),   # wrapped line
    ("a cookie shaped cutter", "cookie cutter", False),  # not the phrase
])
def test_matches_trigger(text, token, expected):
    assert di.matches_trigger(text, token) is expected


@pytest.mark.parametrize("text,token", [("", "ring"), ("a ring", "")])
def test_matches_trigger_handles_empty_input(text, token):
    assert di.matches_trigger(text, token) is False


# ---------------------------------------------------------------------------
# match_requirements — no profile may fire from inside another word
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,absent", [
    ("a part for my printer", "aesthetic_decorative"),   # art
    ("a smart enclosure", "aesthetic_decorative"),       # art
    ("a partial rebuild", "aesthetic_decorative"),       # art
    ("the material list", "precision_fit"),              # mate
    ("estimate the cost", "precision_fit"),              # mate
    ("a heat resistant bracket", "food_contact"),        # eat
    ("create a feature", "food_contact"),                # eat
    ("a great big sign", "food_contact"),                # eat
    ("a software enclosure", "flexibility_required"),    # soft
    ("change the color", "load_bearing"),                # hang
    ("a photo frame", "heat_exposure"),                  # hot
    ("a proven recipe card", "heat_exposure"),           # oven
    ("an engineering bracket", "heat_exposure"),         # engine
    ("take a snapshot", "heat_exposure"),                # hot
    ("a lanyard clip", "outdoor_use"),                   # yard
    ("a constraint diagram stand", "outdoor_use"),       # rain
    ("a cupboard handle", "food_contact"),               # cup
    ("a bowling trophy", "food_contact"),                # bowl
])
def test_no_profile_matches_inside_another_word(text, absent):
    assert absent not in _ids(text)


@pytest.mark.parametrize("text,expected", [
    # Inflections and compounds a word boundary can't reach, restored to the
    # profiles so tightening didn't trade false positives for false negatives.
    ("a mating surface", "precision_fit"),
    ("an alignment jig", "precision_fit"),
    ("a hanging planter", "load_bearing"),
    ("a mounting plate", "load_bearing"),
    ("a leaking pipe collar", "watertight"),
    ("a leakproof lid", "watertight"),
    ("a heated bed spacer", "heat_exposure"),
    ("a hotend fan duct", "heat_exposure"),
    ("a heatbed cable clip", "heat_exposure"),
    ("a sunlight sensor bracket", "outdoor_use"),
    ("a sunny windowsill tray", "outdoor_use"),
    ("something for eating outdoors", "food_contact"),
    ("a case that survives being dropped", "impact_resistant"),
    ("a bending flexure hinge", "flexibility_required"),
])
def test_real_requirements_are_still_matched(text, expected):
    assert expected in _ids(text)


def test_every_trigger_in_every_profile_still_matches_itself():
    """Whole-word matching must not silently retire a trigger — each one still
    fires on a sentence that contains it."""
    for req_id, data in di._get_kb().requirements.items():
        for trigger in data.get("triggers") or []:
            assert di.matches_trigger(f"a {trigger.lower()} thing", trigger), (
                f"{req_id}: trigger {trigger!r} no longer matches itself"
            )


# ---------------------------------------------------------------------------
# The skin-contact caution — same fix, and the compound it cost
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "an engineering bracket",   # ring
    "a spring clip",            # ring
    "a measuring jig",          # ring
    "a bootstrap loader",       # strap
    "a bandwidth monitor",      # band
    "a desk tidy for my husband",  # band
    "a watchdog timer case",    # watch
    "a scuffed panel",          # cuff
    "a bitmask decoder",        # mask
])
def test_skin_caution_does_not_fire_from_inside_another_word(text):
    assert di.use_case_implies_skin_contact(text) is False


@pytest.mark.parametrize("text", [
    "sunglasses frame",     # "glasses" no longer reaches inside this compound
    "a headband",
    "an armband for running",
    "a sweatband",
    "a pair of cufflinks",  # "cuff" no longer reaches inside this one either
])
def test_worn_compounds_are_their_own_triggers(text):
    assert di.use_case_implies_skin_contact(text) is True


@pytest.mark.parametrize("text", [
    "a sunglasses case",
    "a sunglasses holder",
    "a cufflink box",
])
def test_the_new_compounds_carry_their_own_homographs(text):
    """A trigger added for a compound needs its exclusions added too — a word
    boundary stops "glasses case" from covering "sunglasses case"."""
    assert di.use_case_implies_skin_contact(text) is False
