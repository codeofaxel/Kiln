"""The fleet routing doors read each candidate's plate, and route onto none they cannot see.

``route_print_job``, ``fleet_submit_job`` and ``suggest_printer_for_job``
now carry a ``plate`` block per printer from kiln-pro's fleet survey
(``kiln.placement_fleet.for_fleet``: the single-machine placement verdict
asked once per machine).  Public Kiln owns the doors and the job envelope
it reads from the file; the survey, the block and the router's reading of
it are kiln-pro's and pinned there.  These tests stand in a fake survey and
a fake router (public tests cannot import kiln-pro) and pin what the DOORS
do with the answer:

* ``route_print_job`` hands every candidate the job's envelope and the
  registry's own adapters, attaches the block to each candidate before the
  router sees them, and relays a no-room refusal with per-machine sentences;
* ``fleet_submit_job`` surveys the named printer or the whole fleet before
  anything is queued, refuses ``NO_ROOM`` with each machine's sentence and
  never reaches the orchestrator -- and leaves the preview sign-off standing
  for the retry;
* the survey's tier gate is relayed as it is, one gate;
* a server without kiln-pro's survey refuses honestly, never routes blind;
* ``suggest_printer_for_job`` says ``has_room`` per suggestion and, when the
  survey is not available, why not;
* the job envelope: a mesh carries its size, a sliced file carries its path.
"""

from __future__ import annotations

import struct
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiln import _pro_placement_bridge as bridge


def _cube(path: Path, size: float = 20.0) -> str:
    v = [(0, 0, 0), (size, 0, 0), (size, size, 0), (0, size, 0), (0, 0, size), (size, 0, size), (size, size, size), (0, size, size)]
    faces = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(faces)))
        for a, b, c in faces:
            fh.write(struct.pack("<3f", 0, 0, 0))
            for i in (a, b, c):
                fh.write(struct.pack("<3f", *v[i]))
            fh.write(struct.pack("<H", 0))
    return str(path)


def _block(**over) -> dict:
    p = {"status": "clear", "has_room": True, "spot_mm": None, "start": "vendor_start", "occupied_count": 0, "refusals": [], "unknowns": []}
    p.update(over)
    return p


class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


class _Registry:
    def __init__(self, names):
        self.adapters = {n: SimpleNamespace(name=n, get_state=lambda: SimpleNamespace(state=SimpleNamespace(value="idle"))) for n in names}

    def list_names(self):
        return list(self.adapters)

    def get(self, name):
        return self.adapters[name]

    def get_idle_printers(self):
        return list(self.adapters)


class _Survey:
    """A scripted kiln.placement_fleet: records every ask, answers one thing."""

    def __init__(self, plates: dict[str, dict] | None = None, gate: dict | None = None):
        self.plates, self.gate, self.asked = plates or {}, gate, []

    def for_fleet(self, job, names, *, adapters=None, **kw):
        self.asked.append({"job": job, "names": list(names), "adapters": adapters})
        if self.gate is not None:
            return {"ok": False, "plates": {}, "gate": dict(self.gate)}
        return {"ok": True, "plates": {n: self.plates.get(n, _block()) for n in names}, "gate": None}


class _NoRoom(ValueError):
    def __init__(self, per_machine):
        self.per_machine = per_machine
        super().__init__("No printer in the fleet can take this part as the plates stand")


class _Router:
    """A router that honours only the plate rule, and says what it saw."""

    def __init__(self):
        self.seen: list[list[dict]] = []

    def route_job(self, criteria, candidates):
        self.seen.append([dict(c) for c in candidates])
        room = [c for c in candidates if isinstance(c.get("plate"), dict) and c["plate"]["has_room"] and c["plate"]["status"] != "unknown"]
        if not room:
            raise _NoRoom({c["printer_id"]: list(c["plate"]["refusals"]) for c in candidates})
        return SimpleNamespace(to_dict=lambda: {"recommended_printer": {"printer_id": room[0]["printer_id"], "plate": room[0]["plate"]}, "alternatives": [], "excluded": []})


