"""The device doors a plain install used to drop, served -- and their misses said.

Measured 2026-10-17: without kiln-pro beside it, public Kiln dropped the
nozzle-clumping judgement, the abrasive escalation, the nozzle summary, every
flow anomaly the five adapters read, and the per-model start reading its own
generic line promises "free with a Kiln sign-in".  Each now asks Kiln's
servers.  Pinned here against a stand-in for the servers that answers in the
real tools' shapes:

* a start on a Bambu reads the model's own stages, asked once per model; the
  layer-0 line reads the stage a nozzle target implies, once per target;
* a signed-in install that could not ask is told why instead of being
  pointed at a sign-in it already has; a signed-out one keeps the pointer;
* the pre-flight carries the clumping judgement, or a line saying why not;
* a Pro answer's abrasive escalation reaches recommend_material as the
  NOZZLE ADVISORY, and a lower plan's refusal leaves the floor;
* a flow anomaly is reported once per window, off the calling thread.

The real tools at each tier, through the hosted trace, are pinned on the
kiln-pro side (``tests/test_device_doors_served.py``).
"""

from __future__ import annotations

import builtins
import threading
from typing import Any

import pytest

import kiln._pro_nozzle_bridge as nozzle
import kiln.server as srv
import kiln.start_narrative as narrative

STAGES = ["Watched on an A1.", "Heat and chime -- nothing moves yet.", "Cutter -- the clack at the right end."]
CANCEL = "The head cuts the filament at the right end, then parks."


