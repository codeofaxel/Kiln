"""The bridge on a customer's computer runs only the tools on its own list.

The server decides which tools it relays, but this computer does not take its
word for it: a relayed frame naming a tool outside ``BRIDGE_TOOLS``, or
carrying an argument in ``BRIDGE_REFUSED_ARGS``, is refused here before any
fetch, any consent, or any tool runs, and the refusal carries a code the
server can recognise.  These tests drive the real request handler (and, for
the last class, the real tool caller over the real registry).
"""

from __future__ import annotations

import pytest

from kiln import bridge_client
from kiln.bridge_client import (
    BRIDGE_REFUSED_ARGS,
    BRIDGE_REFUSED_CODE,
    BRIDGE_TOOLS,
    handle_relay_request,
)

#: Tools a server must never be able to run on a customer's computer: raw
#: G-code, heat, fans, safety limits, firmware, the credential vault, the
#: install itself, consent and autonomy settings, and outbound hooks.
NEVER_OVER_THE_BRIDGE = (
    "send_gcode",
    "set_temperature",
    "set_fan",
    "home_axes",
    "add_safety_profile",
    "select_printer_variant",
    "update_machine_firmware",
    "roll_back_machine_firmware",
    "retrieve_credential",
    "list_credentials",
    "list_trusted_printers",
    "upgrade_kiln",
    "restart_server",
    "set_autonomy_level",
    "revoke_consent_window",
    "register_webhook",
    "start_print",
    "confirm_action",
    "issue_preview_token",
)


def _approval() -> dict:
    return {"kind": "approval", "id": "pa_1", "grantor": "account:1", "door": "stage"}


@pytest.mark.parametrize("tool", NEVER_OVER_THE_BRIDGE)
def test_a_tool_off_the_list_is_refused_even_when_the_server_names_it(tool):
    ran: list[str] = []
    fetched: list[str] = []
    resp = handle_relay_request(
        {
            "request_id": "r1",
            "tool_name": tool,
            "args": {"commands": "M104 S300", "cloud_artifact_token": "t", "print_authority": _approval()},
        },
        call_tool=lambda name, args: ran.append(name),
        fetch_artifact=lambda token: fetched.append(token) or "/tmp/x.stl",
    )
    assert resp["ok"] is False
    assert resp["request_id"] == "r1"
    assert resp["error"]["code"] == BRIDGE_REFUSED_CODE
    assert resp["error"]["tool"] == tool
    assert ran == [] and fetched == []


def test_a_refused_call_never_sets_a_consent(monkeypatch):
    from kiln import print_consent

    calls: list[object] = []
    monkeypatch.setattr(print_consent, "set_consent", lambda c: calls.append(c))
    handle_relay_request(
        {"request_id": "r2", "tool_name": "send_gcode", "args": {"print_authority": _approval()}},
        call_tool=lambda name, args: None,
        fetch_artifact=lambda token: "",
    )
    assert calls == []


@pytest.mark.parametrize(
    ("tool", "arg"),
    sorted((tool, arg) for tool, args in BRIDGE_REFUSED_ARGS.items() for arg in args),
)
def test_an_argument_naming_a_file_here_is_refused(tool, arg):
    ran: list[str] = []
    resp = handle_relay_request(
        {"request_id": "r3", "tool_name": tool, "args": {arg: "/Users/someone/secret.3mf"}},
        call_tool=lambda name, args: ran.append(name),
        fetch_artifact=lambda token: "/tmp/model.stl",
    )
    assert resp["ok"] is False and resp["error"]["code"] == BRIDGE_REFUSED_CODE
    assert arg in resp["error"]["message"]
    assert ran == []


def test_a_listed_tool_still_runs():
    ran: list[tuple[str, dict]] = []
    resp = handle_relay_request(
        {"request_id": "r4", "tool_name": "pause_print", "args": {"printer_name": "garage"}},
        call_tool=lambda name, args: ran.append((name, args)) or {"success": True},
        fetch_artifact=lambda token: "",
    )
    assert resp["ok"] is True
    assert ran == [("pause_print", {"printer_name": "garage"})]


