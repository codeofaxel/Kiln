"""Tests for kiln.openrouter -- OpenRouter-specific integration."""

from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

from kiln.openrouter import (
    KNOWN_MODELS,
    OPENROUTER_BASE_URL,
    agent_cli,
    create_openrouter_config,
    get_model_tier,
    list_supported_models,
    run_openrouter,
)
from kiln.agent_loop import AgentConfig


# ---------------------------------------------------------------------------
# 1. Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_openrouter_base_url(self):
        assert OPENROUTER_BASE_URL == "https://openrouter.ai/api/v1"


# ---------------------------------------------------------------------------
# 2. KNOWN_MODELS catalog
# ---------------------------------------------------------------------------


class TestKnownModels:
    def test_has_expected_full_tier_models(self):
        full_models = [k for k, v in KNOWN_MODELS.items() if v["tier"] == "full"]
        assert len(full_models) >= 4

    def test_has_expected_standard_tier_models(self):
        standard_models = [k for k, v in KNOWN_MODELS.items() if v["tier"] == "standard"]
        assert len(standard_models) >= 3

    def test_has_expected_essential_tier_models(self):
        essential_models = [k for k, v in KNOWN_MODELS.items() if v["tier"] == "essential"]
        assert len(essential_models) >= 3

    def test_all_models_have_tier_and_context(self):
        for name, info in KNOWN_MODELS.items():
            assert "tier" in info, f"{name} missing 'tier'"
            assert "context" in info, f"{name} missing 'context'"
            assert info["tier"] in ("full", "standard", "essential")
            assert isinstance(info["context"], int)
            assert info["context"] > 0

    def test_gpt4o_is_full(self):
        assert KNOWN_MODELS["openai/gpt-4o"]["tier"] == "full"

    def test_gpt4o_mini_is_standard(self):
        assert KNOWN_MODELS["openai/gpt-4o-mini"]["tier"] == "standard"

    def test_llama_is_essential(self):
        assert KNOWN_MODELS["meta-llama/llama-3.1-8b-instruct"]["tier"] == "essential"

    def test_claude_sonnet_is_full(self):
        assert KNOWN_MODELS["anthropic/claude-sonnet-4"]["tier"] == "full"

    def test_claude_opus_is_full(self):
        assert KNOWN_MODELS["anthropic/claude-opus-4"]["tier"] == "full"

    def test_all_model_ids_have_provider_prefix(self):
        for name in KNOWN_MODELS:
            assert "/" in name, f"Model {name} missing provider/ prefix"


# ---------------------------------------------------------------------------
# 3. list_supported_models
# ---------------------------------------------------------------------------


class TestListSupportedModels:
    def test_returns_dict(self):
        result = list_supported_models()
        assert isinstance(result, dict)

    def test_returns_copy(self):
        r1 = list_supported_models()
        r2 = list_supported_models()
        assert r1 is not r2

    def test_same_content_as_known_models(self):
        result = list_supported_models()
        assert result == KNOWN_MODELS


# ---------------------------------------------------------------------------
# 4. get_model_tier
# ---------------------------------------------------------------------------


class TestGetModelTier:
    def test_known_full_model(self):
        assert get_model_tier("openai/gpt-4o") == "full"

    def test_known_standard_model(self):
        assert get_model_tier("openai/gpt-4o-mini") == "standard"

    def test_known_essential_model(self):
        assert get_model_tier("meta-llama/llama-3.1-8b-instruct") == "essential"

    def test_unknown_model_defaults_to_standard(self):
        assert get_model_tier("some-unknown/model-v99") == "standard"

    def test_empty_string_defaults_to_standard(self):
        assert get_model_tier("") == "standard"

    @pytest.mark.parametrize("model,expected", [
        ("anthropic/claude-sonnet-4", "full"),
        ("anthropic/claude-opus-4", "full"),
        ("google/gemini-pro-1.5", "full"),
        ("google/gemini-2.0-flash", "full"),
        ("google/gemini-flash-1.5", "standard"),
        ("cohere/command-r-plus", "standard"),
        ("mistralai/mistral-large", "standard"),
        ("mistralai/mistral-7b-instruct", "essential"),
        ("microsoft/phi-3-medium-128k-instruct", "essential"),
        ("qwen/qwen-2-72b-instruct", "essential"),
    ])
    def test_all_known_models(self, model, expected):
        assert get_model_tier(model) == expected


