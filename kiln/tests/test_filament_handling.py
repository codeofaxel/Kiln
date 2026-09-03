"""Filament handling: load / unload / purge on every adapter, through every door.

Measured need (2026-09-03): a print failed at layer 1 with a clogged hotend
on an A1.  The AMS reported four PLA slots and ``tray_now`` 255, and Kiln
had no way to load a slot or test whether the melt zone was clear — the
user worked the jam through the touchscreen while Kiln watched, and the
printer's own Load wizard failed at its purge step with HMS 1200-8007.

Covers:
- the contract: every concrete adapter implements the three hooks, none
  overrides the gated public templates, a new adapter cannot instantiate
  without them
- the shared safety gate: not mid-print, cold-extrusion floor, safety
  ceiling, the material's own window, purge-length cap, unsupported
  backends refused by name
- the shared G-code sequence: heat → thermistor → M83/G1 E/M82, firmware
  rejection surfaced, hotend timeout surfaced, no move without heat
- Bambu: AMS-aware load / unload payloads, tray_now as the verification
  signal, fault codes decoded to plain language (1200-8007 included),
  the AMS tray's own temperature window, purge fault watch
- OctoPrint / Moonraker: real transport calls, Klipper macros and
  ``can_extrude``, HTTP refusals surfaced honestly
- Prusa Link / Elegoo: explicit refusals, never a pretended success
- the doors: MCP tool envelope + confirmation gate, ``kiln filament``,
  ``kiln doctor``, the clog recovery plan, ``troubleshoot_printer``
"""

from __future__ import annotations

import inspect
import itertools
import json
import time
from unittest import mock

import pytest
import responses

from kiln.printers.base import (
    DEFAULT_PURGE_LENGTH_MM,
    MIN_EXTRUDE_TEMP_C,
    FilamentHandlingUnsupported,
    FilamentOpPlan,
    FilamentOpResult,
    JobProgress,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterError,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
)

# ---------------------------------------------------------------------------
# Every concrete adapter, with the constructor each needs.
# ---------------------------------------------------------------------------


def _all_adapter_classes() -> dict[str, type[PrinterAdapter]]:
    from kiln.printers.bambu import BambuAdapter
    from kiln.printers.creality import CrealityAdapter
    from kiln.printers.duet import DuetAdapter
    from kiln.printers.elegoo import ElegooAdapter
    from kiln.printers.moonraker import MoonrakerAdapter
    from kiln.printers.octoprint import OctoPrintAdapter
    from kiln.printers.prusalink import PrusaLinkAdapter
    from kiln.printers.serial_adapter import SerialPrinterAdapter

    return {
        "bambu": BambuAdapter,
        "creality": CrealityAdapter,
        "duet": DuetAdapter,
        "elegoo": ElegooAdapter,
        "moonraker": MoonrakerAdapter,
        "octoprint": OctoPrintAdapter,
        "prusalink": PrusaLinkAdapter,
        "serial": SerialPrinterAdapter,
    }


def _build(name: str) -> PrinterAdapter:
    cls = _all_adapter_classes()[name]
    if name == "bambu":
        return cls(host="192.168.1.5", access_code="12345678", serial="01P00A000000001", timeout=2)
    if name == "octoprint":
        return cls(host="http://octopi.local", api_key="KEY")
    if name == "serial":
        serial_mod = pytest.importorskip("serial")
        with mock.patch.object(serial_mod, "Serial", return_value=mock.MagicMock()), \
                mock.patch.object(cls, "_wait_for_startup"), \
                mock.patch.object(cls, "_capture_machine_type"):
            return cls(port="/dev/ttyUSB0")
    if name == "creality":
        ok = mock.MagicMock()
        ok.ok = True
        ok.status_code = 200
        ok.json.return_value = {"result": {"klippy_state": "ready"}}
        with mock.patch("kiln.printers.creality.requests.get", return_value=ok):
            return cls("http://printer.local", timeout=5, retries=1)
    return cls(host="http://printer.local")


#: The backends that really drive filament, and the ones that say they can't.
_SUPPORTED = {"bambu", "octoprint", "moonraker", "duet", "serial", "creality"}
_UNSUPPORTED = {"prusalink", "elegoo"}

_HOOKS = ("_load_filament_impl", "_unload_filament_impl", "_purge_filament_impl")
_TEMPLATES = ("load_filament", "unload_filament", "purge_filament")


