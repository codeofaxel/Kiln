"""A printer Kiln has no row for can state its own limits -- and a person still confirms them.

Three layers, each pinned where it lives: the pure reader over a Klipper
``configfile.config`` mapping (``kiln.machine_limits``), the adapter door
that reads the config fresh and keeps the one-printer rule's refusal
whole, and ``add_safety_profile`` with ``profile`` left out, which puts
the numbers in front of the person as a proposal and takes the same code
as typed limits.  Every config here is synthetic.

A/B: every test in this file fails at 388938e7 (the module, the adapter
method and the ``printer_name`` parameter do not exist there).
"""

from __future__ import annotations

from typing import Any

import pytest

from kiln import screen_code
from kiln.machine_limits import SOURCE_LABEL, limits_from_klipper_config

VORON_LIKE = {
    "printer": {"kinematics": "corexy", "max_velocity": "300", "max_accel": "3000"},
    "stepper_x": {"position_min": "0", "position_max": "350", "position_endstop": "350"},
    "stepper_y": {"position_min": "0", "position_max": "350"},
    "stepper_z": {"position_min": "-5", "position_max": "330"},
    "extruder": {"max_temp": "300", "min_temp": "10", "nozzle_diameter": "0.4"},
    "heater_bed": {"max_temp": "120", "min_temp": "0"},
    "gcode_macro START_PRINT": {"gcode": "G28\nG1 Z10"},
}


class TestTheReader:
    def test_a_full_config_is_a_complete_proposal_naming_its_lines(self):
        out = limits_from_klipper_config(VORON_LIKE)
        assert out is not None and out["missing"] == []
        profile = out["profile"]
        assert profile["max_hotend_temp"] == 300.0
        assert profile["max_bed_temp"] == 120.0
        assert profile["max_feedrate"] == 300.0 * 60  # mm/s in the config, mm/min in the profile
        assert profile["build_volume"] == [350.0, 350.0, 330.0]
        assert profile["notes"].startswith(SOURCE_LABEL)
        assert "[extruder] max_temp: 300" in profile["notes"]
        assert "[heater_bed] max_temp: 120" in profile["notes"]
        assert "[printer] max_velocity: 300 mm/s" in profile["notes"]
        assert "[stepper_z] position_max: 330" in out["read"]["build_volume"]

    def test_a_negative_minimum_is_overtravel_not_bed(self):
        out = limits_from_klipper_config(VORON_LIKE)
        assert out["profile"]["build_volume"][2] == 330.0

    def test_a_positive_minimum_shortens_the_travel_and_is_named(self):
        config = {**VORON_LIKE, "stepper_x": {"position_min": "10", "position_max": "350"}}
        out = limits_from_klipper_config(config)
        assert out["profile"]["build_volume"][0] == 340.0
        assert "position_min: 10" in out["read"]["build_volume"]

    def test_what_the_config_leaves_out_is_listed_not_invented(self):
        config = {k: v for k, v in VORON_LIKE.items() if k != "heater_bed"}
        out = limits_from_klipper_config(config)
        assert out["missing"] == ["max_bed_temp"]
        assert "max_bed_temp" not in out["profile"]
        assert "heater_bed" not in out["profile"]["notes"]

    def test_an_axis_missing_leaves_the_whole_volume_out(self):
        config = {k: v for k, v in VORON_LIKE.items() if k != "stepper_y"}
        out = limits_from_klipper_config(config)
        assert "build_volume" not in out["profile"] and "build_volume" in out["missing"]

    @pytest.mark.parametrize("bad", ["abc", "", "nan", "-5", "0"])
    def test_a_value_that_is_not_a_positive_number_is_not_a_limit(self, bad):
        config = {**VORON_LIKE, "extruder": {"max_temp": bad}}
        out = limits_from_klipper_config(config)
        assert "max_hotend_temp" in out["missing"]

    def test_section_names_are_matched_without_case_or_padding(self):
        config = {" Extruder ": {"max_temp": "280"}}
        out = limits_from_klipper_config(config)
        assert out["profile"]["max_hotend_temp"] == 280.0

    @pytest.mark.parametrize("nothing", [None, {}, [], "x", {"gcode_macro A": {"gcode": "G28"}}])
    def test_nothing_usable_is_none(self, nothing):
        assert limits_from_klipper_config(nothing) is None


