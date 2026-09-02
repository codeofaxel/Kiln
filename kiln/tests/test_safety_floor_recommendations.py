"""Tests for the free-tier safety-floor material recommendation fallback.

When the kiln-pro engineering overlay isn't loaded (free tier), the
``mechanical`` and ``use_case_ratings`` fields on every material are
empty.  ``recommend_material_for_design`` detects that and dispatches
to ``_recommend_from_safety_floor``, which scores materials using
ONLY the safety-floor fields (thermal limits, chemical safety,
process-floor design_limits, brand identification + safety-relevant
tunings) plus a small set of public-domain DIY heuristics.

These tests probe the fallback's behavior on canonical free-tier
prompts and verify:

  - the load-bearing detector trip is always surfaced as an upgrade
    nudge in ``warnings`` so free users see the path to the
    engineering-grade analysis
  - the result is shaped like the Pro+ path (same dataclass,
    ``.to_dict()`` works)
  - the dispatch in ``recommend_material_for_design`` correctly
    routes free-tier callers to the fallback and Pro+ callers to
    the curated path

These tests live in PUBLIC Kiln (not kiln-pro).  The trigger words
and inference rules are public-domain knowledge — material datasheets
and DIY common sense — not curated expertise.  Nothing from the
kiln-pro overlay reaches this fallback.
"""

from __future__ import annotations

import pytest

from .conftest import _ENGINEERING_OVERLAY_PRESENT

from kiln.design_intelligence import (
    MaterialProfile,
    MaterialRecommendation,
    _recommend_from_safety_floor,
    _reset_knowledge_base,
    _get_kb,
    recommend_material_for_design,
)


# These tests are the inverse of ``requires_engineering_overlay`` —
# they verify free-tier behavior, so we skip when the overlay IS
# present (because then the dispatch would route to the curated path
# and the assertions about safety-floor rationale wouldn't apply).
requires_no_overlay = pytest.mark.skipif(
    _ENGINEERING_OVERLAY_PRESENT,
    reason=(
        "kiln-pro engineering overlay is loaded; safety-floor fallback "
        "tests verify free-tier behavior and only run when the overlay "
        "is absent."
    ),
)


@pytest.fixture(autouse=True)
def _reset_kb():
    """Reset the design-intelligence knowledge base before each test."""
    _reset_knowledge_base()
    yield
    _reset_knowledge_base()


# ---------------------------------------------------------------------------
# Canonical free-tier prompts
# ---------------------------------------------------------------------------


@requires_no_overlay
class TestCanonicalFreeUserPrompts:
    """Each test mirrors a real free-tier user question."""

    def test_wall_mount_holds_8_lbs_guitar_recommends_petg_or_better(self):
        """Sustained-load case must NOT recommend PLA (creep risk)."""
        rec = recommend_material_for_design(
            "wall mount that holds 8 lbs guitar",
        )
        assert rec.material.material_id != "pla", (
            "PLA creeps under sustained load — must not be recommended for "
            "an 8 lb guitar mount even on free tier."
        )
        # PETG / ASA / ABS / Nylon / PC / their CF variants are all OK.
        # The exact pick depends on printer-capability filters; verify
        # we landed in the safe-for-load family.
        assert rec.material.material_id in {
            "petg", "petg_hf", "petg_cf", "asa", "abs", "nylon",
            "polycarbonate", "pc_abs", "cf_petg", "cf_nylon", "pa6_gf",
            "pet_cf",
        }
        # Rationale must mention sustained load.
        rationale = " ".join(rec.reasons).lower()
        assert "sustained load" in rationale, (
            f"Expected rationale to cite sustained-load filter; got: {rec.reasons}"
        )

    def test_outdoor_garden_sign_recommends_asa(self):
        """UV-exposure case must NOT recommend PLA (UV poor)."""
        rec = recommend_material_for_design(
            "outdoor garden sign in the sun",
            printer_has_enclosure=True,  # ASA needs enclosure
        )
        assert rec.material.material_id == "asa", (
            f"Expected ASA for UV-exposed outdoor sign; got "
            f"{rec.material.material_id}"
        )
        rationale = " ".join(rec.reasons).lower()
        assert (
            "uv" in rationale or "outdoor" in rationale
        ), f"Expected rationale to mention UV / outdoor; got: {rec.reasons}"

    def test_cookie_cutter_food_safe_recommends_petg(self):
        """Food-contact must require food_safe yes/conditional."""
        rec = recommend_material_for_design("cookie cutter food safe")
        # PETG is food_safe="yes" — the only top-tier hit in the
        # safety-floor catalogue.  PETG-HF is also "yes".
        assert rec.material.material_id in {"petg", "petg_hf"}, (
            f"Expected PETG family for food-contact; got "
            f"{rec.material.material_id}"
        )
        rationale = " ".join(rec.reasons).lower()
        assert "food" in rationale, (
            f"Expected rationale to cite food-contact filter; got: {rec.reasons}"
        )

    def test_flexible_phone_case_recommends_tpu_family(self):
        """Flexibility must select an elastomer (TPU family) — only TPU
        in the safety-floor catalogue qualifies."""
        rec = recommend_material_for_design("flexible phone case")
        assert rec.material.material_id.startswith("tpu"), (
            f"Expected TPU family for flexibility; got "
            f"{rec.material.material_id}"
        )
        rationale = " ".join(rec.reasons).lower()
        assert "flexib" in rationale or "tpu" in rationale, (
            f"Expected rationale to cite flexibility / TPU; got: {rec.reasons}"
        )

    def test_decorative_figurine_recommends_pla(self):
        """Cosmetic case (load detector does NOT trip) prefers PLA for
        surface finish."""
        rec = recommend_material_for_design("decorative figurine")
        # Either PLA itself or one of the PLA family variants
        # (silk_pla, pla_matte, etc.) is acceptable.
        assert rec.material.material_id.startswith("pla") or rec.material.material_id == "silk_pla", (
            f"Expected PLA family for decorative figurine; got "
            f"{rec.material.material_id}"
        )
        # No load-bearing nudge in the warnings.
        warning_text = " ".join(rec.warnings).lower()
        assert "load-bearing" not in warning_text, (
            "Decorative figurine should NOT trip the load-bearing "
            "detector — no upgrade nudge expected."
        )


