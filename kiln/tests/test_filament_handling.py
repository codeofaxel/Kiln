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
import sys
import time
from unittest import mock

import paho.mqtt.client as mqtt
import pytest
import responses
from requests.exceptions import ConnectionError as ReqConnectionError

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
_TEMPLATES = ("load_filament", "unload_filament", "purge_filament", "wipe_nozzle")


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

    @pytest.mark.parametrize("name", sorted(set(_all_adapter_classes()) - {"bambu"}))
    def test_a_backend_with_no_pad_position_refuses_to_wipe_by_default(self, name):
        """The wipe hook is not abstract: its honest default is a refusal
        that names what to use instead, never a stub that returns success."""
        adapter = _build(name)
        plan = FilamentOpPlan(action="wipe", temperature=200.0, temperature_source="test")
        with pytest.raises(FilamentHandlingUnsupported, match="own screen"):
            adapter._wipe_nozzle_impl(plan)

    def test_the_wipe_is_engagement_gated_like_the_others(self):
        from kiln.printers.engagement import GATED_ACTIONS

        assert "wipe_nozzle" in GATED_ACTIONS


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


@pytest.fixture(autouse=True)
def _no_served_network(monkeypatch):
    """No test asks the hosted service for a plan: the served door is a stub
    that answers nothing unless a test replaces it on purpose."""
    monkeypatch.setattr("kiln._pro_motion_bridge._served_plan", lambda request: None)


