"""Reading a Bambu printer's own "nozzle clumping detection" switch.

The A1's probing detector drives the toolhead off the bed to feel for a
blob, and the nozzle leaks a little each time -- the printer's own screen
says so when the switch is turned on, and asks for the purge tower.  Kiln
reads whether the switch is on off the machine and says so wherever a user
meets printer state.  This suite pins:

* one door, ``read_nozzle_clumping_detection``; a backend with no such
  switch returns None, and None is "cannot say", never "off";
* the Bambu read decodes the bit the founder's A1 was measured to flip
  (``home_flag`` bit 24, 2026-09-15), for that family ONLY -- any other
  model gets an honest "unverified", never a decoded guess;
* ``printer_status`` and ``preflight_check`` carry it as a plain fact,
  with the printer's own warning beside an ON reading, and never present
  the detector as a fail-safe.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kiln.printers.base import (
    NozzleClumpingDetection,
    PrinterAdapter,
    PrinterState,
    PrinterStatus,
)

#: The two raw ``home_flag`` values read off the founder's A1 with the
#: screen switch ON and OFF.  Everything else in the report was identical.
HOME_FLAG_ON = 863978896
HOME_FLAG_OFF = 847201680

A1_SERIAL = "039A1B2C3D4E5F6"
P1S_SERIAL = "01P00A123456789"


class TestTheDoor:
    def test_the_default_says_nothing(self):
        assert PrinterAdapter.read_nozzle_clumping_detection(object()) is None

    def test_backends_with_no_such_switch_keep_the_default(self):
        from kiln.printers.duet import DuetAdapter
        from kiln.printers.elegoo import ElegooAdapter
        from kiln.printers.moonraker import MoonrakerAdapter
        from kiln.printers.octoprint import OctoPrintAdapter
        from kiln.printers.prusalink import PrusaLinkAdapter
        from kiln.printers.serial_adapter import SerialPrinterAdapter

        for cls in (DuetAdapter, ElegooAdapter, MoonrakerAdapter, OctoPrintAdapter,
                    PrusaLinkAdapter, SerialPrinterAdapter):
            assert (
                cls.read_nozzle_clumping_detection is PrinterAdapter.read_nozzle_clumping_detection
            ), cls.__name__

    def test_a_reading_knows_whether_it_is_decoded(self):
        decoded = NozzleClumpingDetection(enabled=True, source="x")
        unverified = NozzleClumpingDetection(enabled=None, source="x", unverified_reason="why")
        assert decoded.is_decoded() and not unverified.is_decoded()

    def test_an_unverified_reading_must_carry_its_reason(self):
        with pytest.raises(ValueError):
            NozzleClumpingDetection(enabled=None, source="x")


class TestBambu:
    def _adapter(self, monkeypatch, *, serial, home_flag, age=3.0, modules=None, connected=True):
        from kiln.printers.bambu import BambuAdapter

        adapter = BambuAdapter(host="192.168.1.9", access_code="12345678", serial=serial, timeout=2)
        state = PrinterState(
            connected=connected,
            state=PrinterStatus.IDLE if connected else PrinterStatus.OFFLINE,
            state_age_seconds=age, state_stale_after_seconds=45.0,
        )
        monkeypatch.setattr(adapter, "get_state", lambda: state)
        adapter._last_status = {} if home_flag is None else {"home_flag": home_flag}
        adapter._fw_modules = list(modules or [])
        return adapter

    def test_the_measured_bit_reads_on(self, monkeypatch):
        out = self._adapter(monkeypatch, serial=A1_SERIAL, home_flag=HOME_FLAG_ON,
                            age=120.0, modules=[{"name": "ota", "sw_ver": "01.04.00.00"}]
                            ).read_nozzle_clumping_detection()
        assert out == NozzleClumpingDetection(
            enabled=True, source="bambu_mqtt_home_flag",
            age_seconds=120.0, stale_after_seconds=45.0, firmware_version="01.04.00.00",
        )

    def test_the_measured_bit_reads_off(self, monkeypatch):
        out = self._adapter(monkeypatch, serial=A1_SERIAL, home_flag=HOME_FLAG_OFF).read_nozzle_clumping_detection()
        assert out.enabled is False and out.is_decoded()

    def test_only_bit_24_decides(self, monkeypatch):
        """Every other bit of the flag is somebody else's business."""
        on_only = 1 << 24
        assert self._adapter(monkeypatch, serial=A1_SERIAL, home_flag=on_only).read_nozzle_clumping_detection().enabled is True
        everything_but = (HOME_FLAG_ON | 0xFFFF) & ~(1 << 24)
        assert self._adapter(monkeypatch, serial=A1_SERIAL, home_flag=everything_but).read_nozzle_clumping_detection().enabled is False

    def test_a_report_without_the_flag_is_none(self, monkeypatch):
        assert self._adapter(monkeypatch, serial=A1_SERIAL, home_flag=None).read_nozzle_clumping_detection() is None

    def test_a_flag_that_is_not_a_number_is_none(self, monkeypatch):
        assert self._adapter(monkeypatch, serial=A1_SERIAL, home_flag="garbage").read_nozzle_clumping_detection() is None

    def test_an_unreachable_machine_is_none(self, monkeypatch):
        assert self._adapter(monkeypatch, serial=A1_SERIAL, home_flag=HOME_FLAG_ON, connected=False).read_nozzle_clumping_detection() is None

    def test_another_model_is_unverified_never_decoded(self, monkeypatch):
        """The bit was measured on one A1.  A P1S carrying the same flag value
        gets no verdict -- a reading nobody could verify is unknown, never off."""
        out = self._adapter(monkeypatch, serial=P1S_SERIAL, home_flag=HOME_FLAG_OFF).read_nozzle_clumping_detection()
        assert out is not None
        assert out.enabled is None and not out.is_decoded()
        assert "not been verified" in out.unverified_reason
        assert out.source == "bambu_mqtt_home_flag"

    def test_the_a1_mini_is_unverified_too(self, monkeypatch):
        """Same feature, same screen switch, never measured: no inference across models."""
        out = self._adapter(monkeypatch, serial="030MINI000000000", home_flag=HOME_FLAG_ON).read_nozzle_clumping_detection()
        assert out.enabled is None

    def test_no_firmware_stamp_when_the_module_list_is_cold(self, monkeypatch):
        assert self._adapter(monkeypatch, serial=A1_SERIAL, home_flag=HOME_FLAG_ON).read_nozzle_clumping_detection().firmware_version is None