@pytest.fixture
def doors(monkeypatch, tmp_path):
    """The real public doors with kiln-pro's two modules stood in."""
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
    import kiln.server as srv
    from kiln.plugins import fleet_tools, learning_tools
    from kiln.queue import PrintQueue

    monkeypatch.setattr(srv, "_check_auth", lambda scope: None)
    monkeypatch.setattr(srv, "requires_tier", lambda tier: (lambda fn: fn), raising=False)
    monkeypatch.setattr(srv, "_get_queue", lambda: PrintQueue())

    def make(names, *, plates=None, gate=None, survey_missing=False, submitted=None):
        registry = _Registry(names)
        monkeypatch.setattr(srv, "_get_registry", lambda: registry)
        monkeypatch.setattr(srv, "_registry", registry, raising=False)
        survey = _Survey(plates, gate)
        router = _Router()
        if survey_missing:
            monkeypatch.setitem(sys.modules, "kiln.placement_fleet", None)
        else:
            mod = types.ModuleType("kiln.placement_fleet")
            mod.for_fleet = survey.for_fleet
            monkeypatch.setitem(sys.modules, "kiln.placement_fleet", mod)
        jr = types.ModuleType("kiln.job_router")
        jr.RoutingCriteria = lambda **kw: SimpleNamespace(**kw)
        jr.RoutingValidationError = _NoRoom
        jr.get_job_router = lambda: router
        monkeypatch.setitem(sys.modules, "kiln.job_router", jr)
        orch = SimpleNamespace(calls=[])

        def submit_job_result(file_path, **kw):
            orch.calls.append({"file_path": file_path, **kw})
            return SimpleNamespace(job_id="job-1", to_dict=lambda: {"job_id": "job-1"}), False

        orch.submit_job_result = submit_job_result
        fo = types.ModuleType("kiln.fleet_orchestrator")
        fo.get_fleet_orchestrator = lambda: orch
        monkeypatch.setitem(sys.modules, "kiln.fleet_orchestrator", fo)
        mcp = _FakeMCP()
        fleet_tools._FleetToolsPlugin().register(mcp)
        learning_tools._LearningToolsPlugin().register(mcp)
        return SimpleNamespace(tools=mcp.tools, survey=survey, router=router, orch=orch, registry=registry)

    return make


class TestRoutePrintJob:
    def test_every_candidate_carries_its_plate_before_the_router_sees_it(self, doors, tmp_path):
        d = doors(["a", "b"], plates={"a": _block(), "b": _block(status="occupied", has_room=True, start="quiet_start", spot_mm=[40.0, 40.0], occupied_count=1)})
        path = _cube(tmp_path / "cube.stl")
        result = d.tools["route_print_job"](path, material="PLA")
        assert result["success"] is True, result
        # The survey got the job's envelope and the registry's own adapters.
        ask = d.survey.asked[0]
        assert ask["names"] == ["a", "b"]
        assert ask["job"]["part"]["size_mm"] == [20.0, 20.0, 20.0] and ask["job"]["sliced_gcode_path"] is None
        assert ask["adapters"] == {"a": d.registry.get("a"), "b": d.registry.get("b")}
        # The router saw the block on each candidate.
        by_id = {c["printer_id"]: c["plate"] for c in d.router.seen[0]}
        assert by_id["a"]["start"] == "vendor_start" and by_id["b"]["start"] == "quiet_start"
        assert result["routing"]["recommended_printer"]["plate"] == _block()

    def test_no_room_anywhere_is_refused_with_each_machines_sentence(self, doors, tmp_path):
        d = doors(["a", "b"], plates={
            "a": _block(has_room=False, refusals=["the jar is in the way"]),
            "b": _block(status="unknown", has_room=False, refusals=["nobody has looked at this plate"], unknowns=["plate record"]),
        })
        result = d.tools["route_print_job"](_cube(tmp_path / "cube.stl"), material="PLA")
        assert result["success"] is False
        assert result["error"]["code"] == "NO_ROOM"
        assert result["per_machine"] == {"a": ["the jar is in the way"], "b": ["nobody has looked at this plate"]}
        assert result["plates"]["b"]["status"] == "unknown"
        assert "Nothing was routed" in result["error"]["message"]

    def test_the_surveys_gate_is_relayed_as_it_is(self, doors, tmp_path):
        gate = {"success": False, "error": "Fleet plate placement requires Kiln Business", "code": "TIER_REQUIRED", "required_tier": "business"}
        d = doors(["a"], gate=gate)
        result = d.tools["route_print_job"](_cube(tmp_path / "cube.stl"), material="PLA")
        assert result == gate
        assert d.router.seen == [], "nothing is routed past a gate"

    def test_without_the_survey_routing_refuses_rather_than_routing_blind(self, doors, tmp_path):
        d = doors(["a"], survey_missing=True)
        result = d.tools["route_print_job"](_cube(tmp_path / "cube.stl"), material="PLA")
        assert result["success"] is False and result["error"]["code"] == "ROUTING_UNAVAILABLE"
        assert d.router.seen == []