class TestAdapterContract:
    """A new adapter cannot ship without filament handling."""

    @pytest.mark.parametrize("name", sorted(_all_adapter_classes()))
    def test_every_concrete_adapter_implements_the_hooks(self, name):
        cls = _all_adapter_classes()[name]
        assert not inspect.isabstract(cls), f"{cls.__name__} is abstract"
        for hook in _HOOKS:
            owners = [k for k in cls.__mro__ if k is not PrinterAdapter and hook in vars(k)]
            assert owners, f"{cls.__name__} inherits {hook} from the base — it must implement it"

    @pytest.mark.parametrize("name", sorted(_all_adapter_classes()))
    def test_no_adapter_overrides_the_gated_templates(self, name):
        """The public methods carry the safety gate; overriding one bypasses it."""
        cls = _all_adapter_classes()[name]
        for template in _TEMPLATES:
            owners = [k for k in cls.__mro__ if k is not PrinterAdapter and template in vars(k)]
            assert not owners, f"{cls.__name__} overrides {template}; put backend logic in {template.replace('filament', 'filament_impl')}"

    def test_the_base_templates_are_engagement_gated(self):
        for template in _TEMPLATES:
            assert getattr(getattr(PrinterAdapter, template), "_kiln_engagement_wrapped", False), template

    def test_a_new_adapter_without_the_hooks_cannot_instantiate(self):
        class _AlmostComplete(PrinterAdapter):
            @property
            def name(self):
                return "almost"

            @property
            def capabilities(self):
                return PrinterCapabilities()

            def get_state(self):
                return PrinterState(connected=True, state=PrinterStatus.IDLE)

            def get_job(self):
                return JobProgress()

            def list_files(self):
                return []

            def upload_file(self, file_path):
                return UploadResult(success=True, file_name="", message="")

            def _start_print_impl(self, file_name, **kwargs):
                return PrintResult(success=True, message="")

            def cancel_print(self):
                return PrintResult(success=True, message="")

            def pause_print(self):
                return PrintResult(success=True, message="")

            def _resume_print_impl(self):
                return PrintResult(success=True, message="")

            def emergency_stop(self):
                return PrintResult(success=True, message="")

            def set_tool_temp(self, target):
                return True

            def set_bed_temp(self, target):
                return True

            def send_gcode(self, commands):
                return True

            def delete_file(self, file_path):
                return True

        with pytest.raises(TypeError, match="filament"):
            _AlmostComplete()

    @pytest.mark.parametrize("name", sorted(_all_adapter_classes()))
    def test_capability_flag_tells_the_truth(self, name):
        adapter = _build(name)
        flag = adapter.capabilities.can_handle_filament
        assert flag == (name in _SUPPORTED), f"{name}: can_handle_filament={flag}"
        if not flag:
            plan = FilamentOpPlan(action="purge", temperature=200.0, temperature_source="test", length_mm=10.0)
            for hook in _HOOKS:
                with pytest.raises(FilamentHandlingUnsupported):
                    getattr(adapter, hook)(plan)


# ---------------------------------------------------------------------------
# The shared gate, on a recording stub.
# ---------------------------------------------------------------------------


class _Stub(PrinterAdapter):
    """Records the plan it was handed; never touches hardware."""

    def __init__(self, *, state=PrinterStatus.IDLE, supported=True, tool_temp=25.0):
        self._state = state
        self._supported = supported
        self.tool_temp = tool_temp
        self.plans: list[FilamentOpPlan] = []
        self.gcode: list[list[str]] = []
        self.temps: list[float] = []
        self.reject_gcode: PrinterError | None = None

    @property
    def name(self):
        return "stub"

    @property
    def capabilities(self):
        return PrinterCapabilities(can_handle_filament=self._supported)

    def get_state(self):
        return PrinterState(connected=True, state=self._state, tool_temp_actual=self.tool_temp)

    def get_job(self):
        return JobProgress()

    def list_files(self):
        return []

    def upload_file(self, file_path):
        return UploadResult(success=True, file_name="", message="")

    def _start_print_impl(self, file_name, **kwargs):
        return PrintResult(success=True, message="")

    def cancel_print(self):
        return PrintResult(success=True, message="")

    def pause_print(self):
        return PrintResult(success=True, message="")

    def _resume_print_impl(self):
        return PrintResult(success=True, message="")

    def emergency_stop(self):
        return PrintResult(success=True, message="")

    def set_tool_temp(self, target):
        self.temps.append(target)
        self.tool_temp = target  # the stub's heater is instantaneous
        return True

    def set_bed_temp(self, target):
        return True

    def send_gcode(self, commands):
        if self.reject_gcode is not None:
            raise self.reject_gcode
        self.gcode.append(list(commands))
        return True

    def delete_file(self, file_path):
        return True

    def _record(self, plan):
        self.plans.append(plan)
        return FilamentOpResult(success=True, action=plan.action, message="recorded")

    _load_filament_impl = _record
    _unload_filament_impl = _record
    _purge_filament_impl = _record


@pytest.fixture(autouse=True)
def _no_engagement(monkeypatch):
    """The single-printer rule is another test's subject."""
    from kiln.printers import engagement

    monkeypatch.setattr(engagement, "check_command", lambda adapter, action: None)
    monkeypatch.setattr(engagement, "observe", lambda adapter, action, result: None)


@pytest.fixture
def fast_clock(monkeypatch):
    """A monotonic clock that advances on every read and a sleep that
    costs nothing, so waits terminate instantly and deterministically."""
    counter = itertools.count(0.0, 0.5)
    monkeypatch.setattr(time, "monotonic", lambda: next(counter))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return counter


