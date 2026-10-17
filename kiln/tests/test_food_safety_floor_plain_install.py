"""The food-safety floor holds on a plain install.

Every door that recommends or swaps a material for something that will hold
food, touch a mouth or feed a pet drops the materials the catalog rates not
food safe.  Until 1.5.0 each of the three doors below imported that filter from
kiln-pro and skipped it when kiln-pro was absent, which is every customer's
install: a cereal container was offered ABS.  These tests run each door with
kiln-pro made unimportable, so they measure the install customers have.
"""

from __future__ import annotations

import sys

import pytest

CEREAL = "food storage container for the kitchen, holds cereal"

#: Materials the public catalog rates food_safe="no" that the recommenders
#: can otherwise reach for.
NOT_FOOD_SAFE = {"abs", "asa", "tpu", "nylon_cf", "cf_pla", "silk_pla", "wood_pla"}


@pytest.fixture(autouse=True)
def _plain_install(monkeypatch):
    """No kiln-pro: any ``import kiln_pro...`` raises ImportError."""
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


def _names(rec) -> list[str]:
    d = rec.to_dict() if hasattr(rec, "to_dict") else rec
    picked = [d["material"]] if isinstance(d.get("material"), str) else []
    for alt in d.get("alternatives") or []:
        picked.append(alt.get("name") if isinstance(alt, dict) else str(alt))
    return [p.lower() for p in picked if p]


class TestRecommendMaterial:
    def test_cereal_container_is_never_offered_a_not_food_safe_material(self):
        from kiln.material_routing import recommend_material

        offered = _names(recommend_material(CEREAL))
        assert offered, "the recommender offered nothing at all"
        assert not set(offered) & NOT_FOOD_SAFE, offered

    def test_a_brief_with_no_food_still_sees_the_whole_catalog(self):
        from kiln.material_routing import recommend_material

        offered = _names(recommend_material("outdoor bracket for a garden fence, sun all day"))
        assert set(offered) & {"asa", "abs", "petg"}, offered


def _design_tools():
    from kiln.plugins.design_tools import plugin

    tools: dict = {}

    class _MCP:
        def tool(self, **_kwargs):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

    plugin.register(_MCP())
    return tools


class _AbsFirst:
    """A scorer answer that puts a not-food-safe material on top, so the
    override is exercised rather than left to whatever the scorer picks."""

    def to_dict(self):
        return {
            "material": {"material_id": "abs"},
            "alternatives": [{"material_id": "asa"}, {"material_id": "petg"}, {"material_id": "pla"}],
            "warnings": [],
        }


class TestRecommendDesignMaterial:
    @pytest.fixture
    def tool(self, monkeypatch):
        import kiln.design_intelligence as di

        monkeypatch.setattr(di, "recommend_material_for_design", lambda *a, **k: _AbsFirst())
        return _design_tools()["recommend_design_material"]

    def test_a_top_pick_rated_not_food_safe_is_overridden(self, tool):
        result = tool("a bowl my dog eats from")
        assert result.get("food_contact_use_case_detected") is True, result
        assert result["food_safety"]["primary_material_verdict"] == "refuse"
        assert result["recommended_override"] == {
            "original_recommendation": "abs",
            "override_to": "petg",
            "reason": "food_contact_safety",
        }
        assert result["warnings"][0].startswith("FOOD-SAFETY OVERRIDE"), result["warnings"]

    def test_alternatives_keep_only_what_the_rating_allows(self, tool):
        result = tool("a bowl my dog eats from")
        kept = [a["material_id"] for a in result["alternatives_food_safe_only"]]
        assert kept == ["petg", "pla"]

    def test_no_food_no_override(self, tool):
        result = tool("a shelf bracket for the garage")
        assert "food_safety" not in result
        assert "recommended_override" not in result


class TestFindMaterialSubstitute:
    def test_swapping_food_safe_petg_flags_every_substitute_that_is_not(self):
        from kiln.server import find_material_substitute

        result = find_material_substitute("PETG")
        assert result["success"] is True, result
        check = result.get("food_safety")
        assert check is not None, "a food-safe source got no food-safety check"
        assert check["source_food_safe"] == "yes"
        flagged = {r.lower() for r in check["regressions"]}
        for sub in result["substitutes"]:
            name = str(sub.get("substitute_material") or sub.get("substitute") or sub.get("material") or "")
            if sub.get("food_safe") != "yes":
                assert name.lower() in flagged, (name, check)


class TestTheDetector:
    @pytest.mark.parametrize(
        "text",
        [
            CEREAL,
            "a cereal box insert",
            "dinner plate that sits on the build plate",
            "pet_bowl for my cat",
            "coffee mug",
            "a toothbrush holder",
        ],
    )
    def test_food_briefs_fire(self, text):
        from kiln.material_safety import use_case_implies_food_contact

        assert use_case_implies_food_contact(text)

    @pytest.mark.parametrize(
        "text",
        [
            "slice the model and print it",
            "replace the build plate clip",
            "a pen cup for my desk",
            "cable feeding guide for the printer",
            "a cupboard door hinge",
            "",
        ],
    )
    def test_hardware_briefs_do_not(self, text):
        from kiln.material_safety import use_case_implies_food_contact

        assert not use_case_implies_food_contact(text)

    def test_conditional_stays_in_the_recommendation_and_is_never_called_food_safe(self):
        from kiln.material_safety import assess_food_safety, filter_food_safe_materials

        assert filter_food_safe_materials(["PLA", "ABS", "PETG"], "yes_or_conditional") == ["PLA", "PETG"]
        assert filter_food_safe_materials(["PLA", "ABS", "PETG"], "yes") == ["PETG"]
        pla = assess_food_safety("PLA")
        assert pla["verdict"] == "warn"
        assert "only under conditions" in pla["user_warning"]
        assert "rated food safe" not in pla["user_warning"]

    def test_unknown_material_is_refused_not_assumed_safe(self):
        from kiln.material_safety import assess_food_safety, filter_food_safe_materials

        assert assess_food_safety("unobtainium")["verdict"] == "refuse"
        assert filter_food_safe_materials(["unobtainium"], "yes_or_conditional") == []