class TestFleetSubmitJob:
    def test_no_room_refuses_at_submission_and_the_orchestrator_never_hears_of_it(self, doors, tmp_path):

        d = doors(["a", "b"], plates={
            "a": _block(status="occupied", has_room=False, refusals=["part.gcode was not sliced for a start beside what is on a's plate"]),
            "b": _block(status="unknown", has_room=False, refusals=["Kiln does not know what is on this plate"]),
        })
        path = tmp_path / "part.gcode"
        path.write_text("G28\n")
        result = d.tools["fleet_submit_job"](str(path), material="PLA")
        assert result["success"] is False
        assert result["error"]["code"] == "NO_ROOM"
        assert "Nothing was queued" in result["error"]["message"]
        assert list(result["per_machine"]) == ["a", "b"]
        assert "not sliced for a start" in result["per_machine"]["a"][0]
        assert d.orch.calls == []
        ask = d.survey.asked[0]
        assert ask["names"] == ["a", "b"] and ask["job"]["sliced_gcode_path"] == str(path)

    def test_a_named_printer_is_surveyed_alone(self, doors, tmp_path):
        d = doors(["a", "b"], plates={"b": _block(has_room=False, refusals=["no room on b"])})
        path = tmp_path / "part.gcode"
        path.write_text("G28\n")
        result = d.tools["fleet_submit_job"](str(path), printer_name="b")
        assert result["success"] is False and result["per_machine"] == {"b": ["no room on b"]}
        assert d.survey.asked[0]["names"] == ["b"]
        assert d.orch.calls == []

    def test_with_room_the_job_is_queued_and_the_answer_says_which_plates_have_it(self, doors, tmp_path):
        d = doors(["a", "b"], plates={"a": _block(), "b": _block(status="occupied", has_room=False, refusals=["the jar"])})
        path = tmp_path / "part.gcode"
        path.write_text("G28\n")
        result = d.tools["fleet_submit_job"](str(path), material="PLA")
        assert result["success"] is True, result
        assert result["submission"] == "queued"
        assert result["plates"]["a"]["has_room"] is True and result["plates"]["b"]["has_room"] is False
        assert len(d.orch.calls) == 1 and d.orch.calls[0]["file_path"] == str(path)

    def test_the_refusal_comes_before_the_sign_off_is_spent(self, doors, tmp_path, monkeypatch):
        """A person's yes on the preview stands for the retry that clears a plate."""
        from kiln import print_signoff

        d = doors(["a"], plates={"a": _block(has_room=False, refusals=["the jar"])})
        cleared: list[bool] = []
        monkeypatch.setattr(print_signoff, "clear", lambda: cleared.append(True))
        path = tmp_path / "part.gcode"
        path.write_text("G28\n")
        result = d.tools["fleet_submit_job"](str(path))
        assert result["error"]["code"] == "NO_ROOM"
        assert cleared == []

    def test_the_gate_and_a_missing_survey_are_honest_refusals(self, doors, tmp_path):
        path = tmp_path / "part.gcode"
        path.write_text("G28\n")
        gate = {"success": False, "error": "Fleet plate placement requires Kiln Business", "code": "TIER_REQUIRED"}
        d = doors(["a"], gate=gate)
        assert d.tools["fleet_submit_job"](str(path)) == gate
        assert d.orch.calls == []
        d = doors(["a"], survey_missing=True)
        result = d.tools["fleet_submit_job"](str(path))
        assert result["success"] is False and result["error"]["code"] == "ROUTING_UNAVAILABLE"
        assert d.orch.calls == []