# ---------------------------------------------------------------------------
# Where a user meets it
# ---------------------------------------------------------------------------


def _reading(enabled, reason=None):
    return NozzleClumpingDetection(
        enabled=enabled, source="bambu_mqtt_home_flag", age_seconds=2.0,
        stale_after_seconds=45.0, unverified_reason=reason,
    )


def _status_adapter(reading):
    from kiln.printers.base import JobProgress
    from kiln.printers.bambu import BambuAdapter

    adapter = MagicMock(spec=BambuAdapter)
    adapter.get_state.return_value = PrinterState(state=PrinterStatus.IDLE, connected=True)
    adapter.get_job.return_value = JobProgress()
    adapter.read_nozzle_clumping_detection.return_value = reading
    adapter.capabilities.to_dict.return_value = {}
    return adapter


class TestPrinterStatus:
    def test_full_status_carries_the_switch_as_a_plain_fact(self):
        from kiln import server

        with patch("kiln.server._get_adapter", return_value=_status_adapter(_reading(True))):
            out = server.printer_status()
        block = out["nozzle_clumping_detection"]
        assert block["enabled"] is True
        assert block["read_from"] == "bambu_mqtt_home_flag"
        assert block["state_age_seconds"] == 2.0
        assert "is on" in block["statement"]
        assert "purge" in block["statement"] or "prime" in block["statement"]

    def test_off_is_said_plainly(self):
        from kiln import server

        with patch("kiln.server._get_adapter", return_value=_status_adapter(_reading(False))):
            out = server.printer_status()
        assert out["nozzle_clumping_detection"]["enabled"] is False
        assert "is off" in out["nozzle_clumping_detection"]["statement"]

    def test_unverified_is_never_off(self):
        from kiln import server

        with patch("kiln.server._get_adapter", return_value=_status_adapter(_reading(None, "not verified on this model"))):
            out = server.printer_status()
        block = out["nozzle_clumping_detection"]
        assert block["enabled"] is None
        assert "off" not in block["statement"].split("has not")[0]
        assert "not verified" in block["statement"]

    def test_never_a_fail_safe(self):
        from kiln import server

        with patch("kiln.server._get_adapter", return_value=_status_adapter(_reading(True))):
            out = server.printer_status()
        statement = out["nozzle_clumping_detection"]["statement"].casefold()
        assert "fail-safe" not in statement or "not a fail-safe" in statement

    def test_a_backend_that_cannot_say_leaves_the_key_out(self):
        from kiln import server

        with patch("kiln.server._get_adapter", return_value=_status_adapter(None)):
            out = server.printer_status()
        assert "nozzle_clumping_detection" not in out

    def test_lite_polling_omits_it(self):
        from kiln import server

        with patch("kiln.server._get_adapter", return_value=_status_adapter(_reading(True))):
            out = server.printer_status(detail="lite")
        assert "nozzle_clumping_detection" not in out