class _Servers:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.tier = "free"
        self.offline = False
        self.signed_out = False

    def __call__(self, tool_name: str, _timeout: float | None = None, _asked_by_user: bool = True, **kw: Any):
        self.calls.append((tool_name, dict(kw)))
        if self.offline:
            return {"status": "error", "success": False, "code": "SERVER_UNREACHABLE", "error": "x", "why": "offline"}
        if self.signed_out:
            return {"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "sign in", "why": "signed_out"}
        if tool_name == "start_stages":
            known = kw["printer_model"] == "bambu_a1"
            target = kw.get("nozzle_target_c")
            return {
                "success": True, "printer_model": kw["printer_model"], "known": known,
                "verified": known, "stages": STAGES if known else [], "cancel": CANCEL if known else None,
                "cancel_verified": known,
                "stage_reading": "Nozzle target 250 C is the AMS load and flush." if known and target == 250.0 else None,
            }
        if tool_name == "clumping_detection":
            return {"success": True, "printer_model": kw["printer_model"],
                    "block": {"statement": "On, read off the printer.", "warnings": ["No prime tower: ooze lands on the print."]}}
        if tool_name == "abrasive_escalation":
            if self.tier == "free":
                return {"success": False, "code": "TIER_REQUIRED", "error": "Unlock nozzle intelligence with Kiln Pro."}
            return {"success": True, "severity": "red", "escalated": True, "filament_class": "abrasive",
                    "nozzle_material": "brass", "user_warning": f"{kw['material']} on brass wears the bore fast."}
        if tool_name == "get_nozzle_state":
            return {"success": True, "found": True, "trusted_for_verdicts": True, "material": "brass",
                    "diameter_mm": 0.4, "provenance": "user_override", "grams_through": 120.0}
        if tool_name == "record_extrusion_event":
            return {"success": True, "status": "ok"}
        raise AssertionError(f"unexpected wire {tool_name}")


@pytest.fixture
def servers(monkeypatch):
    """A plain install (no kiln-pro), its served door answered by the stand-in."""
    real_import = builtins.__import__

    def _no_pro(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "kiln_pro" or name.startswith("kiln_pro."):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    fake = _Servers()
    monkeypatch.setattr(builtins, "__import__", _no_pro)
    monkeypatch.setattr(srv, "_pro_api_call", fake)
    return fake


class _Adapter:
    name = "bambu"

    def __init__(self, model: str = "bambu_a1") -> None:
        self._printer_model = model


def _asked(fake: _Servers, tool: str) -> list[dict[str, Any]]:
    return [kw for name, kw in fake.calls if name == tool]


class TestTheStartReading:
    def test_a_start_reads_the_models_own_stages_once_per_model(self, servers):
        assert narrative.start_narrative(_Adapter()) == STAGES
        assert narrative.cancel_narrative(_Adapter()) == [CANCEL]
        assert _asked(servers, "start_stages") == [{"printer_model": "bambu_a1"}]

    def test_the_layer_zero_line_reads_the_stage_a_target_implies(self, servers):
        line = narrative.start_stage_line(_Adapter(), nozzle_target_c=250.0, layer=0)
        assert line == "Nozzle target 250 C is the AMS load and flush."
        narrative.start_stage_line(_Adapter(), nozzle_target_c=250.0, layer=0)
        assert len(_asked(servers, "start_stages")) == 1, "one ask per target"

    def test_a_model_with_no_reading_keeps_the_generic_line(self, servers):
        assert narrative.start_narrative(_Adapter("bambu_x1c")) == [narrative.GENERIC_BAMBU_START]

    def test_signed_out_keeps_the_sign_in_pointer(self, servers):
        servers.signed_out = True
        assert narrative.start_narrative(_Adapter()) == [narrative.GENERIC_BAMBU_START]

    def test_signed_in_but_offline_says_why_instead_of_pointing_at_a_sign_in(self, servers):
        servers.offline = True
        (line,) = narrative.start_narrative(_Adapter())
        assert "free with a Kiln sign-in" not in line
        assert line.startswith("Bambu's own start sequence runs first")
        assert "Kiln can't get this model's stage-by-stage reading right now (this computer is offline)" in line
        assert line.endswith(".")
        stage = narrative.start_stage_line(_Adapter(), nozzle_target_c=250.0, layer=0)
        assert "(this computer is offline)" in stage


class TestTheClumpingJudgement:
    def test_the_judgement_arrives_with_the_files_facts(self, servers):
        block = nozzle.consult_clumping_detection(
            printer_model="bambu_a1", reading={"enabled": True},
            file={"print_mode": "vase", "prime_tower_in_file": False, "footprint": None},
        )
        assert block["warnings"] == ["No prime tower: ooze lands on the print."]
        assert _asked(servers, "clumping_detection") == [{
            "printer_model": "bambu_a1", "reading": {"enabled": True}, "print_mode": "vase", "prime_tower_in_file": False,
        }]
        assert nozzle.clumping_unchecked("bambu_a1") is None

    def test_a_miss_is_a_line_the_pre_flight_carries(self, servers):
        servers.signed_out = True
        assert nozzle.consult_clumping_detection(printer_model="bambu_a1", reading={"enabled": True}, file=None) is None
        gap = nozzle.clumping_unchecked("bambu_a1")
        assert gap["word"] == "unchecked" and gap["why"] == "signed_out"
        assert "Kiln can't judge the switch against the file right now because Kiln is signed out" in gap["line"]


class TestTheAbrasiveEscalation:
    def test_a_pro_answer_arrives_in_the_shape_the_recommendation_reads(self, servers):
        # recommend_material prepends the NOZZLE ADVISORY on exactly this
        # shape (red, raised by the nozzle); test_material_routing pins that.
        servers.tier = "pro"
        verdict = nozzle.consult_abrasive_escalation("PETG-CF", "shop_a1")
        assert verdict["severity"] == "red" and verdict["escalated"] is True
        assert _asked(servers, "abrasive_escalation") == [{"material": "PETG-CF", "printer_id": "shop_a1"}]

    def test_a_lower_plans_refusal_leaves_the_floor(self, servers):
        servers.tier = "free"
        assert nozzle.consult_abrasive_escalation("PETG-CF", "shop_a1") is None


class TestTheNozzleSummary:
    def test_one_request_answers_the_summary_and_the_record(self, servers):
        summary = nozzle.consult_nozzle_summary("shop_a1")
        assert summary == {"material": "brass", "diameter_mm": 0.4, "provenance": "user_override",
                           "grams_through": 120.0, "trusted_for_verdicts": True}
        assert nozzle.consult_recorded_nozzle("shop_a1") == {"diameter_mm": 0.4, "answered": True}
        assert len(_asked(servers, "get_nozzle_state")) == 1


class TestAFlowAnomaly:
    def test_reported_once_per_window_and_never_on_the_calling_thread(self, servers, monkeypatch):
        seen_threads: list[str] = []
        real = servers.__call__

        def spy(tool_name, _timeout=None, _asked_by_user=True, **kw):
            seen_threads.append(threading.current_thread().name)
            return real(tool_name, _timeout, _asked_by_user, **kw)

        monkeypatch.setattr(srv, "_pro_api_call", spy)
        for _ in range(4):
            nozzle.report_flow_anomaly("shop_a1", "under_extrusion", "medium")
        nozzle.report_flow_anomaly("shop_a1", "clog_warn", "high")
        for t in threading.enumerate():
            if t.name == "kiln-flow-report":
                t.join(5)
        assert _asked(servers, "record_extrusion_event") == [
            {"printer_id": "shop_a1", "event_type": "under_extrusion", "severity": "medium"},
            {"printer_id": "shop_a1", "event_type": "clog_warn", "severity": "high"},
        ]
        assert seen_threads and all(name == "kiln-flow-report" for name in seen_threads)

    def test_every_adapter_reports_through_the_one_door(self):
        import inspect

        from kiln.printers import bambu, elegoo, moonraker, octoprint, prusalink

        for module in (bambu, elegoo, moonraker, octoprint, prusalink):
            source = inspect.getsource(module)
            assert "report_flow_anomaly(" in source, module.__name__
            assert "record_extrusion_event_for_printer" not in source, module.__name__