class TestSharedGate:
    def test_refuses_while_printing(self):
        stub = _Stub(state=PrinterStatus.PRINTING)
        with pytest.raises(PrinterError, match="print is running"):
            stub.purge_filament(temperature=200)
        assert stub.plans == []

    def test_paused_is_allowed_it_is_the_recovery_case(self):
        stub = _Stub(state=PrinterStatus.PAUSED)
        result = stub.purge_filament(temperature=200)
        assert result.success is True
        assert stub.plans[0].action == "purge"

    def test_refuses_below_the_cold_extrusion_floor(self):
        stub = _Stub()
        with pytest.raises(PrinterError, match="cold-extrusion floor"):
            stub.purge_filament(temperature=MIN_EXTRUDE_TEMP_C - 1)
        assert stub.plans == []

    def test_refuses_above_the_adapter_ceiling(self):
        stub = _Stub()
        stub._MAX_HOTEND_C = 250.0
        with pytest.raises(PrinterError, match="exceeds safety limit"):
            stub.load_filament(temperature=260)

    def test_a_bound_safety_profile_tightens_the_ceiling(self):
        stub = _Stub()
        stub.set_safety_profile("ender3")
        from kiln.safety_profiles import get_profile

        ceiling = get_profile("ender3").max_hotend_temp
        with pytest.raises(PrinterError, match="exceeds safety limit"):
            stub.purge_filament(temperature=ceiling + 10)

    def test_no_temperature_and_no_material_is_refused_not_guessed(self):
        stub = _Stub()
        with pytest.raises(PrinterError, match="no temperature"):
            stub.purge_filament()
        assert stub.plans == []

    def test_material_alone_picks_the_middle_of_its_window(self):
        stub = _Stub()
        result = stub.purge_filament(material="PLA")
        assert result.success is True
        plan = stub.plans[0]
        assert plan.temperature == 200  # PLA 180–220 in Kiln's table
        assert plan.material_window[:2] == (180.0, 220.0)
        assert "material table" in plan.temperature_source

    def test_temperature_outside_the_material_window_is_refused(self):
        stub = _Stub()
        with pytest.raises(PrinterError, match="outside the 180–220°C window"):
            stub.purge_filament(material="PLA", temperature=250)

    def test_unknown_material_with_a_temperature_still_runs(self):
        stub = _Stub()
        result = stub.purge_filament(material="unobtainium", temperature=210)
        assert result.success is True
        assert stub.plans[0].material_window is None

    @pytest.mark.parametrize("length", [0.5, 151, 5000])
    def test_purge_length_is_capped(self, length):
        stub = _Stub()
        with pytest.raises(PrinterError, match="outside 1–150 mm"):
            stub.purge_filament(temperature=200, length_mm=length)

    def test_negative_slot_is_refused(self):
        stub = _Stub()
        with pytest.raises(PrinterError, match="slot must be >= 0"):
            stub.load_filament(temperature=200, slot=-1)

    def test_an_unsupported_backend_is_refused_by_name_before_the_hook(self):
        stub = _Stub(supported=False)
        with pytest.raises(FilamentHandlingUnsupported, match="stub cannot purge"):
            stub.purge_filament(temperature=200)
        assert stub.plans == []

    def test_offline_printer_is_a_refusal_not_a_traceback(self):
        stub = _Stub()
        stub.get_state = lambda: (_ for _ in ()).throw(PrinterError("no route"))
        with pytest.raises(PrinterError, match="did not answer"):
            stub.purge_filament(temperature=200)

    def test_options_ride_the_plan(self):
        stub = _Stub()
        stub.load_filament(temperature=200, slot=2, wait_seconds=7)
        plan = stub.plans[0]
        assert plan.slot == 2
        assert plan.options == {"wait_seconds": 7}
        assert plan.length_mm == 60.0  # the generic default feed

    def test_plan_and_result_serialise(self):
        plan = FilamentOpPlan(action="purge", temperature=200.0, temperature_source="caller", material_window=(180.0, 220.0, "t"))
        assert plan.to_dict()["material_window"] == [180.0, 220.0, "t"]
        result = FilamentOpResult(success=False, action="purge", message="m", extrusion_verified=None)
        assert result.to_dict()["extrusion_verified"] is None


# ---------------------------------------------------------------------------
# The shared G-code sequence.
# ---------------------------------------------------------------------------


class _GcodeStub(_Stub):
    """Runs the shared sequence for real instead of recording."""

    def _purge_filament_impl(self, plan):
        return self._gcode_filament_move(plan, signed_length_mm=plan.length_mm, mechanism="test")

    def _unload_filament_impl(self, plan):
        return self._gcode_filament_move(plan, signed_length_mm=-plan.length_mm, mechanism="test")

    _load_filament_impl = _purge_filament_impl


class TestSharedGcodeSequence:
    def test_heats_waits_then_one_relative_move(self, fast_clock):
        stub = _GcodeStub()
        result = stub.purge_filament(temperature=205, length_mm=30)
        assert stub.temps == [205.0]
        assert stub.gcode == [["M83", "G1 E30 F180", "M82"]]
        assert result.success is True
        assert result.extrusion_verified is None
        assert result.verification_source == "command_accepted_only"
        assert "look at the nozzle" in result.message

    def test_unload_retracts(self, fast_clock):
        stub = _GcodeStub()
        stub.unload_filament(temperature=205, length_mm=80)
        assert stub.gcode == [["M83", "G1 E-80 F180", "M82"]]

    def test_firmware_rejection_is_reported_with_its_words(self, fast_clock):
        stub = _GcodeStub()
        stub.reject_gcode = PrinterError("Error: cold extrusion prevented")
        result = stub.purge_filament(temperature=205)
        assert result.success is False
        assert result.extrusion_verified is False
        assert result.verification_source == "firmware_rejected_move"
        assert "cold extrusion prevented" in result.error_hint

    def test_no_move_when_the_hotend_never_arrives(self, fast_clock):
        stub = _GcodeStub()
        stub.set_tool_temp = lambda t: stub.temps.append(t) or True  # heater never heats
        result = stub.purge_filament(temperature=205)
        assert result.success is False
        assert result.verification_source == "thermistor"
        assert stub.gcode == []
        assert "Nothing was extruded" in result.message

    def test_pre_move_check_refuses_on_a_genuine_signal(self, fast_clock):
        stub = _GcodeStub()
        plan = stub._prepare_filament_op("purge", slot=None, material=None, temperature=205, length_mm=20)
        result = stub._gcode_filament_move(
            plan, signed_length_mm=20, mechanism="test",
            pre_move_check=lambda: ("can_extrude is false", "klipper_can_extrude"),
        )
        assert result.success is False
        assert result.verification_source == "klipper_can_extrude"
        assert stub.gcode == []