@pytest.fixture
def no_kiln_pro(monkeypatch):
    """Run as a public-only install: every ``kiln_pro`` import fails.

    The developer machine has kiln-pro installed and its overlay on disk,
    so a test of the public floor must say so explicitly or it measures
    the served behaviour instead.  The printer-intel merge caches the
    overlay object per process; it is dropped here so the floor is
    re-read.
    """
    for name in list(sys.modules):
        if name == "kiln_pro" or name.startswith("kiln_pro."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    import kiln.printer_intelligence as _pi

    monkeypatch.setattr(_pi, "_merged_cache", None)


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

    def test_a_paused_print_is_warned_about_the_ooze(self):
        """The nozzle is parked over the part; resuming onto a blob is the
        failure this sentence prevents."""
        stub = _Stub(state=PrinterStatus.PAUSED)
        result = stub.purge_filament(temperature=200)
        assert result.details["printer_paused"] is True
        assert "PAUSED" in result.message
        assert "wipe the nozzle" in result.message

    def test_an_idle_printer_gets_no_such_warning(self):
        stub = _Stub(state=PrinterStatus.IDLE)
        result = stub.purge_filament(temperature=200)
        assert "printer_paused" not in result.details
        assert "PAUSED" not in result.message

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
    def test_heats_waits_then_one_relative_move(self, no_kiln_pro, fast_clock):
        stub = _GcodeStub()
        result = stub.purge_filament(temperature=205, length_mm=30)
        assert stub.temps == [205.0, 0]  # heat, then the end-of-op heater off
        # the move, then the end-of-op pull-back that stops the drool; the
        # served cool-down (fan on, wait, fan off) is not run without kiln-pro
        assert stub.gcode == [["M83", "G1 E30 F180", "M82"], ["M83", "G1 E-0.8 F1800", "M82"]]
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


class TestEveryPurgeSaysWhereItWent:
    """Measured 2026-09-15: a purge oozed at home and the answer said only
    that no fault was raised.  The shared sequence now names the place on
    every backend, whether or not it knows a purge position."""

    def test_a_generic_backend_says_in_place_and_why(self, fast_clock):
        stub = _GcodeStub()
        result = stub.purge_filament(temperature=200, length_mm=10)
        assert result.success is True
        assert "Extruded in place" in result.message
        assert "no printer_model" in result.message
        station = result.details["purge_station"]
        assert station["status"] == "in_place"
        assert station["wiped"] is None

    def test_a_declared_model_with_no_station_is_named(self, fast_clock):
        stub = _GcodeStub()
        stub._printer_model = "prusa_mk4"
        result = stub.purge_filament(temperature=200, length_mm=10)
        assert "no verified position record for prusa_mk4" in result.message
        assert result.details["purge_station"]["printer_id"] == "prusa_mk4"

    def test_a_backend_with_no_emitter_purges_in_place_even_with_a_record(self, fast_clock):
        """A record is figures, not a sequence.  A generic backend declared
        as a model that HAS a record (a Klipper build wearing ``bambu_a1``
        in config.yaml) never sends the park, so its answer must not claim
        one.  Before this pin the base gate read any record as "parked"."""
        stub = _GcodeStub()
        stub._printer_model = "bambu_a1"
        stub.purge_station = lambda: {"printer_id": "bambu_a1", "kinematics": "bed_slinger", "chute": {"x_park_mm": -48.2}}
        result = stub.purge_filament(temperature=200, length_mm=10)
        assert result.success
        assert result.details["purge_station"]["status"] == "in_place"
        assert "Extruded in place" in result.message and "Parked" not in result.message
        assert "no motion sequence" in result.message and "bambu_a1" in result.message
        assert stub.gcode[0] == ["M83", "G1 E10 F180", "M82"]  # no park script before the move

    def test_a_reported_position_is_quoted(self, fast_clock):
        stub = _GcodeStub()
        stub.get_tool_position = lambda: {"x": 12.0, "y": 34.5, "z": 5.0}
        result = stub.purge_filament(temperature=200, length_mm=10)
        assert "X12 Y34.5 Z5" in result.message
        assert result.details["purge_station"]["position"] == {"x": 12.0, "y": 34.5, "z": 5.0}

    def test_a_paused_print_gives_the_paused_reason(self, fast_clock):
        stub = _GcodeStub(state=PrinterStatus.PAUSED)
        result = stub.purge_filament(temperature=200, length_mm=10)
        assert "paused" in result.details["purge_station"]["reason"]
        assert "Extruded in place" in result.message

    def test_a_retract_carries_no_placement(self, fast_clock):
        stub = _GcodeStub()
        result = stub.unload_filament(temperature=200, length_mm=10)
        assert "purge_station" not in result.details
        assert "in place" not in result.message

    def test_the_station_lookup_never_asks_the_global_resolver(self, monkeypatch):
        """The record is read for the CONFIG-DECLARED model only: with two
        machines registered, the global resolver answers for the default
        printer, and that is how the second would be driven to the first
        one's chute.  The served catalogue is faked here; the public file
        carries no station block."""
        import kiln.printer_intelligence as pi
        from kiln import printer_model_resolver as resolver

        stub = _GcodeStub()
        monkeypatch.setattr(resolver, "resolve_printer_model", lambda: "bambu_a1")
        served = mock.Mock(purge_station={"chute": {"x_park_mm": -48.2}})
        monkeypatch.setattr(pi, "_profiles_for_caller", lambda: {"bambu_a1": served})
        assert stub.purge_station() is None
        stub._printer_model = "bambu_a1"
        assert stub.purge_station()["printer_id"] == "bambu_a1"


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
    adapter._mqtt_client.publish.return_value = mock.MagicMock(rc=mqtt.MQTT_ERR_SUCCESS)
    adapter._confirm_window_s = 0.0
    adapter._fw_modules_requested = True  # skip the get_version round trip
    adapter._last_status = json.loads(json.dumps(_AMS))
    adapter._last_state_time = float("inf")
    return adapter


def _published(adapter) -> list[dict]:
    return [json.loads(c.args[1]) for c in adapter._mqtt_client.publish.call_args_list]


def _status_after_sleep(adapter, monkeypatch, **changes):
    """Let the first sleep mutate the push cache, as a printer would.

    ``tray_now`` lands inside the ``ams`` section, where every Bambu push
    carries it; the top-level key is not a shape the printer sends.
    """
    calls = {"n": 0}

    def _sleep(_s):
        calls["n"] += 1
        for key, value in changes.items():
            if key == "tray_now":
                adapter._last_status["ams"]["tray_now"] = value
            else:
                adapter._last_status[key] = value

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
        bambu._last_status["ams"]["tray_now"] = "1"
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

    def test_hms_list_entries_count_as_faults_too(self, bambu, monkeypatch):
        _status_after_sleep(bambu, monkeypatch, hms=[{"attr": 0x12002000, "code": 0x00020006}])
        result = bambu.load_filament(slot=0)
        assert result.success is False
        assert result.error_code == "1200_2000_0002_0006"
        assert "extruder may be clogged" in result.error_hint
        assert result.details["code_kind"] == "hms"

    def test_a_fault_result_carries_no_vendor_link(self, bambu, monkeypatch):
        """One door links out. A fault must not answer differently depending
        on which surface asked, and the hosted wire strips a nested link
        while a local caller would have seen it."""
        _status_after_sleep(bambu, monkeypatch, print_error=0x12008007)
        result = bambu.load_filament(slot=0)
        assert "hms_wiki_url" not in result.details
        assert not any(
            isinstance(v, str) and "http" in v
            for v in (result.error_hint, result.message)
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
        bambu._last_status["ams"]["tray_now"] = "0"
        counter = itertools.count(0.0, 0.5)
        monkeypatch.setattr(time, "monotonic", lambda: next(counter))

        def _sleep(_s):
            bambu._last_status["nozzle_temper"] = 210.0

        monkeypatch.setattr(time, "sleep", _sleep)
        result = bambu.purge_filament(length_mm=25)
        scripts = [p["print"]["param"] for p in _published(bambu) if p.get("print", {}).get("command") == "gcode_line"]
        assert "M104 S210" in scripts[0]
        assert any("G1 E25 F180" in sc for sc in scripts)
        assert result.success is True
        assert result.extrusion_verified is None
        assert result.verification_source == "no_fault_within_window"
        assert result.temperature == 210  # tray 0's window midpoint, no material named

    def test_a_fault_during_the_purge_turns_it_false(self, bambu, monkeypatch):
        bambu._last_status["ams"]["tray_now"] = "0"
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
        bambu._last_status["ams"]["tray_now"] = "255"
        with pytest.raises(PrinterError, match="no temperature"):
            bambu.purge_filament()


def _scripts(adapter) -> list[str]:
    return [p["print"]["param"] for p in _published(adapter) if p.get("print", {}).get("command") == "gcode_line"]


def _hot(adapter, monkeypatch, temp=210.0, cold=138.0):
    """Clock and thermistor: the first sleep brings the hotend to *temp*;
    once the heater has been switched off (``M104 S0`` published) each
    sleep reads *cold* instead, as a nozzle under the fan would.  Pass
    ``cold=None`` for a nozzle that never cools."""
    counter = itertools.count(0.0, 0.5)
    monkeypatch.setattr(time, "monotonic", lambda: next(counter))

    def _tick(_s):
        off = cold is not None and any(script == "M104 S0" for script in _scripts(adapter))
        adapter._last_status["nozzle_temper"] = cold if off else temp

    monkeypatch.setattr(time, "sleep", _tick)


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

    def test_every_reading_in_that_table_is_for_the_print_error_field(self):
        """Both classifiers that seeded it are handed ``print_error``.

        Whatever the same digits mean in the HMS namespace, these readings
        are for the other field, so none of them may answer an HMS lookup.
        """
        import inspect

        from kiln.printers import bambu

        for fn in (bambu._is_nozzle_clump_error, bambu._classify_flow_anomaly):
            assert "error_code" in inspect.signature(fn).parameters
        source = inspect.getsource(bambu.BambuAdapter._bambu_fault_codes)
        assert '"print_error"' in source and '"hms"' in source

    def test_the_namespace_collisions_are_recorded_not_reclassified(self):
        """Checked against wiki.bambulab.com/en/hms/home on 2026-09-03."""
        from kiln.printers.bambu import (
            _BAMBU_PRINT_ERROR_FAULTS,
            _HMS_NAMESPACE_COLLISIONS,
        )

        assert set(_HMS_NAMESPACE_COLLISIONS) == {
            "03001900", "03001A00", "03001800", "03000900",
        }
        # A collision is still a print_error entry — that is the point.
        for prefix in _HMS_NAMESPACE_COLLISIONS:
            assert prefix in _BAMBU_PRINT_ERROR_FAULTS
        for only_print_error in ("03008003", "03008005", "05000900", "05000B00"):
            assert only_print_error in _BAMBU_PRINT_ERROR_FAULTS
            assert only_print_error not in _HMS_NAMESPACE_COLLISIONS

    def test_the_contradictory_collision_is_flagged_as_a_hint(self):
        """0300-1900 means something unrelated in HMS, and Kiln's own
        print_error reading for it is unconfirmed. Neither may sound certain."""
        from kiln.printers.bambu import (
            _BAMBU_PRINT_ERROR_FAULTS,
            _HMS_NAMESPACE_COLLISIONS,
            describe_bambu_filament_fault,
        )

        assert "Y-axis" in _HMS_NAMESPACE_COLLISIONS["03001900"]
        assert "could not confirm" in _BAMBU_PRINT_ERROR_FAULTS["03001900"]
        text, url = describe_bambu_filament_fault("03001900", kind="print_error")
        assert "hint" in text and url is None

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
        bodies = [json.loads(c.request.body) for c in responses.calls if c.request.url.endswith("/api/printer/command")]
        assert bodies[0]["commands"] == ["M83", "G1 E20 F180", "M82"]
        assert bodies[1]["commands"] == ["M83", "G1 E-0.8 F1800", "M82"]  # the end-of-op pull-back
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
            if url.endswith("/printer/objects/list"):
                return _moon_response(json_data={"result": {"objects": ["extruder"]}})
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
            if url.endswith("/printer/objects/list"):
                return _moon_response(json_data={"result": {"objects": ["extruder"]}})
            if url.endswith("/printer/gcode/help"):
                return _moon_response(json_data={"result": {"G28": "Home"}})
            if url.endswith("/printer/objects/query"):
                return _moon_response(json_data={"result": {"status": {"extruder": {"can_extrude": True, "temperature": 210.0}}}})
            return _moon_response(json_data={"result": "ok"})

        with mock.patch.object(adapter._session, "request", side_effect=_request) as req:
            result = adapter.load_filament(temperature=210, length_mm=90)
        scripts = [(c.kwargs.get("params") or {}).get("script") for c in req.call_args_list if (c.kwargs.get("params") or {}).get("script")]
        assert "M83\nG1 E90 F180\nM82" in scripts
        assert result.success is True

    @pytest.mark.parametrize("mmu_object", ["mmu", "AFC"])
    def test_a_unit_owns_the_filament_path_so_load_is_refused_by_name(self, fast_clock, mmu_object):
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

    @pytest.mark.parametrize("failure", ["raises", "garbage"])
    def test_a_failed_probe_refuses_rather_than_assuming_no_mmu(self, fast_clock, failure):
        """A probe that FAILED is not a printer without a unit, and an answer
        in a shape Kiln does not understand is a failed read too.

        Collapsing the two is how an extruder-driven feed ends up fighting
        a Happy-Hare unit whose Moonraker happened not to answer. A wrong
        refusal costs a retry; a wrong proceed drives filament into a unit
        that may be mid-move.
        """
        adapter = self._adapter()

        def _request(method, url, **kw):
            if url.endswith("/printer/objects/list"):
                if failure == "raises":
                    raise ReqConnectionError("moonraker unreachable")
                return _moon_response(json_data={"result": {"not_objects": 1}})
            return _moon_response(json_data={"result": "ok"})

        with mock.patch.object(adapter._session, "request", side_effect=_request) as req:
            with pytest.raises(FilamentHandlingUnsupported, match="could not read"):
                adapter.load_filament(temperature=210)
            with pytest.raises(FilamentHandlingUnsupported, match="could not read"):
                adapter.unload_filament(temperature=210)
        assert not any(
            "G1 E" in ((c.kwargs.get("params") or {}).get("script") or "")
            for c in req.call_args_list
        )

    def test_a_clean_probe_showing_no_unit_proceeds(self, fast_clock):
        """The other side of the three-way answer: 'none' is not 'unknown'."""
        adapter = self._adapter()

        def _request(method, url, **kw):
            if url.endswith("/printer/objects/list"):
                return _moon_response(json_data={"result": {"objects": ["extruder", "toolhead"]}})
            if url.endswith("/printer/gcode/help"):
                return _moon_response(json_data={"result": {"G28": "Home"}})
            if url.endswith("/printer/objects/query"):
                return _moon_response(json_data={"result": {"status": {"extruder": {"can_extrude": True}}}})
            return _moon_response(json_data={"result": "ok"})

        with mock.patch.object(adapter._session, "request", side_effect=_request):
            result = adapter.load_filament(temperature=210)
        assert result.success is True

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
        for name in ("load_filament", "unload_filament", "purge_filament", "wipe_nozzle"):
            assert name in tools, name
            assert srv._get_safety_level(name) == "confirm", name
            assert srv._TOOL_RATE_LIMITS.get(name), name

    def test_wipe_success_envelope_carries_the_burn_warning(self, door):
        from kiln.plugins.filament_handling_tools import wipe_nozzle

        door._wipe_nozzle_impl = door._record
        out = wipe_nozzle(temperature=200)
        assert out["success"] is True
        assert out["action"] == "wipe"
        assert "safety" in out  # a hand goes near the pad next
        assert door.plans[0].action == "wipe" and door.plans[0].length_mm is None

    def test_wipe_on_a_backend_with_no_pad_is_UNSUPPORTED_with_what_to_do(self, door):
        from kiln.plugins.filament_handling_tools import wipe_nozzle

        out = wipe_nozzle(temperature=200)
        assert out["error"]["code"] == "UNSUPPORTED"
        assert "own screen" in out["error"]["message"]
        assert door.plans == []

    def test_wipe_gate_refusals_name_the_nozzle_not_filament(self, door):
        from kiln.plugins.filament_handling_tools import wipe_nozzle

        door._state = PrinterStatus.PRINTING
        out = wipe_nozzle(temperature=200)
        assert "wipe the nozzle while a print is running" in out["error"]["message"]


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

    def test_wipe_command_runs_the_same_tool(self, door):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        door._wipe_nozzle_impl = door._record
        result = CliRunner().invoke(cli, ["filament", "wipe", "--temp", "200", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["action"] == "wipe"
        assert door.plans[0].action == "wipe"

    def test_wipe_refusal_exits_nonzero_with_what_to_do(self, door):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        result = CliRunner().invoke(cli, ["filament", "wipe", "--temp", "200"])
        assert result.exit_code == 1
        assert "own screen" in result.output

    def test_the_group_has_all_four_verbs(self):
        from kiln.cli.main import cli

        assert set(cli.commands["filament"].commands) == {"load", "unload", "purge", "wipe"}


class TestOtherDoors:
    def test_doctor_reports_the_capability(self):
        import kiln.cli.main as main

        src = inspect.getsource(main.verify.callback)
        assert '"filament_handling"' in src

    def test_doctor_names_the_wipe_door_and_reads_the_station(self):
        import kiln.cli.main as main

        src = inspect.getsource(main.verify.callback)
        assert "wipe_nozzle" in src
        assert "purge_station()" in src

    def test_clog_recovery_plan_names_the_test(self):
        from kiln.failure_recovery import FailureType, _build_recovery

        plan = _build_recovery(FailureType.NOZZLE_CLOG)
        assert any("purge_filament" in step for step in plan.steps)
        assert any("wipe_nozzle" in step for step in plan.steps)

    def test_troubleshoot_printer_points_at_the_test(self):
        import kiln.server as srv

        out = srv.troubleshoot_printer("bambu_a1", symptom="nozzle clog, no extrusion")
        assert "purge_filament" in out.get("filament_next_step", "")
        assert "wipe_nozzle" in out.get("filament_next_step", "")

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

    def test_tool_safety_json_classifies_all_four_as_confirm(self):
        from pathlib import Path

        data = json.loads((Path(__file__).parent.parent / "src" / "kiln" / "data" / "tool_safety.json").read_text())
        for name in ("load_filament", "unload_filament", "purge_filament", "wipe_nozzle"):
            assert data["classifications"][name] == {"level": "confirm", "physical_effect": True}


# ---------------------------------------------------------------------------
# The position record: every figure is the vendor's, pinned to its source.
# ---------------------------------------------------------------------------

_DATA = __import__("pathlib").Path(__file__).parent.parent / "src" / "kiln" / "data"


def test_default_purge_length_is_a_clog_test_not_a_runaway():
    assert 10 <= DEFAULT_PURGE_LENGTH_MM <= 50


# ---------------------------------------------------------------------------
# The watch fits the host's request window, and says what to read next.
#
# Measured 2026-09-15 on an A1: ``load_filament(slot=3, wait_seconds=180)``
# came back "Request timed out" from the MCP client while the load ran to
# completion on the printer.  The tool did its job and the caller got
# nothing -- no step, no fault code, no extrusion_verified.
# ---------------------------------------------------------------------------


class TestWaitBudget:
    def test_plan_wait_is_bounded_by_the_ceiling(self):
        plan = FilamentOpPlan(action="load", temperature=210, temperature_source="caller",
                              options={"wait_seconds": 180, "wait_ceiling_seconds": 40})
        assert plan.wait_seconds(120) == 40

    def test_plan_wait_ceiling_bounds_the_adapter_default_too(self):
        plan = FilamentOpPlan(action="load", temperature=210, temperature_source="caller",
                              options={"wait_ceiling_seconds": 40})
        assert plan.wait_seconds(120) == 40

    def test_plan_wait_without_a_ceiling_is_what_was_asked(self):
        plan = FilamentOpPlan(action="load", temperature=210, temperature_source="caller",
                              options={"wait_seconds": 180})
        assert plan.wait_seconds(120) == 180
        assert FilamentOpPlan(action="load", temperature=210, temperature_source="caller").wait_seconds(120) == 120

    def test_a_short_wait_under_the_ceiling_is_kept(self):
        plan = FilamentOpPlan(action="purge", temperature=210, temperature_source="caller",
                              options={"wait_seconds": 5, "wait_ceiling_seconds": 40})
        assert plan.wait_seconds(10) == 5

    def test_the_door_declares_the_window_and_says_when_it_clamped(self, door, monkeypatch):
        import kiln.plugins.filament_handling_tools as fht

        monkeypatch.setattr(fht, "_WAIT_BUDGET_S", 40.0)
        out = fht.load_filament(temperature=200, wait_seconds=180)
        assert door.plans[0].options["wait_ceiling_seconds"] == 40.0
        assert door.plans[0].wait_seconds(120) == 40
        assert out["wait_seconds_requested"] == 180
        assert out["wait_seconds_applied"] == 40
        assert "KILN_FILAMENT_WAIT_BUDGET_S" in out["wait_note"]

    def test_the_same_window_binds_unload_and_purge(self, door, monkeypatch):
        import kiln.plugins.filament_handling_tools as fht

        monkeypatch.setattr(fht, "_WAIT_BUDGET_S", 40.0)
        fht.unload_filament(temperature=200, wait_seconds=90)
        fht.purge_filament(temperature=200, wait_seconds=60)
        assert [p.wait_seconds(999) for p in door.plans] == [40, 40]

    def test_a_wait_inside_the_window_is_not_mentioned(self, door, monkeypatch):
        import kiln.plugins.filament_handling_tools as fht

        monkeypatch.setattr(fht, "_WAIT_BUDGET_S", 40.0)
        out = fht.load_filament(temperature=200, wait_seconds=20)
        assert "wait_seconds_requested" not in out
        assert door.plans[0].wait_seconds(120) == 20

    def test_zero_disables_the_window(self, door, monkeypatch):
        import kiln.plugins.filament_handling_tools as fht

        monkeypatch.setattr(fht, "_WAIT_BUDGET_S", 0.0)
        fht.load_filament(temperature=200, wait_seconds=180)
        assert "wait_ceiling_seconds" not in door.plans[0].options
        assert door.plans[0].wait_seconds(120) == 180

    def test_the_bambu_watch_honours_the_ceiling_over_its_own_default(self, bambu, monkeypatch):
        _status_after_sleep(bambu, monkeypatch)  # nothing ever changes
        result = bambu.load_filament(slot=0, wait_ceiling_seconds=5)
        assert result.verification_source == "timeout_no_signal"
        assert result.details["waited_seconds"] == 5


class TestOutcomeVocabulary:
    """One field, three values -- the shape ``set_temperature`` and
    ``start_print`` already use, so a caller branches the same way."""

    def test_a_verified_load_is_confirmed(self, door):
        from kiln.plugins.filament_handling_tools import load_filament

        door._load_filament_impl = lambda plan: FilamentOpResult(
            success=True, action="load", message="done", extrusion_verified=True, verification_source="ams_tray_now"
        )
        assert load_filament(temperature=200)["outcome"] == "confirmed"

    def test_a_printer_fault_is_failed(self, door):
        from kiln.plugins.filament_handling_tools import purge_filament

        door._purge_filament_impl = lambda plan: FilamentOpResult(
            success=False, action="purge", message="no flow", extrusion_verified=False, error_code="1200_8007", error_hint="clogged"
        )
        out = purge_filament(temperature=200)
        assert out["outcome"] == "failed"
        assert out["error"]["code"] == "FILAMENT_FAULT"

    def test_a_watch_that_ran_out_is_accepted_and_points_at_the_next_read(self, door):
        from kiln.plugins.filament_handling_tools import load_filament

        door._load_filament_impl = lambda plan: FilamentOpResult(
            success=False, action="load", message="no signal within 40s", extrusion_verified=None,
            verification_source="timeout_no_signal",
            details={"tray_now": 255, "waited_seconds": 40, "next_read": "ams_status: tray_now == 3"},
        )
        out = load_filament(slot=3, temperature=200)
        assert out["success"] is False
        assert out["outcome"] == "accepted"
        assert out["error"]["code"] == "FILAMENT_UNCONFIRMED"
        assert out["next_read"] == "ams_status: tray_now == 3"

    def test_an_accepted_purge_with_no_flow_signal_is_accepted(self, door):
        from kiln.plugins.filament_handling_tools import purge_filament

        assert purge_filament(temperature=200)["outcome"] == "accepted"


class TestBambuTrayNowLocation:
    """The printer reports ``tray_now`` inside the ``ams`` section of every
    push; the watch has to read it there or it never sees the load finish."""

    def test_load_confirms_from_the_ams_section(self, bambu, monkeypatch):
        assert "tray_now" not in bambu._last_status  # only the nested one exists

        def _sleep(_s):
            bambu._last_status["ams"]["tray_now"] = "1"

        monkeypatch.setattr(time, "sleep", _sleep)
        counter = itertools.count(0.0, 0.5)
        monkeypatch.setattr(time, "monotonic", lambda: next(counter))
        result = bambu.load_filament(slot=1)
        assert result.success is True
        assert result.verification_source == "ams_tray_now"

    def test_purge_reads_the_feeding_tray_from_the_ams_section(self, bambu, monkeypatch):
        bambu._last_status["ams"]["tray_now"] = "3"  # the PETG tray: 230-260
        bambu._last_status["nozzle_temper"] = 245.0
        counter = itertools.count(0.0, 0.5)
        monkeypatch.setattr(time, "monotonic", lambda: next(counter))
        monkeypatch.setattr(time, "sleep", lambda s: None)
        result = bambu.purge_filament(length_mm=20)
        assert result.temperature == 245  # midpoint of tray 3's window, not tray 0's

    def test_the_timeout_names_the_read_that_finishes_the_answer(self, bambu, monkeypatch):
        _status_after_sleep(bambu, monkeypatch)
        result = bambu.load_filament(slot=1, wait_seconds=3)
        assert result.verification_source == "timeout_no_signal"
        nxt = result.details["next_read"]
        assert "ams_status" in nxt and "tray_now" in nxt and "1" in nxt
        assert "printer_status" in nxt and "print_error" in nxt
        assert "not" in result.message.lower() and "again" in result.message.lower()


class TestEveryFilamentOpEndsHeaterOff:
    """A filament op leaves the machine the way a person would: heater off, said.

    2026-09-16, on a real machine: a purge pushed 30 mm, reported success
    -- and left the nozzle at working temperature with nothing in the
    answer about it.  It kept oozing after 'done'.  Every op now ends with
    the end-of-print pull-back, the hotend target at 0, and says so;
    ``keep_hot=True`` is for a caller about to print and has to be asked
    for; a paused print keeps its heat.  The cool-down that follows (fan
    on, wait for the hand-off temperature, fan off) is served through
    kiln-pro; without it the answer says the nozzle is still hot rather
    than claim a cool-down that did not run.
    """

    def test_purge_retracts_then_turns_the_heater_off_and_says_so(self, no_kiln_pro, fast_clock):
        stub = _GcodeStub()
        result = stub.purge_filament(temperature=205, length_mm=25)
        assert result.success
        assert result.details["heater"] == "off" and result.details["end_retract_mm"] == 0.8
        assert "Retracted 0.8 mm and heater off" in result.message
        assert stub.gcode[-1] == ["M83", "G1 E-0.8 F1800", "M82"]  # the pull-back is the last script
        assert stub.temps[-1] == 0  # and the heater goes off after it

    def test_without_the_served_cool_down_the_answer_says_the_nozzle_is_still_hot(self, no_kiln_pro, fast_clock):
        stub = _GcodeStub()
        result = stub.purge_filament(temperature=205, length_mm=25)
        assert result.details["fan"] == "not driven" and result.details["cooled_below_c"] is None
        assert "still at working temperature" in result.message and "keep hands clear" in result.message
        assert "waited until" not in result.message and "over the chute" not in result.message
        assert not any("M106" in line for script in stub.gcode for line in script)

    def test_a_plans_finish_block_runs_the_cool_down_and_says_so(self, fast_clock, monkeypatch):
        stub = _GcodeStub()
        monkeypatch.setattr(stub, "_wait_for_hotend_below", lambda threshold, timeout: (True, 138.0))
        original = stub._purge_filament_impl

        def _impl(plan):
            result = original(plan)
            result.details["finish"] = {"fan_on": "M106 S255", "handoff_c": 140, "timeout_s": 150, "fan_off": "M106 S0", "over_chute": True}
            return result

        monkeypatch.setattr(stub, "_purge_filament_impl", _impl)
        result = stub.purge_filament(temperature=205, length_mm=25)
        assert result.details["fan"] == "off" and result.details["cooled_below_c"] == 140
        assert "waited until the nozzle read 138 °C" in result.message
        assert stub.gcode[-2:] == [["M106 S255"], ["M106 S0"]]
        # in place, so the drip is under the nozzle, not in a chute the op never went to
        assert "under the nozzle" in result.message and "over the chute" not in result.message

    def test_unload_does_not_retract_what_is_already_out(self, no_kiln_pro, fast_clock):
        stub = _GcodeStub()
        result = stub.unload_filament(temperature=205, length_mm=40)
        assert "end_retract_mm" not in result.details
        assert result.details["heater"] == "off"
        assert not any("G1 E-0.8 F1800" in line for script in stub.gcode for line in script)

    def test_keep_hot_leaves_it_on_and_says_that_instead(self, no_kiln_pro, fast_clock):
        stub = _GcodeStub()
        result = stub.purge_filament(temperature=205, length_mm=25, keep_hot=True)
        assert "left ON" in result.details["heater"] and "Heater left ON" in result.message
        assert stub.temps == [205.0]
        assert not any("G1 E-0.8 F1800" in line for script in stub.gcode for line in script)

    def test_a_paused_print_keeps_its_heat(self, no_kiln_pro, fast_clock, monkeypatch):
        stub = _GcodeStub()
        monkeypatch.setattr(
            stub, "get_state",
            lambda: PrinterState(connected=True, state=PrinterStatus.PAUSED, tool_temp_actual=205.0),
        )
        result = stub.purge_filament(temperature=205, length_mm=10)
        assert "left at" in result.details["heater"] and "paused" in result.details["heater"]
        assert stub.temps == [205.0]

    def test_a_refused_heater_off_is_a_warning_not_a_silence(self, no_kiln_pro, fast_clock, monkeypatch):
        from kiln.printers.command_verdict import CommandVerdict

        stub = _GcodeStub()
        monkeypatch.setattr(stub, "set_tool_temp", lambda t: CommandVerdict.refused("no"))
        result = stub.purge_filament(temperature=205, length_mm=10)
        assert "could not be switched off" in result.details["heater"]
        assert "WARNING" in result.message and "set_temperature(0)" in result.message

    def test_the_doors_carry_keep_hot(self, door):
        from click.testing import CliRunner

        from kiln.cli.main import cli
        from kiln.plugins.filament_handling_tools import purge_filament

        out = purge_filament(temperature=200, length_mm=10, keep_hot=True)
        assert "left ON" in out["details"]["heater"]
        out = purge_filament(temperature=200, length_mm=10)
        assert out["details"]["heater"] == "off"
        result = CliRunner().invoke(cli, ["filament", "purge", "--temp", "200", "--length", "15", "--keep-hot", "--json"])
        assert result.exit_code == 0, result.output
        assert "left ON" in result.output


def _fake_purge_doc(printer_id: str = "bambu_a1") -> dict:
    return {
        "schema": "motion_plan/1", "printer_id": printer_id, "verb": "purge", "ok": True, "steps": [],
        "pre_gcode": ["G91", "G1 Z7 F100", "G90", "G28 X", "G1 X-9 F100"],
        "post_gcode": ["M400", "G1 E-0.5 F100", "M400"],
        "park_gcode": ["G91", "G1 Z7 F100", "G90", "G28 X", "G1 X-9 F100", "M999"],
        "after": "then snapped the tail and shook it off on the chute wiper",
        "placement": {"status": "parked", "printer_id": printer_id, "position": {"x_mm": -9}, "wiped": "chute wiper",
                      "reason": "the position the machine's own start sequence flushes at"},
        "watch_seconds": 10, "raise_clearance_mm": 7.0,
        "finish": {"fan_on": "M106 S255", "handoff_c": 140, "timeout_s": 150, "fan_off": "M106 S0", "over_chute": True},
    }


def _fake_wipe_doc(printer_id: str = "bambu_a1") -> dict:
    return {
        "schema": "motion_plan/1", "printer_id": printer_id, "verb": "wipe", "ok": True, "steps": [],
        "pre_gcode": ["G91", "G1 Z7 F100", "G90", "G28 X", "G1 X-9 F100"],
        "post_gcode": ["G90", "M106 S255", "M104 S150", "M109 S150", "G1 Y99 F100", "G28 Z", "G1 Z3 F100", "M109 S120", "G28 X", "G1 X-9 F100", "M106 S0"],
        "placement": {"status": "parked", "printer_id": printer_id, "position": {"x_mm": -9}, "wiped": "brush pad",
                      "reason": "the position the machine's own start sequence flushes at",
                      "after": "snapped the tail and ran the brush passes"},
        "watch_seconds": 45, "end_retract_mm": 1.0, "retract_feed_mm_min": 500, "raise_clearance_mm": 7.0,
        "details": {"wipe_c": 150, "done_below_c": 120, "resting_position": {"over": "the chute"}},
        "summary": "Wiped the nozzle on the pad, the machine's own way.",
        "finish": {"fan_on": "M106 S255", "handoff_c": 120, "timeout_s": 150, "fan_off": "M106 S0", "over_chute": True},
    }


def _serve_plans(monkeypatch, docs):
    from kiln import _pro_motion_bridge as bridge

    monkeypatch.setattr(bridge, "plan_for", lambda adapter, verb, *, axes="XYZ", on_plate_ok=False: (docs or {}).get(verb))
    monkeypatch.setattr(bridge, "station_supports", lambda *a: None)


class TestBambuWithoutAPlan:
    """The public floor on a Bambu: no plan, no motion, an honest answer.

    The head is raised, parked over its chute, wiped and homed by a plan
    this install is handed one at a time; without one it never invents a
    coordinate.  A purge runs where the head is and says so; the wipe
    refuses by name and says what to use instead; the public catalogue
    carries no station block to read.
    """

    def test_the_public_catalogue_carries_no_station_block(self, no_kiln_pro):
        import json
        from pathlib import Path

        import kiln.printers.base as base_mod

        data = json.loads((Path(base_mod.__file__).resolve().parent.parent / "data" / "printer_intelligence.json").read_text())
        rows = {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)}
        assert rows and not any("purge_station" in v for v in rows.values())
        assert "purge_station_note" not in data.get("_meta", {})

    def test_a_declared_bambu_has_no_station_and_purges_in_place(self, no_kiln_pro, bambu, monkeypatch):
        _serve_plans(monkeypatch, None)
        bambu._printer_model = "bambu_a1"
        assert bambu.purge_station() is None
        bambu._last_status["ams"]["tray_now"] = "0"
        _hot(bambu, monkeypatch)
        result = bambu.purge_filament(length_mm=25)
        assert result.success and result.details["purge_station"]["status"] == "in_place"
        assert "served one plan at a time" in result.message and "bambu_a1" in result.message
        assert not any("G28 X" in s for s in _scripts(bambu))

    def test_a_plan_that_says_no_purges_in_place_in_its_own_words(self, no_kiln_pro, bambu, monkeypatch):
        doc = {**_fake_purge_doc("bambu_x1c"), "ok": False, "refusal": {"code": "UNSUPPORTED", "message": "the chute is a firmware macro"}}
        _serve_plans(monkeypatch, {"purge": doc})
        bambu._printer_model = "bambu_x1c"
        bambu._last_status["ams"]["tray_now"] = "0"
        _hot(bambu, monkeypatch)
        result = bambu.purge_filament(length_mm=10)
        assert result.details["purge_station"]["status"] == "in_place" and "firmware macro" in result.message
        ok, why = bambu._station_supports(None, "purge")
        assert ok is False and "firmware macro" in why

    def test_the_wipe_refuses_by_name_and_says_what_to_use(self, no_kiln_pro, bambu, monkeypatch):
        _serve_plans(monkeypatch, None)
        bambu._printer_model = "bambu_a1"
        bambu._last_status["ams"]["tray_now"] = "0"
        _hot(bambu, monkeypatch)
        with pytest.raises(FilamentHandlingUnsupported) as info:
            bambu.wipe_nozzle()
        text = str(info.value)
        assert "bambu_a1" in text and "served one plan at a time" in text
        assert "printer's own screen" in text and "start sequence wipes on the pad" in text
        assert "Home button" not in text  # the wipe refusal points at the wizard, not at homing
        assert _scripts(bambu) == []

    def test_a_paused_wipe_is_refused_before_anything_is_asked(self, no_kiln_pro, bambu, monkeypatch):
        _serve_plans(monkeypatch, None)
        bambu._printer_model = "bambu_a1"
        bambu._last_status["ams"]["tray_now"] = "0"
        _hot(bambu, monkeypatch)
        monkeypatch.setattr(bambu, "get_state", lambda: PrinterState(connected=True, state=PrinterStatus.PAUSED, tool_temp_actual=210.0))
        result = bambu.wipe_nozzle()
        assert result.success is False and result.verification_source == "refused_paused"

    def test_a_load_runs_the_firmware_routine_where_the_head_is_and_says_so(self, no_kiln_pro, bambu, monkeypatch):
        _serve_plans(monkeypatch, None)
        bambu._printer_model = "bambu_a1"
        _status_after_sleep(bambu, monkeypatch, tray_now="1")
        result = bambu.load_filament(slot=1, material="PLA")
        assert result.success and result.details["purge_station"]["status"] == "in_place"


class TestBambuRunsAFilamentPlan:
    """A purge or wipe plan that answers is run through the shared move: the
    plan's park lines before the heater, its tail after the extrude, its
    finish after the heater goes off -- and the answer says where it went."""

    def test_a_purge_plan_parks_snaps_and_cools(self, bambu, monkeypatch):
        _serve_plans(monkeypatch, {"purge": _fake_purge_doc()})
        bambu._printer_model = "bambu_a1"
        bambu._last_status["ams"]["tray_now"] = "0"
        _hot(bambu, monkeypatch)
        result = bambu.purge_filament(length_mm=25)
        assert result.success and result.details["purge_station"]["status"] == "parked"
        scripts = _scripts(bambu)
        assert scripts[0].startswith("G91\nG1 Z7 F100\nG90\nG28 X\nG1 X-9 F100")  # the park, before the heater
        assert any("G1 E25 F180\nM400\nG1 E-0.5 F100\nM400" in s for s in scripts)  # the tail rides after the extrude
        assert "snapped the tail" in result.message and "landed there" in result.message
        assert result.details["cooled_below_c"] == 140 and result.details["fan"] == "off"

    def test_a_wipe_plan_runs_the_pad_pass_and_reports_its_figures(self, bambu, monkeypatch):
        _serve_plans(monkeypatch, {"wipe": _fake_wipe_doc()})
        bambu._printer_model = "bambu_a1"
        bambu._last_status["ams"]["tray_now"] = "0"
        _hot(bambu, monkeypatch, cold=110.0)  # the plan's hand-off is 120; the nozzle must get there
        result = bambu.wipe_nozzle()
        assert result.success and result.action == "wipe"
        scripts = _scripts(bambu)
        assert any("G1 E-1 F500\nG90\nM106 S255\nM104 S150" in s for s in scripts)  # the vendor's retract, then the pad pass
        assert result.details["end_retract_mm"] == 1.0 and result.details["wipe_c"] == 150 and result.details["done_below_c"] == 120
        assert result.message.startswith("Wiped the nozzle on the pad") and "look at the tip" in result.message
        assert not any("G1 E-0.8 F1800" in s for s in scripts)  # the plan's own retract is not doubled
        assert result.details["cooled_below_c"] == 120  # the plan's hand-off, not a public constant

    def test_a_load_parks_first_when_the_plan_carries_a_park(self, bambu, monkeypatch):
        _serve_plans(monkeypatch, {"purge": _fake_purge_doc()})
        bambu._printer_model = "bambu_a1"
        _status_after_sleep(bambu, monkeypatch, tray_now="1")
        result = bambu.load_filament(slot=1, material="PLA")
        assert result.details["purge_station"]["status"] == "parked"
        assert _scripts(bambu)[0].endswith("M999")  # the park with the limits restored, then the firmware routine
        assert "fell into the chute" in result.message

    def test_a_recorded_part_taller_than_the_raise_keeps_the_purge_in_place(self, bambu, monkeypatch):
        from kiln.plate_state import PlateJob, mark_occupied

        _serve_plans(monkeypatch, {"purge": _fake_purge_doc()})
        bambu._printer_model = "bambu_a1"
        bambu._last_status["ams"]["tray_now"] = "0"
        _hot(bambu, monkeypatch)
        mark_occupied(bambu, PlateJob(file="vase.gcode", max_z_mm=60.0))
        result = bambu.purge_filament(length_mm=10)
        assert result.details["purge_station"]["status"] == "in_place" and "vase.gcode" in result.message
        assert not any("G28 X" in s for s in _scripts(bambu))
