"""The adaptive slicing tools ask Kiln's servers for a material's tuned settings.

A material's tuned settings, and the layer plan built from them, are
kiln-pro's (https://kiln3d.com); a plain install asks for them through
``kiln._pro_adaptive_bridge`` and relays what comes back.  These tests pin
the doors' side of that wire with the servers stood in (public tests cannot
import kiln-pro):

* what is sent, and that the served answer is relayed: the caller's own
  model name and printer filled in, the nozzle block added here;
* Kiln's own refusal of a value (a material it does not tune) is relayed as
  it is;
* a miss (offline, signed out, the servers silent or saying no) still
  answers: a plan or profile from generic settings, ``basic: true`` and one
  line saying what it is, why, and what gets the tuned answer, with nothing
  in it that depends on the material;
* a generic plan's exported config says so inside it;
* the per-material table is not in this package.
"""

from __future__ import annotations

import contextlib
import errno
import importlib.util
import json
import signal
import sys
import threading
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from kiln import _pro_adaptive_bridge as bridge
from kiln.plugins import adaptive_slicing_tools as doors

#: The bridge's own reader, kept before any test stands it in.
_REAL_LOCAL = bridge._local
WIRE = "adaptive_slicing_answer"
REPO = Path(__file__).resolve().parents[2]
REGIONS = [
    {"region_type": "bottom_surface", "z_start_mm": 0.0, "z_end_mm": 0.5, "area_pct": 100.0},
    {"region_type": "overhang", "z_start_mm": 3.0, "z_end_mm": 6.0, "area_pct": 12.0},
    {"region_type": "top_surface", "z_start_mm": 9.0, "z_end_mm": 10.0, "area_pct": 100.0},
]


@pytest.fixture(autouse=True)
def _plain_install(monkeypatch):
    """No kiln-pro on this machine, whatever the test runner has on its path."""
    monkeypatch.setattr(bridge, "_local", lambda *a, **k: bridge._NOT_HERE)


@pytest.fixture
def served(monkeypatch):
    import kiln.server as srv

    calls: list[tuple[str, dict]] = []
    answers: list[Any] = []

    def fake(tool_name, _timeout=None, _asked_by_user=True, **kwargs):
        calls.append((tool_name, kwargs))
        answer = answers.pop(0) if answers else {}
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(srv, "_pro_api_call", fake)
    return calls, answers


def _served_plan(material: str = "PETG", **extra: Any) -> dict[str, Any]:
    """A plan as Kiln's servers send it: no model name, no printer."""
    plan = {
        "plan_id": "abc123", "model_name": "", "material": material, "printer": None,
        "mode": "balanced", "nozzle_diameter_mm": 0.4, "total_layers": 3, "total_height_mm": 0.61,
        "min_layer_height_mm": 0.11, "max_layer_height_mm": 0.29, "layer_heights": [0.27, 0.17, 0.17],
        "layer_regions": [["bottom_surface"], [], []], "layer_speeds": [0.45, 1.0, 1.0],
        "layer_cooling": [0.0, 100.0, 100.0], "estimated_time_minutes": 0.3, "estimated_savings_pct": None,
        "regions_detected": [], "created_at": "2026-01-01T00:00:00+00:00",
    }
    plan.update(extra)
    return {"success": True, "status": "ok", "plan": plan}


SERVED_PROFILE = {
    "material": "PETG", "min_layer_height_mm": 0.11, "max_layer_height_mm": 0.29,
    "optimal_layer_height_mm": 0.17, "bridge_layer_height_mm": 0.13, "overhang_max_angle": 47.0,
    "overhang_speed_factor": 0.55, "overhang_fan_pct": 70.0, "bridge_speed_mm_s": 22.0,
    "bridge_fan_pct": 85.0, "first_layer_height_mm": 0.27, "first_layer_speed_factor": 0.45,
    "top_surface_layers": 6, "top_surface_speed_factor": 0.65, "cooling_threshold_layer_time_s": 11.0,
    "notes": "A served note.",
}


# ---------------------------------------------------------------------------
# The served answer
# ---------------------------------------------------------------------------