# ---------------------------------------------------------------------------
# Bambu: AMS-aware, with the printer's own signals.
# ---------------------------------------------------------------------------

_AMS = {
    "ams": {
        "ams_exist_bits": "1",
        "tray_exist_bits": "f",
        "tray_now": "255",
        "ams": [
            {
                "id": 0,
                "humidity": "3",
                "tray": [
                    {"id": 0, "tray_type": "PLA", "tray_color": "FF0000FF", "nozzle_temp_min": "190", "nozzle_temp_max": "230", "tag_uid": "0"},
                    {"id": 1, "tray_type": "PLA", "tray_color": "00FF00FF", "nozzle_temp_min": "190", "nozzle_temp_max": "230", "tag_uid": "0"},
                    {"id": 2, "tray_type": "", "tray_color": "", "tag_uid": "0"},
                    {"id": 3, "tray_type": "PETG", "tray_color": "0000FFFF", "nozzle_temp_min": "230", "nozzle_temp_max": "260", "tag_uid": "0"},
                ],
            }
        ],
    },
    "gcode_state": "IDLE",
    "print_error": 0,
    "nozzle_temper": 25.0,
    "nozzle_target_temper": 0,
}


@pytest.fixture
def bambu(monkeypatch):
    monkeypatch.setenv("KILN_BAMBU_TLS_PIN_FILE", "/dev/null")
    adapter = _build("bambu")
    adapter._mqtt_connected.set()
    adapter._connected = True
    adapter._mqtt_client = mock.MagicMock()
    adapter._mqtt_client.publish.return_value = mock.MagicMock()
    adapter._fw_modules_requested = True  # skip the get_version round trip
    adapter._last_status = json.loads(json.dumps(_AMS))
    adapter._last_state_time = float("inf")
    return adapter


def _published(adapter) -> list[dict]:
    return [json.loads(c.args[1]) for c in adapter._mqtt_client.publish.call_args_list]


def _status_after_sleep(adapter, monkeypatch, **changes):
    """Let the first sleep mutate the push cache, as a printer would."""
    calls = {"n": 0}

    def _sleep(_s):
        calls["n"] += 1
        adapter._last_status.update(changes)

    monkeypatch.setattr(time, "sleep", _sleep)
    counter = itertools.count(0.0, 0.5)
    monkeypatch.setattr(time, "monotonic", lambda: next(counter))
    return calls


class TestBambuLoad:
    def test_load_publishes_ams_change_filament_for_the_tray(self, bambu, monkeypatch):
        _status_after_sleep(bambu, monkeypatch, tray_now="1")
        result = bambu.load_filament(slot=1)
        cmds = [p["print"] for p in _published(bambu) if "print" in p]
        change = [c for c in cmds if c["command"] == "ams_change_filament"]
        assert change and change[0]["target"] == 1
        assert change[0]["tar_temp"] == 210  # midpoint of the tray's 190–230
        assert result.success is True
        assert result.extrusion_verified is True
        assert result.verification_source == "ams_tray_now"
        assert result.slot == 1

    def test_external_spool_is_tray_254(self, bambu, monkeypatch):
        _status_after_sleep(bambu, monkeypatch, tray_now="254")
        result = bambu.load_filament(temperature=210)
        change = [p["print"] for p in _published(bambu) if p.get("print", {}).get("command") == "ams_change_filament"]
        assert change[0]["target"] == 254
        assert result.success is True

    def test_unload_is_tray_255(self, bambu, monkeypatch):
        bambu._last_status["tray_now"] = "1"
        _status_after_sleep(bambu, monkeypatch, tray_now="255")
        result = bambu.unload_filament(temperature=210)
        change = [p["print"] for p in _published(bambu) if p.get("print", {}).get("command") == "ams_change_filament"]
        assert change[0]["target"] == 255
        assert result.success is True
        assert "no tray is feeding" in result.message

    def test_empty_tray_is_refused_before_anything_moves(self, bambu):
        with pytest.raises(PrinterError, match="tray 2 reports no filament"):
            bambu.load_filament(slot=2, temperature=210)
        assert not [p for p in _published(bambu) if p.get("print", {}).get("command") == "ams_change_filament"]

    def test_missing_tray_is_refused(self, bambu):
        with pytest.raises(PrinterError, match="tray 7 is not present"):
            bambu.load_filament(slot=7, temperature=210)

    def test_tray_window_beats_the_caller(self, bambu):
        # Tray 3 is PETG 230–260; 200 °C is inside PLA's table but not this tray's.
        with pytest.raises(PrinterError, match="outside the 230–260°C window the AMS tray 3"):
            bambu.load_filament(slot=3, temperature=200)

    def test_the_wizards_fault_is_read_in_plain_language(self, bambu, monkeypatch):
        """The measured case: the load's purge step raises 1200-8007.

        It is a print_error, not an HMS code — it appears nowhere in
        Bambu's HMS index — so the reading must carry NO wiki link.
        """
        _status_after_sleep(bambu, monkeypatch, print_error=0x12008007)
        result = bambu.load_filament(slot=0)
        assert result.success is False
        assert result.extrusion_verified is False
        assert result.verification_source == "bambu_fault_code"
        assert result.error_code == "1200_8007"
        assert "did not come through the nozzle" in result.error_hint
        assert result.details["code_kind"] == "print_error"
        assert "hms_wiki_url" not in result.details

    def test_hms_list_entries_count_as_faults_too(self, bambu, monkeypatch):
        _status_after_sleep(bambu, monkeypatch, hms=[{"attr": 0x12002000, "code": 0x00020006}])
        result = bambu.load_filament(slot=0)
        assert result.success is False
        assert result.error_code == "1200_2000_0002_0006"
        assert "extruder may be clogged" in result.error_hint
        assert result.details["code_kind"] == "hms"
        # The A1-only page really does live under /a1-mini/, not /x1/.
        assert result.details["hms_wiki_url"] == (
            "https://wiki.bambulab.com/en/a1-mini/troubleshooting/"
            "hmscode/1200_2000_0002_0006"
        )

    def test_an_hms_code_wins_over_a_print_error_when_both_land(self, bambu, monkeypatch):
        """HMS is the namespace Bambu documents, so it is the better reading."""
        _status_after_sleep(
            bambu,
            monkeypatch,
            print_error=0x12008007,
            hms=[{"attr": 0x07007000, "code": 0x00020006}],
        )
        result = bambu.load_filament(slot=0)
        assert result.error_code == "0700_7000_0002_0006"
        assert result.details["code_kind"] == "hms"
        assert {"code": "1200_8007", "kind": "print_error"} in result.details["all_new_faults"]

    def test_a_fault_already_latched_before_the_load_is_not_blamed_on_it(self, bambu, monkeypatch):
        bambu._last_status["print_error"] = 0x12008007
        _status_after_sleep(bambu, monkeypatch, tray_now="0")
        result = bambu.load_filament(slot=0)
        assert result.success is True

    def test_timeout_is_reported_as_unknown_not_success(self, bambu, monkeypatch):
        _status_after_sleep(bambu, monkeypatch)  # nothing ever changes
        result = bambu.load_filament(slot=0, wait_seconds=3)
        assert result.success is False
        assert result.extrusion_verified is None
        assert result.verification_source == "timeout_no_signal"
        assert "never reported tray_now=0" in result.message


