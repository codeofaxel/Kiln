"""Tests for kiln.agent_loop -- Generic agent loop for OpenAI-compatible APIs."""

from __future__ import annotations

import json
from unittest import mock

import pytest

from kiln.agent_loop import (
    AgentConfig,
    AgentLoopError,
    AgentMessage,
    AgentResult,
    _call_llm,
    _execute_tool,
    _execute_tool_call,
    _get_default_system_prompt,
    _messages_to_agent_messages,
    run_agent_loop,
)


# ---------------------------------------------------------------------------
# 1. AgentConfig
# ---------------------------------------------------------------------------


class TestAgentConfig:
    def test_defaults(self):
        cfg = AgentConfig(api_key="sk-test-key")
        assert cfg.base_url == "https://openrouter.ai/api/v1"
        assert cfg.model == "openai/gpt-4o"
        assert cfg.tool_tier == "full"
        assert cfg.max_turns == 20
        assert cfg.temperature == 0.1
        assert cfg.system_prompt is None
        assert cfg.timeout == 120

    def test_custom_values(self):
        cfg = AgentConfig(
            api_key="my-key",
            base_url="http://localhost:8080/v1",
            model="meta/llama-3",
            tool_tier="essential",
            max_turns=5,
            temperature=0.7,
            system_prompt="You are helpful.",
            timeout=60,
        )
        assert cfg.model == "meta/llama-3"
        assert cfg.tool_tier == "essential"
        assert cfg.max_turns == 5

    def test_to_dict_redacts_api_key(self):
        cfg = AgentConfig(api_key="sk-or-very-secret-key-12345")
        d = cfg.to_dict()
        assert "very-secret" not in d["api_key"]
        assert d["api_key"].startswith("sk-or-ve")
        assert d["api_key"].endswith("...")

    def test_to_dict_redacts_short_key(self):
        cfg = AgentConfig(api_key="short")
        d = cfg.to_dict()
        # "short" is 5 chars, [:8] would be "short" then "..."
        assert d["api_key"] == "short..."

    def test_to_dict_empty_key(self):
        cfg = AgentConfig(api_key="")
        d = cfg.to_dict()
        assert d["api_key"] == ""

    def test_to_dict_contains_all_fields(self):
        cfg = AgentConfig(api_key="test")
        d = cfg.to_dict()
        expected_keys = {
            "api_key", "base_url", "model", "tool_tier",
            "max_turns", "temperature", "system_prompt", "timeout",
        }
        assert set(d.keys()) == expected_keys


# ---------------------------------------------------------------------------
# 2. AgentMessage
# ---------------------------------------------------------------------------


class TestAgentMessage:
    def test_minimal_message(self):
        msg = AgentMessage(role="user", content="Hello")
        d = msg.to_dict()
        assert d == {"role": "user", "content": "Hello"}

    def test_excludes_none_fields(self):
        msg = AgentMessage(role="assistant", content="Hi")
        d = msg.to_dict()
        assert "tool_calls" not in d
        assert "tool_call_id" not in d
        assert "name" not in d

    def test_includes_tool_calls_when_present(self):
        tc = [{"id": "call_1", "function": {"name": "foo", "arguments": "{}"}}]
        msg = AgentMessage(role="assistant", tool_calls=tc)
        d = msg.to_dict()
        assert d["tool_calls"] == tc
        assert "content" not in d  # content is None

    def test_tool_result_message(self):
        msg = AgentMessage(
            role="tool",
            content='{"status": "ok"}',
            tool_call_id="call_123",
            name="printer_status",
        )
        d = msg.to_dict()
        assert d["role"] == "tool"
        assert d["tool_call_id"] == "call_123"
        assert d["name"] == "printer_status"
        assert d["content"] == '{"status": "ok"}'

    def test_system_message(self):
        msg = AgentMessage(role="system", content="You are a helpful assistant.")
        d = msg.to_dict()
        assert d == {"role": "system", "content": "You are a helpful assistant."}


