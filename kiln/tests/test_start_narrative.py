"""What a Bambu start and cancel look like, said on every door.

2026-09-16, a Bambu A1: three times in one night the head made a move that
looked like a crash -- the filament cutter firing into its stop at the far
right of the rail, a hot flush pushed off the left edge of the plate into the
purge chute, a Z home by touch on the bare steel behind the plate -- and the
person at the machine cut the power each time.  All three are in the vendor's
own start file.  Nothing Kiln said had warned them.

Public Kiln owns the MECHANISM these tests pin: one ``what_you_will_see``
field beside the message on every start door (through the shared verdict) and
on the cancel engine, absent off Bambu, and one early-stage line in the
monitor report while the start block is still running.  The per-model,
stage-by-stage reading is kiln-pro's; here it is a fake bridge, so these tests
say nothing about what any model's list contains beyond the generic line.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock

import pytest

from kiln import start_narrative
from kiln.print_start_verdict import resolve_print_start
from kiln.printers.base import PrinterState, PrinterStatus, PrintResult
from kiln.start_narrative import (
    GENERIC_BAMBU_CANCEL,
    GENERIC_BAMBU_START,
    GENERIC_BAMBU_START_STAGE_LINE,
    cancel_narrative,
    start_stage_line,
)
from kiln.start_narrative import (
    start_narrative as start_lines,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Adapter:
    """A printer that answers a start with *result* and reports *status* live."""

    def __init__(
        self,
        *,
        name: str,
        model: str = "",
        result: PrintResult | None = None,
        status: PrinterStatus = PrinterStatus.PRINTING,
    ) -> None:
        self.name = name
        self._printer_model = model
        self._result = result or PrintResult(success=True, message="Started printing part.3mf.")
        self._status = status

    def get_state(self) -> PrinterState:
        return PrinterState(connected=True, state=self._status)


_A1_STAGES = [
    "Heat and chime.",
    "A bang at the far RIGHT end of the rail is the filament cutter, not a crash.",
    "A hot blob off the LEFT edge of the plate is the flush into the purge chute.",
]
_A1_CANCEL = "The head cuts the filament at the far right end of the rail, lifts, and parks."
_A1_BLOCK = {"known": True, "stages": list(_A1_STAGES), "cancel": _A1_CANCEL}


def _fake_pro(*, block: dict | None = _A1_BLOCK, reading: str | None = None, available: bool = True):
    di = SimpleNamespace(
        start_stages_block=lambda model: block,
        start_stage_reading=lambda model, nozzle_target_c: reading,
    )
    return SimpleNamespace(
        is_available=lambda feature: available and feature == "device_intelligence",
        device_intelligence=di,
    )


@pytest.fixture
def no_pro(monkeypatch):
    """The bridge is not installed: the generic line is the whole answer."""
    monkeypatch.setattr(start_narrative, "_pro_block", lambda model: None)


def _verdict_dict(adapter: _Adapter, **kw) -> dict:
    return resolve_print_start(
        adapter, adapter._result, sent_at=time.monotonic(), file_name="part.3mf", **kw
    ).to_dict()


# ---------------------------------------------------------------------------
# The start doors, through the one verdict they all publish
# ---------------------------------------------------------------------------


class TestTheStartField:
    def test_a_bambu_start_carries_what_you_will_see(self, no_pro):
        out = _verdict_dict(_Adapter(name="bambu", model="bambu_a1"))
        assert out["success"] is True
        assert out["what_you_will_see"] == [GENERIC_BAMBU_START]

    def test_the_generic_line_names_the_three_moves_that_read_as_crashes(self):
        line = GENERIC_BAMBU_START.lower()
        assert "cutter" in line
        assert "flush" in line
        assert "behind the plate" in line
        assert "sign-in" in line, "the per-model reading is free with a Kiln sign-in, and the line says so"

    def test_an_accepted_but_unconfirmed_start_carries_it_too(self, no_pro):
        adapter = _Adapter(name="bambu", model="bambu_a1", status=PrinterStatus.BUSY)
        out = _verdict_dict(adapter)
        assert out["print_start"] == "accepted"
        assert out["what_you_will_see"] == [GENERIC_BAMBU_START]

    def test_a_non_bambu_start_carries_no_field(self, no_pro):
        for backend in ("octoprint", "moonraker", "prusalink", "elegoo", "serial"):
            out = _verdict_dict(_Adapter(name=backend, model="prusa_mk4"))
            assert out["success"] is True
            assert "what_you_will_see" not in out, backend

    def test_a_refused_start_carries_no_field(self, no_pro):
        adapter = _Adapter(
            name="bambu",
            model="bambu_a1",
            result=PrintResult(success=False, message="Printer refused the job."),
            status=PrinterStatus.IDLE,
        )
        out = _verdict_dict(adapter)
        assert out["print_start"] == "failed"
        assert "what_you_will_see" not in out

    def test_a_resume_3mf_start_carries_no_field(self, no_pro):
        """A resume preamble is heat, lift, home and travel -- not the vendor's
        load and calibration -- so the list would describe moves that file
        never makes."""
        out = _verdict_dict(_Adapter(name="bambu", model="bambu_a1"), vendor_start_block=False)
        assert out["success"] is True
        assert "what_you_will_see" not in out

    def test_kiln_pro_reading_replaces_the_generic_line(self, monkeypatch):
        monkeypatch.setattr(start_narrative, "_pro_block", lambda model: dict(_A1_BLOCK))
        out = _verdict_dict(_Adapter(name="bambu", model="bambu_a1"))
        assert out["what_you_will_see"] == _A1_STAGES
        assert GENERIC_BAMBU_START not in out["what_you_will_see"]

    def test_a_model_kiln_pro_has_no_row_for_gets_the_generic_line(self, monkeypatch):
        monkeypatch.setattr(start_narrative, "_pro_block", lambda model: None)
        out = _verdict_dict(_Adapter(name="bambu", model="bambu_h2d"))
        assert out["what_you_will_see"] == [GENERIC_BAMBU_START]

    def test_a_failing_bridge_never_fails_the_start(self, monkeypatch):
        def _boom(model):
            raise RuntimeError("overlay unreachable")

        monkeypatch.setattr(start_narrative, "_pro_block", _boom)
        out = _verdict_dict(_Adapter(name="bambu", model="bambu_a1"))
        assert out["success"] is True
        assert "what_you_will_see" not in out


class TestTheBridgeCall:
    """The helper asks kiln-pro through the documented import surface and
    believes only a block that says it knows the model."""

    def test_the_block_is_read_through_pro_features(self):
        with mock.patch.dict("sys.modules", {"kiln_pro": mock.MagicMock(), "kiln_pro.bridge": SimpleNamespace(pro_features=_fake_pro())}):
            assert start_lines(_Adapter(name="bambu", model="bambu_a1")) == _A1_STAGES
            assert cancel_narrative(_Adapter(name="bambu", model="bambu_a1")) == [_A1_CANCEL]

    def test_an_unknown_block_falls_back_to_the_generic_lines(self):
        pro = _fake_pro(block={"known": False, "stages": [], "cancel": None})
        with mock.patch.dict("sys.modules", {"kiln_pro": mock.MagicMock(), "kiln_pro.bridge": SimpleNamespace(pro_features=pro)}):
            assert start_lines(_Adapter(name="bambu", model="bambu_a1")) == [GENERIC_BAMBU_START]
            assert cancel_narrative(_Adapter(name="bambu", model="bambu_a1")) == [GENERIC_BAMBU_CANCEL]

    def test_a_feature_the_caller_is_not_entitled_to_is_the_generic_line(self):
        pro = _fake_pro(available=False)
        with mock.patch.dict("sys.modules", {"kiln_pro": mock.MagicMock(), "kiln_pro.bridge": SimpleNamespace(pro_features=pro)}):
            assert start_lines(_Adapter(name="bambu", model="bambu_a1")) == [GENERIC_BAMBU_START]

    def test_no_kiln_pro_installed_is_the_generic_line(self):
        with mock.patch.dict("sys.modules", {"kiln_pro": None, "kiln_pro.bridge": None}):
            assert start_lines(_Adapter(name="bambu", model="bambu_a1")) == [GENERIC_BAMBU_START]
            assert cancel_narrative(_Adapter(name="bambu", model="bambu_a1")) == [GENERIC_BAMBU_CANCEL]

    def test_an_undeclared_model_never_asks_and_gets_the_generic_line(self):
        asked: list[str] = []

        def _record(model):
            asked.append(model)
            return _A1_BLOCK

        pro = _fake_pro()
        pro.device_intelligence = SimpleNamespace(start_stages_block=_record, start_stage_reading=lambda m, nozzle_target_c: None)
        with mock.patch.dict("sys.modules", {"kiln_pro": mock.MagicMock(), "kiln_pro.bridge": SimpleNamespace(pro_features=pro)}):
            assert start_lines(_Adapter(name="bambu", model="")) == [GENERIC_BAMBU_START]
        assert asked == [], "a guessed model would name the wrong start file confidently"


# ---------------------------------------------------------------------------
# The cancel engine
# ---------------------------------------------------------------------------


class TestTheCancelField:
    def test_the_cancel_line_is_generic_about_where_the_cutter_sits(self):
        line = GENERIC_BAMBU_CANCEL.lower()
        assert "cut" in line and "lifts" in line and "parks" in line
        for side in ("left", "right", "front", "rear"):
            assert side not in line, side

    def test_a_bambu_cancel_carries_the_one_liner(self, no_pro):
        assert cancel_narrative(_Adapter(name="bambu", model="bambu_a1")) == [GENERIC_BAMBU_CANCEL]

    def test_a_non_bambu_cancel_carries_nothing(self, no_pro):
        assert cancel_narrative(_Adapter(name="moonraker", model="voron_2_4")) is None

    def test_the_cancel_engine_attaches_the_field_beside_the_message(self, no_pro):
        """The real engine (``_cancel_print_on``), the one the tool and the
        fleet fan-out both call, with a Bambu-named adapter."""
        from kiln import server

        adapter = MagicMock()
        adapter.name = "bambu"
        adapter._printer_model = "bambu_a1"
        adapter.cancel_print.return_value = PrintResult(success=True, message="Print cancelled.")
        adapter.capabilities.cancel_during_calibration_faults = False
        with mock.patch.object(server, "_stop_print_watchdog"), mock.patch.object(
            server, "_is_heater_watchdog_machine", return_value=False
        ), mock.patch.object(server, "_audit"):
            out = server._cancel_print_on(adapter, "default")
        assert out["success"] is True
        assert out["message"] == "Print cancelled."
        assert out["what_you_will_see"] == [GENERIC_BAMBU_CANCEL]

    def test_the_cancel_engine_leaves_other_printers_alone(self, no_pro):
        from kiln import server

        adapter = MagicMock()
        adapter.name = "moonraker"
        adapter.cancel_print.return_value = PrintResult(success=True, message="Print cancelled.")
        adapter.capabilities.cancel_during_calibration_faults = False
        with mock.patch.object(server, "_stop_print_watchdog"), mock.patch.object(
            server, "_is_heater_watchdog_machine", return_value=False
        ), mock.patch.object(server, "_audit"):
            out = server._cancel_print_on(adapter, "default")
        assert out["success"] is True
        assert "what_you_will_see" not in out


# ---------------------------------------------------------------------------
# The monitor report's early-stage line
# ---------------------------------------------------------------------------


class TestTheStageLine:
    def test_layer_zero_on_a_bambu_gets_the_line(self, no_pro):
        line = start_stage_line(_Adapter(name="bambu", model="bambu_a1"), nozzle_target_c=250.0, layer=0)
        assert line == GENERIC_BAMBU_START_STAGE_LINE

    def test_the_reading_names_the_stage_the_temperature_suggests(self):
        pro = _fake_pro(reading="Nozzle target 250 C at layer 0 is the AMS load and flush at the left chute.")
        with mock.patch.dict("sys.modules", {"kiln_pro": mock.MagicMock(), "kiln_pro.bridge": SimpleNamespace(pro_features=pro)}):
            line = start_stage_line(_Adapter(name="bambu", model="bambu_a1"), nozzle_target_c=250.0, layer=0)
        assert line == "Nozzle target 250 C at layer 0 is the AMS load and flush at the left chute."

    def test_no_reading_for_this_target_is_the_generic_line(self):
        pro = _fake_pro(reading=None)
        with mock.patch.dict("sys.modules", {"kiln_pro": mock.MagicMock(), "kiln_pro.bridge": SimpleNamespace(pro_features=pro)}):
            line = start_stage_line(_Adapter(name="bambu", model="bambu_a1"), nozzle_target_c=999.0, layer=0)
        assert line == GENERIC_BAMBU_START_STAGE_LINE

    @pytest.mark.parametrize("layer", [1, 2, 40])
    def test_past_layer_zero_there_is_no_line(self, no_pro, layer):
        assert start_stage_line(_Adapter(name="bambu", model="bambu_a1"), nozzle_target_c=220.0, layer=layer) is None

    def test_without_a_layer_count_the_percent_stands_in(self, no_pro):
        adapter = _Adapter(name="bambu", model="bambu_a1")
        assert start_stage_line(adapter, nozzle_target_c=220.0, layer=None, completion=1.0) == GENERIC_BAMBU_START_STAGE_LINE
        assert start_stage_line(adapter, nozzle_target_c=220.0, layer=None, completion=42.0) is None
        assert start_stage_line(adapter, nozzle_target_c=220.0, layer=None, completion=None) is None

    def test_off_bambu_there_is_never_a_line(self, no_pro):
        assert start_stage_line(_Adapter(name="octoprint"), nozzle_target_c=250.0, layer=0) is None

    def test_the_line_reaches_the_monitor_report_at_layer_zero_only(self, no_pro):
        """The real report, in the Comments line, one line, and gone by layer 3."""
        from kiln import server

        def _adapter(layer: int) -> MagicMock:
            adapter = MagicMock()
            adapter.name = "bambu"
            adapter._printer_model = "bambu_a1"
            state = MagicMock()
            state.state = "printing"
            state.to_dict.return_value = {
                "state": "printing",
                "tool_temp_actual": 248.0,
                "tool_temp_target": 250.0,
                "bed_temp_actual": 65.0,
                "bed_temp_target": 65.0,
            }
            adapter.get_state.return_value = state
            job = MagicMock()
            job.completion = 1.0
            job.file_name = "part.3mf"
            job.print_time_elapsed = 60
            job.print_time_left = 3000
            job.to_dict.return_value = {
                "completion": 1.0,
                "file_name": "part.3mf",
                "current_layer": layer,
                "total_layers": 120,
            }
            adapter.get_job.return_value = job
            adapter.get_snapshot.return_value = None
            adapter.get_temperatures.return_value = {}
            return adapter

        with mock.patch.object(server, "_get_adapter", return_value=_adapter(0)), mock.patch.object(
            server, "_pro_bridge", return_value=None
        ):
            report = server.monitor_print(include_snapshot=False)
        assert isinstance(report, str), report
        comments = [ln for ln in report.splitlines() if ln.startswith("Comments:")]
        assert len(comments) == 1
        assert GENERIC_BAMBU_START_STAGE_LINE in comments[0]

        with mock.patch.object(server, "_get_adapter", return_value=_adapter(3)), mock.patch.object(
            server, "_pro_bridge", return_value=None
        ):
            report = server.monitor_print(include_snapshot=False)
        assert isinstance(report, str), report
        assert GENERIC_BAMBU_START_STAGE_LINE not in report
