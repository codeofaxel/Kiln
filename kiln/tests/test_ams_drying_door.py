"""The AMS dryer door runs beside the printer; a start is checked first.

Until 2026-10-17 ``ams_filament_drying`` existed only where kiln-pro was
installed, and Kiln's servers refused it (it needs the printer), so no
customer could run the dryer through Kiln at any tier.  The door is now
public Kiln's: a STOP sends the one fixed payload the emergency stop sends,
offline included; a START is checked against the unit's own limits, in
kiln-pro, before it is sent -- in-process where kiln-pro is installed,
through the served ``ams_drying_command`` everywhere else -- and a start
that cannot be checked is not sent.  Pinned here against a stand-in for the
servers; the real check at every tier is pinned on the kiln-pro side
(``tests/test_ams_control_tool.py``).
"""

from __future__ import annotations

import builtins
from typing import Any

import pytest

import kiln.server as srv
from kiln import served_answer as sa
from kiln.plugins import ams_drying_tools as door


class BambuAdapter:
    """A Bambu with an AMS 2 Pro on unit 0, recording what it is sent."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []

    def get_ams_status(self) -> dict[str, Any]:
        return {"units": [{"unit_id": 0, "module_name": "n3f/0"}]}

    def publish_print_command(self, command: str, params: Any = None) -> Any:
        self.sent.append((command, params))
        return True


class PrusaLinkAdapter:
    def send_gcode(self, commands: Any) -> bool:
        return True


class _Servers:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.offline = False
        self.signed_out = False

    def __call__(self, tool_name: str, _timeout: float | None = None, _asked_by_user: bool = True, **kw: Any):
        self.calls.append((tool_name, dict(kw)))
        if self.offline:
            return {"status": "error", "success": False, "code": "SERVER_UNREACHABLE", "error": "x", "why": "offline"}
        if self.signed_out:
            return {"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "sign in", "why": "signed_out"}
        assert tool_name == "ams_drying_command"
        if kw.get("temp_c", 0) > 65 and kw.get("module_name") == "n3f/0":
            return {"success": False, "code": "INVALID_DRYING_COMMAND",
                    "error": "temp_c 70 C exceeds the AMS 2 Pro drying ceiling of 65 C."}
        return {"success": True, "unit_type": "AMS 2 Pro", "command": {
            "ams_id": kw["ams_id"], "mode": 1, "duration": kw["duration_min"], "temp": kw["temp_c"],
            "cooling_temp": kw["cooling_temp_c"],
        }}


@pytest.fixture
def printer(monkeypatch) -> BambuAdapter:
    adapter = BambuAdapter()
    monkeypatch.setattr(srv, "_check_auth", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_resolve_control_target", lambda name: (adapter, name or "default"))
    return adapter


@pytest.fixture
def servers(monkeypatch) -> _Servers:
    """A plain install: kiln-pro unimportable, the served door answered here."""
    real_import = builtins.__import__

    def _no_pro(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "kiln_pro" or name.startswith("kiln_pro."):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    fake = _Servers()
    monkeypatch.setattr(builtins, "__import__", _no_pro)
    monkeypatch.setattr(srv, "_pro_api_call", fake)
    return fake


START = {"ams_id": 0, "mode": 1, "duration_min": 480, "temp_c": 60, "cooling_temp_c": 50}


class TestAStart:
    def test_the_checked_command_is_what_the_printer_is_sent(self, printer, servers):
        out = door.ams_filament_drying(**START)
        assert out["status"] == "success" and out["action"] == "start" and out["outcome"] == "accepted"
        assert out["confirmed"] is False, "the AMS reports no drying state Kiln can read back"
        assert printer.sent == [("ams_filament_drying", {"ams_id": 0, "mode": 1, "duration": 480, "temp": 60, "cooling_temp": 50})]
        assert servers.calls[0][1]["module_name"] == "n3f/0", "the unit's own report sets its ceiling"

    def test_a_start_over_the_units_ceiling_is_never_sent(self, printer, servers):
        out = door.ams_filament_drying(**{**START, "temp_c": 70})
        assert out["code"] == "INVALID_DRYING_COMMAND" and "65 C" in out["error"]
        assert printer.sent == []

    @pytest.mark.parametrize("cause, words", [("offline", "this computer is offline"), ("signed_out", "Kiln is signed out")])
    def test_a_start_that_cannot_be_checked_is_not_sent_and_says_why(self, printer, servers, cause, words):
        setattr(servers, cause, True)
        out = door.ams_filament_drying(**START)
        assert out["code"] == "DRYING_CHECK_UNAVAILABLE" and out["why"] == cause
        assert f"Kiln can't check this drying start right now because {words}" in out["error"]
        assert "started nothing and the dryer is as it was" in out["error"]
        assert printer.sent == []

    def test_the_advisor_is_named_once(self, printer, servers):
        first = door.ams_filament_drying(**START)
        second = door.ams_filament_drying(**START)
        assert "drying_advisor" in first["pro_tip"] and "pro_tip" not in second


class TestAStop:
    def test_a_stop_goes_out_offline_and_asks_nothing(self, printer, servers):
        servers.offline = True
        out = door.ams_filament_drying(ams_id=0, mode=0)
        assert out["status"] == "success" and out["action"] == "stop"
        assert printer.sent == [("ams_filament_drying", {"ams_id": 0, "mode": 0, "duration": 0})]
        assert servers.calls == []

    def test_the_stop_is_the_emergency_stops_own_payload(self, printer, servers):
        from kiln.emergency import EmergencyCoordinator

        door.ams_filament_drying(ams_id=1, mode=0)
        assert printer.sent == [("ams_filament_drying", EmergencyCoordinator._build_ams_stop_command(1))]


class TestTheDoor:
    def test_another_maker_is_told_plainly(self, monkeypatch, servers):
        monkeypatch.setattr(srv, "_check_auth", lambda *a, **k: None)
        monkeypatch.setattr(srv, "_resolve_control_target", lambda name: (PrusaLinkAdapter(), "mk4"))
        out = door.ams_filament_drying(**START)
        assert out["error"]["code"] == "UNSUPPORTED_PRINTER"

    def test_a_mode_that_is_neither_is_refused(self, printer, servers):
        assert door.ams_filament_drying(ams_id=0, mode=3)["error"]["code"] == "INVALID_DRYING_COMMAND"

    def test_classified_as_a_command_to_confirm(self):
        import json
        from pathlib import Path

        data = json.loads((Path(door.__file__).parent.parent / "data" / "tool_safety.json").read_text())
        assert data["classifications"]["ams_filament_drying"] == {"level": "confirm", "physical_effect": True}

    @pytest.mark.parametrize("cause", sa.CAUSES)
    def test_the_miss_reads_in_the_shared_shape(self, cause):
        from kiln import _pro_ams_bridge

        text = _pro_ams_bridge._miss(sa.Miss(cause, "X_CODE", "The servers said no." if cause == "refused" else ""))["error"]
        assert "Kiln can't" in text and "right now because" in text and ", so it" in text
        assert "X_CODE" not in text and text.endswith(".")