# ---------------------------------------------------------------------------
# 3. AgentResult
# ---------------------------------------------------------------------------


class TestAgentResult:
    def test_to_dict(self):
        messages = [
            AgentMessage(role="system", content="sys"),
            AgentMessage(role="user", content="hi"),
            AgentMessage(role="assistant", content="hello"),
        ]
        result = AgentResult(
            response="hello",
            messages=messages,
            tool_calls_made=0,
            turns=1,
            model="gpt-4o",
            total_tokens=100,
        )
        d = result.to_dict()
        assert d["response"] == "hello"
        assert d["tool_calls_made"] == 0
        assert d["turns"] == 1
        assert d["model"] == "gpt-4o"
        assert d["total_tokens"] == 100
        assert len(d["messages"]) == 3

    def test_to_dict_null_tokens(self):
        result = AgentResult(
            response="ok",
            messages=[],
            tool_calls_made=0,
            turns=1,
            model="test",
        )
        d = result.to_dict()
        assert d["total_tokens"] is None


# ---------------------------------------------------------------------------
# 4. _call_llm
# ---------------------------------------------------------------------------


class TestCallLLM:
    """Test _call_llm by mocking requests.post."""

    def _make_config(self, **overrides):
        defaults = dict(
            api_key="sk-test",
            base_url="https://api.example.com/v1",
            model="test-model",
        )
        defaults.update(overrides)
        return AgentConfig(**defaults)

    def _mock_response(self, status_code=200, json_data=None, text="", headers=None):
        resp = mock.MagicMock()
        resp.status_code = status_code
        resp.text = text
        resp.headers = headers or {}
        resp.json.return_value = json_data or {}
        return resp

    @mock.patch("kiln.agent_loop.requests.post")
    def test_sends_correct_url(self, mock_post):
        mock_post.return_value = self._mock_response(json_data={"choices": []})
        config = self._make_config(base_url="https://api.test.com/v1/")

        try:
            _call_llm([], [], config)
        except AgentLoopError:
            pass  # empty choices is expected

        args, kwargs = mock_post.call_args
        assert args[0] == "https://api.test.com/v1/chat/completions"

    @mock.patch("kiln.agent_loop.requests.post")
    def test_sends_auth_header(self, mock_post):
        mock_post.return_value = self._mock_response(
            json_data={"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
        )
        config = self._make_config(api_key="sk-my-secret")

        _call_llm([], [], config)

        _, kwargs = mock_post.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer sk-my-secret"

    @mock.patch("kiln.agent_loop.requests.post")
    def test_sends_model_and_temperature(self, mock_post):
        mock_post.return_value = self._mock_response(
            json_data={"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
        )
        config = self._make_config(model="gpt-4o", temperature=0.5)

        _call_llm([], [], config)

        _, kwargs = mock_post.call_args
        body = kwargs["json"]
        assert body["model"] == "gpt-4o"
        assert body["temperature"] == 0.5

    @mock.patch("kiln.agent_loop.requests.post")
    def test_includes_tools_when_provided(self, mock_post):
        mock_post.return_value = self._mock_response(
            json_data={"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
        )
        config = self._make_config()
        tools = [{"type": "function", "function": {"name": "test"}}]

        _call_llm([], tools, config)

        _, kwargs = mock_post.call_args
        body = kwargs["json"]
        assert body["tools"] == tools
        assert body["tool_choice"] == "auto"

    @mock.patch("kiln.agent_loop.requests.post")
    def test_omits_tools_when_empty(self, mock_post):
        mock_post.return_value = self._mock_response(
            json_data={"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
        )
        config = self._make_config()

        _call_llm([], [], config)

        _, kwargs = mock_post.call_args
        body = kwargs["json"]
        assert "tools" not in body
        assert "tool_choice" not in body

    @mock.patch("kiln.agent_loop.requests.post")
    def test_rate_limit_429(self, mock_post):
        mock_post.return_value = self._mock_response(
            status_code=429,
            headers={"Retry-After": "30"},
        )
        config = self._make_config()

        with pytest.raises(AgentLoopError, match="rate limited"):
            _call_llm([], [], config)

    @mock.patch("kiln.agent_loop.requests.post")
    def test_server_error_500(self, mock_post):
        mock_post.return_value = self._mock_response(
            status_code=500,
            text="Internal Server Error",
        )
        config = self._make_config()

        with pytest.raises(AgentLoopError, match="HTTP 500"):
            _call_llm([], [], config)

    @mock.patch("kiln.agent_loop.requests.post")
    def test_timeout_error(self, mock_post):
        from requests.exceptions import ReadTimeout
        mock_post.side_effect = ReadTimeout("timed out")
        config = self._make_config(timeout=30)

        with pytest.raises(AgentLoopError, match="timed out"):
            _call_llm([], [], config)

    @mock.patch("kiln.agent_loop.requests.post")
    def test_connection_error(self, mock_post):
        from requests.exceptions import ConnectionError
        mock_post.side_effect = ConnectionError("refused")
        config = self._make_config()

        with pytest.raises(AgentLoopError, match="Cannot connect"):
            _call_llm([], [], config)

    @mock.patch("kiln.agent_loop.requests.post")
    def test_non_json_response(self, mock_post):
        resp = self._mock_response(status_code=200)
        resp.json.side_effect = ValueError("not json")
        mock_post.return_value = resp
        config = self._make_config()

        with pytest.raises(AgentLoopError, match="non-JSON"):
            _call_llm([], [], config)


# ---------------------------------------------------------------------------
# 5. _execute_tool (with mocked tool cache)
# ---------------------------------------------------------------------------


class TestExecuteTool:
    """Test _execute_tool with mocked MCP server."""

    @mock.patch("kiln.agent_loop._ensure_tool_cache")
    def test_unknown_tool_returns_error(self, mock_cache):
        mock_cache.return_value = {}
        result = _execute_tool("nonexistent", {})
        parsed = json.loads(result)
        assert parsed["error"] == "Unknown tool: nonexistent"
        assert parsed["success"] is False

    @mock.patch("kiln.agent_loop._ensure_tool_cache")
    def test_known_tool_name_in_cache(self, mock_cache):
        mock_cache.return_value = {
            "printer_status": {"name": "printer_status", "schema": {}},
        }
        # The actual tool call will fail since we can't mock call_tool easily,
        # but we can verify it gets past the cache check.
        # Let's mock the mcp server call too.
        with mock.patch("kiln.agent_loop._get_mcp_server") as mock_server:
            import asyncio
            mock_mcp = mock.MagicMock()

            # call_tool is async, make it return a mock result
            async def mock_call_tool(name, args):
                result_item = mock.MagicMock()
                result_item.text = '{"status": "operational"}'
                return [result_item]

            mock_mcp.call_tool = mock_call_tool
            mock_server.return_value = mock_mcp

            result = _execute_tool("printer_status", {})
            assert "operational" in result


# ---------------------------------------------------------------------------
# 6. _execute_tool_call
# ---------------------------------------------------------------------------


class TestExecuteToolCall:
    @mock.patch("kiln.agent_loop._execute_tool")
    def test_parses_arguments_json(self, mock_exec):
        mock_exec.return_value = '{"result": "ok"}'
        tc = {
            "function": {
                "name": "printer_status",
                "arguments": '{"host": "192.168.1.10"}',
            }
        }
        result = _execute_tool_call(tc)
        mock_exec.assert_called_once_with("printer_status", {"host": "192.168.1.10"})
        assert result == '{"result": "ok"}'

    @mock.patch("kiln.agent_loop._execute_tool")
    def test_handles_dict_arguments(self, mock_exec):
        mock_exec.return_value = '{"ok": true}'
        tc = {
            "function": {
                "name": "test",
                "arguments": {"x": 1},
            }
        }
        _execute_tool_call(tc)
        mock_exec.assert_called_once_with("test", {"x": 1})

    def test_invalid_json_arguments(self):
        tc = {
            "function": {
                "name": "test",
                "arguments": "not valid json {{{",
            }
        }
        result = _execute_tool_call(tc)
        parsed = json.loads(result)
        assert "Invalid JSON" in parsed["error"]
        assert parsed["success"] is False

    @mock.patch("kiln.agent_loop._execute_tool")
    def test_empty_arguments_default(self, mock_exec):
        mock_exec.return_value = "{}"
        tc = {"function": {"name": "test"}}
        _execute_tool_call(tc)
        mock_exec.assert_called_once_with("test", {})


# ---------------------------------------------------------------------------
# 7. _messages_to_agent_messages
# ---------------------------------------------------------------------------


class TestMessagesToAgentMessages:
    def test_converts_list(self):
        raw = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello", "tool_calls": None},
        ]
        result = _messages_to_agent_messages(raw)
        assert len(result) == 3
        assert all(isinstance(m, AgentMessage) for m in result)
        assert result[0].role == "system"
        assert result[1].content == "hi"
        assert result[2].role == "assistant"

    def test_empty_list(self):
        assert _messages_to_agent_messages([]) == []

    def test_preserves_tool_call_id(self):
        raw = [{"role": "tool", "content": "{}", "tool_call_id": "abc", "name": "test"}]
        result = _messages_to_agent_messages(raw)
        assert result[0].tool_call_id == "abc"
        assert result[0].name == "test"


# ---------------------------------------------------------------------------
# 8. run_agent_loop
# ---------------------------------------------------------------------------


class TestRunAgentLoop:
    """Test the main agent loop by mocking _call_llm and tool execution."""

    def _make_config(self, **overrides):
        defaults = dict(api_key="sk-test", max_turns=5)
        defaults.update(overrides)
        return AgentConfig(**defaults)

    @mock.patch("kiln.agent_loop.get_all_tool_schemas", return_value=[])
    @mock.patch("kiln.agent_loop._call_llm")
    def test_returns_text_response(self, mock_llm, mock_schemas):
        """When LLM returns text with no tool calls, loop stops."""
        mock_llm.return_value = {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "The printer is online."},
                    "finish_reason": "stop",
                }
            ],
        }
        config = self._make_config()
        result = run_agent_loop("What is the printer status?", config)

        assert result.response == "The printer is online."
        assert result.turns == 1
        assert result.tool_calls_made == 0
        assert result.model == config.model

    @mock.patch("kiln.agent_loop.get_all_tool_schemas", return_value=[])
    @mock.patch("kiln.agent_loop._call_llm")
    def test_stops_after_max_turns(self, mock_llm, mock_schemas):
        """Loop should stop after max_turns even if LLM keeps calling tools."""
        # Always return a tool call, never a final response
        mock_llm.return_value = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {
                                    "name": "printer_status",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
        config = self._make_config(max_turns=3)

        with mock.patch("kiln.agent_loop._execute_tool_call", return_value='{"status":"ok"}'):
            result = run_agent_loop("status", config)

        assert result.turns == 3
        assert result.tool_calls_made == 3
        assert "maximum" in result.response.lower() or result.response != ""

    @mock.patch("kiln.agent_loop.get_all_tool_schemas", return_value=[])
    @mock.patch("kiln.agent_loop._call_llm")
    def test_executes_tool_calls_and_feeds_back(self, mock_llm, mock_schemas):
        """Loop should execute tool calls and feed results back to the LLM."""
        # First call: LLM requests a tool call
        # Second call: LLM returns final text
        mock_llm.side_effect = [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {
                                        "name": "printer_status",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "The printer is idle.",
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
        ]
        config = self._make_config()

        with mock.patch(
            "kiln.agent_loop._execute_tool_call",
            return_value='{"status": "idle"}',
        ):
            result = run_agent_loop("Check printer", config)

        assert result.response == "The printer is idle."
        assert result.turns == 2
        assert result.tool_calls_made == 1

    @mock.patch("kiln.agent_loop.get_all_tool_schemas", return_value=[])
    @mock.patch("kiln.agent_loop._call_llm")
    def test_tracks_token_usage(self, mock_llm, mock_schemas):
        mock_llm.return_value = {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "done"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"total_tokens": 250},
        }
        config = self._make_config()
        result = run_agent_loop("test", config)
        assert result.total_tokens == 250

    @mock.patch("kiln.agent_loop.get_all_tool_schemas", return_value=[])
    @mock.patch("kiln.agent_loop._call_llm")
    def test_empty_choices_raises(self, mock_llm, mock_schemas):
        mock_llm.return_value = {"choices": []}
        config = self._make_config()

        with pytest.raises(AgentLoopError, match="empty choices"):
            run_agent_loop("test", config)

    @mock.patch("kiln.agent_loop.get_all_tool_schemas", return_value=[])
    @mock.patch("kiln.agent_loop._call_llm")
    def test_uses_custom_system_prompt(self, mock_llm, mock_schemas):
        mock_llm.return_value = {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "response"},
                    "finish_reason": "stop",
                }
            ],
        }
        config = self._make_config(system_prompt="Custom instructions here")
        run_agent_loop("hi", config)

        # Check that the first message sent was our custom system prompt
        call_args = mock_llm.call_args
        messages = call_args[0][0]  # first positional arg
        assert messages[0]["role"] == "system"
        assert messages[0]["content"].startswith("Custom instructions here")

    @mock.patch("kiln.agent_loop.get_all_tool_schemas", return_value=[])
    @mock.patch("kiln.agent_loop._call_llm")
    def test_uses_default_system_prompt_when_none(self, mock_llm, mock_schemas):
        mock_llm.return_value = {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
        }
        config = self._make_config()
        run_agent_loop("hi", config)

        call_args = mock_llm.call_args
        messages = call_args[0][0]
        assert messages[0]["role"] == "system"
        assert "3D printing" in messages[0]["content"]

    @mock.patch("kiln.agent_loop.get_all_tool_schemas", return_value=[])
    @mock.patch("kiln.agent_loop._call_llm")
    def test_continues_existing_conversation(self, mock_llm, mock_schemas):
        # Capture messages at call time (list is mutated after _call_llm)
        captured_messages = []

        def capture_and_respond(msgs, tools, cfg):
            captured_messages.extend(list(msgs))  # snapshot
            return {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "continued"},
                        "finish_reason": "stop",
                    }
                ],
            }

        mock_llm.side_effect = capture_and_respond

        config = self._make_config()
        history = [
            AgentMessage(role="system", content="sys"),
            AgentMessage(role="user", content="first msg"),
            AgentMessage(role="assistant", content="first reply"),
        ]
        result = run_agent_loop("follow up", config, conversation=history)

        # At call time: 3 history + 1 new user message = 4
        assert len(captured_messages) == 4
        assert captured_messages[0]["content"] == "sys"
        assert captured_messages[3]["content"] == "follow up"