class TestBambuPurge:
    def test_purge_goes_through_gcode_line_and_watches_for_a_fault(self, bambu, monkeypatch):
        bambu._last_status["tray_now"] = "0"
        counter = itertools.count(0.0, 0.5)
        monkeypatch.setattr(time, "monotonic", lambda: next(counter))

        def _sleep(_s):
            bambu._last_status["nozzle_temper"] = 210.0

        monkeypatch.setattr(time, "sleep", _sleep)
        result = bambu.purge_filament(length_mm=25)
        scripts = [p["print"]["param"] for p in _published(bambu) if p.get("print", {}).get("command") == "gcode_line"]
        assert "M104 S210" in scripts[0]
        assert "G1 E25 F180" in scripts[-1]
        assert result.success is True
        assert result.extrusion_verified is None
        assert result.verification_source == "no_fault_within_window"
        assert result.temperature == 210  # tray 0's window midpoint, no material named

    def test_a_fault_during_the_purge_turns_it_false(self, bambu, monkeypatch):
        bambu._last_status["tray_now"] = "0"
        bambu._last_status["nozzle_temper"] = 210.0
        counter = itertools.count(0.0, 0.5)
        monkeypatch.setattr(time, "monotonic", lambda: next(counter))
        monkeypatch.setattr(time, "sleep", lambda s: bambu._last_status.__setitem__("print_error", 0x03008003))
        result = bambu.purge_filament(temperature=210)
        assert result.success is False
        assert result.extrusion_verified is False
        assert result.error_code == "0300_8003"
        assert "cannot pull filament" in result.error_hint
        assert result.details["code_kind"] == "print_error"

    def test_no_tray_and_no_material_is_refused(self, bambu):
        bambu._last_status["tray_now"] = "255"
        with pytest.raises(PrinterError, match="no temperature"):
            bambu.purge_filament()