def _pf_state():
    state = MagicMock()
    state.connected = True
    state.state = PrinterStatus.IDLE
    state.tool_temp_actual = 25.0
    state.tool_temp_target = 0.0
    state.bed_temp_actual = 25.0
    state.bed_temp_target = 0.0
    return state


class TestPreflight:
    def _run(self, reading, mock_adapter, mock_registry):
        mock_adapter.return_value.get_state.return_value = _pf_state()
        mock_adapter.return_value.read_nozzle_clumping_detection.return_value = reading
        mock_registry.count = 1
        mock_registry.list_names.return_value = ["default"]
        from kiln.server import preflight_check

        result = preflight_check()
        checks = [c for c in result["checks"] if c["name"] == "nozzle_clumping_detection"]
        return result, checks

    @patch("kiln.server._get_adapter")
    @patch("kiln.server._get_temp_limits", return_value=(280.0, 120.0))
    @patch("kiln.server.get_db")
    @patch("kiln.server._registry")
    def test_on_is_an_advisory_carrying_the_printers_own_warning(self, mock_registry, mock_get_db, mock_limits, mock_adapter):
        result, checks = self._run(_reading(True), mock_adapter, mock_registry)
        assert len(checks) == 1
        check = checks[0]
        assert check["passed"] is True and check.get("advisory") is True
        assert check["enabled"] is True
        assert "purge" in check["message"] or "prime" in check["message"]
        assert "nozzle_clog_detect=False" in check["message"]
        assert "turns it back on" in check["message"]  # measured: the skip is the switch; Kiln restores it
        assert result["ready"] is True

    @patch("kiln.server._get_adapter")
    @patch("kiln.server._get_temp_limits", return_value=(280.0, 120.0))
    @patch("kiln.server.get_db")
    @patch("kiln.server._registry")
    def test_off_is_reported_and_asks_nothing(self, mock_registry, mock_get_db, mock_limits, mock_adapter):
        _result, checks = self._run(_reading(False), mock_adapter, mock_registry)
        assert checks[0]["enabled"] is False
        assert "is off" in checks[0]["message"]
        assert "purge" not in checks[0]["message"]

    @patch("kiln.server._get_adapter")
    @patch("kiln.server._get_temp_limits", return_value=(280.0, 120.0))
    @patch("kiln.server.get_db")
    @patch("kiln.server._registry")
    def test_unverified_says_so_and_is_never_off(self, mock_registry, mock_get_db, mock_limits, mock_adapter):
        _result, checks = self._run(_reading(None, "not verified on this model"), mock_adapter, mock_registry)
        assert checks[0]["enabled"] is None
        assert "not verified" in checks[0]["message"]

    @patch("kiln.server._get_adapter")
    @patch("kiln.server._get_temp_limits", return_value=(280.0, 120.0))
    @patch("kiln.server.get_db")
    @patch("kiln.server._registry")
    def test_a_backend_that_cannot_say_adds_no_check(self, mock_registry, mock_get_db, mock_limits, mock_adapter):
        _result, checks = self._run(None, mock_adapter, mock_registry)
        assert checks == []