# ---------------------------------------------------------------------------
# 5. create_openrouter_config
# ---------------------------------------------------------------------------


class TestCreateOpenrouterConfig:
    def test_sets_correct_base_url(self):
        config = create_openrouter_config(api_key="sk-test")
        assert config.base_url == OPENROUTER_BASE_URL

    def test_default_model(self):
        config = create_openrouter_config(api_key="sk-test")
        assert config.model == "openai/gpt-4o"

    def test_custom_model(self):
        config = create_openrouter_config(api_key="sk-test", model="anthropic/claude-sonnet-4")
        assert config.model == "anthropic/claude-sonnet-4"

    def test_auto_detects_tier_full(self):
        config = create_openrouter_config(api_key="sk-test", model="openai/gpt-4o")
        assert config.tool_tier == "full"

    def test_auto_detects_tier_standard(self):
        config = create_openrouter_config(api_key="sk-test", model="openai/gpt-4o-mini")
        assert config.tool_tier == "standard"

    def test_auto_detects_tier_essential(self):
        config = create_openrouter_config(
            api_key="sk-test",
            model="meta-llama/llama-3.1-8b-instruct",
        )
        assert config.tool_tier == "essential"

    def test_explicit_tier_override(self):
        config = create_openrouter_config(
            api_key="sk-test",
            model="openai/gpt-4o",
            tool_tier="essential",
        )
        assert config.tool_tier == "essential"

    def test_returns_agent_config(self):
        config = create_openrouter_config(api_key="sk-test")
        assert isinstance(config, AgentConfig)

    def test_passes_max_turns(self):
        config = create_openrouter_config(api_key="sk-test", max_turns=10)
        assert config.max_turns == 10

    def test_passes_temperature(self):
        config = create_openrouter_config(api_key="sk-test", temperature=0.8)
        assert config.temperature == 0.8

    def test_passes_system_prompt(self):
        config = create_openrouter_config(
            api_key="sk-test",
            system_prompt="Custom prompt",
        )
        assert config.system_prompt == "Custom prompt"

    def test_passes_timeout(self):
        config = create_openrouter_config(api_key="sk-test", timeout=60)
        assert config.timeout == 60

    def test_unknown_model_tier_defaults_to_standard(self):
        config = create_openrouter_config(api_key="sk-test", model="unknown/model-x")
        assert config.tool_tier == "standard"


# ---------------------------------------------------------------------------
# 6. run_openrouter
# ---------------------------------------------------------------------------


class TestRunOpenrouter:
    @mock.patch("kiln.openrouter.run_agent_loop")
    def test_calls_run_agent_loop(self, mock_loop):
        from kiln.agent_loop import AgentResult, AgentMessage

        mock_loop.return_value = AgentResult(
            response="test",
            messages=[],
            tool_calls_made=0,
            turns=1,
            model="openai/gpt-4o",
        )

        result = run_openrouter("Hello", api_key="sk-test")
        assert result.response == "test"
        mock_loop.assert_called_once()

        # Verify config was passed correctly
        call_args = mock_loop.call_args
        assert call_args[0][0] == "Hello"  # prompt
        config = call_args[0][1]  # config
        assert isinstance(config, AgentConfig)
        assert config.api_key == "sk-test"
        assert config.base_url == OPENROUTER_BASE_URL

    @mock.patch("kiln.openrouter.run_agent_loop")
    def test_forwards_kwargs(self, mock_loop):
        from kiln.agent_loop import AgentResult

        mock_loop.return_value = AgentResult(
            response="ok",
            messages=[],
            tool_calls_made=0,
            turns=1,
            model="test",
        )

        run_openrouter(
            "hi",
            api_key="sk-test",
            model="anthropic/claude-sonnet-4",
            tool_tier="essential",
            max_turns=3,
        )

        config = mock_loop.call_args[0][1]
        assert config.model == "anthropic/claude-sonnet-4"
        assert config.tool_tier == "essential"
        assert config.max_turns == 3


# ---------------------------------------------------------------------------
# 7. agent_cli
# ---------------------------------------------------------------------------


