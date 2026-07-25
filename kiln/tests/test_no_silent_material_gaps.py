"""No catalogue material may be met with silence, on any intelligence surface.

This is the universal version of a bug this repo has now shipped three times,
in three different tables, each time the same shape:

  * skin contact went quiet for 13 of the 25 materials in v1.2.0 — a worn part
    in TPU 95A, wood-fill or matte PLA got no warning at all;
  * the load lookup answered for five materials and returned ``None`` for the
    other thirty, while telling the user only five existed;
  * co-print answered ``abs`` about ``tpu`` and stayed silent when asked the
    same question from the other side.

In every case nothing failed, no test went red, and no error was raised. The
lookup simply returned nothing, and an assistant reading nothing reports "no
data available" — which a user hears as "no concern here". For a safety
surface that is the worst possible failure: silence is indistinguishable from
an all-clear, and it is reached by doing nothing at all.

So this file asserts the one property that makes the whole class impossible:
EVERY material in the catalogue gets a real answer from EVERY per-material
intelligence surface. An answer may be a value, or it may be an explicit,
reasoned refusal ("this model does not describe an elastomer", "nobody has
characterised this material") — both are answers. Returning ``None`` or ``{}``
is not.

Coverage of the DATA is tracked separately, per table, in
test_intelligence_parity.py. This file is about the ANSWER: a material may
legitimately be absent from a table, but the surface built on that table must
still say something true when asked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiln import design_intelligence as di

DATA = Path(__file__).parent.parent / "src" / "kiln" / "data" / "design_knowledge"

# A representative geometry — small enough to be realistic, large enough that
# no surface can dodge the question by treating it as degenerate.
_AREA_MM2 = 24.0
_LENGTH_MM = 50.0


def _materials() -> list[str]:
    raw = json.loads((DATA / "materials.json").read_text(encoding="utf-8"))
    return sorted(k for k in raw if not k.startswith("_"))


MATERIALS = _materials()


def test_the_catalogue_is_not_empty():
    """Guard the guard: an empty roster would make every test below vacuous."""
    assert len(MATERIALS) >= 38, (
        f"only {len(MATERIALS)} materials found — if the catalogue really "
        "shrank, update this floor deliberately; otherwise the loader is broken "
        "and every per-material test in this file is silently passing on nothing"
    )


@pytest.mark.parametrize("material", MATERIALS)
def test_skin_contact_always_answers(material: str):
    """A part worn against skin never gets silence — see the three-tier floor."""
    floor = di.get_skin_contact_floor(material)
    assert floor is not None, (
        f"{material}: no skin-contact answer at all. Silence reads as 'no "
        "concern' on a safety question; the floor must fall back to a related "
        "family or to the generic uncharacterised record."
    )
    assert floor.honesty_note, f"{material}: skin floor returned but says nothing"
    assert floor.refer_to_medical, f"{material}: skin floor lost its medical boundary"


@pytest.mark.parametrize("material", MATERIALS)
def test_load_question_always_answers(material: str):
    """Strength OR deflection — every material gets one of the two.

    An elastomer is not 'unknown': it is governed by deflection rather than
    rupture. Returning nothing for it told users the material was unsupported
    when the honest answer was that they were asking the wrong question.
    """
    strength = di.estimate_load_capacity(material, _AREA_MM2, _LENGTH_MM)
    if strength is not None:
        assert strength.max_load_n >= 0.0
        return
    flex = di.estimate_deflection_limited_load(material, _AREA_MM2, _LENGTH_MM)
    assert flex is not None, (
        f"{material}: neither a strength nor a deflection answer. Every "
        "material must be answerable by one model or the other, and the "
        "answer must name which."
    )
    assert flex["limit_mode"] == "deflection"
    assert flex["load_at_deflection_limit_n"] >= 0.0
    assert flex["reasoning"], f"{material}: deflection answer with no explanation"


def test_load_tool_never_advertises_a_stale_material_list():
    """The available-materials list is derived, never hardcoded.

    A hardcoded list told users only five materials were supported long after
    the table carried thirty-five — the feature looked a seventh of its real
    size, and nothing failed.
    """
    advertised = set(di.load_table_materials())
    actual = {
        k for k in json.loads((DATA / "load_tables.json").read_text(encoding="utf-8"))
        if not k.startswith("_")
    }
    assert advertised == actual, (
        "the advertised load-table materials have drifted from the table "
        f"itself: only-advertised={sorted(advertised - actual)}, "
        f"only-in-table={sorted(actual - advertised)}"
    )
    assert advertised >= set(MATERIALS), (
        "every catalogue material must be answerable by the load lookup: "
        f"missing {sorted(set(MATERIALS) - advertised)}"
    )


@pytest.mark.parametrize("material", MATERIALS)
def test_environment_question_always_answers(material: str):
    """Durability in the real world — sun, heat, damp, chemicals, wear."""
    report = di.check_environment_compatibility(material, "outdoor use in direct sun")
    assert report is not None, (
        f"{material}: no environment answer. A material with no durability "
        "record must still say so rather than return nothing."
    )


@pytest.mark.parametrize("material", MATERIALS)
def test_co_print_question_always_answers(material: str):
    """Asked against a common partner, every material says something.

    Interface adhesion is a property of a boundary, so this must also hold
    from either side — a pair that answered one way round and stayed silent
    the other shipped in this repo.
    """
    for partner in ("pla", "petg"):
        if partner == material:
            continue
        forward = di.check_multi_material_compatibility(material, partner)
        reverse = di.check_multi_material_compatibility(partner, material)
        assert forward is not None, f"{material}+{partner}: no answer"
        assert reverse is not None, f"{partner}+{material}: no answer (reverse)"
        assert forward.interface_adhesion, (
            f"{material}+{partner}: answered with an empty adhesion verdict"
        )
        assert forward.interface_adhesion == reverse.interface_adhesion, (
            f"{material}+{partner}: answers differ by direction — "
            f"{forward.interface_adhesion} vs {reverse.interface_adhesion}"
        )