# ---------------------------------------------------------------------------
# The switch rides the nozzle reading a local Kiln sends with a hosted call
# ---------------------------------------------------------------------------


class TestTheSwitchTravelsWithTheNozzleReading:
    """The hosted nozzle doors already receive this machine's nozzle reading;
    the switch is one more fact off the same report, so it rides the same
    observation instead of a second wire."""

    def _wire(self, monkeypatch, machine):
        from kiln.printers.base import NozzleSetting

        class _Registry:
            def list_names(self):
                return ["default"]

            def get(self, name):
                return machine

        monkeypatch.setattr("kiln.registry.get_printer_registry", lambda: _Registry())
        monkeypatch.setattr("kiln.printer_model_resolver.resolve_printer_model_for", lambda name: None)
        return NozzleSetting

    def _machine(self, monkeypatch, switch):
        NozzleSetting = self._wire(monkeypatch, None)

        class _Machine:
            name = "bambu"
            serial = A1_SERIAL
            host = ""

            def read_nozzle_setting(self):
                return NozzleSetting(material="stainless_steel", diameter_mm=0.4, source="bambu_mqtt_report")

            def read_nozzle_clumping_detection(self):
                return switch

        machine = _Machine()
        self._wire(monkeypatch, machine)
        return machine

    def test_a_decoded_switch_rides_the_observation(self, monkeypatch):
        from kiln import printer_nozzle_reading as reading

        self._machine(monkeypatch, _reading(True))
        obs = reading.observe_printer_nozzle("default")
        assert obs["clumping_detection"] == {
            "enabled": True, "read_from": "bambu_mqtt_home_flag", "value_kind": "switch",
            "state_age_seconds": 2.0, "stale_after_seconds": 45.0, "firmware_version": None,
        }

    def test_an_unverified_switch_rides_with_its_reason(self, monkeypatch):
        from kiln import printer_nozzle_reading as reading

        self._machine(monkeypatch, _reading(None, "not verified on this model"))
        obs = reading.observe_printer_nozzle("default")
        assert obs["clumping_detection"]["enabled"] is None
        assert obs["clumping_detection"]["unverified_reason"] == "not verified on this model"

    def test_a_backend_that_cannot_say_sends_no_switch(self, monkeypatch):
        from kiln import printer_nozzle_reading as reading

        self._machine(monkeypatch, None)
        obs = reading.observe_printer_nozzle("default")
        assert obs is not None and "clumping_detection" not in obs


# ---------------------------------------------------------------------------
# What the sliced FILE says: a tower or not, the mode, where the part sits
# ---------------------------------------------------------------------------


def _gcode(tmp_path, *, tower=False, spiral=False, by_object=False, name="part.gcode"):
    lines = [
        "; generated by PrusaSlicer 2.9.4",
        "G28",
        "G1 X-30 Y262 F6000 ; purge line, outside the plate on purpose",
        ";LAYER_CHANGE",
        ";Z:0.2",
        ";TYPE:Perimeter",
        "G1 X100 Y100 E1",
        "G1 X120 Y100 E1",
        "G1 X120 Y130 E1",
        "G1 X100 Y130 E1",
    ]
    if tower:
        lines += [";TYPE:Wipe tower", "G1 X200 Y200 E1", "G1 X235 Y200 E1"]
    lines += [
        ";LAYER_CHANGE",
        ";Z:0.4",
        ";TYPE:Perimeter",
        "G1 X100 Y100 E1",
        "; prusaslicer_config = begin",
        f"; spiral_vase = {1 if spiral else 0}",
        f"; complete_objects = {1 if by_object else 0}",
        f"; wipe_tower = {1 if tower else 0}",
        "; prusaslicer_config = end",
    ]
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n")
    return str(path)


