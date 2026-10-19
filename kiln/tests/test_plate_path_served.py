"""The path around a part on the plate reaches an install without kiln-pro.

Until 2026-10-17 :func:`kiln.plate_state.plan_motion_around_plate` answered
``None`` whenever kiln-pro was not importable, so a pip install always
refused to home or park beside a finished part.  Now it asks Kiln's servers
(the ``motion_plan`` door, free, for a machine this install has reported),
sending only what the planner reads, and every step that comes back is still
read the same way before any of it may run.  Pinned against a stand-in for
the servers; the real planner at each tier is pinned on the kiln-pro side
(``tests/test_motion_plan_around_plate_served.py``).
"""

from __future__ import annotations

import builtins
from typing import Any

import pytest

import kiln._pro_motion_bridge as bridge
import kiln.server as srv
from kiln.plate_state import PlateJob, PlateState, plan_motion_around_plate

STEPS = [
    {"label": "Raise clear of the part", "you_will_see": "The bed drops", "stops_when": "It stops",
     "gcode": ["G91", "G1 Z45 F600", "G90"], "leaves": []},
    {"label": "Home X and Y", "you_will_see": "The head moves to a corner", "stops_when": "It stops",
     "gcode": ["G28 X Y"], "leaves": []},
]


class _Servers:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.answer: Any = {"around_plate": STEPS}
        self.raises: Exception | None = None

    def __call__(self, tool_name: str, _timeout: float | None = None, _asked_by_user: bool = True, **kw: Any):
        self.calls.append((tool_name, dict(kw)))
        if self.raises is not None:
            raise self.raises
        return self.answer


@pytest.fixture
def servers(monkeypatch) -> _Servers:
    real_import = builtins.__import__

    def _no_pro(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "kiln_pro" or name.startswith("kiln_pro."):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    fake = _Servers()
    monkeypatch.setattr(builtins, "__import__", _no_pro)
    monkeypatch.setattr(srv, "_pro_api_call", fake)
    monkeypatch.setattr(bridge, "_service_down_until", 0.0)
    bridge._misses.clear()
    return fake


def _plate() -> PlateState:
    return PlateState(
        machine="shop_a1", status="occupied", source="kiln_start", since="2026-10-17T10:00:00+00:00",
        job=PlateJob(file="/Users/someone/prints/bracket.gcode", footprint_mm=[80.0, 80.0, 140.0, 140.0],
                     max_z_mm=40.0, printer_id="bambu_a1"),
        note="left on the plate",
    )


def test_an_install_without_kiln_pro_is_served_the_path(servers):
    steps = plan_motion_around_plate(_plate(), None, action="home", clearance_mm=None, printer_model="bambu_a1")
    assert [s["label"] for s in steps] == ["Raise clear of the part", "Home X and Y"]
    ((tool, sent),) = servers.calls
    assert tool == "motion_plan" and sent["printer_id"] == "bambu_a1" and sent["verb"] == "home"
    assert sent["plate"] == {
        "status": "occupied",
        "job": {"file": "bracket.gcode", "footprint_mm": [80.0, 80.0, 140.0, 140.0],
                "max_z_mm": 40.0, "printer_id": "bambu_a1"},
    }, "only what the planner reads leaves this computer, and never a path"


def test_a_malformed_step_is_no_plan(servers):
    servers.answer = {"around_plate": [{"label": "x", "you_will_see": "y", "stops_when": "z", "gcode": "G28"}]}
    assert plan_motion_around_plate(_plate(), None, action="park", clearance_mm=None, printer_model="bambu_a1") is None


def test_no_path_is_no_plan_and_not_a_miss(servers):
    servers.answer = {"around_plate": None}
    assert plan_motion_around_plate(_plate(), None, action="home", clearance_mm=None, printer_model="bambu_a1") is None
    assert bridge._misses == {}


def test_a_machine_with_no_model_asks_nothing(servers):
    assert plan_motion_around_plate(_plate(), None, action="home", clearance_mm=None, printer_model=None) is None
    assert servers.calls == []


@pytest.mark.parametrize("answer, cause", [
    ({"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "sign in", "why": "signed_out"}, "signed_out"),
    ({"status": "error", "code": "MACHINE_NOT_PAIRED", "error": "not reported"}, "refused"),
])
def test_a_refusal_is_no_plan_with_why_kept(servers, answer, cause):
    servers.answer = answer
    assert plan_motion_around_plate(_plate(), None, action="home", clearance_mm=None, printer_model="bambu_a1") is None
    assert bridge._misses[("bambu_a1", "", "around_plate:home", "", False)].cause == cause


def test_offline_is_no_plan_and_the_service_is_left_alone(servers):
    servers.raises = OSError("no route to host")
    assert plan_motion_around_plate(_plate(), None, action="home", clearance_mm=None, printer_model="bambu_a1") is None
    assert plan_motion_around_plate(_plate(), None, action="park", clearance_mm=None, printer_model="bambu_a1") is None
    assert len(servers.calls) == 1, "a second ask inside the backoff is not sent"
