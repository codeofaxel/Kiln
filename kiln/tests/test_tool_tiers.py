"""Tests for kiln.tool_tiers -- Tool tier definitions and model-to-tier mapping."""

from __future__ import annotations

import pytest

from kiln.tool_tiers import (
    TIER_ESSENTIAL,
    TIER_FULL,
    TIER_STANDARD,
    TIERS,
    get_tier,
    suggest_tier,
    _ESSENTIAL_PREFIXES,
    _FULL_PREFIXES,
    _STANDARD_PREFIXES,
)


# ---------------------------------------------------------------------------
# 1. Tier size and subset invariants
# ---------------------------------------------------------------------------


class TestTierSizes:
    """Verify tier sizes and the subset/superset relationships."""

    def test_essential_has_15_items(self):
        assert len(TIER_ESSENTIAL) == 15

    def test_standard_includes_all_essential(self):
        for tool in TIER_ESSENTIAL:
            assert tool in TIER_STANDARD, f"{tool!r} missing from STANDARD"

    def test_standard_has_approximately_43_items(self):
        # The spec says ~43.  TIER_STANDARD = TIER_ESSENTIAL (15) + 28 = 43.
        assert len(TIER_STANDARD) >= 40
        assert len(TIER_STANDARD) <= 50

    def test_full_has_105_items(self):
        assert len(TIER_FULL) == 105

    def test_full_includes_all_standard(self):
        for tool in TIER_STANDARD:
            assert tool in TIER_FULL, f"{tool!r} missing from FULL"

    def test_no_duplicates_in_essential(self):
        assert len(TIER_ESSENTIAL) == len(set(TIER_ESSENTIAL))

    def test_no_duplicates_in_standard(self):
        assert len(TIER_STANDARD) == len(set(TIER_STANDARD))

    def test_no_duplicates_in_full(self):
        assert len(TIER_FULL) == len(set(TIER_FULL))


# ---------------------------------------------------------------------------
# 2. TIERS dict
# ---------------------------------------------------------------------------


class TestTiersDict:
    def test_tiers_has_three_keys(self):
        assert set(TIERS.keys()) == {"essential", "standard", "full"}

    def test_tiers_essential_is_same_list(self):
        assert TIERS["essential"] is TIER_ESSENTIAL

    def test_tiers_standard_is_same_list(self):
        assert TIERS["standard"] is TIER_STANDARD

    def test_tiers_full_is_same_list(self):
        assert TIERS["full"] is TIER_FULL


# ---------------------------------------------------------------------------
# 3. get_tier()
# ---------------------------------------------------------------------------


class TestGetTier:
    def test_get_tier_essential(self):
        result = get_tier("essential")
        assert result is TIER_ESSENTIAL

    def test_get_tier_standard(self):
        result = get_tier("standard")
        assert result is TIER_STANDARD

    def test_get_tier_full(self):
        result = get_tier("full")
        assert result is TIER_FULL

    def test_get_tier_unknown_raises_key_error(self):
        with pytest.raises(KeyError, match="Unknown tier"):
            get_tier("mega")

    def test_get_tier_empty_string_raises_key_error(self):
        with pytest.raises(KeyError, match="Unknown tier"):
            get_tier("")

    def test_get_tier_case_sensitive(self):
        with pytest.raises(KeyError, match="Unknown tier"):
            get_tier("Essential")


# ---------------------------------------------------------------------------
# 4. suggest_tier() -- model name mapping
# ---------------------------------------------------------------------------


class TestSuggestTierFull:
    """Models that should map to 'full'."""

    @pytest.mark.parametrize("model", [
        "claude-3-opus",
        "claude-3-sonnet",
        "claude-3-haiku",
        "gpt-4o",
        "gpt-4-turbo",
        "gpt-4",
        "gemini-pro-1.5",
        "gemini-1.5-pro",
        "gemini-2.0-flash",
        "o1-preview",
        "o3-mini",
        "o4-mini",
        "deepseek-v3-base",
        "deepseek-r1",
    ])
    def test_full_tier_models(self, model):
        assert suggest_tier(model) == "full", f"{model} should be 'full'"