# ---------------------------------------------------------------------------
# Load-bearing detector trip ALWAYS attaches the upgrade nudge
# ---------------------------------------------------------------------------


@requires_no_overlay
class TestLoadBearingNudgeAlwaysAttached:
    """When the load-bearing detector trips, the result MUST include an
    upgrade-nudge string in ``warnings``."""

    @pytest.mark.parametrize(
        "prompt",
        [
            "wall mount that holds 8 lbs guitar",
            "shelf bracket that holds 10 lbs",
            "load-bearing component for my drone",
            "structural bracket",
            "ceiling hook for a hanging plant",
        ],
    )
    def test_load_bearing_prompts_attach_upgrade_nudge(self, prompt):
        rec = recommend_material_for_design(prompt)
        warning_text = " ".join(rec.warnings).lower()
        assert "load-bearing" in warning_text, (
            f"Load-bearing prompt {prompt!r} did not attach the upgrade "
            f"nudge to warnings; got: {rec.warnings}"
        )
        # The funnel link must be present.
        assert "kiln3d.com" in " ".join(rec.warnings), (
            f"Load-bearing nudge missing the funnel link; got: {rec.warnings}"
        )

    @pytest.mark.parametrize(
        "prompt",
        [
            "decorative figurine",
            "phone stand for my desk",
            "drink coaster",
            "cookie cutter food safe",
        ],
    )
    def test_decoy_prompts_do_not_attach_upgrade_nudge(self, prompt):
        rec = recommend_material_for_design(prompt)
        warning_text = " ".join(rec.warnings).lower()
        assert "load-bearing" not in warning_text, (
            f"Decoy prompt {prompt!r} attached the upgrade nudge "
            f"unexpectedly; got: {rec.warnings}"
        )


# ---------------------------------------------------------------------------
# Result shape — must mirror the Pro+ path
# ---------------------------------------------------------------------------


@requires_no_overlay
class TestResultShape:
    """``MaterialRecommendation`` shape must be identical to the Pro+ path."""

    def test_result_is_material_recommendation_dataclass(self):
        rec = recommend_material_for_design("simple prototype")
        assert isinstance(rec, MaterialRecommendation)
        assert isinstance(rec.material, MaterialProfile)
        assert isinstance(rec.score, float)
        assert isinstance(rec.reasons, list)
        assert isinstance(rec.warnings, list)
        assert isinstance(rec.alternatives, list)
        assert isinstance(rec.design_limits_summary, dict)

    def test_to_dict_round_trips(self):
        rec = recommend_material_for_design("flexible phone case")
        d = rec.to_dict()
        assert isinstance(d, dict)
        assert "material" in d
        assert "score" in d
        assert "reasons" in d
        assert "warnings" in d
        assert "alternatives" in d
        # Nested material must also serialize.
        assert isinstance(d["material"], dict)
        assert d["material"]["material_id"].startswith("tpu")

    def test_alternatives_list_has_entries(self):
        rec = recommend_material_for_design("strong functional part")
        assert len(rec.alternatives) > 0, (
            "Safety-floor fallback should still surface alternatives."
        )
        # Each alt has the same shape the Pro+ path produces.
        for alt in rec.alternatives:
            assert "material_id" in alt
            assert "display_name" in alt
            assert "score" in alt

    def test_no_supported_materials_match_uses_pla_fallback(self):
        """If the printer's allowlist excludes everything, we still
        return a valid recommendation (PLA absolute fallback)."""
        rec = recommend_material_for_design(
            "flexible gasket",
            supported_materials=["pla"],  # TPU not in allowlist
        )
        # TPU got filtered to the only candidate but is unsupported.
        # The function should still produce a valid MaterialRecommendation
        # rather than crashing.
        assert isinstance(rec, MaterialRecommendation)
        assert rec.material is not None