class TestFileFacts:
    def test_a_file_with_a_tower_and_its_footprint(self, tmp_path):
        from kiln.nozzle_clumping_detection import file_facts

        facts = file_facts(_gcode(tmp_path, tower=True))
        assert facts["prime_tower_in_file"] is True
        assert facts["print_mode"] == "normal"
        fp = facts["footprint"]
        assert fp["x_min"] == 100.0 and fp["x_max"] == 235.0 and fp["y_max"] == 200.0

    def test_a_file_without_a_tower(self, tmp_path):
        from kiln.nozzle_clumping_detection import file_facts

        facts = file_facts(_gcode(tmp_path))
        assert facts["prime_tower_in_file"] is False
        assert facts["footprint"]["x_max"] == 120.0

    @pytest.mark.parametrize("kw, mode", [({"spiral": True}, "spiral_vase"), ({"by_object": True}, "by_object")])
    def test_the_modes_that_defeat_the_probe_are_named(self, tmp_path, kw, mode):
        from kiln.nozzle_clumping_detection import file_facts

        assert file_facts(_gcode(tmp_path, **kw))["print_mode"] == mode

    def test_a_bambu_3mf_is_read_through_its_gcode(self, tmp_path):
        import zipfile

        from kiln.nozzle_clumping_detection import file_facts

        gcode = _gcode(tmp_path, tower=True)
        threemf = tmp_path / "part.gcode.3mf"
        with zipfile.ZipFile(threemf, "w") as zf:
            zf.writestr("Metadata/plate_1.gcode", open(gcode).read())
            zf.writestr("Metadata/plate_1.json", '{"is_seq_print": true}')
        facts = file_facts(str(threemf))
        assert facts["prime_tower_in_file"] is True
        assert facts["print_mode"] == "by_object"

    def test_an_unreadable_file_is_none(self, tmp_path):
        from kiln.nozzle_clumping_detection import file_facts

        assert file_facts(str(tmp_path / "missing.gcode")) is None
        bad = tmp_path / "x.gcode"
        bad.write_bytes(b"\x00\x01")
        facts = file_facts(str(bad))
        assert facts is None or facts["prime_tower_in_file"] is False


