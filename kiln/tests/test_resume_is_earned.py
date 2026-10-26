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
        occupied.start_refusal_sentence.return_value = "A part from bracket.gcode is on the plate."
        occupied.to_dict.return_value = {"status": "occupied"}
        monkeypatch.setattr(plate_state, "read", lambda adapter: occupied)
        offer = MagicMock()
        offer.sentence = "Here is the plate now: /tmp/plate.jpg"
        offer.fields.return_value = {"snapshot_path": "/tmp/plate.jpg", "look": {"camera": "own"}}
        monkeypatch.setattr(plate_state, "offer_look", lambda adapter, state: offer)

        from kiln.server import preflight_check

        result = preflight_check()
        check = next(c for c in result["checks"] if c["name"] == "plate_clear")
        assert check["passed"] is False
        assert "bracket.gcode" in check["message"] and "/tmp/plate.jpg" in check["message"]
        assert check["snapshot_path"] == "/tmp/plate.jpg"
        assert result["ready"] is False
        assert any("plate" in e.lower() for e in result["errors"])

    def test_an_unrecorded_plate_passes_and_says_a_look_would_settle_it(self, quiet_preflight, monkeypatch):
        from kiln import plate_state

        unknown = MagicMock()
        unknown.occupied, unknown.clear = False, False
        unknown.to_dict.return_value = {"status": "unknown"}
        monkeypatch.setattr(plate_state, "read", lambda adapter: unknown)

        from kiln.server import preflight_check

        result = preflight_check()
        check = next(c for c in result["checks"] if c["name"] == "plate_clear")
        assert check["passed"] is True and "look_at_plate" in check["message"]
        assert result["ready"] is True

    def test_a_recorded_clear_plate_passes(self, quiet_preflight, monkeypatch):
        from kiln import plate_state

        clear = MagicMock()
        clear.occupied, clear.clear = False, True
        clear.to_dict.return_value = {"status": "clear"}
        monkeypatch.setattr(plate_state, "read", lambda adapter: clear)

        from kiln.server import preflight_check

        check = next(c for c in preflight_check()["checks"] if c["name"] == "plate_clear")
        assert check["passed"] is True and "clear" in check["message"]