# ---------------------------------------------------------------------------
# Dispatch correctness — overlay presence routes to the curated path
# ---------------------------------------------------------------------------


@requires_no_overlay
class TestDispatchToSafetyFloor:
    """Verify the dispatch in ``recommend_material_for_design`` routes
    free-tier callers to the safety-floor fallback, and Pro+ callers
    to the curated path."""

    def test_free_tier_routes_to_safety_floor(self):
        """In the absence of the overlay (this test's environment),
        the result should bear the safety-floor fingerprint:
        rationale string starts with one of our prefix patterns."""
        rec = recommend_material_for_design("wall mount that holds 8 lbs guitar")
        assert rec.reasons, "Safety-floor fallback should populate reasons."
        first = rec.reasons[0].lower()
        assert (
            "recommended for:" in first or "recommended" in first
        ), f"Safety-floor rationale fingerprint missing: {rec.reasons[0]!r}"

    def test_overlay_present_routes_to_curated_path(self, monkeypatch):
        """Simulate overlay presence by injecting a ``mechanical``
        field into the in-memory materials dict, and verify the
        function takes the curated path rather than the safety floor.

        We detect the curated path by the absence of the safety-floor
        rationale prefix ('Recommended for:' or '... recommended —').
        The curated path's rationale uses different phrasing
        ("Required by functional constraints.", "Preferred for these
        requirements.").
        """
        kb = _get_kb()
        # Pre-populate the cache so we can mutate it.
        _ = kb.materials  # triggers _load
        # Inject a tiny mechanical record into PLA so the overlay
        # probe in recommend_material_for_design returns truthy.
        # The rest of the curated path will work off the constraint
        # rules in functional_requirements.json — it doesn't strictly
        # need use_case_ratings to function (just downgrades quality).
        for mid in kb.materials:
            kb.materials[mid].setdefault(
                "mechanical", {"_synthetic_for_test": True}
            )

        rec = recommend_material_for_design("strong functional part")
        # The curated path doesn't emit our safety-floor rationale
        # prefix; it builds rationale from the use_case_rating loop
        # which emits strings like "Required by functional constraints."
        # or empty if no constraints fired.  We assert the absence
        # of our distinctive prefix.
        assert isinstance(rec, MaterialRecommendation)
        rationale_joined = " ".join(rec.reasons).lower()
        assert "recommended for:" not in rationale_joined, (
            "Overlay-present mode should not use the safety-floor "
            "fallback rationale prefix."
        )


# ---------------------------------------------------------------------------
# Direct calls to _recommend_from_safety_floor (unit-level)
# ---------------------------------------------------------------------------


@requires_no_overlay
class TestSafetyFloorDirectCalls:
    """Directly invoke ``_recommend_from_safety_floor`` to keep the
    contract tight even if the dispatch changes."""

    def test_direct_call_returns_recommendation(self):
        kb = _get_kb()
        rec = _recommend_from_safety_floor(
            "wall mount that holds 8 lbs guitar",
            kb.materials,
        )
        assert isinstance(rec, MaterialRecommendation)
        assert rec.material.material_id != "pla"

    def test_hot_environment_excludes_pla(self):
        kb = _get_kb()
        rec = _recommend_from_safety_floor(
            "phone holder for car dashboard in summer",
            kb.materials,
            printer_has_enclosure=True,
        )
        # PLA's max_service_temp_c is 50C; car summer interior > 60C.
        assert rec.material.material_id != "pla"

    def test_indoor_outgassing_prefers_minimal(self):
        kb = _get_kb()
        rec = _recommend_from_safety_floor(
            "indoor office organizer for my bedroom",
            kb.materials,
        )
        # Outgassing="minimal" boost favors PLA family / PETG.  We
        # don't pin the exact pick (depends on tie-breakers), but we
        # verify nothing with moderate outgassing beats them.
        chemical = rec.material.chemical
        assert chemical.get("outgassing") in ("minimal", "low"), (
            f"Indoor prompt picked material with outgassing="
            f"{chemical.get('outgassing')!r}"
        )

    def test_no_kw_args_works_with_defaults(self):
        kb = _get_kb()
        rec = _recommend_from_safety_floor(
            "decorative figurine",
            kb.materials,
        )
        assert isinstance(rec, MaterialRecommendation)
        assert rec.material.material_id.startswith("pla") or rec.material.material_id == "silk_pla"
