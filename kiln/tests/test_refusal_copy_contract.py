"""A refusal has two readers, and neither one should get the other's half.

The defect this pins is not a typo, it is a shape.  Fourteen refusals across
``server.py`` and the plugin surface each hand-wrote their own copy, and every
one of them ended by telling a PERSON to run a terminal command — a person who
had asked for a coaster, through an assistant that could have run the command
itself.  Half of them called the command ``kiln login`` and half called it
``kiln signin``, for the identical action, because there were fourteen
definitions and so nothing to disagree with.

So these tests are deliberately about the CONTRACT rather than the wording:

* the half a person reads names the situation and carries no command syntax,
* the half the agent reads carries the command and says to RUN it,
* one command name, everywhere,

which leaves the copy free to be rewritten without touching this file, while a
fifteenth refusal that reintroduces the shape fails.  They exercise the real
registered tools and the real fallback gates, not the string builders alone —
the builders being right proves nothing about whether the tools call them.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from kiln.tiers_and_terms import (
    AGENT_ACCOUNT_NUDGE,
    AGENT_SIGNIN_HINT,
    ALREADY_SUBSCRIBED_LINE,
    SIGNIN_COMMAND,
    session_expired_message,
    signed_out_message,
    signin_hint_fields,
    tier_required_message,
)

# Shapes that mean "somebody is being handed a shell".  Backticks are the tell
# that matters most: every one of the original fourteen used them to quote a
# command, and nothing else in refusal copy needs code formatting.
_COMMAND_SHAPES = ("`", "python3 -m", "kiln signin", "kiln login", "kiln pair", "$ ")


def _assert_reads_as_prose(text: str, label: str) -> None:
    """Fail if *text* is a person-facing string carrying a command."""
    assert text, f"{label} is empty"
    for shape in _COMMAND_SHAPES:
        assert shape not in text, f"{label} hands the user a command: {shape!r} in {text!r}"


def _capture_tools(plugin_module: str) -> dict:
    """Register a plugin against a fake MCP and return its real tool callables."""
    plugin = importlib.import_module(plugin_module).plugin
    tools: dict = {}

    class FakeMCP:
        def tool(self_mcp):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

    plugin.register(FakeMCP())
    return tools


# ---------------------------------------------------------------------------
# The person-facing half
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        tier_required_message("decorate_surface", "pro"),
        tier_required_message("This feature", "business"),
        tier_required_message("The 'x' profile", "pro", "Free profiles: default"),
        session_expired_message(),
        session_expired_message("user@example.com"),
        signed_out_message(),
        ALREADY_SUBSCRIBED_LINE,
    ],
)
def test_user_facing_copy_carries_no_command(message):
    _assert_reads_as_prose(message, "refusal copy")


def test_tier_message_names_the_tier_and_where_to_read_more():
    msg = tier_required_message("decorate_surface", "pro")
    assert "decorate_surface" in msg
    assert "Kiln Pro" in msg
    assert "kiln3d.com/pricing" in msg


def test_tier_message_puts_the_free_alternative_before_the_pricing_link():
    """The only sentence that helps a user who will not upgrade goes first.

    A reader who has already reached a pricing URL has stopped reading, so an
    alternative appended after it is an alternative nobody sees.
    """
    msg = tier_required_message("SVG decoration", "pro", "PNG and JPG still work")
    assert msg.index("PNG and JPG still work") < msg.index("kiln3d.com/pricing")


# ---------------------------------------------------------------------------
# The agent-facing half
# ---------------------------------------------------------------------------


def test_agent_hint_carries_the_command_and_says_to_run_it():
    fields = signin_hint_fields()
    assert fields["setup_hint"] == SIGNIN_COMMAND == "kiln signin"
    assert SIGNIN_COMMAND in fields["agent_hint"]
    lowered = fields["agent_hint"].lower()
    assert "run" in lowered
    # The instruction that stops the agent relaying the command onward.
    assert "do not ask them to type" in lowered


def test_agent_hint_fields_are_not_a_shared_mutable():
    first = signin_hint_fields()
    first["agent_hint"] = "clobbered"
    assert signin_hint_fields()["agent_hint"] == AGENT_SIGNIN_HINT


def test_account_nudge_points_the_agent_at_the_tool_not_the_terminal():
    assert "kiln_signin" in AGENT_ACCOUNT_NUDGE
    assert "rather than asking them to type a command" in AGENT_ACCOUNT_NUDGE
    # Nothing is blocked by being signed out, so the pacing survives edits.
    assert "never block work on it" in AGENT_ACCOUNT_NUDGE


# ---------------------------------------------------------------------------
# The real refusal paths
# ---------------------------------------------------------------------------


def _kiln_pro_licensing_present() -> bool:
    """True when kiln-pro supplies ``kiln.licensing`` on this machine.

    Public Kiln ships no ``licensing`` module, so on a free install — and in
    this repo's CI — ``server.py``'s fallback stubs ARE the live tier gate.
    With kiln-pro installed (a developer machine) the real implementation wins
    and there is nothing local left to assert.
    """
    try:
        import kiln.licensing  # noqa: F401
    except ImportError:
        return False
    return True


_needs_free_install = pytest.mark.skipif(
    _kiln_pro_licensing_present(),
    reason="kiln-pro supplies kiln.licensing here; the free-install fallback "
    "under test is only reachable without it (see the structural test below, "
    "which pins the same wiring on every machine)",
)


@_needs_free_install
def test_free_tier_gate_splits_its_refusal_by_audience():
    """``requires_tier`` is the gate a free install actually hits."""
    import kiln.server as server

    @server.requires_tier("pro")
    def some_pro_tool():  # pragma: no cover - refused before the body runs
        return {"success": True}

    result = some_pro_tool()
    assert result["success"] is False
    assert result["code"] == "TIER_REQUIRED"
    _assert_reads_as_prose(result["error"], "requires_tier error")
    assert "some_pro_tool" in result["error"]
    assert SIGNIN_COMMAND in result["agent_hint"]
    assert result["setup_hint"] == SIGNIN_COMMAND


@_needs_free_install
def test_free_tier_check_returns_prose_for_the_caller_to_show():
    import kiln.server as server

    ok, message = server.check_tier("business")
    assert ok is False
    _assert_reads_as_prose(message, "check_tier message")
    assert "Business" in message


def test_free_tier_fallback_gates_delegate_to_the_shared_copy():
    """Pin the WIRING on every machine, including where kiln-pro shadows it.

    A shared builder nobody calls is the same bug with extra steps, and the
    behavioural tests above cannot run on a developer machine — so this reads
    the fallback block itself and proves both gates route through the one
    definition instead of re-typing a refusal.
    """
    import inspect

    import kiln.server as server

    src = inspect.getsource(server)
    head = src[: src.index("from kiln.log_config import")]
    assert head.count("tier_required_message(") == 2, (
        "both fallback gates (check_tier, requires_tier) must build their "
        "message from the shared builder"
    )
    assert "**signin_hint_fields()" in head, (
        "the requires_tier fallback must carry the agent-addressed fields"
    )


@pytest.mark.parametrize(
    ("plugin_module", "tool_name", "error_key"),
    [
        ("kiln.plugins.consumer_tools", "tax_estimate", "error"),
        ("kiln.plugins.consumer_tools", "tax_jurisdictions", "error"),
        ("kiln.plugins.consumer_tools", "tax_jurisdiction_lookup", "error"),
    ],
)
def test_pro_only_tools_refuse_without_handing_over_a_terminal(
    plugin_module, tool_name, error_key, monkeypatch
):
    """Exercise the registered tool, not the builder underneath it.

    These refuse on ``ImportError`` for kiln-pro, which is the state of every
    free install, so the refusal is reached without any mocking beyond making
    the import fail on a machine that happens to have kiln-pro present.
    """
    import builtins

    real_import = builtins.__import__

    def _no_kiln_pro(name, *args, **kwargs):
        if name.startswith("kiln_pro"):
            raise ImportError("kiln-pro not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_kiln_pro)

    tools = _capture_tools(plugin_module)
    tool = tools[tool_name]
    result = tool("US-CA") if tool_name == "tax_jurisdiction_lookup" else tool()

    assert result["status"] == "error"
    _assert_reads_as_prose(result[error_key], f"{tool_name} error")
    assert SIGNIN_COMMAND in result["agent_hint"]
    assert result["setup_hint"] == SIGNIN_COMMAND


def test_license_status_expired_session_splits_by_audience(monkeypatch):
    from kiln.server import _annotate_session_liveness

    class _Session:
        token = ""
        state = "signed_out"
        detail = ""

    monkeypatch.setattr(
        "kiln.auth_session.resolve_session_bearer", lambda *a, **k: _Session()
    )
    payload = {"source": "oauth", "tier": "pro", "is_valid": True}
    _annotate_session_liveness(payload)

    assert payload["is_valid"] is False
    _assert_reads_as_prose(payload["action_required"], "action_required")
    assert SIGNIN_COMMAND in payload["agent_hint"]


# ---------------------------------------------------------------------------
# One command, one name
# ---------------------------------------------------------------------------


def test_no_shipping_source_says_kiln_login():
    """``kiln login`` is a real alias and keeps working; it is not what we SAY.

    Scoped to the surfaces a person meets through an agent.  ``cli/`` is
    excluded on purpose — those messages address someone already at a prompt,
    where naming a command is correct — and ``tiers_and_terms`` is excluded
    because it documents the alias and this history on purpose.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "kiln"
    offenders = []
    for path in src.rglob("*.py"):
        rel = path.relative_to(src)
        if rel.parts[0] == "cli" or rel.name == "tiers_and_terms.py":
            continue
        for lineno, line in enumerate(
            path.read_text(errors="replace").splitlines(), start=1
        ):
            if "kiln login" in line:
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, "say `kiln signin`, not `kiln login`:\n" + "\n".join(offenders)