class TestBambuFaultReadings:
    """The two namespaces, and the per-unit / per-slot synonyms.

    Every HMS string asserted here is the title of that code's own page on
    wiki.bambulab.com, read 2026-09-03.
    """

    def test_known_hms_code_gets_the_vendors_words_and_a_link(self):
        from kiln.printers.bambu import describe_bambu_filament_fault

        text, url = describe_bambu_filament_fault("0700-7000-0002-0006")
        assert "Timed out purging the old filament" in text
        assert url == ("https://wiki.bambulab.com/en/x1/troubleshooting/"
                       "hmscode/0700_7000_0002_0006")

    def test_a_print_error_never_gets_an_hms_link(self):
        """/hmscode/1200_8007 is a 404 — Bambu documents no print_error pages."""
        from kiln.printers.bambu import describe_bambu_filament_fault

        text, url = describe_bambu_filament_fault("1200-8007", kind="print_error")
        assert url is None
        assert "purge_filament" in text

    def test_the_same_digits_read_differently_per_namespace(self):
        from kiln.printers.bambu import describe_bambu_filament_fault

        as_hms = describe_bambu_filament_fault("1200_8000_0002_0001", kind="hms")
        as_err = describe_bambu_filament_fault("1200_8000", kind="print_error")
        assert as_hms[1] is not None
        assert as_err[1] is None
        assert as_hms[0] != as_err[0]

    @pytest.mark.parametrize(
        "code,unit,slot",
        [
            ("0700_7000_0002_0003", "AMS A", "slot 1"),
            ("0701_7200_0002_0003", "AMS B", "slot 3"),
            ("0703_7300_0002_0003", "AMS D", "slot 4"),
            ("1202_2100_0002_0006", "AMS C", "slot 2"),
        ],
    )
    def test_unit_and_slot_variants_share_one_reading(self, code, unit, slot):
        """Bambu files these as synonyms of one entry; so does Kiln."""
        from kiln.printers.bambu import describe_bambu_filament_fault

        text, url = describe_bambu_filament_fault(code, kind="hms")
        assert unit in text and slot in text
        # The link points at the canonical unit-A / slot-1 page that exists.
        assert url.endswith(("/0700_7000_0002_0003", "/1200_2000_0002_0006"))
        assert "/en/" in url

    def test_unknown_hms_code_gets_family_and_the_index_never_a_guessed_page(self):
        """A page Kiln has not harvested may live under any model segment,
        so the searchable index is offered rather than a link that 404s."""
        from kiln.printers.bambu import describe_bambu_filament_fault

        text, url = describe_bambu_filament_fault("0700_7000_0009_0009")
        assert "load / unload path" in text
        assert url == "https://wiki.bambulab.com/en/hms/home"

    def test_every_tabulated_page_path_is_one_of_the_real_model_segments(self):
        """Guards the harvest: a model segment typo is a 404 nobody sees."""
        from kiln.printers.bambu import _BAMBU_HMS_FILAMENT_FAULTS

        known = {"x1", "x1e", "x2d", "a1", "a1-mini", "a2l", "p2s", "h2", "h2s", "h2c", "h2d", "h2d-pro"}
        for code, (reading, model) in _BAMBU_HMS_FILAMENT_FAULTS.items():
            assert model in known, f"{code} -> {model!r}"
            assert reading and reading[0].isupper() and reading.endswith(".")

    def test_a_completely_unknown_code_admits_it(self):
        from kiln.printers.bambu import describe_bambu_filament_fault

        text, _ = describe_bambu_filament_fault("9999_9999", kind="print_error")
        assert "no reading for" in text

    def test_garbage_is_not_mistaken_for_a_code(self):
        from kiln.printers.bambu import describe_bambu_filament_fault

        text, url = describe_bambu_filament_fault("oops")
        assert url is None
        assert "not a readable HMS code" in text


# ---------------------------------------------------------------------------
# OctoPrint / Moonraker: real transport, honest refusals.
# ---------------------------------------------------------------------------

OCTO = "http://octopi.local"
MOON = "http://printer.local"


class TestOctoPrint:
    @responses.activate
    def test_purge_heats_then_extrudes(self, fast_clock):
        adapter = _build("octoprint")
        temps = iter([25.0, 25.0, 208.0, 210.0, 210.0, 210.0])
        adapter.get_state = lambda: PrinterState(connected=True, state=PrinterStatus.IDLE, tool_temp_actual=next(temps, 210.0))
        responses.add(responses.POST, f"{OCTO}/api/printer/tool", status=204)
        responses.add(responses.POST, f"{OCTO}/api/printer/command", status=204)
        result = adapter.purge_filament(temperature=210, length_mm=20)
        assert result.success is True
        tool = json.loads(responses.calls[0].request.body)
        assert tool["targets"]["tool0"] == 210
        cmd = json.loads(responses.calls[-1].request.body)
        assert cmd["commands"] == ["M83", "G1 E20 F180", "M82"]
        assert result.extrusion_verified is None

    @responses.activate
    def test_a_409_is_a_refusal_not_a_success(self, fast_clock):
        adapter = _build("octoprint")
        adapter.get_state = lambda: PrinterState(connected=True, state=PrinterStatus.IDLE, tool_temp_actual=210.0)
        responses.add(responses.POST, f"{OCTO}/api/printer/tool", status=204)
        responses.add(responses.POST, f"{OCTO}/api/printer/command", status=409, body="Printer is not operational")
        result = adapter.load_filament(temperature=210)
        assert result.success is False
        assert result.verification_source == "firmware_rejected_move"
        assert "409" in result.error_hint


def _moon_response(status_code=200, json_data=None, text=""):
    import requests

    resp = mock.MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.text = text or (json.dumps(json_data) if json_data is not None else "")
    resp.json.return_value = json_data if json_data is not None else {}
    return resp