class TestTheServedAnswer:
    def test_a_plan_is_asked_for_and_relayed_with_the_callers_own_names(self, served):
        calls, answers = served
        answers.append(_served_plan())
        out = doors.generate_adaptive_slicing_plan(
            REGIONS, "petg", 10.0, model_name="bracket", printer="shop_a1", nozzle_diameter_mm=0.4,
        )
        assert calls == [(WIRE, {
            "ask": "plan", "material": "petg", "nozzle_diameter_mm": 0.4, "model_height_mm": 10.0,
            "regions": REGIONS, "mode": "balanced",
        })]
        assert out["success"] is True and "basic" not in out and "basic" not in out["plan"]
        assert out["plan"]["model_name"] == "bracket" and out["plan"]["printer"] == "shop_a1"
        assert out["plan"]["layer_heights"] == [0.27, 0.17, 0.17]
        assert out["nozzle"]["diameter_mm"] == 0.4 and out["nozzle"]["source"] == "stated"

    def test_a_quick_plan_is_asked_for_as_one(self, served):
        calls, answers = served
        answers.append(_served_plan("PLA"))
        out = doors.quick_adaptive_plan("PLA", 10.0, nozzle_diameter_mm=0.6, mode="speed_first")
        assert calls == [(WIRE, {
            "ask": "quick_plan", "material": "PLA", "nozzle_diameter_mm": 0.6, "model_height_mm": 10.0,
            "regions": None, "mode": "speed_first",
        })]
        assert out["success"] is True and out["plan"]["material"] == "PLA" and "basic" not in out

    def test_a_profile_is_relayed_as_served(self, served):
        calls, answers = served
        answers.append({"success": True, "status": "ok", "profile": SERVED_PROFILE})
        out = doors.get_material_slicing_profile("petg", nozzle_diameter_mm=0.4)
        assert calls == [(WIRE, {"ask": "profile", "material": "petg", "nozzle_diameter_mm": 0.4})]
        assert out == {"success": True, "profile": SERVED_PROFILE, "nozzle": out["nozzle"]}

    def test_the_list_of_materials_is_relayed_as_served(self, served):
        calls, answers = served
        rows = [{"material": "X", "layer_range_mm": "0.11-0.29", "optimal_mm": 0.17, "bridge_mm": 0.13,
                 "overhang_max_angle": 47.0}]
        answers.append({"success": True, "status": "ok", "materials": rows, "count": 1})
        out = doors.list_supported_materials()
        assert calls == [(WIRE, {"ask": "materials"})]
        assert out == {"success": True, "materials": rows, "count": 1}

    @pytest.mark.parametrize("door", ["generate", "quick", "profile"])
    def test_kilns_own_refusal_is_relayed_as_it_is(self, served, door):
        _, answers = served
        ruling = {"success": False, "code": "INVALID_INPUT", "error": "Unknown material 'wood'. Available: A, B."}
        answers.append(ruling)
        if door == "generate":
            out = doors.generate_adaptive_slicing_plan([], "wood", 10.0, nozzle_diameter_mm=0.4)
        elif door == "quick":
            out = doors.quick_adaptive_plan("wood", 10.0, nozzle_diameter_mm=0.4)
        else:
            out = doors.get_material_slicing_profile("wood", nozzle_diameter_mm=0.4)
        assert out == {"success": False, "error": ruling["error"]}

    def test_a_mode_the_planner_does_not_know_is_refused_before_a_quick_plan_asks(self, served):
        calls, _ = served
        out = doors.quick_adaptive_plan("PLA", 10.0, nozzle_diameter_mm=0.4, mode="turbo")
        assert calls == []
        assert out["success"] is False and "Invalid mode 'turbo'" in out["error"]


# ---------------------------------------------------------------------------
# A miss still answers, with generic settings, and says so
# ---------------------------------------------------------------------------


