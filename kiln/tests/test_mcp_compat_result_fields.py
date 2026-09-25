"""A tool result's structured content is read and written through ``mcp_compat``.

SDK 2 renamed ``CallToolResult``'s fields ``structured_content`` and
``is_error`` and kept the wire names only as aliases.  Every mutator on the
``tools/call`` chain -- the stage, the monitor, the update and onboarding
nudges, the standing-window note -- read and wrote ``structuredContent`` by
attribute.  On SDK 2 the read found nothing and the write raised (the result
is a pydantic model with no field of that name); each mutator swallows its
own errors by design, so the result reached the host without the thing it
was meant to carry, and nothing said so.  CI pins SDK 2 and stayed green,
because every test handed the mutators an SDK 1-shaped stand-in.

So each check here runs a result of BOTH shapes, the installed SDK's real
type, and real mutators through a real client.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import anyio
import pytest

from kiln.mcp_compat import (
    FastMCP,
    result_is_error,
    result_structured_content,
    set_result_structured_content,
)


def _blocks(payload: dict) -> list:
    return [SimpleNamespace(type="text", text=json.dumps(payload))]


def _sdk1_result(*, structured=None, is_error=False, payload=None):
    return SimpleNamespace(
        content=_blocks(payload or {"success": True}),
        structuredContent=structured,
        isError=is_error,
    )


def _sdk2_result(*, structured=None, is_error=False, payload=None):
    return SimpleNamespace(
        content=_blocks(payload or {"success": True}),
        structured_content=structured,
        is_error=is_error,
    )


_SHAPES = pytest.mark.parametrize("make", [_sdk1_result, _sdk2_result], ids=["sdk1", "sdk2"])


class TestTheAccessors:
    @_SHAPES
    def test_reads_whichever_attribute_the_sdk_uses(self, make):
        assert result_structured_content(make(structured={"a": 1})) == {"a": 1}
        assert result_structured_content(make()) is None

    @_SHAPES
    def test_writes_back_under_the_same_attribute(self, make):
        result = make()
        set_result_structured_content(result, {"a": 1})
        assert result_structured_content(result) == {"a": 1}
        assert set(vars(result)) == set(vars(make()))

    @_SHAPES
    def test_reads_the_error_flag_on_either_shape(self, make):
        assert result_is_error(make(is_error=True)) is True
        assert result_is_error(make()) is False

    def test_an_object_with_neither_is_none_not_an_error_and_refuses_a_write(self):
        bare = SimpleNamespace(content=[])
        assert result_structured_content(bare) is None
        assert result_is_error(bare) is False
        with pytest.raises(AttributeError):
            set_result_structured_content(bare, {})

    def test_the_installed_sdks_result_carries_the_write_onto_the_wire(self):
        from mcp.types import CallToolResult, TextContent

        result = CallToolResult(content=[TextContent(type="text", text="{}")])
        set_result_structured_content(result, {"kiln_update": {"latest": "9.9.9"}})
        assert result.model_dump(by_alias=True)["structuredContent"] == {
            "kiln_update": {"latest": "9.9.9"}
        }
        failed = CallToolResult(content=[], isError=True)
        assert result_is_error(failed) is True


# ---------------------------------------------------------------------------
# The note mutators, on both shapes
# ---------------------------------------------------------------------------


_INFO = {
    "available": True,
    "current": "1.1.9",
    "latest": "1.3.2",
    "command": "pip install --upgrade kiln3d",
    "summary": "Kiln 1.3.2 is available",
    "offer": "Want me to update Kiln for you now?",
    "action": "upgrade_kiln",
}


@pytest.fixture
def update_nudge_on(monkeypatch):
    from kiln import update_nudge

    update_nudge._reset_for_tests()
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    monkeypatch.setattr(update_nudge, "_update_checks_on", lambda: True)
    monkeypatch.setattr("kiln.version_check.check_for_update", lambda *a, **k: _INFO)
    monkeypatch.setattr("kiln.daily_stats.record_update_nudge", lambda *a, **k: None)
    yield update_nudge
    update_nudge._reset_for_tests()


@pytest.fixture
def onboarding_nudge_on(monkeypatch):
    from kiln import onboarding_nudge

    onboarding_nudge._reset_for_tests()
    monkeypatch.delenv("KILN_NO_ONBOARDING_NUDGE", raising=False)
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    monkeypatch.setattr("kiln.daily_stats.record_event", lambda *a, **k: None)
    yield onboarding_nudge
    onboarding_nudge._reset_for_tests()


class TestTheNotesLandOnEitherShape:
    @_SHAPES
    def test_the_update_nudge(self, make, update_nudge_on):
        result = make()
        update_nudge_on._attach(result, None, "list_materials")
        sc = result_structured_content(result)
        assert sc[update_nudge_on.RESULT_KEY]["latest"] == "1.3.2"
        assert sc["success"] is True
        assert set(vars(result)) == set(vars(make()))

    @_SHAPES
    def test_the_onboarding_nudge(self, make, onboarding_nudge_on):
        result = make()
        onboarding_nudge_on._attach(result, None, "list_materials")
        sc = result_structured_content(result)
        assert sc[onboarding_nudge_on.RESULT_KEY]["note"]
        assert sc["success"] is True

    @_SHAPES
    def test_the_standing_window_note(self, make, monkeypatch):
        from kiln import consent_window_note, server

        tool = next(iter(server._CONSENT_FILE_ARG))
        block = {"note": "prints on default are allowed until 21:00"}
        monkeypatch.setattr(consent_window_note, "note_for", lambda aimed: block)
        result = make()
        consent_window_note._attach(result, None, tool, {"printer_name": "default"})
        sc = result_structured_content(result)
        assert sc[consent_window_note.RESULT_KEY] == block
        assert sc["success"] is True

    @_SHAPES
    def test_an_error_result_carries_no_note_on_either_shape(self, make, update_nudge_on):
        result = make(is_error=True)
        update_nudge_on._attach(result, None, "list_materials")
        assert result_structured_content(result) is None


# ---------------------------------------------------------------------------
# Real mutators, a real server, a real client -- the installed SDK end to end
# ---------------------------------------------------------------------------


def _server_with_a_tool():
    mcp = FastMCP("result-fields")

    @mcp.tool(name="list_materials")
    def list_materials() -> dict:
        return {"success": True, "materials": ["PLA"]}

    return mcp


def _call(mcp, name: str, arguments: dict | None = None):
    """``tools/call`` the way a host sends it.  SDK 2 connects ``Client`` to
    a server object directly; 1.x has the memory-stream helper that 2.x
    removed."""
    try:
        from mcp import Client
    except ImportError:
        from mcp.shared.memory import create_connected_server_and_client_session

        def _client():
            return create_connected_server_and_client_session(mcp)
    else:

        def _client():
            return Client(mcp)

    async def _once():
        async with _client() as client:
            return await client.call_tool(name, arguments or {})

    return anyio.run(_once)


class TestTheClientReceivesTheNote:
    def test_the_update_nudge_reaches_the_client(self, update_nudge_on):
        mcp = _server_with_a_tool()
        assert update_nudge_on.install(mcp)
        sc = result_structured_content(_call(mcp, "list_materials"))
        assert isinstance(sc, dict), "the result reached the client with no structured content"
        assert sc[update_nudge_on.RESULT_KEY]["latest"] == "1.3.2"
        assert sc["materials"] == ["PLA"]

    def test_the_onboarding_nudge_reaches_the_client(self, onboarding_nudge_on):
        mcp = _server_with_a_tool()
        assert onboarding_nudge_on.install(mcp)
        sc = result_structured_content(_call(mcp, "list_materials"))
        assert isinstance(sc, dict), "the result reached the client with no structured content"
        assert sc[onboarding_nudge_on.RESULT_KEY]["note"]
        assert sc["materials"] == ["PLA"]