class TestMoonraker:
    def _adapter(self):
        adapter = _all_adapter_classes()["moonraker"](host=MOON, timeout=5, retries=1)
        adapter.get_state = lambda: PrinterState(connected=True, state=PrinterStatus.IDLE, tool_temp_actual=210.0)
        return adapter

    def test_uses_the_configs_own_load_macro_when_it_exists(self, fast_clock):
        adapter = self._adapter()

        def _request(method, url, **kw):
            if url.endswith("/printer/gcode/help"):
                return _moon_response(json_data={"result": {"LOAD_FILAMENT": "Load", "G28": "Home"}})
            return _moon_response(json_data={"result": "ok"})

        with mock.patch.object(adapter._session, "request", side_effect=_request) as req:
            result = adapter.load_filament(temperature=210)
        scripts = [(c.kwargs.get("params") or {}).get("script") for c in req.call_args_list if c.kwargs.get("params")]
        assert "M104 S210" in scripts
        assert "LOAD_FILAMENT" in scripts
        assert result.success is True
        assert result.details["mechanism"] == "klipper_macro"

    def test_falls_back_to_the_generic_feed_and_checks_can_extrude(self, fast_clock):
        adapter = self._adapter()

        def _request(method, url, **kw):
            if url.endswith("/printer/gcode/help"):
                return _moon_response(json_data={"result": {"G28": "Home"}})
            if url.endswith("/printer/objects/query"):
                return _moon_response(json_data={"result": {"status": {"extruder": {"can_extrude": True, "temperature": 210.0}}}})
            return _moon_response(json_data={"result": "ok"})

        with mock.patch.object(adapter._session, "request", side_effect=_request) as req:
            result = adapter.load_filament(temperature=210, length_mm=90)
        scripts = [(c.kwargs.get("params") or {}).get("script") for c in req.call_args_list if (c.kwargs.get("params") or {}).get("script")]
        assert scripts[-1] == "M83\nG1 E90 F180\nM82"
        assert result.success is True

    @pytest.mark.parametrize("mmu_object", ["mmu", "AFC"])
    def test_an_mmu_owns_the_filament_path_so_load_is_refused_by_name(self, fast_clock, mmu_object):
        """Happy-Hare (``mmu``) / AFC (``AFC``) register their own MMU_LOAD /
        MMU_UNLOAD commands; an extruder-driven feed behind their back
        fights the unit.  Refuse, naming the unit and its own commands."""
        adapter = self._adapter()

        def _request(method, url, **kw):
            if url.endswith("/printer/objects/list"):
                return _moon_response(json_data={"result": {"objects": ["extruder", mmu_object, "toolhead"]}})
            if url.endswith("/printer/gcode/help"):
                return _moon_response(json_data={"result": {"LOAD_FILAMENT": "Load", "MMU_LOAD": "x"}})
            return _moon_response(json_data={"result": "ok"})

        with mock.patch.object(adapter._session, "request", side_effect=_request) as req:
            with pytest.raises(FilamentHandlingUnsupported, match="owns the filament path"):
                adapter.load_filament(temperature=210)
            with pytest.raises(FilamentHandlingUnsupported, match="owns the filament path"):
                adapter.unload_filament(temperature=210)
        assert not any((c.kwargs.get("params") or {}).get("script") for c in req.call_args_list)

    def test_purge_still_runs_with_an_mmu_the_extruder_owns_the_melt_zone(self, fast_clock):
        adapter = self._adapter()

        def _request(method, url, **kw):
            if url.endswith("/printer/objects/list"):
                return _moon_response(json_data={"result": {"objects": ["extruder", "mmu"]}})
            if url.endswith("/printer/objects/query"):
                return _moon_response(json_data={"result": {"status": {"extruder": {"can_extrude": True}}}})
            return _moon_response(json_data={"result": "ok"})

        with mock.patch.object(adapter._session, "request", side_effect=_request):
            result = adapter.purge_filament(temperature=210)
        assert result.success is True

    def test_klippers_can_extrude_false_stops_the_move(self, fast_clock):
        adapter = self._adapter()

        def _request(method, url, **kw):
            if url.endswith("/printer/objects/query"):
                return _moon_response(json_data={"result": {"status": {"extruder": {"can_extrude": False, "temperature": 160.0}}}})
            return _moon_response(json_data={"result": "ok"})

        with mock.patch.object(adapter._session, "request", side_effect=_request) as req:
            result = adapter.purge_filament(temperature=210)
        assert result.success is False
        assert result.extrusion_verified is False
        assert result.verification_source == "klipper_can_extrude"
        assert not any("G1 E" in ((c.kwargs.get("params") or {}).get("script") or "") for c in req.call_args_list)

    def test_a_klipper_rejection_carries_its_words(self, fast_clock):
        adapter = self._adapter()

        def _request(method, url, **kw):
            if url.endswith("/printer/objects/query"):
                return _moon_response(json_data={"result": {"status": {"extruder": {"can_extrude": True}}}})
            script = (kw.get("params") or {}).get("script", "")
            if "G1 E" in script:
                return _moon_response(400, json_data={"error": {"message": "Move exceeds maximum extrusion (1.234mm^2 vs 0.640mm^2)"}})
            return _moon_response(json_data={"result": "ok"})

        with mock.patch.object(adapter._session, "request", side_effect=_request):
            result = adapter.purge_filament(temperature=210)
        assert result.success is False
        assert "maximum extrusion" in result.error_hint


class TestUnsupportedBackends:
    @pytest.mark.parametrize("name", sorted(_UNSUPPORTED))
    def test_refused_by_name_with_what_to_do_instead(self, name):
        adapter = _build(name)
        adapter.get_state = lambda: PrinterState(connected=True, state=PrinterStatus.IDLE)
        with pytest.raises(FilamentHandlingUnsupported, match="cannot purge filament"):
            adapter.purge_filament(temperature=210)


# ---------------------------------------------------------------------------
# The doors.
# ---------------------------------------------------------------------------