def test_slice_and_print_from_a_saved_make_still_runs():
    ran: list[tuple[str, dict]] = []
    resp = handle_relay_request(
        {"request_id": "r5", "tool_name": "slice_and_print", "args": {"cloud_artifact_token": "tok"}},
        call_tool=lambda name, args: ran.append((name, args)) or {"success": True},
        fetch_artifact=lambda token: "/tmp/model.stl",
    )
    assert resp["ok"] is True
    assert ran[0][1]["input_path"] == "/tmp/model.stl"


class TestTheRealCaller:
    """The real tool caller over the real registry, so a second way into it
    cannot run a tool the list leaves out, and every listed name is a tool."""

    @pytest.fixture()
    def caller(self, monkeypatch):
        from kiln import server

        monkeypatch.setattr(server, "ensure_runtime_config", lambda: None)
        monkeypatch.setattr(server, "register_configured_printers", lambda: None)
        return bridge_client._default_tool_caller()

    def test_the_caller_refuses_a_tool_off_the_list(self, caller):
        with pytest.raises(ValueError, match="does not run send_gcode"):
            caller("send_gcode", {"commands": "M104 S300"})

    def test_through_the_handler_the_tool_body_never_runs(self, caller, monkeypatch):
        from kiln import server

        tool = server.mcp._tool_manager._tools["send_gcode"]
        ran: list[dict] = []
        monkeypatch.setattr(tool, "fn", lambda **kw: ran.append(kw) or {"success": True})
        resp = handle_relay_request(
            {"request_id": "r6", "tool_name": "send_gcode", "args": {"commands": "M104 S300"}},
            call_tool=caller,
            fetch_artifact=lambda token: "",
        )
        assert resp["ok"] is False and resp["error"]["code"] == BRIDGE_REFUSED_CODE
        assert ran == []

    def test_every_listed_tool_is_a_registered_tool(self, caller):
        from kiln import server

        registered = set(server.mcp._tool_manager._tools)
        assert sorted(BRIDGE_TOOLS - registered) == []

    def test_no_listed_tool_is_one_that_must_never_cross(self):
        assert sorted(BRIDGE_TOOLS & set(NEVER_OVER_THE_BRIDGE)) == []


#: An argument that names a place on this computer, or that stands for the
#: person or a safety check here: a confirmation, a forced or skipped check,
#: heaters kept on.  Judged by name over every listed tool's real signature.
_LOCAL_ONLY_ARGUMENT = (
    r"(^|_)(path|paths|file|files|dir|folder|profile)$"
    r"|^force$|^skip_|_confirmed$|^preserve_|^keep_|^leave_|^bypass|^override"
)

#: Arguments of that kind a relayed call still carries, each with its reason.
#: A confirmation the server carries is one a server that is not Kiln's could
#: make up; that is the separate item "print approvals the server cannot
#: forge", not a reason to break the flow below.
_CARRIED_ON_PURPOSE = {
    ("set_printer_model", "bed_confirmed"): (
        "the bed size a person confirmed in Kiln's web app or a connector chat; a print on a "
        "printer outside the catalogue waits for it (kiln-pro "
        "tests/test_relay_safe_tools_registry.py)"
    ),
}


def test_every_listed_tool_refuses_what_only_this_computer_may_give():
    """No tool on the bridge's list takes a file location or a person's or
    safety check's say from a relayed call.

    Reads every listed tool's real signature: an argument that names a place
    on this computer, or a confirmation, a forced or skipped check, or heaters
    kept on, is refused for a relayed call (``BRIDGE_REFUSED_ARGS``) or carried
    on purpose with its reason (``_CARRIED_ON_PURPOSE``).
    """
    import re

    from kiln import server as _server

    pattern = re.compile(_LOCAL_ONLY_ARGUMENT)
    tools = {tool.name: tool for tool in _server.mcp._tool_manager.list_tools()}
    missing = []
    for name in sorted(BRIDGE_TOOLS):
        tool = tools.get(name)
        if tool is None:
            continue  # registered only where its plugin loads; judged where it does
        for arg in sorted((tool.parameters or {}).get("properties", {})):
            if (pattern.search(arg) and arg not in BRIDGE_REFUSED_ARGS.get(name, frozenset())
                    and (name, arg) not in _CARRIED_ON_PURPOSE):
                missing.append(f"{name}({arg})")
    assert not missing, f"listed tools take arguments only this computer may give: {missing}"