class TestPreflightReadsTheFile:
    @patch("kiln.server._get_adapter")
    @patch("kiln.server._get_temp_limits", return_value=(280.0, 120.0))
    @patch("kiln.server.get_db")
    @patch("kiln.server._registry")
    def test_an_on_switch_with_no_tower_in_the_file_says_so(self, mock_registry, mock_get_db, mock_limits, mock_adapter, tmp_path, monkeypatch):
        mock_adapter.return_value.get_state.return_value = _pf_state()
        mock_adapter.return_value.read_nozzle_clumping_detection.return_value = _reading(True)
        mock_registry.count = 1
        mock_registry.list_names.return_value = ["default"]
        monkeypatch.setattr("kiln._pro_nozzle_bridge.consult_clumping_detection", lambda **kw: None)
        from kiln.server import preflight_check

        result = preflight_check(file_path=_gcode(tmp_path))
        check = next(c for c in result["checks"] if c["name"] == "nozzle_clumping_detection")
        assert check["file"]["prime_tower_in_file"] is False
        assert "no prime tower" in check["message"]
        assert check["advisory"] is True and result["ready"] is True

    @patch("kiln.server._get_adapter")
    @patch("kiln.server._get_temp_limits", return_value=(280.0, 120.0))
    @patch("kiln.server.get_db")
    @patch("kiln.server._registry")
    def test_kiln_pro_warnings_ride_the_check(self, mock_registry, mock_get_db, mock_limits, mock_adapter, tmp_path, monkeypatch):
        mock_adapter.return_value.get_state.return_value = _pf_state()
        mock_adapter.return_value.read_nozzle_clumping_detection.return_value = _reading(True)
        mock_registry.count = 1
        mock_registry.list_names.return_value = ["default"]
        seen = {}

        def fake(**kw):
            seen.update(kw)
            return {"statement": "curated sentence", "warnings": ["a part sits in the detection area"], "detection_area": {"x_min": 226.0}}

        monkeypatch.setattr("kiln._pro_nozzle_bridge.consult_clumping_detection", fake)
        from kiln.server import preflight_check

        result = preflight_check(file_path=_gcode(tmp_path))
        check = next(c for c in result["checks"] if c["name"] == "nozzle_clumping_detection")
        assert "a part sits in the detection area" in check["warnings"]
        assert check["detail"]["statement"] == "curated sentence"
        assert seen["reading"]["enabled"] is True
        assert seen["file"]["prime_tower_in_file"] is False
        assert "a part sits in the detection area" in check["message"]

    @patch("kiln.server._get_adapter")
    @patch("kiln.server._get_temp_limits", return_value=(280.0, 120.0))
    @patch("kiln.server.get_db")
    @patch("kiln.server._registry")
    def test_an_off_switch_reads_no_file(self, mock_registry, mock_get_db, mock_limits, mock_adapter, tmp_path, monkeypatch):
        """Nothing to warn about, so the file is not scanned for it."""
        mock_adapter.return_value.get_state.return_value = _pf_state()
        mock_adapter.return_value.read_nozzle_clumping_detection.return_value = _reading(False)
        mock_registry.count = 1
        mock_registry.list_names.return_value = ["default"]
        called = []
        monkeypatch.setattr("kiln._pro_nozzle_bridge.consult_clumping_detection", lambda **kw: called.append(kw))
        from kiln.server import preflight_check

        result = preflight_check(file_path=_gcode(tmp_path))
        check = next(c for c in result["checks"] if c["name"] == "nozzle_clumping_detection")
        assert "file" not in check and called == []