class TestTheAdapterDoor:
    def _adapter(self, **attrs):
        from kiln.printers.base import PrinterAdapter

        class Fake(PrinterAdapter):
            name = "fake"

            def __init__(self):
                pass

        # Only the base-class door is under test; the abstract commands are
        # never reached.
        Fake.__abstractmethods__ = frozenset()
        fake = Fake()
        for key, value in attrs.items():
            setattr(fake, key, value)
        return fake

    def test_a_klipper_backend_answers_the_proposal(self):
        fake = self._adapter(get_printer_config=lambda: VORON_LIKE)
        assert fake.read_machine_limits()["profile"]["max_hotend_temp"] == 300.0

    def test_it_reads_the_file_as_it_is_now_not_the_motion_cache(self):
        calls: list[int] = []
        fake = self._adapter(get_printer_config=lambda: calls.append(1) or VORON_LIKE)
        fake._motion_machine_source = ("klipper_config", {"extruder": {"max_temp": "999"}})
        assert fake.read_machine_limits()["profile"]["max_hotend_temp"] == 300.0
        fake.read_machine_limits()
        assert len(calls) == 2

    def test_a_backend_with_no_config_reader_answers_none(self):
        assert self._adapter().read_machine_limits() is None

    def test_a_machine_that_could_not_be_asked_answers_none(self):
        from kiln.printers.base import PrinterError

        def boom():
            raise PrinterError("offline")

        assert self._adapter(get_printer_config=boom).read_machine_limits() is None

    def test_it_is_a_setup_read_the_one_printer_rule_does_not_cover(self):
        from kiln.printers import engagement

        assert "read_machine_limits" in engagement.UNGATED_METHODS
        assert not engagement.is_gated("read_machine_limits")

    def test_the_config_is_read_as_kilns_own_while_another_machine_is_driven(self):
        """``get_printer_config`` stays gated on its own; the limits read
        goes through it as an internal read, so setting up a second printer
        while Kiln drives the first still gets its numbers."""
        from kiln.printers import engagement

        seen: list[bool] = []

        def config():
            seen.append(engagement._in_internal_read())
            return VORON_LIKE

        fake = self._adapter(get_printer_config=config)
        assert fake.read_machine_limits()["profile"]["max_hotend_temp"] == 300.0
        assert seen == [True]


# ---------------------------------------------------------------------------
# The tool door
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _own_override_file(tmp_path, monkeypatch):
    from kiln import safety_profiles as sp

    monkeypatch.setattr(sp, "_LOCAL_DIR", tmp_path)
    monkeypatch.setattr(sp, "_LOCAL_OVERRIDE_FILE", tmp_path / "local_printer_overrides.json")
    monkeypatch.setattr(sp, "_LEGACY_OVERRIDE_FILE", tmp_path / "community_profiles.json")
    monkeypatch.setattr(sp, "_LOCK_FILE", tmp_path / "locks.json", raising=False)
    monkeypatch.setattr(sp, "_local_override_cache", {})
    monkeypatch.setattr(sp, "_local_overrides_loaded", False, raising=False)
    monkeypatch.setattr(sp, "_locked_profiles", set(), raising=False)
    monkeypatch.setattr(sp, "_locks_loaded", False, raising=False)
    yield
    monkeypatch.setattr(sp, "_local_override_cache", {})


def _tools() -> dict[str, Any]:
    from kiln.plugins.safety_tools import _SafetyToolsPlugin

    tools: dict[str, Any] = {}

    class FakeMCP:
        def tool(self_mcp, **_kwargs):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

    _SafetyToolsPlugin().register(FakeMCP())
    return tools


class _FakePrinter:
    name = "voron"

    def __init__(self, config=VORON_LIKE, *, raises: Exception | None = None):
        self._config = config
        self._raises = raises
        self.asked = 0

    def read_machine_limits(self):
        self.asked += 1
        if self._raises is not None:
            raise self._raises
        return limits_from_klipper_config(self._config)