# ---------------------------------------------------------------------------
# 9. AgentLoopError
# ---------------------------------------------------------------------------


class TestAgentLoopError:
    def test_basic(self):
        err = AgentLoopError("Something failed")
        assert str(err) == "Something failed"
        assert err.status_code is None

    def test_with_status_code(self):
        err = AgentLoopError("Rate limited", status_code=429)
        assert err.status_code == 429


# ---------------------------------------------------------------------------
# 10. _get_default_system_prompt
# ---------------------------------------------------------------------------


class TestDefaultSystemPrompt:
    def test_contains_key_instructions(self):
        prompt = _get_default_system_prompt()
        assert "3D printing" in prompt
        assert "printer_status" in prompt
        assert "preflight_check" in prompt
        assert "fleet_status" in prompt



# ---------------------------------------------------------------------------
# Tier-aware error messages in _execute_tool
# ---------------------------------------------------------------------------


class TestExecuteToolTierAwareErrors:
    """Test that _execute_tool returns tier-aware errors for tools in higher tiers."""

    @mock.patch("kiln.agent_loop._ensure_tool_cache")
    def test_tool_in_higher_tier_returns_tier_error(self, mock_cache):
        """When an agent on 'essential' calls a tool only in 'full', the error
        message should include the required tier and suggest alternatives."""
        import kiln.agent_loop as al

        mock_cache.return_value = {}  # Tool not in cache (tier-filtered)
        old_tier = al._current_tier
        al._current_tier = "essential"
        try:
            with mock.patch(
                "kiln.tool_schema._find_tier_for_tool", return_value="full"
            ) as mock_find, mock.patch(
                "kiln.tool_schema._suggest_alternatives",
                return_value=["printer_status", "fleet_status"],
            ) as mock_suggest:
                result = _execute_tool("manage_webhooks", {})
                parsed = json.loads(result)

                assert parsed["success"] is False
                assert "'full'" in parsed["error"]
                assert "'essential'" in parsed["error"]
                assert "manage_webhooks" in parsed["error"]
                assert "printer_status" in parsed["error"]
                assert "fleet_status" in parsed["error"]

                mock_find.assert_called_once_with("manage_webhooks")
                mock_suggest.assert_called_once_with("manage_webhooks", "essential")
        finally:
            al._current_tier = old_tier

    @mock.patch("kiln.agent_loop._ensure_tool_cache")
    def test_tool_in_higher_tier_no_alternatives(self, mock_cache):
        """When no alternatives are found, the error omits the alternatives clause."""
        import kiln.agent_loop as al

        mock_cache.return_value = {}
        old_tier = al._current_tier
        al._current_tier = "essential"
        try:
            with mock.patch(
                "kiln.tool_schema._find_tier_for_tool", return_value="standard"
            ), mock.patch(
                "kiln.tool_schema._suggest_alternatives",
                return_value=[],
            ):
                result = _execute_tool("some_unique_tool", {})
                parsed = json.loads(result)

                assert parsed["success"] is False
                assert "'standard'" in parsed["error"]
                assert "'essential'" in parsed["error"]
                assert "Alternatives" not in parsed["error"]
        finally:
            al._current_tier = old_tier

    @mock.patch("kiln.agent_loop._ensure_tool_cache")
    def test_completely_nonexistent_tool_returns_generic_error(self, mock_cache):
        """When a tool doesn't exist in any tier, the generic 'Unknown tool'
        message is returned."""
        import kiln.agent_loop as al

        mock_cache.return_value = {}
        old_tier = al._current_tier
        al._current_tier = "essential"
        try:
            with mock.patch(
                "kiln.tool_schema._find_tier_for_tool", return_value=None
            ):
                result = _execute_tool("totally_fake_tool_xyz", {})
                parsed = json.loads(result)

                assert parsed["error"] == "Unknown tool: totally_fake_tool_xyz"
                assert parsed["success"] is False
        finally:
            al._current_tier = old_tier

    @mock.patch("kiln.agent_loop._ensure_tool_cache")
    def test_no_tier_set_returns_generic_error(self, mock_cache):
        """When _current_tier is None (no active loop), fall back to generic error."""
        import kiln.agent_loop as al

        mock_cache.return_value = {}
        old_tier = al._current_tier
        al._current_tier = None
        try:
            result = _execute_tool("some_tool", {})
            parsed = json.loads(result)

            assert parsed["error"] == "Unknown tool: some_tool"
            assert parsed["success"] is False
        finally:
            al._current_tier = old_tier