class TestTheBridge:
    def test_without_kiln_pro_it_is_none(self, monkeypatch):
        import builtins

        from kiln import _pro_nozzle_bridge as bridge

        real = builtins.__import__

        def no_pro(name, *a, **k):
            if name.startswith("kiln_pro"):
                raise ImportError(name)
            return real(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", no_pro)
        assert bridge.consult_clumping_detection(printer_model="bambu_a1", reading=_reading(True).__dict__, file=None) is None


class TestTheFaultTheCardThrew:
    def test_the_microsd_code_names_the_card_not_the_ams(self):
        from kiln.printers.bambu import describe_bambu_filament_fault

        reading, _page = describe_bambu_filament_fault("0500C010", kind="print_error")
        assert "microSD" in reading and "AMS" not in reading

    def test_the_0500_family_no_longer_blames_the_ams(self):
        from kiln.printers.bambu import describe_bambu_filament_fault

        reading, _page = describe_bambu_filament_fault("0500ABCD", kind="print_error")
        assert "AMS" not in reading


# ---------------------------------------------------------------------------
# Leave the machine as you found it: the skip is the printer's own switch
# ---------------------------------------------------------------------------


class TestRestoreAfterASkippedPrint:
    """``nozzle_clog_detect=False`` flips the printer's own switch OFF and
    leaves it off (measured 2026-09-15).  So Kiln reads the switch before it
    sends the skip and, when the print ends, puts back what it found -- only
    when it found ON, only once, never forcing ON for someone who had it OFF."""

    def _adapter(self, monkeypatch, reading):
        from kiln.printers.bambu import BambuAdapter

        adapter = BambuAdapter(host="192.168.1.9", access_code="12345678", serial=A1_SERIAL, timeout=2)
        sent: list[dict] = []
        monkeypatch.setattr(adapter, "_publish_command", lambda payload: sent.append(payload))
        monkeypatch.setattr(adapter, "read_nozzle_clumping_detection", lambda: reading)
        return adapter, sent

    @staticmethod
    def _enables(sent):
        return [p for p in sent if p.get("print", {}).get("command") == "print_option"
                and p["print"].get("nozzle_blob_detect") is True]

    def test_on_before_means_off_now_and_on_again_when_the_print_ends(self, monkeypatch):
        adapter, sent = self._adapter(monkeypatch, _reading(True))
        note = adapter._skip_nozzle_detection_for_print({})
        assert adapter._restore_nozzle_detection is True
        assert "turn it back on" in note
        assert any(p.get("print", {}).get("nozzle_blob_detect") is False for p in sent)
        assert not self._enables(sent)

        adapter._maybe_restore_nozzle_detection("running", "finish")
        assert len(self._enables(sent)) == 1
        assert adapter._restore_nozzle_detection is False
        adapter._maybe_restore_nozzle_detection("running", "finish")
        assert len(self._enables(sent)) == 1, "restored once, never again"

    def test_a_cancelled_or_failed_print_restores_too(self, monkeypatch):
        for end in ("failed", "idle"):
            adapter, sent = self._adapter(monkeypatch, _reading(True))
            adapter._skip_nozzle_detection_for_print({})
            adapter._maybe_restore_nozzle_detection("running", end)
            assert len(self._enables(sent)) == 1, end

    def test_a_non_terminal_transition_restores_nothing(self, monkeypatch):
        adapter, sent = self._adapter(monkeypatch, _reading(True))
        adapter._skip_nozzle_detection_for_print({})
        adapter._maybe_restore_nozzle_detection("prepare", "running")
        assert not self._enables(sent) and adapter._restore_nozzle_detection is True

    def test_off_before_is_left_off(self, monkeypatch):
        adapter, sent = self._adapter(monkeypatch, _reading(False))
        note = adapter._skip_nozzle_detection_for_print({})
        assert adapter._restore_nozzle_detection is False
        adapter._maybe_restore_nozzle_detection("running", "finish")
        assert not self._enables(sent)
        assert note == ""

    def test_unverified_is_never_forced_on_and_says_so(self, monkeypatch):
        adapter, sent = self._adapter(monkeypatch, _reading(None, "not verified on this model"))
        note = adapter._skip_nozzle_detection_for_print({})
        assert adapter._restore_nozzle_detection is False
        assert "may leave" in note and "switch" in note
        adapter._maybe_restore_nozzle_detection("running", "finish")
        assert not self._enables(sent)

    def test_a_backend_that_cannot_read_never_forces_on(self, monkeypatch):
        adapter, sent = self._adapter(monkeypatch, None)
        note = adapter._skip_nozzle_detection_for_print({})
        assert adapter._restore_nozzle_detection is False and "may leave" in note

    def test_the_opt_out_leaves_it_off_on_purpose(self, monkeypatch):
        adapter, sent = self._adapter(monkeypatch, _reading(True))
        note = adapter._skip_nozzle_detection_for_print({"restore_nozzle_detection": False})
        assert adapter._restore_nozzle_detection is False
        assert "leave it off" in note
        adapter._maybe_restore_nozzle_detection("running", "finish")
        assert not self._enables(sent)

    def test_a_start_that_failed_restores_immediately(self, monkeypatch):
        adapter, sent = self._adapter(monkeypatch, _reading(True))
        adapter._skip_nozzle_detection_for_print({})
        adapter._restore_nozzle_detection_now("the print never started")
        assert len(self._enables(sent)) == 1 and adapter._restore_nozzle_detection is False

    def test_the_enable_command_is_the_measured_one(self, monkeypatch):
        adapter, sent = self._adapter(monkeypatch, _reading(True))
        adapter._skip_nozzle_detection_for_print({})
        adapter._restore_nozzle_detection_now("test")
        cmd = self._enables(sent)[0]["print"]
        assert cmd == {"sequence_id": cmd["sequence_id"], "command": "print_option", "nozzle_blob_detect": True}


class TestTheStartPrintDoorCarriesTheOptOut:
    def test_the_tool_accepts_restore_nozzle_detection(self):
        import inspect

        from kiln import server

        params = inspect.signature(server.start_print).parameters
        assert params["restore_nozzle_detection"].default is True