@pytest.fixture
def door(monkeypatch):
    """Route the shared door at a stub, with the server's gates neutral."""
    import kiln.server as srv

    stub = _Stub()
    monkeypatch.setattr(srv, "_resolve_control_target", lambda name: (stub, "bench"))
    monkeypatch.setattr(srv, "_emergency_latch_error", lambda tool, name: None)
    monkeypatch.setattr(srv, "_resolve_effective_printer_name", lambda name=None: "bench")
    monkeypatch.setattr(srv, "_is_heater_watchdog_machine", lambda adapter: False)
    monkeypatch.setattr(srv, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_check_auth", lambda scope: None)
    monkeypatch.setattr(srv, "_check_rate_limit", lambda tool: None)
    monkeypatch.setattr(srv, "_CONFIRM_MODE", False)
    return stub


class TestSharedDoor:
    def test_success_envelope(self, door):
        from kiln.plugins.filament_handling_tools import purge_filament

        out = purge_filament(temperature=200, length_mm=20)
        assert out["success"] is True
        assert out["printer_name"] == "bench"
        assert out["action"] == "purge"
        assert door.plans[0].length_mm == 20

    def test_failure_is_a_structured_error_with_the_result_attached(self, door):
        from kiln.plugins.filament_handling_tools import purge_filament

        def _fail(plan):
            return FilamentOpResult(success=False, action="purge", message="no flow", extrusion_verified=False, error_code="1200_8007", error_hint="clogged")

        door._purge_filament_impl = _fail
        out = purge_filament(temperature=200)
        assert out["success"] is False
        assert out["error"]["code"] == "FILAMENT_FAULT"
        assert out["filament"]["error_hint"] == "clogged"

    def test_gate_refusals_are_errors_not_tracebacks(self, door):
        from kiln.plugins.filament_handling_tools import purge_filament

        out = purge_filament(temperature=100)
        assert out["success"] is False
        assert "cold-extrusion floor" in out["error"]["message"]

    def test_unsupported_backend_is_UNSUPPORTED(self, door):
        from kiln.plugins.filament_handling_tools import load_filament

        door._supported = False
        out = load_filament(temperature=200)
        assert out["error"]["code"] == "UNSUPPORTED"

    def test_confirm_mode_asks_first(self, door, monkeypatch):
        import kiln.server as srv
        from kiln.plugins.filament_handling_tools import load_filament

        monkeypatch.setattr(srv, "_CONFIRM_MODE", True)
        out = load_filament(slot=1, temperature=200)
        assert out.get("confirmation_required") is True
        assert out["tool"] == "load_filament"
        assert out["args"]["slot"] == 1
        assert door.plans == []

    def test_tools_are_registered_and_classified(self):
        import kiln.server as srv
        from kiln.plugin_loader import register_all_plugins

        register_all_plugins(srv.mcp)
        tools = srv.mcp._tool_manager._tools
        for name in ("load_filament", "unload_filament", "purge_filament"):
            assert name in tools, name
            assert srv._get_safety_level(name) == "confirm", name
            assert srv._TOOL_RATE_LIMITS.get(name), name


class TestCliDoor:
    def test_purge_command_runs_the_same_tool(self, door):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        result = CliRunner().invoke(cli, ["filament", "purge", "--temp", "200", "--length", "15", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["action"] == "purge"
        assert door.plans[0].length_mm == 15

    def test_a_printer_fault_exits_nonzero_with_the_reading(self, door):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        door._purge_filament_impl = lambda plan: FilamentOpResult(
            success=False, action="purge", message="no flow", error_code="1200_8007", error_hint="clogged at the purge step"
        )
        result = CliRunner().invoke(cli, ["filament", "purge", "--temp", "200"])
        assert result.exit_code == 1
        assert "clogged at the purge step" in result.output

    def test_the_group_has_all_three_verbs(self):
        from kiln.cli.main import cli

        assert set(cli.commands["filament"].commands) == {"load", "unload", "purge"}


class TestOtherDoors:
    def test_doctor_reports_the_capability(self):
        import kiln.cli.main as main

        src = inspect.getsource(main.verify.callback)
        assert '"filament_handling"' in src

    def test_clog_recovery_plan_names_the_test(self):
        from kiln.failure_recovery import FailureType, _build_recovery

        plan = _build_recovery(FailureType.NOZZLE_CLOG)
        assert any("purge_filament" in step for step in plan.steps)

    def test_troubleshoot_printer_points_at_the_test(self):
        import kiln.server as srv

        out = srv.troubleshoot_printer("bambu_a1", symptom="nozzle clog, no extrusion")
        assert "purge_filament" in out.get("filament_next_step", "")

    def test_troubleshoot_printer_links_the_page_that_actually_exists(self):
        """The A1's own AMS-lite code lives under /a1-mini/, not /x1/."""
        import kiln.server as srv

        out = srv.troubleshoot_printer("bambu_a1", hms_code="1200-2000-0002-0006")
        assert out["hms_code_kind"] == "hms"
        assert out["hms_wiki_url"] == (
            "https://wiki.bambulab.com/en/a1-mini/troubleshooting/"
            "hmscode/1200_2000_0002_0006"
        )

    def test_troubleshoot_printer_offers_no_page_for_a_print_error(self):
        """1200-8007 is a print_error; Bambu publishes no page for it."""
        import kiln.server as srv

        out = srv.troubleshoot_printer("bambu_a1", hms_code="1200-8007")
        assert out["hms_code"] == "1200_8007"
        assert out["hms_code_kind"] == "print_error"
        assert "hms_wiki_url" not in out

    def test_troubleshoot_printer_falls_back_to_the_index_not_a_guess(self):
        import kiln.server as srv

        out = srv.troubleshoot_printer("bambu_a1", hms_code="0300-0100-0001-0003")
        assert out["hms_wiki_url"] == "https://wiki.bambulab.com/en/hms/home"

    def test_tool_safety_json_classifies_all_three_as_confirm(self):
        from pathlib import Path

        data = json.loads((Path(__file__).parent.parent / "src" / "kiln" / "data" / "tool_safety.json").read_text())
        for name in ("load_filament", "unload_filament", "purge_filament"):
            assert data["classifications"][name] == {"level": "confirm", "physical_effect": True}


def test_default_purge_length_is_a_clog_test_not_a_runaway():
    assert 10 <= DEFAULT_PURGE_LENGTH_MM <= 50
