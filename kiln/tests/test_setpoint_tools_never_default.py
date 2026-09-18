"""A setpoint the caller did not send is refused, never defaulted.

Measured 2026-09-18 on an A1: a call meant as ``set_fan(speed=0)`` reached
Kiln with no ``percent`` key at all -- the caller's own argument name had
been dropped before the request arrived, so the unknown-argument gate
(which refuses ``speed`` by name at the dispatch door, pinned below) never
saw it.  The old default of ``percent=100`` then ran the part fan flat out
and the answer confirmed it.  The layer that owns that outcome is the
default: ``set_fan`` and ``set_printer_light`` now require their setpoint,
at the MCP door and the CLI door alike, and a call without one is refused
with the field named.
"""

from __future__ import annotations

import asyncio

import pytest

from kiln import server


@pytest.fixture(autouse=True)
def _open_door(monkeypatch):
    monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: True)
    monkeypatch.setattr(server, "_check_auth", lambda *a, **k: None)
    monkeypatch.setattr(server, "_check_rate_limit", lambda *a, **k: None)


class _Recorder:
    """An adapter that records every fan and light command it is handed."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def set_fan(self, node, percent):
        self.calls.append(("fan", node, percent))
        return True

    def set_light(self, node, mode):
        self.calls.append(("light", node, mode))
        return True


def _dispatch(name: str, arguments: dict):
    """The one door every MCP call uses: the tool manager's call_tool."""
    return asyncio.run(server.mcp._tool_manager.call_tool(name, arguments))


def _required(name: str) -> set[str]:
    tool = server.mcp._tool_manager._tools[name]  # noqa: SLF001
    return set(tool.parameters.get("required") or ())


class TestSetFan:
    def test_an_undeclared_argument_is_refused_by_name(self, monkeypatch):
        """The gate already covered this shape; pinned so it stays covered."""
        adapter = _Recorder()
        monkeypatch.setattr(server, "_get_adapter", lambda: adapter)
        with pytest.raises(RuntimeError, match=r"set_fan does not accept the argument: speed"):
            _dispatch("set_fan", {"speed": 0})
        assert adapter.calls == []

    def test_a_call_without_a_setpoint_is_refused_not_run_at_full(self, monkeypatch):
        """The live failure's exact wire shape: no percent at all."""
        adapter = _Recorder()
        monkeypatch.setattr(server, "_get_adapter", lambda: adapter)
        out = _dispatch("set_fan", {})
        assert adapter.calls == [], f"the fan ran at a default: {adapter.calls}"
        assert out["success"] is False
        assert out["error"]["code"] == "INVALID_ARGS"
        assert "percent" in out["error"]["message"]

    def test_the_schema_says_percent_is_required(self):
        assert "percent" in _required("set_fan")

    def test_a_sent_setpoint_still_dispatches(self, monkeypatch):
        adapter = _Recorder()
        monkeypatch.setattr(server, "_get_adapter", lambda: adapter)
        out = _dispatch("set_fan", {"percent": 0})
        assert adapter.calls == [("fan", "part", 0)]
        assert out["success"] is True and out["percent"] == 0


class TestSetPrinterLight:
    def test_a_call_without_a_mode_is_refused_not_turned_on(self, monkeypatch):
        adapter = _Recorder()
        monkeypatch.setattr(server, "_get_adapter", lambda: adapter)
        out = _dispatch("set_printer_light", {})
        assert adapter.calls == [], f"the light took a default: {adapter.calls}"
        assert out["success"] is False and out["error"]["code"] == "INVALID_ARGS"
        assert "mode" in out["error"]["message"]

    def test_a_misnamed_mode_is_refused_by_name(self, monkeypatch):
        adapter = _Recorder()
        monkeypatch.setattr(server, "_get_adapter", lambda: adapter)
        with pytest.raises(RuntimeError, match=r"set_printer_light does not accept the argument: state"):
            _dispatch("set_printer_light", {"state": "off"})
        assert adapter.calls == []

    def test_the_schema_says_mode_is_required(self):
        assert "mode" in _required("set_printer_light")


class TestNoControlToolDefaultsASetpoint:
    """Every registered ``set_*`` / ``fleet_set_*`` tool: a parameter named
    like a setpoint either is required or defaults to nothing (``None``).
    A non-zero default on a setpoint is the trap this file exists for."""

    SETPOINT_WORDS = ("percent", "speed", "temp", "mode", "bright", "power", "level")

    def test_setpoints_are_required_or_none(self):
        offenders = []
        for name, tool in server.mcp._tool_manager._tools.items():  # noqa: SLF001
            if not (name.startswith("set_") or name.startswith("fleet_set_")):
                continue
            props = tool.parameters.get("properties") or {}
            required = set(tool.parameters.get("required") or ())
            for param, schema in props.items():
                if param in required or not any(w in param for w in self.SETPOINT_WORDS):
                    continue
                default = schema.get("default")
                if default not in (None, 0, 0.0, ""):
                    offenders.append(f"{name}({param}={default!r})")
        assert offenders == [], f"setpoints with a live default: {offenders}"


class TestTheCommandLineDoor:
    def test_fan_without_percent_is_refused(self):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        out = CliRunner().invoke(cli, ["fan"])
        assert out.exit_code != 0
        assert "--percent" in out.output

    def test_light_without_mode_is_refused(self):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        out = CliRunner().invoke(cli, ["light"])
        assert out.exit_code != 0
        assert "--mode" in out.output