class TestAgentCli:
    def test_exits_on_missing_api_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            # Remove KILN_OPENROUTER_KEY if set
            env = {k: v for k, v in os.environ.items() if k != "KILN_OPENROUTER_KEY"}
            with mock.patch.dict(os.environ, env, clear=True):
                with pytest.raises(SystemExit) as exc_info:
                    agent_cli()
                assert exc_info.value.code == 1

    @mock.patch("kiln.openrouter.run_agent_loop")
    @mock.patch("builtins.input", side_effect=["quit"])
    def test_quit_command(self, mock_input, mock_loop):
        with mock.patch.dict(os.environ, {"KILN_OPENROUTER_KEY": "sk-test"}):
            agent_cli()  # Should exit cleanly
            mock_loop.assert_not_called()

    @mock.patch("kiln.openrouter.run_agent_loop")
    @mock.patch("builtins.input", side_effect=["exit"])
    def test_exit_command(self, mock_input, mock_loop):
        with mock.patch.dict(os.environ, {"KILN_OPENROUTER_KEY": "sk-test"}):
            agent_cli()
            mock_loop.assert_not_called()

    @mock.patch("kiln.openrouter.run_agent_loop")
    @mock.patch("builtins.input", side_effect=EOFError)
    def test_eof_exits_cleanly(self, mock_input, mock_loop):
        with mock.patch.dict(os.environ, {"KILN_OPENROUTER_KEY": "sk-test"}):
            agent_cli()

    @mock.patch("kiln.openrouter.run_agent_loop")
    @mock.patch("builtins.input", side_effect=["models", "quit"])
    def test_models_command(self, mock_input, mock_loop, capsys):
        with mock.patch.dict(os.environ, {"KILN_OPENROUTER_KEY": "sk-test"}):
            agent_cli()
            output = capsys.readouterr().out
            assert "Known models:" in output

    @mock.patch("kiln.openrouter.run_agent_loop")
    @mock.patch("builtins.input", side_effect=["model", "quit"])
    def test_model_command(self, mock_input, mock_loop, capsys):
        with mock.patch.dict(os.environ, {"KILN_OPENROUTER_KEY": "sk-test"}):
            agent_cli()
            output = capsys.readouterr().out
            assert "Current model:" in output

    @mock.patch("builtins.input", side_effect=["reset", "quit"])
    def test_reset_command(self, mock_input, capsys):
        with mock.patch.dict(os.environ, {"KILN_OPENROUTER_KEY": "sk-test"}):
            agent_cli()
            output = capsys.readouterr().out
            assert "reset" in output.lower()

    @mock.patch("builtins.input", side_effect=["", "quit"])
    def test_empty_input_skipped(self, mock_input):
        with mock.patch.dict(os.environ, {"KILN_OPENROUTER_KEY": "sk-test"}):
            agent_cli()

    @mock.patch("kiln.openrouter.run_agent_loop")
    @mock.patch("builtins.input", side_effect=["hello", "quit"])
    def test_sends_prompt_to_agent(self, mock_input, mock_loop):
        from kiln.agent_loop import AgentResult

        mock_loop.return_value = AgentResult(
            response="Hello back!",
            messages=[],
            tool_calls_made=0,
            turns=1,
            model="openai/gpt-4o",
        )
        with mock.patch.dict(os.environ, {"KILN_OPENROUTER_KEY": "sk-test"}):
            agent_cli()
            mock_loop.assert_called_once()
            assert mock_loop.call_args[0][0] == "hello"

    @mock.patch("kiln.openrouter.run_agent_loop")
    @mock.patch("builtins.input", side_effect=["test", "quit"])
    def test_handles_agent_error(self, mock_input, mock_loop, capsys):
        mock_loop.side_effect = Exception("API error")
        with mock.patch.dict(os.environ, {"KILN_OPENROUTER_KEY": "sk-test"}):
            agent_cli()
            output = capsys.readouterr().out
            assert "Error:" in output

    @mock.patch("builtins.input", side_effect=["quit"])
    def test_cli_model_argument(self, mock_input):
        with mock.patch.dict(os.environ, {"KILN_OPENROUTER_KEY": "sk-test"}):
            with mock.patch.object(sys, "argv", ["openrouter", "gpt-4o"]):
                agent_cli()  # Should resolve "gpt-4o" to "openai/gpt-4o"