class TestSuggestPrinterForJob:
    def test_each_suggestion_says_has_room_from_its_plate(self, doors, monkeypatch):
        import kiln.persistence as persistence

        ranked = [{"printer_name": "a", "success_rate": 0.9, "total_prints": 4}, {"printer_name": "b", "success_rate": 0.8, "total_prints": 2}]
        monkeypatch.setattr(persistence, "get_db", lambda: SimpleNamespace(suggest_printer_for_outcome=lambda **kw: list(ranked)))
        d = doors(["a", "b"], plates={"a": _block(), "b": _block(status="occupied", has_room=False, refusals=["the jar"], occupied_count=1)})
        result = d.tools["suggest_printer_for_job"](material_type="PLA", file_name="cube.stl")
        assert result["success"] is True, result
        rooms = {s["printer_name"]: (s["has_room"], s["plate"]["status"]) for s in result["suggestions"]}
        assert rooms == {"a": (True, "clear"), "b": (False, "occupied")}
        assert result["plate_note"] is None
        ask = d.survey.asked[0]
        assert ask["job"] == {"file": "cube.stl", "part": None, "sliced_gcode_path": None}
        assert ask["adapters"] == {"a": d.registry.get("a"), "b": d.registry.get("b")}

    def test_below_the_tier_or_without_kiln_pro_has_room_is_null_and_the_note_says_why(self, doors, monkeypatch):
        import kiln.persistence as persistence

        ranked = [{"printer_name": "a", "success_rate": 0.9, "total_prints": 4}]
        monkeypatch.setattr(persistence, "get_db", lambda: SimpleNamespace(suggest_printer_for_outcome=lambda **kw: list(ranked)))
        d = doors(["a"], gate={"success": False, "error": "Fleet plate placement requires Kiln Business", "code": "TIER_REQUIRED"})
        result = d.tools["suggest_printer_for_job"](material_type="PLA")
        assert result["success"] is True
        assert result["suggestions"][0]["has_room"] is None and result["suggestions"][0]["plate"] is None
        assert result["plate_note"] == "Fleet plate placement requires Kiln Business"
        d = doors(["a"], survey_missing=True)
        result = d.tools["suggest_printer_for_job"](material_type="PLA")
        assert result["success"] is True
        assert result["suggestions"][0]["has_room"] is None
        assert "kiln-pro" in result["plate_note"]


class TestTheJobEnvelope:
    def test_a_mesh_carries_its_size_and_a_sliced_file_its_path(self, tmp_path):
        mesh = bridge.job_envelope(_cube(tmp_path / "cube.stl", size=30.0))
        assert mesh["file"] == "cube.stl" and mesh["sliced_gcode_path"] is None
        assert mesh["part"]["size_mm"] == [30.0, 30.0, 30.0]
        assert mesh["part"]["skirt_mm"] == 6.0 and mesh["part"]["layer_height_mm"] == 0.2
        gcode = tmp_path / "part.gcode"
        gcode.write_text(";LAYER_CHANGE\n;Z:0.4\nG1 X1 Y1 E1\n")
        sliced = bridge.job_envelope(str(gcode))
        assert sliced == {"file": "part.gcode", "part": None, "sliced_gcode_path": str(gcode)}

    def test_an_unreadable_file_is_an_envelope_with_no_part_never_an_error(self, tmp_path):
        assert bridge.job_envelope(str(tmp_path / "missing.stl")) == {"file": "missing.stl", "part": None, "sliced_gcode_path": None}
