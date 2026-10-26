"""A resume is offered only where Kiln has reason to, and the plate question
lands at the pre-flight, before anyone presses start.

* The wire carries the printer's catalogue model word and the person's two
  answers about the resume beside the failure's facts, and never the
  printer's name.
* A plan that arrives with ``not_offered`` keeps it, so the person hears
  why Kiln did not offer to pick the print back up and what to answer.
* ``preflight_check`` asks the plate question the start door asks: a
  recorded part fails the check and carries a look; an unrecorded plate
  passes and says a look would settle it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kiln import print_recovery as pr


def _failure(**overrides):
    base = dict(
        failure_id="F", failure_type=pr.FailureType.LAYER_SHIFT, detected_at="T", printer_name="garage-a1",
        failed_layer=50, total_layers=200, failure_z_mm=10.0, material_type="petg",
        telemetry_snapshot={"hotend_temp": 240, "bed_temp": 80, "serial": "01P00A123456789"},
    )
    base.update(overrides)
    return pr.FailureReport(**base)


class TestTheWireCarriesTheModelAndTheAnswers:
    def test_the_model_word_goes_and_the_name_never_does(self, monkeypatch):
        monkeypatch.setattr(pr, "_printer_model_word", lambda name: "bambu_a1" if name == "garage-a1" else None)
        facts = pr._wire_facts(_failure(), part_attached=True, try_untested_resume=False)
        assert facts["printer_model"] == "bambu_a1"
        assert facts["part_attached"] is True and facts["try_untested_resume"] is False
        assert "garage-a1" not in repr(facts) and "01P00A" not in repr(facts)

    def test_no_printer_on_record_sends_no_model(self):
        facts = pr._wire_facts(_failure(printer_name="nobody-has-this"))
        assert facts["printer_model"] is None
        assert facts["part_attached"] is None and facts["try_untested_resume"] is False

    def test_the_door_passes_the_answers_to_the_wire(self, monkeypatch):
        from kiln import _pro_recovery_bridge as bridge

        sent: list = []

        def ask(kind, facts):
            sent.append(facts)
            return bridge.Asked(included=True, miss=None, answer=None)

        monkeypatch.setattr(bridge, "ask", ask)
        pr.PrintRecovery().plan_recovery(_failure(), part_attached=False, try_untested_resume=True)
        assert sent and sent[0]["part_attached"] is False and sent[0]["try_untested_resume"] is True


class TestAPlanSaysWhyAResumeWasNotOffered:
    def test_not_offered_survives_the_wire_and_the_dict(self):
        held = {
            "strategy": "resume_from_layer", "standing": "untested",
            "why": "Kiln has not yet seen a resume it built work on a bambu a1 mini.", "ask": "try_untested_resume",
        }
        answer = {"plan": {
            "strategy": "safe_abort", "confidence": "low", "resume_layer": None, "layer_overlap": 3,
            "preparation_steps": ["Stop"], "parameter_adjustments": {}, "estimated_success_pct": None,
            "estimated_time_minutes": None, "risks": [], "requires_confirmation": True,
            "reason": "Safe abort", "not_offered": held,
        }, "gcode": None}
        plan = pr._plan_from_wire(answer, _failure())
        assert plan is not None and plan.not_offered == held
        assert plan.to_dict()["not_offered"] == held

    def test_a_plan_with_nothing_held_back_says_so(self):
        plan = pr.RecoveryPlan(plan_id="p", failure_id="f", strategy=pr.RecoveryStrategy.SAFE_ABORT,
                               confidence=pr.RecoveryConfidence.HIGH)
        assert plan.to_dict()["not_offered"] is None


def _state(connected=True):
    from kiln.printers.base import PrinterStatus

    state = MagicMock()
    state.connected = connected
    state.state = PrinterStatus.IDLE
    state.tool_temp_actual = state.bed_temp_actual = 25.0
    state.tool_temp_target = state.bed_temp_target = 0.0
    return state


@pytest.fixture()
def quiet_preflight():
    with (
        patch("kiln.server._get_adapter") as adapter,
        patch("kiln.server._get_temp_limits", return_value=(280.0, 120.0)),
        patch("kiln.server.get_db") as db,
        patch("kiln.server._registry") as registry,
    ):
        adapter.return_value.get_state.return_value = _state()
        registry.count = 1
        registry.list_names.return_value = ["voron"]
        db.return_value.get_printer_learning_insights.return_value = {"total_outcomes": 0}
        yield adapter.return_value


class TestThePreFlightAsksAboutThePlate:
    def test_a_recorded_part_fails_the_pre_flight_and_carries_a_look(self, quiet_preflight, monkeypatch):
        from kiln import plate_state

        occupied = MagicMock()
        occupied.occupied, occupied.clear = True, False
        occupied.to_dict.return_value = {"status": "occupied"}
        monkeypatch.setattr(plate_state, "read", lambda adapter: occupied)
        seen: list = []

        def refusal(adapter, *, resume=False, file_name=None, local_path=None):
            seen.append((resume, file_name, local_path))
            return {
                "success": False,
                "error": {"code": plate_state.START_NOT_YET_CODE, "message": "A part from bracket.gcode is on the plate. Here is the plate now: /tmp/plate.jpg"},
                "plate": {"status": "occupied"}, "snapshot_path": "/tmp/plate.jpg",
            }

        monkeypatch.setattr(plate_state, "start_refusal", refusal)
        from kiln.server import preflight_check

        result = preflight_check(remote_file="next.gcode")
        check = next(c for c in result["checks"] if c["name"] == "plate_clear")
        assert check["passed"] is False and "bracket.gcode" in check["message"]
        assert check["snapshot_path"] == "/tmp/plate.jpg"
        assert result["ready"] is False
        assert seen == [(False, "next.gcode", None)], "the pre-flight asks the start door's own gate"

    def test_an_unrecorded_plate_passes_and_says_a_look_would_settle_it(self, quiet_preflight, monkeypatch):
        from kiln import plate_state

        unknown = MagicMock()
        unknown.occupied, unknown.clear = False, False
        unknown.to_dict.return_value = {"status": "unknown"}
        monkeypatch.setattr(plate_state, "read", lambda adapter: unknown)
        monkeypatch.setattr(plate_state, "start_refusal", lambda adapter, **kw: None)
        from kiln.server import preflight_check

        result = preflight_check()
        check = next(c for c in result["checks"] if c["name"] == "plate_clear")
        assert check["passed"] is True and "look_at_plate" in check["message"]
        assert result["ready"] is True


# --- defects a lap found (2026-10-25; each fails at aa6e2bf9) ---


class TestLapFindings:
    """The first cut judged the plate on its own: an occupied record failed
    the pre-flight.  But every start door runs the pre-flight AFTER its own
    plate gate, and that gate deliberately passes two starts onto an occupied
    plate -- a resume of the paused job, and a file planned beside the part --
    so the pre-flight inside the door refused exactly those starts.  Its tests
    mocked the record and never ran a door; an independent review and the lap
    both caught it."""

    def _occupied(self, monkeypatch):
        from kiln import plate_state

        occupied = MagicMock()
        occupied.occupied, occupied.clear = True, False
        occupied.to_dict.return_value = {"status": "occupied"}
        monkeypatch.setattr(plate_state, "read", lambda adapter: occupied)
        monkeypatch.setattr(plate_state, "offer_look", lambda adapter, state=None, **kw: MagicMock(sentence="", fields=lambda: {}))
        return plate_state

    def test_inside_a_start_door_the_door_owns_the_plate_gate(self, quiet_preflight, monkeypatch):
        self._occupied(monkeypatch)
        from kiln.server import preflight_check

        result = preflight_check(start_door_gates_plate=True)
        check = next(c for c in result["checks"] if c["name"] == "plate_clear")
        assert check["passed"] is True and result["ready"] is True

    def test_a_resume_onto_the_part_passes_a_standalone_pre_flight(self, quiet_preflight, monkeypatch):
        self._occupied(monkeypatch)
        from kiln.server import preflight_check

        check = next(c for c in preflight_check(accept_paused=True)["checks"] if c["name"] == "plate_clear")
        assert check["passed"] is True

    def test_every_start_door_says_it_owns_the_plate_gate(self):
        """Structural pin: every call to ``preflight_check`` in Kiln's own
        code is a start door (the plate gate ran first), so each says so.  A
        new door that forgets would refuse the starts its own gate let through."""
        import ast
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src" / "kiln"
        missing = []
        for path in src.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    fn = node.func
                    name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                    if name == "preflight_check":
                        kws = {k.arg: k.value for k in node.keywords}
                        flag = kws.get("start_door_gates_plate")
                        if not (isinstance(flag, ast.Constant) and flag.value is True):
                            missing.append(f"{path.relative_to(src)}:{node.lineno}")
        assert not missing, f"start doors that run the pre-flight without saying they gate the plate: {missing}"