@pytest.fixture
def door(monkeypatch):
    import kiln.server as srv
    from kiln import auth_session

    monkeypatch.setattr(srv, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr(srv, "_audit", lambda *_a, **_k: None)
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    monkeypatch.setattr(
        auth_session, "resolve_api_bearer",
        lambda *a, **k: auth_session.ApiBearer(token="tok", state="license"),
    )
    printers: dict[str, Any] = {}

    def resolve(name=None):
        from kiln.registry import PrinterNotFoundError

        key = name or "default"
        if key not in printers:
            raise PrinterNotFoundError(f"no printer named {key!r}")
        return printers[key]

    monkeypatch.setattr(srv, "_resolve_adapter", resolve)
    screen_code._reset_for_tests()
    shown: list[screen_code.Issued] = []
    monkeypatch.setattr(screen_code, "_show_hook", lambda issued: shown.append(issued) or True)
    monkeypatch.setattr(screen_code, "screen_available", lambda: True)
    monkeypatch.setattr(screen_code, "MIN_READ_S", 0.0)
    yield _tools(), shown, printers
    screen_code._reset_for_tests()


class TestAddSafetyProfileReadsThePrinter:
    def test_the_numbers_come_off_the_printer_and_the_person_still_confirms(self, door):
        tools, shown, printers = door
        printers["default"] = _FakePrinter()
        first = tools["add_safety_profile"]("my_voron")
        assert first["success"] is False and first["error"]["code"] == "CONFIRMATION_REQUIRED"
        assert first["proposal"]["max_hotend_temp"] == 300.0
        assert first["read_from_printer"]["max_bed_temp"] == "[heater_bed] max_temp: 120"
        assert len(shown) == 1
        assert tools["give_print_code"](shown[0].code)["success"]
        second = tools["add_safety_profile"]("my_voron")
        assert second["success"] is True
        assert second["proposal"]["build_volume"] == [350.0, 350.0, 330.0]
        assert "read off the printer" in second["message"]
        from kiln.safety_profiles import get_profile

        saved = get_profile("my_voron")
        assert saved.max_hotend_temp == 300.0 and saved.max_bed_temp == 120.0
        assert SOURCE_LABEL in saved.notes
        assert "max_hotend_temp" in saved.owner_supplied

    def test_a_named_printer_is_the_one_read(self, door):
        tools, _shown, printers = door
        printers["default"] = _FakePrinter({**VORON_LIKE, "extruder": {"max_temp": "250"}})
        printers["hot"] = _FakePrinter()
        result = tools["add_safety_profile"]("my_voron", printer_name="hot")
        assert result["proposal"]["max_hotend_temp"] == 300.0
        assert printers["default"].asked == 0

    def test_typed_numbers_win_and_the_printer_is_not_asked(self, door):
        tools, _shown, printers = door
        printers["default"] = _FakePrinter()
        typed = {"max_hotend_temp": 240.0, "max_bed_temp": 80.0, "max_feedrate": 6000, "build_volume": [200, 200, 200]}
        result = tools["add_safety_profile"]("my_voron", typed, printer_name="default")
        assert result["success"] is True and "proposal" not in result
        assert printers["default"].asked == 0

    def test_the_code_is_for_these_numbers_a_changed_config_needs_a_new_one(self, door):
        tools, shown, printers = door
        printers["default"] = _FakePrinter()
        tools["add_safety_profile"]("my_voron")
        tools["give_print_code"](shown[0].code)
        printers["default"]._config = {**VORON_LIKE, "extruder": {"max_temp": "350"}}
        again = tools["add_safety_profile"]("my_voron")
        assert again["success"] is False and again["error"]["code"] == "CONFIRMATION_REQUIRED"
        assert again["proposal"]["max_hotend_temp"] == 350.0

    def test_no_printer_set_up_says_so_and_asks_for_the_numbers(self, door):
        tools, shown, _printers = door
        result = tools["add_safety_profile"]("my_voron")
        assert result["success"] is False and result["error"]["code"] == "PRINTER_NOT_FOUND"
        assert "profile" in result["error"]["message"] and shown == []

    def test_a_backend_that_states_nothing_says_so(self, door):
        tools, shown, printers = door
        printers["default"] = _FakePrinter(config=None)
        printers["default"].name = "bambu"
        result = tools["add_safety_profile"]("my_x1")
        assert result["error"]["code"] == "MACHINE_STATES_NO_LIMITS"
        assert "Klipper" in result["error"]["message"] and "bambu" in result["error"]["message"] and shown == []

    def test_a_partial_config_hands_back_what_it_read(self, door):
        tools, shown, printers = door
        printers["default"] = _FakePrinter({k: v for k, v in VORON_LIKE.items() if k != "heater_bed"})
        result = tools["add_safety_profile"]("my_voron")
        assert result["error"]["code"] == "MACHINE_STATES_PARTIAL_LIMITS"
        assert result["missing"] == ["max_bed_temp"]
        assert result["proposal"]["max_hotend_temp"] == 300.0 and shown == []

    def test_an_unreachable_printer_is_retryable_not_a_ruling(self, door):
        from kiln.printers.base import PrinterError

        tools, _shown, printers = door
        printers["default"] = _FakePrinter(raises=PrinterError("connection refused"))
        result = tools["add_safety_profile"]("my_voron")
        assert result["error"]["code"] == "PRINTER_UNREACHABLE" and result["error"].get("retryable") is True

    def test_the_hosted_server_reads_no_printer(self, door, monkeypatch):
        tools, _shown, printers = door
        printers["default"] = _FakePrinter()
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        result = tools["add_safety_profile"]("my_voron")
        assert result["error"]["code"] == "NOT_HERE" and printers["default"].asked == 0

    def test_signed_out_is_still_the_account_wall_with_the_numbers_shown(self, door, monkeypatch):
        from kiln import auth_session

        tools, shown, printers = door
        printers["default"] = _FakePrinter()
        monkeypatch.setattr(auth_session, "resolve_api_bearer", lambda *a, **k: auth_session.ApiBearer(token="", state="none"))
        monkeypatch.setattr("kiln.daily_stats.record_account_wall", lambda *_a, **_k: None)
        result = tools["add_safety_profile"]("my_voron")
        assert result["code"] == "KILN_ACCOUNT_NOT_PAIRED"
        assert result["proposal"]["max_hotend_temp"] == 300.0 and shown == []

    def test_limits_at_the_floor_need_no_code_even_read_off_the_printer(self, door):
        tools, shown, printers = door
        printers["default"] = _FakePrinter({
            **VORON_LIKE,
            "extruder": {"max_temp": "240"}, "heater_bed": {"max_temp": "80"},
            "printer": {"max_velocity": "100"},
        })
        result = tools["add_safety_profile"]("my_voron")
        assert result["success"] is True and shown == []
        assert result["proposal"]["max_feedrate"] == 6000.0