MISSES = {
    "offline": urllib.error.URLError(OSError(errno.ENETUNREACH, "Network is unreachable")),
    "signed_out": {"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "Sign in.", "why": "signed_out"},
    "unanswered": ConnectionRefusedError("refused"),
    "refused": {"status": "error", "code": "SOMETHING_ELSE", "error": "Not for this account."},
    "no_answer_inside": {"success": True},
}
CAUSE = {"offline": "offline", "signed_out": "signed_out", "unanswered": "unanswered",
         "refused": "refused", "no_answer_inside": "unanswered"}
FIX_WORDS = {"offline": "Reconnect to the internet", "signed_out": "Sign in", "unanswered": "Wait a minute"}


def _ask_door(door: str, material: str = "PLA") -> dict[str, Any]:
    if door == "generate":
        return doors.generate_adaptive_slicing_plan(REGIONS, material, 10.0, model_name="m", printer="p",
                                                    nozzle_diameter_mm=0.4)
    if door == "quick":
        return doors.quick_adaptive_plan(material, 10.0, nozzle_diameter_mm=0.4)
    return doors.get_material_slicing_profile(material, nozzle_diameter_mm=0.4)


class TestAMissAnswersWithGenericSettings:
    @pytest.mark.parametrize("door", ["generate", "quick", "profile"])
    @pytest.mark.parametrize("miss", sorted(MISSES))
    def test_a_generic_answer_marked_basic_with_one_line(self, served, door, miss):
        _, answers = served
        answers.append(MISSES[miss])
        out = _ask_door(door)
        assert out["success"] is True and out["basic"] is True
        assert out["why"] == CAUSE[miss]
        line = out["basic_note"]
        assert "generic settings worked out from the nozzle size, not settings tuned to PLA" in line
        assert "Kiln can't" in line and "right now because" in line and ", so it" in line
        if CAUSE[miss] in FIX_WORDS:
            assert f"{FIX_WORDS[CAUSE[miss]]} and ask again for settings tuned to PLA." in line
        if door == "profile":
            assert out["profile"]["top_surface_layers"] is None
            assert out["profile"]["overhang_max_angle"] == _floor_angle()
        else:
            assert out["plan"]["basic"] is True and out["plan"]["total_layers"] > 0
            assert out["plan"]["min_layer_height_mm"] == 0.1 and out["plan"]["max_layer_height_mm"] == 0.3

    @pytest.mark.parametrize("miss", sorted(MISSES))
    def test_the_list_says_none_and_shows_the_generic_settings(self, served, miss):
        _, answers = served
        answers.append(MISSES[miss])
        out = doors.list_supported_materials()
        assert out["success"] is True and out["basic"] is True
        assert out["materials"] == [] and out["count"] == 0
        assert out["generic_settings"]["layer_range_mm"] == "0.1-0.3"
        assert "every material plans with generic settings" in out["basic_note"]

    def test_signed_out_says_sign_in_for_settings_tuned_to_the_material(self, served):
        _, answers = served
        answers.append(MISSES["signed_out"])
        out = doors.quick_adaptive_plan("petg", 10.0, nozzle_diameter_mm=0.4)
        assert out["basic_note"].endswith("Sign in and ask again for settings tuned to PETG.")

    @pytest.mark.parametrize("door", ["generate", "quick", "profile"])
    def test_a_basic_answer_does_not_depend_on_the_material(self, served, door):
        """Built only from the nozzle and rules written in the code: the same
        answer for every material, whatever Kiln's tuned settings say."""
        _, answers = served
        replies = []
        for material in ("PLA", "PETG", "ABS", "TPU", "unobtainium"):
            answers.append(MISSES["signed_out"])
            out = _ask_door(door, material)
            out.pop("basic_note")
            body = out.get("plan") or out.get("profile")
            assert body.pop("material") == material.upper()
            body.pop("plan_id", None)
            body.pop("created_at", None)
            replies.append(out)
        assert all(reply == replies[0] for reply in replies[1:])

    def test_a_mode_the_planner_does_not_know_is_still_refused(self, served):
        _, answers = served
        answers.append(MISSES["offline"])
        out = doors.generate_adaptive_slicing_plan(REGIONS, "PLA", 10.0, nozzle_diameter_mm=0.4, mode="turbo")
        assert out["success"] is False and "Invalid mode 'turbo'" in out["error"]

    def test_a_generic_plans_config_says_so_inside_it(self, served):
        _, answers = served
        answers.append(MISSES["signed_out"])
        plan = doors.quick_adaptive_plan("PLA", 10.0, nozzle_diameter_mm=0.4)["plan"]
        for slicer in ("prusaslicer", "orcaslicer", "cura", "generic"):
            out = doors.export_adaptive_slicer_config(plan, slicer=slicer)
            assert out["success"] is True and out["basic"] is True
            assert out["config"]["basic"] is True
            assert out["config"]["notes"][0].startswith("Generic settings worked out from the nozzle size")
        savings = doors.estimate_adaptive_time_savings(plan)
        assert savings["basic"] is True

    def test_a_tuned_plans_config_carries_no_generic_label(self, served):
        _, answers = served
        answers.append(_served_plan())
        plan = doors.quick_adaptive_plan("PETG", 10.0, nozzle_diameter_mm=0.4)["plan"]
        out = doors.export_adaptive_slicer_config(plan, slicer="cura")
        assert "basic" not in out and "basic" not in out["config"]
        assert not any("Generic settings" in note for note in out["config"]["notes"])


def _floor_angle() -> float:
    from kiln.printability import _resolve_overhang_threshold

    return _resolve_overhang_threshold(None, None, None)


@contextlib.contextmanager
def _deadline(seconds: float):
    """Fail, instead of waiting forever, where the platform has a timer."""
    if not hasattr(signal, "setitimer") or threading.current_thread() is not threading.main_thread():
        yield
        return

    def _expired(_signum, _frame):
        raise TimeoutError(f"still planning after {seconds} s")

    previous = signal.signal(signal.SIGALRM, _expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class TestAMissRefusesWhatNoPlanIsMadeFor:
    """The generic answer is bounded the same way the served one is."""

    @pytest.mark.parametrize("height", [float("nan"), float("inf"), 1e7])
    @pytest.mark.parametrize("door", ["generate", "quick"])
    def test_a_height_no_plan_is_made_for(self, served, door, height):
        _, answers = served
        answers.append(MISSES["signed_out"])
        with _deadline(2.0):
            if door == "generate":
                out = doors.generate_adaptive_slicing_plan([], "PLA", height, nozzle_diameter_mm=0.4)
            else:
                out = doors.quick_adaptive_plan("PLA", height, nozzle_diameter_mm=0.4)
        assert out["success"] is False, out
        assert "finite number" in out["error"] or "taller than any printer" in out["error"]

    @pytest.mark.parametrize("nozzle", [0.01, 3.0])
    @pytest.mark.parametrize("door", ["generate", "quick", "profile"])
    def test_a_nozzle_no_plan_is_made_for(self, served, door, nozzle):
        _, answers = served
        answers.append(MISSES["offline"])
        with _deadline(2.0):
            if door == "generate":
                out = doors.generate_adaptive_slicing_plan(REGIONS, "PLA", 10.0, nozzle_diameter_mm=nozzle)
            elif door == "quick":
                out = doors.quick_adaptive_plan("PLA", 10.0, nozzle_diameter_mm=nozzle)
            else:
                out = doors.get_material_slicing_profile("PLA", nozzle_diameter_mm=nozzle)
        assert out["success"] is False and "Nozzle diameter must be between" in out["error"]

    def test_too_many_regions(self, served):
        _, answers = served
        answers.append(MISSES["unanswered"])
        many = [{"region_type": "standard", "z_start_mm": 0.0, "z_end_mm": 10.0, "area_pct": 1.0}] * 201
        with _deadline(5.0):
            out = doors.generate_adaptive_slicing_plan(many, "PLA", 10.0, nozzle_diameter_mm=0.4)
        assert out["success"] is False and "at most 200 regions" in out["error"]


# ---------------------------------------------------------------------------
# kiln-pro on this computer answers in process, and a fault there stays here
# ---------------------------------------------------------------------------


def test_kiln_pro_on_this_computer_answers_without_asking_the_servers(served, monkeypatch):
    calls, _ = served
    monkeypatch.setattr(bridge, "_local", lambda question, fields: _served_plan("ABS"))
    out = doors.quick_adaptive_plan("ABS", 10.0, nozzle_diameter_mm=0.4)
    assert calls == [] and out["success"] is True and "basic" not in out


def test_a_fault_in_kiln_pro_here_is_a_generic_answer_that_names_nothing(served, monkeypatch, caplog):
    import logging
    import types

    caplog.set_level(logging.DEBUG)
    secret = "/srv/kiln/tenant-7f3a/private.sqlite"

    def boom(*_a, **_k):
        raise RuntimeError(secret)

    parts = bridge.LOCAL_MODULE.split(".")
    for depth in range(1, len(parts)):
        package = types.ModuleType(".".join(parts[:depth]))
        package.__path__ = []
        monkeypatch.setitem(sys.modules, package.__name__, package)
    engine = types.ModuleType(bridge.LOCAL_MODULE)
    engine.answer = boom
    monkeypatch.setitem(sys.modules, bridge.LOCAL_MODULE, engine)
    monkeypatch.setattr(bridge, "_local", _REAL_LOCAL)
    calls, _ = served
    out = doors.quick_adaptive_plan("PLA", 10.0, nozzle_diameter_mm=0.4)
    assert calls == [], "a fault here is not a reason to ask the servers"
    assert out["success"] is True and out["basic"] is True
    assert secret not in json.dumps(out)
    assert secret in caplog.text, "the fault was not logged on this computer"


# ---------------------------------------------------------------------------
# The table is not in this package
# ---------------------------------------------------------------------------


def test_the_per_material_table_is_not_in_this_package():
    import kiln.adaptive_slicer as eng

    assert not hasattr(eng, "_MATERIAL_PROFILES")
    assert not hasattr(eng.AdaptiveSlicer, "get_material_profile")
    assert not hasattr(eng.AdaptiveSlicer, "list_supported_materials")
    spec = importlib.util.spec_from_file_location(
        "audit_public_material_tables_for_adaptive", REPO / "scripts" / "audit_public_material_tables.py"
    )
    gate = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gate
    spec.loader.exec_module(gate)
    names = gate.material_names()
    for module in ("adaptive_slicer.py", "_pro_adaptive_bridge.py", "plugins/adaptive_slicing_tools.py"):
        source = (gate.SRC / module).read_text(encoding="utf-8")
        assert list(gate.tables_in_python(source, names)) == [], module
