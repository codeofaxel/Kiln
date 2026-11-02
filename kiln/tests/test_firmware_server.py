"""Tests for the machine firmware tools (plugins/firmware_tools.py).

Covers:
- check_machine_firmware -- component versions, one machine or several
- update_machine_firmware -- start an update, one machine or several
- roll_back_machine_firmware -- return a component to its earlier version
- the fleet half (several machines in one call) is Kiln Business, and one
  machine at a time stays open on every plan
- the six names these replaced answer with a pointer to the new tool
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kiln.printers.base import (
    FirmwareComponent,
    FirmwareStatus,
    FirmwareUpdateResult,
    PrinterCapabilities,
    PrinterError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_caps(can_update: bool = True) -> PrinterCapabilities:
    return PrinterCapabilities(can_update_firmware=can_update)


def _make_firmware_status(
    busy: bool = False,
    updates: int = 1,
) -> FirmwareStatus:
    return FirmwareStatus(
        busy=busy,
        updates_available=updates,
        components=[
            FirmwareComponent(
                name="klipper",
                current_version="v0.12.0",
                remote_version="v0.12.1",
                update_available=True,
                component_type="git_repo",
                channel="stable",
            ),
        ],
    )


def _make_update_result(
    success: bool = True,
    component: str | None = None,
) -> FirmwareUpdateResult:
    return FirmwareUpdateResult(
        success=success,
        message="Update started." if success else "Failed.",
        component=component,
    )


def _adapter(can_update: bool = True) -> MagicMock:
    adapter = MagicMock()
    adapter.capabilities = _make_caps(can_update=can_update)
    adapter.get_firmware_status.return_value = _make_firmware_status()
    adapter.update_firmware.return_value = _make_update_result()
    adapter.rollback_firmware.return_value = _make_update_result(component="klipper")
    return adapter


def _fleet(machines: dict[str, MagicMock]):
    """Patch the lookup and the registry onto *machines*."""
    registry = MagicMock()
    registry.list_machines.return_value = sorted(machines)

    def resolve(name=None):
        return machines[name]

    return (
        patch("kiln.server._resolve_adapter", side_effect=resolve),
        patch("kiln.server._get_registry", return_value=registry),
    )


def _plan(covers: bool):
    """Patch the plan check: Business (covers) or below it."""
    verdict = (True, None) if covers else (
        False,
        "This feature needs Kiln Business. Otherwise, see what Business includes at kiln3d.com/pricing",
    )
    return patch("kiln.licensing.check_tier", return_value=verdict)


# ---------------------------------------------------------------------------
# Plugin registration fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def tools():
    """Register the firmware plugin on a mock MCP and return captured tools."""
    captured: dict[str, callable] = {}

    class _MockMCP:
        def tool(self, **_kwargs):
            def decorator(fn):
                captured[fn.__name__] = fn
                return fn
            return decorator

    from kiln.plugins.firmware_tools import plugin

    plugin.register(_MockMCP())
    return captured


@pytest.fixture(autouse=True)
def _auth_open():
    with patch("kiln.server._check_auth", return_value=None):
        yield


def test_the_plugin_registers_exactly_the_three_machine_tools(tools) -> None:
    assert set(tools) == {
        "check_machine_firmware",
        "update_machine_firmware",
        "roll_back_machine_firmware",
    }


# ---------------------------------------------------------------------------
# check_machine_firmware -- one machine
# ---------------------------------------------------------------------------


class TestCheckOneMachine:
    def test_the_connected_machine_when_none_is_named(self, tools) -> None:
        adapter = _adapter()
        with patch("kiln.server._resolve_adapter", return_value=adapter) as resolve:
            result = tools["check_machine_firmware"]()

        resolve.assert_called_once_with(None)
        assert result["success"] is True
        assert result["updates_available"] == 1
        assert result["components"][0]["name"] == "klipper"
        assert result["components"][0]["remote_version"] == "v0.12.1"
        assert "machine" not in result

    def test_a_named_machine_is_looked_up_and_named_back(self, tools) -> None:
        adapter = _adapter()
        with patch("kiln.server._resolve_adapter", return_value=adapter) as resolve:
            result = tools["check_machine_firmware"](machine="voron")

        resolve.assert_called_once_with("voron")
        assert result["success"] is True
        assert result["machine"] == "voron"

    def test_one_name_in_the_list_is_one_machine_on_any_plan(self, tools) -> None:
        adapter = _adapter()
        with _plan(covers=False), \
             patch("kiln.server._resolve_adapter", return_value=adapter) as resolve:
            result = tools["check_machine_firmware"](machines=["voron"])

        resolve.assert_called_once_with("voron")
        assert result["success"] is True

    def test_unsupported_machine(self, tools) -> None:
        with patch("kiln.server._resolve_adapter", return_value=_adapter(can_update=False)):
            result = tools["check_machine_firmware"]()

        assert result["success"] is False
        assert result["error"]["code"] == "UNSUPPORTED"

    def test_status_unavailable(self, tools) -> None:
        adapter = _adapter()
        adapter.get_firmware_status.return_value = None
        with patch("kiln.server._resolve_adapter", return_value=adapter):
            result = tools["check_machine_firmware"]()

        assert result["success"] is False
        assert result["error"]["code"] == "UNAVAILABLE"

    def test_no_machine_set_up_is_said_not_logged_as_a_fault(self, tools, caplog) -> None:
        missing = RuntimeError("No printer is set up in Kiln yet. Run `kiln setup` to add one.")
        with patch("kiln.server._resolve_adapter", side_effect=missing), \
             caplog.at_level("ERROR", logger="kiln.plugins.firmware_tools"):
            result = tools["check_machine_firmware"]()

        assert result["success"] is False
        assert result["error"]["code"] == "NO_PRINTER_CONFIGURED"
        assert not caplog.records

    def test_an_unknown_name_is_said(self, tools) -> None:
        from kiln.registry import PrinterNotFoundError

        with patch("kiln.server._resolve_adapter", side_effect=PrinterNotFoundError("ghost")):
            result = tools["check_machine_firmware"](machine="ghost")

        assert result["success"] is False
        assert "ghost" in result["error"]["message"]

    def test_printer_error_handled(self, tools) -> None:
        adapter = _adapter()
        adapter.get_firmware_status.side_effect = PrinterError("offline")
        with patch("kiln.server._resolve_adapter", return_value=adapter):
            result = tools["check_machine_firmware"]()

        assert result["success"] is False
        assert "offline" in result["error"]["message"]


# ---------------------------------------------------------------------------
# update_machine_firmware / roll_back_machine_firmware -- one machine
# ---------------------------------------------------------------------------


class TestChangeOneMachine:
    def test_update_the_connected_machine(self, tools) -> None:
        adapter = _adapter()
        with patch("kiln.server._resolve_adapter", return_value=adapter):
            result = tools["update_machine_firmware"]()

        assert result["success"] is True
        adapter.update_firmware.assert_called_once_with(component=None)

    def test_update_one_component_on_a_named_machine(self, tools) -> None:
        adapter = _adapter()
        adapter.update_firmware.return_value = _make_update_result(component="klipper")
        with patch("kiln.server._resolve_adapter", return_value=adapter) as resolve:
            result = tools["update_machine_firmware"](component="klipper", machine="voron")

        resolve.assert_called_once_with("voron")
        adapter.update_firmware.assert_called_once_with(component="klipper")
        assert result["component"] == "klipper"
        assert result["machine"] == "voron"

    def test_update_refused_mid_print_says_why(self, tools) -> None:
        adapter = _adapter()
        adapter.update_firmware.side_effect = PrinterError(
            "Cannot update firmware while printing. Wait for the current print to finish."
        )
        with patch("kiln.server._resolve_adapter", return_value=adapter):
            result = tools["update_machine_firmware"]()

        assert result["success"] is False
        assert "while printing" in result["error"]["message"]

    def test_update_unsupported(self, tools) -> None:
        with patch("kiln.server._resolve_adapter", return_value=_adapter(can_update=False)):
            result = tools["update_machine_firmware"]()

        assert result["error"]["code"] == "UNSUPPORTED"

    def test_update_auth_rejected_before_anything_is_touched(self, tools) -> None:
        adapter = _adapter()
        denied = {"success": False, "error": {"code": "AUTH", "message": "denied"}}
        with patch("kiln.server._check_auth", return_value=denied), \
             patch("kiln.server._resolve_adapter", return_value=adapter):
            result = tools["update_machine_firmware"]()

        assert result is denied
        adapter.update_firmware.assert_not_called()

    def test_roll_back_a_component(self, tools) -> None:
        adapter = _adapter()
        with patch("kiln.server._resolve_adapter", return_value=adapter):
            result = tools["roll_back_machine_firmware"](component="klipper")

        assert result["success"] is True
        assert result["component"] == "klipper"
        adapter.rollback_firmware.assert_called_once_with("klipper")

    def test_roll_back_error_points_at_the_check(self, tools) -> None:
        adapter = _adapter()
        adapter.rollback_firmware.side_effect = PrinterError("no rollback version")
        with patch("kiln.server._resolve_adapter", return_value=adapter):
            result = tools["roll_back_machine_firmware"](component="klipper")

        assert result["success"] is False
        assert "check_machine_firmware" in result["error"]["message"]


# ---------------------------------------------------------------------------
# Arguments that cannot be served
# ---------------------------------------------------------------------------


class TestArguments:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"machine": "a", "machines": ["b", "c"]},
            {"machine": "a", "all_machines": True},
            {"machines": ["a", "b"], "all_machines": True},
        ],
    )
    def test_more_than_one_way_of_naming_machines_is_refused(self, tools, kwargs) -> None:
        adapter = _adapter()
        with patch("kiln.server._resolve_adapter", return_value=adapter):
            result = tools["update_machine_firmware"](**kwargs)

        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_ARGS"
        adapter.update_firmware.assert_not_called()

    def test_an_empty_list_is_refused(self, tools) -> None:
        result = tools["check_machine_firmware"](machines=["", "  "])

        assert result["error"]["code"] == "INVALID_ARGS"


# ---------------------------------------------------------------------------
# The fleet half: several machines in one call
# ---------------------------------------------------------------------------


class TestFleetBelowBusiness:
    @pytest.mark.parametrize(
        ("tool", "kwargs", "subject"),
        [
            ("check_machine_firmware", {"all_machines": True}, "Checking several machines"),
            ("update_machine_firmware", {"machines": ["a", "b"]}, "Updating several machines"),
            ("roll_back_machine_firmware", {"component": "klipper", "all_machines": True},
             "Rolling back several machines"),
        ],
    )
    def test_refused_by_plan_naming_what_was_asked_and_what_still_works(
        self, tools, tool, kwargs, subject,
    ) -> None:
        machines = {"a": _adapter(), "b": _adapter()}
        lookup, registry = _fleet(machines)
        with _plan(covers=False), lookup, registry:
            result = tools[tool](**kwargs)

        assert result["success"] is False
        assert result["code"] == "TIER_REQUIRED"
        assert result["required_tier"] == "business"
        assert result["error"].startswith(subject)
        assert result["error"].endswith("One machine at a time works on every plan.")
        assert "pricing One" not in result["error"]
        for adapter in machines.values():
            adapter.update_firmware.assert_not_called()
            adapter.rollback_firmware.assert_not_called()
            adapter.get_firmware_status.assert_not_called()


class TestFleetOnBusiness:
    def test_all_machines_checks_each_machine_the_registry_holds(self, tools) -> None:
        machines = {"a": _adapter(), "b": _adapter(can_update=False)}
        lookup, registry = _fleet(machines)
        with _plan(covers=True), lookup, registry:
            result = tools["check_machine_firmware"](all_machines=True)

        assert [r["machine"] for r in result["machines"]] == ["a", "b"]
        assert result["machines"][0]["success"] is True
        assert result["machines"][1]["error"]["code"] == "UNSUPPORTED"
        assert result["summary"] == "1 of 2 machine(s) done."
        assert result["success"] is True

    def test_a_printing_machine_is_left_alone_and_the_rest_are_updated(self, tools) -> None:
        busy = _adapter()
        busy.update_firmware.side_effect = PrinterError(
            "Cannot update firmware while printing. Wait for the current print to finish."
        )
        machines = {"a": _adapter(), "busy": busy, "c": _adapter()}
        lookup, registry = _fleet(machines)
        with _plan(covers=True), lookup, registry:
            result = tools["update_machine_firmware"](component="klipper", all_machines=True)

        by_name = {r["machine"]: r for r in result["machines"]}
        assert by_name["a"]["success"] is True
        assert by_name["c"]["success"] is True
        assert by_name["busy"]["success"] is False
        assert "while printing" in by_name["busy"]["error"]["message"]
        machines["a"].update_firmware.assert_called_once_with(component="klipper")
        machines["c"].update_firmware.assert_called_once_with(component="klipper")
        assert result["summary"] == "2 of 3 machine(s) done."

    def test_named_machines_only_and_each_once(self, tools) -> None:
        machines = {"a": _adapter(), "b": _adapter(), "c": _adapter()}
        lookup, registry = _fleet(machines)
        with _plan(covers=True), lookup, registry:
            result = tools["roll_back_machine_firmware"](
                component="klipper", machines=["a", "c", "a"],
            )

        assert [r["machine"] for r in result["machines"]] == ["a", "c"]
        machines["a"].rollback_firmware.assert_called_once_with("klipper")
        machines["b"].rollback_firmware.assert_not_called()

    def test_an_unknown_name_is_one_failed_line_not_a_failed_call(self, tools) -> None:
        machines = {"a": _adapter()}

        def resolve(name=None):
            if name not in machines:
                raise PrinterError(f"No printer named {name!r}.")
            return machines[name]

        with _plan(covers=True), patch("kiln.server._resolve_adapter", side_effect=resolve):
            result = tools["check_machine_firmware"](machines=["a", "ghost"])

        by_name = {r["machine"]: r for r in result["machines"]}
        assert by_name["a"]["success"] is True
        assert by_name["ghost"]["success"] is False
        assert "ghost" in by_name["ghost"]["error"]["message"]

    def test_no_machines_registered(self, tools) -> None:
        lookup, registry = _fleet({})
        with _plan(covers=True), lookup, registry:
            result = tools["check_machine_firmware"](all_machines=True)

        assert result["error"]["code"] == "NO_PRINTERS"


# ---------------------------------------------------------------------------
# The names these tools replaced
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("firmware_status", "check_machine_firmware"),
        ("check_firmware_status", "check_machine_firmware"),
        ("update_firmware", "update_machine_firmware"),
        ("update_printer_firmware", "update_machine_firmware"),
        ("rollback_firmware", "roll_back_machine_firmware"),
        ("rollback_printer_firmware", "roll_back_machine_firmware"),
    ],
)
def test_an_old_name_points_at_the_tool_that_does_the_job(old, new) -> None:
    from kiln.retired_tools import replacement_tool, retired_message

    message = retired_message(old)
    assert message is not None
    assert new in message
    from kiln.retired_tools import RETIRED

    assert replacement_tool(RETIRED[old]) == new


def test_no_old_name_is_still_registered() -> None:
    import asyncio

    from kiln.server import mcp

    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"check_machine_firmware", "update_machine_firmware", "roll_back_machine_firmware"} <= names
    assert not names & {
        "firmware_status", "check_firmware_status", "update_firmware",
        "update_printer_firmware", "rollback_firmware", "rollback_printer_firmware",
    }