class TestSuggestTierStandard:
    """Models that should map to 'standard'."""

    @pytest.mark.parametrize("model", [
        "gpt-4o-mini",
        "gpt-3.5-turbo",
        "gemini-flash-1.5",
        "gemini-1.5-flash",
        "command-r-plus",
        "command-r+",
        "deepseek-v2",
        "yi-34b",
    ])
    def test_standard_tier_models(self, model):
        assert suggest_tier(model) == "standard", f"{model} should be 'standard'"


class TestSuggestTierEssential:
    """Models that should map to 'essential'."""

    @pytest.mark.parametrize("model", [
        "llama-3-8b",
        "llama-3-70b",
        "mistral-7b",
        "mixtral-8x7b",
        "phi-3-mini",
        "qwen-2-72b",
        "gemma-7b",
        "tinyllama-1.1b",
        "codellama-34b",
        "vicuna-13b",
        "openchat-3.5",
    ])
    def test_essential_tier_models(self, model):
        assert suggest_tier(model) == "essential", f"{model} should be 'essential'"


class TestSuggestTierOpenRouterIDs:
    """OpenRouter model IDs with provider/ prefix."""

    def test_anthropic_claude_opus(self):
        assert suggest_tier("anthropic/claude-3-opus") == "full"

    def test_openai_gpt4o(self):
        assert suggest_tier("openai/gpt-4o") == "full"

    def test_openai_gpt4o_mini(self):
        assert suggest_tier("openai/gpt-4o-mini") == "standard"

    def test_meta_llama_3_8b(self):
        assert suggest_tier("meta-llama/llama-3-8b") == "essential"

    def test_mistralai_mistral_7b(self):
        assert suggest_tier("mistralai/mistral-7b-instruct") == "essential"

    def test_google_gemini_pro(self):
        assert suggest_tier("google/gemini-pro-1.5") == "full"

    def test_cohere_command_r_plus(self):
        assert suggest_tier("cohere/command-r-plus") == "standard"


class TestSuggestTierEdgeCases:
    """Edge cases and defaults."""

    def test_unknown_model_defaults_to_standard(self):
        assert suggest_tier("some-random-model-v2") == "standard"

    def test_empty_string_defaults_to_standard(self):
        assert suggest_tier("") == "standard"

    def test_case_insensitive_claude(self):
        assert suggest_tier("Claude-3-Opus") == "full"

    def test_case_insensitive_llama(self):
        assert suggest_tier("LLAMA-3-70B") == "essential"

    def test_gpt4o_mini_prioritized_over_gpt4o(self):
        """gpt-4o-mini must match 'standard', not 'full' (gpt-4o prefix)."""
        assert suggest_tier("gpt-4o-mini") == "standard"

    def test_deeply_nested_openrouter_id(self):
        """Only the first / is used for splitting."""
        assert suggest_tier("provider/claude-3-sonnet") == "full"


# ---------------------------------------------------------------------------
# 5. Specific tool presence checks
# ---------------------------------------------------------------------------


class TestToolPresence:
    """Verify key tools appear in the expected tiers."""

    def test_printer_status_in_essential(self):
        assert "printer_status" in TIER_ESSENTIAL

    def test_kiln_health_in_essential(self):
        assert "kiln_health" in TIER_ESSENTIAL

    def test_slice_model_in_standard(self):
        assert "slice_model" in TIER_STANDARD

    def test_slice_model_not_in_essential(self):
        assert "slice_model" not in TIER_ESSENTIAL

    def test_register_webhook_in_full(self):
        assert "register_webhook" in TIER_FULL

    def test_register_webhook_not_in_standard(self):
        assert "register_webhook" not in TIER_STANDARD

    def test_billing_tools_only_in_full(self):
        billing = ["billing_summary", "billing_setup_url", "billing_status", "billing_history"]
        for tool in billing:
            assert tool in TIER_FULL
            assert tool not in TIER_STANDARD
            assert tool not in TIER_ESSENTIAL
