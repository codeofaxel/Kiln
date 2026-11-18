"""A door that says why in a clause names the fix for a version floor too.

When Kiln's servers refuse an install older than they serve (HTTP 426), the
fix is this install's own: update Kiln.  A door that words its miss as a
whole sentence says so; a door that follows its own reason with a clause
(the material record, the printer record, the printer's own melt figures)
says it as well, never only the need.  On an install without kiln-pro,
with Kiln's servers answering the version floor the way their door does.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from kiln.served_answer import Miss, classify_answer, clause, envelope_for_http

#: The servers' version-floor answer, as their tool door sends it.
_FLOOR_BODY = {
    "success": False,
    "error": "upgrade_required",
    "code": "UPGRADE_REQUIRED",
    "upgrade": {"headline": "Kiln 99.0.0 is required to continue (you're on 1.5.1)."},
}


@pytest.fixture(autouse=True)
def _floor(monkeypatch):
    import kiln.server as srv
    from kiln import _pro_catalogue_bridge, _pro_material_bridge, _pro_melt_bridge

    monkeypatch.setitem(sys.modules, "kiln_pro", None)

    def floor(tool_name, _timeout=None, _asked_by_user=True, _feature=None, **_kwargs) -> dict[str, Any]:
        return envelope_for_http(tool_name, 426, dict(_FLOOR_BODY))

    def served(key: str):
        return None, classify_answer(floor("record"))

    monkeypatch.setattr(srv, "_pro_api_call", floor)
    # The suite serves every record from a stand-in; here the records' served
    # door meets the floor too, read the way the bridges read any answer.
    for bridge in (_pro_catalogue_bridge, _pro_material_bridge):
        monkeypatch.setattr(bridge, "available", lambda: False)
        monkeypatch.setattr(bridge, "_served", served)
    for bridge in (_pro_catalogue_bridge, _pro_material_bridge, _pro_melt_bridge):
        bridge.forget()
    yield
    for bridge in (_pro_catalogue_bridge, _pro_material_bridge, _pro_melt_bridge):
        bridge.forget()


def _material_record() -> str | None:
    from kiln import _pro_material_bridge as bridge

    bridge.material_record("petg")
    return bridge.unanswered("petg")


def _printer_record() -> str | None:
    from kiln import _pro_catalogue_bridge as bridge

    bridge.printer_record("bambu_x1c")
    return bridge.unanswered("bambu_x1c")


def _melt_rate() -> str | None:
    from kiln import _pro_melt_bridge as bridge

    bridge.printer_melt_rate("bambu_x1c", 0.4, "pla")
    return bridge.unanswered("bambu_x1c", 0.4)


@pytest.mark.parametrize("door", [_material_record, _printer_record, _melt_rate])
def test_a_clause_door_names_the_update(door):
    said = door()
    assert said and "required to continue" in said, said
    assert "update Kiln" in said, said


def test_the_clause_keeps_the_fix_for_the_floor_and_no_other_refusal():
    floor = Miss("refused", "UPGRADE_REQUIRED", "Kiln 99.0.0 is required to continue")
    other = Miss("refused", "INVALID_INPUT", "That request cannot be read")
    assert clause(floor, feature="servers", cannot="get it").endswith("; update Kiln and try again")
    assert ";" not in clause(other, feature="servers", cannot="get it")
