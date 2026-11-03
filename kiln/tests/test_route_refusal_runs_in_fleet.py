"""When no printer in the fleet can melt the filament, the refusal says what each one runs.

``route_print_job`` refuses a print no printer can take.  When every printer
is out because its hotend cannot melt the filament, the refusal carries the
one-printer start gate's code (``MATERIAL_EXCEEDS_HOTEND``), and this
install adds ``runs_in_fleet``: per printer, the filaments its stock hotend
runs, read on this computer from the same list the start gate gives one
printer (``runs_here``).  With the router stood in (public tests cannot
import kiln-pro), these pin the door's side:

* the served route on a plain install: the servers' refusal is relayed as
  it came, with ``runs_in_fleet`` added from each candidate's own model;
* the router beside the door: the code, whether to retry and every other
  key the router's refusal carries are relayed, with ``runs_in_fleet``;
* a router older than this release (no help on its refusal) still refuses
  ``NO_ROOM`` exactly as before, and a refusal for room carries no list.
"""

from __future__ import annotations

import sys

import pytest

from tests.test_fleet_plate_doors import _cube, doors  # noqa: F401 -- fixture

MODELS = {"m5": "anker_m5", "a1": "bambu_a1"}
SENTENCE = "No printer in the fleet can melt PEEK -- m5: too cool; a1: too cool. Nothing was routed."
#: The refusal's help as the router builds it:
#: everything but the code and the retry rides at the top of the answer.
HELP = {
    "code": "MATERIAL_EXCEEDS_HOTEND",
    "retryable": False,
    "excluded": [{"printer_id": n, "hotend": {"material": "PEEK", "why": "too cool"}, "why": ["too cool"]} for n in MODELS],
    "enrichment": {"tier": "pro", "fixes": ["material_swap"], "material_swap": {"available": True, "candidates": [
        {"material_id": "cf_nylon", "display_name": "Carbon Fiber Nylon (PA-CF)", "printers": ["a1", "m5"]}]}},
    "swap_plan": {"material": "cf_nylon", "routing": {"recommended_printer": {"printer_id": "a1"}}, "note": "A proposal."},
}


def _runs(model: str) -> dict:
    from kiln.printers.print_gate import _filaments_that_run

    runs = _filaments_that_run(model, "PEEK")
    assert runs and runs["materials"], f"premise: the start gate lists what {model} runs"
    return runs


def _declare(registry, models: dict[str, str | None]) -> None:
    for name, model in models.items():
        if model is not None:
            registry.adapters[name]._printer_model = model


@pytest.fixture
def served_door(doors, monkeypatch):  # noqa: F811 -- the fixture, passed in
    """``route_print_job`` on a plain install, the servers answering *answer*."""
    import kiln.server as srv

    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)

    def make(models: dict[str, str | None], answer: dict):
        d = doors(list(models), survey_missing=True)
        monkeypatch.setitem(sys.modules, "kiln.job_router", None)
        _declare(d.registry, models)
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, _timeout=None, _asked_by_user=True, **kw: answer)
        return d.tools["route_print_job"]

    return make


