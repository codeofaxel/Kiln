"""The post-processing table names each finish by what it is, not what it is sold as.

A tool list that says "a brush-on two-part epoxy coating that self-levels"
tells a person what to look for in any shop, in any country; a retail product
name tells them one shelf.  Four technique names used to carry a product in
brackets or after "with", and a technique name is a lookup key, so an old
spelling still resolves: the lookup matches on the technique's CORE (the
bracketed tail, a "with ..." tail and the form word dropped) rather than on a
table of old names, which would keep the products in this repo.
"""

from __future__ import annotations

import json

import pytest

from kiln.design_intelligence import (
    _DATA_DIR,
    find_post_processing_technique,
    get_public_post_processing,
    technique_core,
)

# (material, a spelling of the kind the table used to carry, the name it carries now)
_OLD_SPELLINGS = [
    ("pla", "Epoxy Coating (some product)", "Epoxy Coating"),
    ("nylon", "Dyeing with some product", "Dyeing"),
    ("tpu", "Flexible Paint (brand a / brand b)", "Flexible Rubber Paint"),
    ("tpu_85a", "Flexible Coating (brand a / rubber coating)", "Flexible Rubber Coating"),
]


@pytest.fixture()
def guide_tool():
    tools: dict[str, object] = {}

    class _MCP:
        def tool(self, **_kwargs):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

    from kiln.plugins.design_tools import plugin

    plugin.register(_MCP())
    return tools["get_post_processing_guide"]


@pytest.mark.parametrize(("material", "old", "new"), _OLD_SPELLINGS)
def test_an_old_technique_spelling_still_answers_through_the_tool(guide_tool, material, old, new):
    result = guide_tool(material, old)

    assert result["success"] is True, result
    assert result["goal"] == new
    technique = result["answer"]["technique"]
    assert technique["name"] == new
    assert str(technique.get("safety_notes") or "").strip()


def test_old_spellings_resolve_in_any_case():
    techniques = get_public_post_processing("pla").techniques
    found = find_post_processing_technique(techniques, "  epoxy COATING (anything at all) ")
    assert found is not None and found["name"] == "Epoxy Coating"


def test_the_core_rule():
    assert technique_core("Epoxy Coating (some product)") == "epoxy coating"
    assert technique_core("Dyeing with some product") == "dyeing"
    assert technique_core("Flexible Rubber Paint") == "flexible paint"
    assert technique_core("  IPA Vapor Smoothing (Structural) ") == "ipa vapor smoothing"
    assert technique_core("") == ""


def test_no_two_techniques_of_one_material_share_a_core():
    """The core is the lookup key for an old spelling: two techniques with one
    core would make an old name ambiguous, and the lookup answers None."""
    raw = json.loads((_DATA_DIR / "post_processing.json").read_text(encoding="utf-8"))
    for key, record in raw.items():
        if key.startswith("_"):
            continue
        cores = [technique_core(t["name"]) for t in record.get("techniques") or []]
        assert len(cores) == len(set(cores)), (key, sorted(cores))


def test_an_ambiguous_old_spelling_answers_none_not_a_guess():
    rows = [{"name": "Dyeing"}, {"name": "Dyeing with a brush"}]
    assert find_post_processing_technique(rows, "Dyeing with anything") is None
    assert find_post_processing_technique(rows, "Dyeing with a brush")["name"] == "Dyeing with a brush"