class TestTheServedRoute:
    def test_the_servers_hotend_refusal_is_relayed_with_what_each_printer_runs(self, served_door, tmp_path):
        answer = {"success": False, "error": {"code": "MATERIAL_EXCEEDS_HOTEND", "message": SENTENCE, "retryable": False},
                  "per_machine": {n: ["too cool"] for n in MODELS},
                  **{k: v for k, v in HELP.items() if k not in ("code", "retryable")}}
        door = served_door(MODELS, dict(answer))
        result = door(_cube(tmp_path / "cube.stl"), material="PEEK")
        assert {k: v for k, v in result.items() if k != "runs_in_fleet"} == answer
        assert result["runs_in_fleet"] == {name: _runs(model) for name, model in MODELS.items()}

    def test_a_printer_nobody_declared_a_model_for_is_left_out_of_the_list(self, served_door, tmp_path):
        answer = {"success": False, "error": {"code": "MATERIAL_EXCEEDS_HOTEND", "message": SENTENCE}}
        door = served_door({"m5": "anker_m5", "mystery": None}, dict(answer))
        result = door(_cube(tmp_path / "cube.stl"), material="PEEK")
        assert list(result["runs_in_fleet"]) == ["m5"]

    def test_a_refusal_for_room_carries_no_list(self, served_door, tmp_path):
        answer = {"success": False, "error": {"code": "NO_ROOM", "message": "No printer ... Nothing was routed."},
                  "per_machine": {"a1": ["the jar"]}}
        door = served_door(MODELS, dict(answer))
        result = door(_cube(tmp_path / "cube.stl"), material="PEEK")
        assert result == answer


class _NoRoomWithHelp(ValueError):
    """The router's refusal as a router with help raises it."""

    def __init__(self, per_machine: dict, help_: dict | None):
        self.per_machine = per_machine
        if help_ is not None:
            self.help = help_
        super().__init__(SENTENCE.removesuffix(". Nothing was routed."))


@pytest.fixture
def local_door(doors, monkeypatch):  # noqa: F811 -- the fixture, passed in
    """``route_print_job`` with the router beside it, refusing with *help_*."""
    def make(help_: dict | None):
        d = doors(list(MODELS), plates={n: {"status": "clear", "has_room": True} for n in MODELS})
        _declare(d.registry, MODELS)
        jr = sys.modules["kiln.job_router"]
        jr.RoutingValidationError = _NoRoomWithHelp

        def refuse(criteria, candidates):
            raise _NoRoomWithHelp({n: ["too cool"] for n in MODELS}, help_)

        d.router.route_job = refuse
        return d.tools["route_print_job"]

    return make


class TestTheRouterBesideTheDoor:
    def test_the_routers_help_is_relayed_with_what_each_printer_runs(self, local_door, tmp_path):
        result = local_door(dict(HELP))(_cube(tmp_path / "cube.stl"), material="PEEK")
        assert result["success"] is False
        assert result["error"] == {"code": "MATERIAL_EXCEEDS_HOTEND", "message": SENTENCE, "retryable": False}
        for key in ("excluded", "enrichment", "swap_plan"):
            assert result[key] == HELP[key], key
        assert result["per_machine"] == {n: ["too cool"] for n in MODELS}
        assert result["runs_in_fleet"] == {name: _runs(model) for name, model in MODELS.items()}

    def test_a_router_older_than_this_release_still_refuses_no_room(self, local_door, tmp_path):
        result = local_door(None)(_cube(tmp_path / "cube.stl"), material="PEEK")
        assert result["error"]["code"] == "NO_ROOM" and result["error"]["message"] == SENTENCE
        assert "runs_in_fleet" not in result and "excluded" not in result

    def test_the_help_never_overwrites_the_doors_own_keys(self, local_door, tmp_path):
        result = local_door({**HELP, "per_machine": {"x": ["forged"]}, "success": True})(
            _cube(tmp_path / "cube.stl"), material="PEEK",
        )
        assert result["success"] is False and result["per_machine"] == {n: ["too cool"] for n in MODELS}


def test_the_door_reads_each_candidates_model_and_never_the_servers(served_door, tmp_path):
    """The list is this install's: built here from the candidates it sent,
    never taken from the answer (Kiln's servers do not build it)."""
    answer = {"success": False, "error": {"code": "MATERIAL_EXCEEDS_HOTEND", "message": SENTENCE},
              "runs_in_fleet": {"a1": {"materials": ["forged"]}}}
    door = served_door(MODELS, dict(answer))
    result = door(_cube(tmp_path / "cube.stl"), material="PEEK")
    assert result["runs_in_fleet"] == {name: _runs(model) for name, model in MODELS.items()}
