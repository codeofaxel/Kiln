"""Reading the nozzle a printer holds on record for itself, off the machine.

A printer keeps its own record of the nozzle fitted to it and acts on that
record when it prints.  Kiln can read it over the connection it already
holds, so a local Kiln sends the reading along with any hosted nozzle call
and the comparison can be made for that machine.  This suite pins:

* every backend answers through one door, ``read_nozzle_setting``; a
  backend whose protocol holds no such setting returns None, and None is
  "could not read", never "agrees";
* what each backend that CAN answer actually reports, from its own protocol;
* which physical machine an id means, so ``default`` and a model id reach
  the same printer and two of a model are never guessed between;
* the reading is stamped when the MACHINE said it, not when Kiln asked;
* the hosted stub sends this machine's reading with the request, only for
  the tools that take one, and never over a reading the caller supplied.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

from kiln import printer_identity as identity
from kiln import printer_nozzle_reading as reading
from kiln.printers.base import NozzleSetting, PrinterAdapter, PrinterError, PrinterState, PrinterStatus

# ---------------------------------------------------------------------------
# Bench
# ---------------------------------------------------------------------------


class _Machine:
    """A registered adapter as the reading sees it: a family, an identity,
    and one optional door."""

    def __init__(self, name, *, serial="", host="", setting=None, raises=False, delay=0.0):
        self.name = name
        self.serial = serial
        self.host = host
        self._setting = setting
        self._raises = raises
        self._delay = delay

    def read_nozzle_setting(self):
        if self._delay:
            time.sleep(self._delay)
        if self._raises:
            raise RuntimeError("offline")
        return self._setting


class _Registry:
    def __init__(self, **printers):
        self._by_name = dict(printers)

    def list_names(self):
        return sorted(self._by_name)

    def get(self, name):
        if name not in self._by_name:
            from kiln.printers.base import PrinterNotFoundError

            raise PrinterNotFoundError(name)
        return self._by_name[name]


def _wire(monkeypatch, models=None, **printers):
    monkeypatch.setattr("kiln.registry.get_printer_registry", lambda: _Registry(**printers))
    monkeypatch.setattr(
        "kiln.printer_model_resolver.resolve_printer_model_for",
        lambda name: (models or {}).get(name),
    )


def _bambu_setting(material="stainless_steel", diameter=0.4, age=3.0):
    return NozzleSetting(
        material=material, diameter_mm=diameter, source="bambu_mqtt_report",
        age_seconds=age, stale_after_seconds=45.0, firmware_version="01.04.00.00",
    )


# ---------------------------------------------------------------------------
# One door, every backend
# ---------------------------------------------------------------------------


class TestTheDoor:
    def test_the_default_says_nothing(self):
        assert PrinterAdapter.read_nozzle_setting(object()) is None

    def test_backends_with_no_such_setting_keep_the_default(self):
        """RepRapFirmware's object model, the SDCP protocol and Marlin over
        USB carry no nozzle setting; their adapters must not pretend."""
        from kiln.printers.duet import DuetAdapter
        from kiln.printers.elegoo import ElegooAdapter
        from kiln.printers.serial_adapter import SerialPrinterAdapter

        for cls in (DuetAdapter, ElegooAdapter, SerialPrinterAdapter):
            assert cls.read_nozzle_setting is PrinterAdapter.read_nozzle_setting, cls.__name__

    def test_an_empty_setting_knows_it_is_empty(self):
        assert NozzleSetting(material=None, diameter_mm=None, source="x").is_empty()
        assert not NozzleSetting(material=None, diameter_mm=0.4, source="x").is_empty()


class TestMoonraker:
    def _adapter(self, monkeypatch, settings, *, info_version="v0.12.0-1", raises=False):
        from kiln.printers.moonraker import MoonrakerAdapter

        adapter = MoonrakerAdapter(host="http://klipper.local:7125", timeout=5, retries=1)

        def fake_get_json(path, **kwargs):
            if raises:
                raise PrinterError("offline")
            if path == "/printer/objects/query":
                assert kwargs["params"] == {"configfile": "settings"}
                return {"result": {"status": {"configfile": {"settings": settings}}}}
            if path == "/printer/info":
                return {"result": {"software_version": info_version}}
            raise AssertionError(path)

        monkeypatch.setattr(adapter, "_get_json", fake_get_json)
        return adapter

    def test_diameter_from_the_extruder_section_and_no_material(self, monkeypatch):
        adapter = self._adapter(monkeypatch, {"extruder": {"nozzle_diameter": 0.6}})

        out = adapter.read_nozzle_setting()

        assert out == NozzleSetting(
            material=None, diameter_mm=0.6, source="klipper_configfile", firmware_version="v0.12.0-1"
        )

    def test_material_from_the_documented_macro(self, monkeypatch):
        adapter = self._adapter(monkeypatch, {
            "extruder": {"nozzle_diameter": "0.4"},
            "gcode_macro KILN_NOZZLE": {"variable_material": "'hardened_steel'", "gcode": ""},
        })

        assert adapter.read_nozzle_setting().material == "hardened_steel"

    def test_a_configuration_with_neither_is_none(self, monkeypatch):
        assert self._adapter(monkeypatch, {"extruder": {"max_temp": 300}}).read_nozzle_setting() is None

    def test_an_unreachable_machine_is_none(self, monkeypatch):
        assert self._adapter(monkeypatch, {}, raises=True).read_nozzle_setting() is None


class TestBambu:
    def _adapter(self, monkeypatch, *, nozzle_type, nozzle_diameter, age=3.0, modules=None):
        from kiln.printers.bambu import BambuAdapter

        adapter = BambuAdapter(host="192.168.1.9", access_code="12345678", serial="01P00A123", timeout=2)
        state = PrinterState(
            connected=True, state=PrinterStatus.IDLE,
            nozzle_type=nozzle_type, nozzle_diameter=nozzle_diameter,
            state_age_seconds=age, state_stale_after_seconds=45.0,
        )
        monkeypatch.setattr(adapter, "get_state", lambda: state)
        adapter._fw_modules = list(modules or [])
        return adapter

    def test_the_configured_type_and_diameter_with_their_age_and_budget(self, monkeypatch):
        adapter = self._adapter(monkeypatch, nozzle_type="stainless_steel", nozzle_diameter="0.4",
                                age=120.0, modules=[{"name": "ota", "sw_ver": "01.04.00.00"}])

        out = adapter.read_nozzle_setting()

        assert out == NozzleSetting(
            material="stainless_steel", diameter_mm=0.4, source="bambu_mqtt_report",
            age_seconds=120.0, stale_after_seconds=45.0, firmware_version="01.04.00.00",
        )

    def test_a_report_without_nozzle_fields_is_none(self, monkeypatch):
        assert self._adapter(monkeypatch, nozzle_type=None, nozzle_diameter=None).read_nozzle_setting() is None

    def test_the_word_is_kept_as_the_machine_said_it(self, monkeypatch):
        """No vocabulary here: what the word means is decided where the
        comparison is made."""
        out = self._adapter(monkeypatch, nozzle_type="tungsten_carbide_cht", nozzle_diameter="0.6").read_nozzle_setting()
        assert out.material == "tungsten_carbide_cht"

    def test_no_firmware_stamp_when_the_module_list_is_cold(self, monkeypatch):
        assert self._adapter(monkeypatch, nozzle_type="stainless_steel", nozzle_diameter="0.4").read_nozzle_setting().firmware_version is None


class TestPrusaLink:
    def test_the_machines_own_diameter_from_the_info_endpoint(self, monkeypatch):
        from kiln.printers.prusalink import PrusaLinkAdapter

        adapter = PrusaLinkAdapter(host="http://prusa.local", api_key="k")
        monkeypatch.setattr(adapter, "_get_json", lambda path, **kw: {"nozzle_diameter": 0.4} if path == "/api/v1/info" else {})

        assert adapter.read_nozzle_setting() == NozzleSetting(material=None, diameter_mm=0.4, source="prusalink_info")

    def test_an_info_without_the_field_is_none(self, monkeypatch):
        from kiln.printers.prusalink import PrusaLinkAdapter

        adapter = PrusaLinkAdapter(host="http://prusa.local", api_key="k")
        monkeypatch.setattr(adapter, "_get_json", lambda path, **kw: {"hostname": "prusa"})

        assert adapter.read_nozzle_setting() is None


class TestOctoPrint:
    def test_the_current_profiles_diameter(self, monkeypatch):
        from kiln.printers.octoprint import OctoPrintAdapter

        adapter = OctoPrintAdapter(host="http://octopi.local", api_key="k")
        monkeypatch.setattr(adapter, "_get_json", lambda path, **kw: {"profiles": {
            "_default": {"current": False, "extruder": {"nozzleDiameter": 0.4}},
            "ender": {"current": True, "extruder": {"nozzleDiameter": 0.6}},
        }})

        assert adapter.read_nozzle_setting() == NozzleSetting(material=None, diameter_mm=0.6, source="octoprint_printer_profile")

    def test_no_current_profile_is_none(self, monkeypatch):
        from kiln.printers.octoprint import OctoPrintAdapter

        adapter = OctoPrintAdapter(host="http://octopi.local", api_key="k")
        monkeypatch.setattr(adapter, "_get_json", lambda path, **kw: {"profiles": {}})

        assert adapter.read_nozzle_setting() is None


class TestCreality:
    def test_a_klipper_based_machine_answers_through_its_moonraker_backend(self, monkeypatch):
        from unittest.mock import MagicMock, patch

        from kiln.printers.creality import CrealityAdapter

        with patch("kiln.printers.creality.requests.get") as mock_get:
            probe = MagicMock(ok=True, status_code=200)
            probe.json.return_value = {"result": {"klippy_state": "ready"}}
            mock_get.return_value = probe
            adapter = CrealityAdapter("k1.local", timeout=5, retries=1)
        monkeypatch.setattr(
            adapter._backend, "read_nozzle_setting",
            lambda: NozzleSetting(material=None, diameter_mm=0.4, source="klipper_configfile"),
        )

        assert adapter.read_nozzle_setting().source == "klipper_configfile"


# ---------------------------------------------------------------------------
# Which machine an id means
# ---------------------------------------------------------------------------


class TestWhichMachineAnIdMeans:
    def test_default_and_the_model_id_are_one_machine(self, monkeypatch):
        one = _Machine("bambu", serial="SN1")
        _wire(monkeypatch, models={"default": "bambu_a1", "a1": "bambu_a1"}, default=one, a1=one)

        for given in ("default", "a1", "bambu_a1", "Bambu-A1"):
            assert set(identity.machine_aliases(given)) == {given, "default", "a1", "bambu_a1"}
            assert identity.machine_aliases(given)[0] == given

    def test_a_model_id_finds_the_one_machine_of_that_model(self, monkeypatch):
        _wire(monkeypatch, models={"default": "bambu_a1"}, default=_Machine("bambu", serial="SN1"))

        adapter, name, how = identity.resolve_machine("bambu_a1")

        assert (name, how) == ("default", "model")

    def test_two_of_a_model_are_not_guessed_between(self, monkeypatch):
        _wire(monkeypatch, models={"kitchen": "bambu_a1", "garage": "bambu_a1"},
              kitchen=_Machine("bambu", serial="SN1"), garage=_Machine("bambu", serial="SN2"))

        assert identity.resolve_machine("bambu_a1") is None
        assert identity.machine_aliases("kitchen") == ["kitchen"]

    def test_an_unregistered_name_is_never_matched_by_prefix(self, monkeypatch):
        _wire(monkeypatch, models={"default": "bambu_a1"}, default=_Machine("bambu", serial="SN1"))

        assert identity.resolve_machine("bambu_a1_kitchen") is None

    def test_no_registry_means_a_name_is_only_itself(self, monkeypatch):
        _wire(monkeypatch)
        assert identity.machine_aliases("default") == ["default"]

    def test_record_home_prefers_the_exact_id_then_the_first_alias_with_a_record(self, monkeypatch):
        one = _Machine("bambu", serial="SN1")
        _wire(monkeypatch, models={"default": "bambu_a1"}, default=one)

        assert identity.record_home("default", lambda a: a == "bambu_a1") == ("bambu_a1", [])
        assert identity.record_home("default", lambda a: a in {"default", "bambu_a1"}) == ("default", ["bambu_a1"])
        assert identity.record_home("default", lambda a: False) == ("default", [])

    def test_an_id_that_is_only_itself_is_looked_up_nowhere_else(self, monkeypatch):
        _wire(monkeypatch)
        asked = []
        identity.record_home("default", lambda a: asked.append(a) or False)
        assert asked == []


# ---------------------------------------------------------------------------
# The reading
# ---------------------------------------------------------------------------


class TestTheReading:
    def test_a_bambu_reading_with_its_stamps(self, monkeypatch):
        _wire(monkeypatch, models={"default": "bambu_a1"},
              default=_Machine("bambu", serial="01P00A123", setting=_bambu_setting(age=1200.0)))

        obs = reading.observe_printer_nozzle("bambu_a1")

        assert obs["machine"] == "default"
        assert obs["resolved_by"] == "model"
        assert obs["family"] == "bambu"
        assert obs["fingerprint"] == "bambu:serial:01p00a123"
        assert obs["material"] == "stainless_steel"
        assert obs["nozzle_diameter_mm"] == 0.4
        assert obs["read_from"] == "bambu_mqtt_report"
        assert obs["firmware_version"] == "01.04.00.00"
        assert obs["stale_after_seconds"] == 45.0
        assert obs["value_kind"] == "configured"
        behind = (datetime.now(timezone.utc) - datetime.fromisoformat(obs["read_at"])).total_seconds()
        assert 1150 < behind < 1260, "stamped when the machine said it, not when Kiln asked"

    def test_a_backend_that_cannot_say_reads_nothing(self, monkeypatch):
        _wire(monkeypatch, duet=_Machine("duet", host="duet.local", setting=None))
        assert reading.observe_printer_nozzle("duet") is None

    def test_an_unregistered_id_reads_nothing(self, monkeypatch):
        _wire(monkeypatch, default=_Machine("bambu", serial="SN1", setting=_bambu_setting()))
        assert reading.observe_printer_nozzle("k1c") is None

    def test_a_machine_that_raises_reads_nothing(self, monkeypatch):
        _wire(monkeypatch, voron=_Machine("moonraker", host="voron.local", raises=True))
        assert reading.observe_printer_nozzle("voron") is None

    def test_a_machine_past_the_deadline_reads_nothing(self, monkeypatch):
        monkeypatch.setattr(reading, "LIVE_READ_DEADLINE_S", 0.05)
        _wire(monkeypatch, voron=_Machine("moonraker", host="voron.local", setting=_bambu_setting(), delay=0.5))
        assert reading.observe_printer_nozzle("voron") is None

    def test_a_machine_kiln_is_not_driving_is_not_read(self, monkeypatch):
        _wire(monkeypatch, default=_Machine("bambu", serial="SN1", setting=_bambu_setting()))
        monkeypatch.setattr("kiln.printers.engagement.check_command",
                            lambda adapter, action: {"success": False, "code": "ENGAGED_ELSEWHERE"})
        assert reading.observe_printer_nozzle("default") is None


# ---------------------------------------------------------------------------
# The reading travels with the hosted request
# ---------------------------------------------------------------------------


class TestTheReadingTravelsWithTheRequest:
    def test_the_three_nozzle_tools_get_this_machines_reading(self, monkeypatch):
        _wire(monkeypatch, default=_Machine("bambu", serial="SN1", setting=_bambu_setting()))

        for tool in sorted(reading.TOOLS_THAT_TAKE_A_READING):
            out = reading.with_local_reading(tool, {"printer_id": "default"})
            assert out["printer_reading"]["material"] == "stainless_steel", tool
            assert out["printer_id"] == "default"

    def test_other_tools_are_untouched(self, monkeypatch):
        _wire(monkeypatch, default=_Machine("bambu", serial="SN1", setting=_bambu_setting()))
        assert reading.with_local_reading("cloud_remote_list", {"printer_id": "default"}) == {"printer_id": "default"}

    def test_a_reading_the_caller_supplied_is_never_overwritten(self, monkeypatch):
        _wire(monkeypatch, default=_Machine("bambu", serial="SN1", setting=_bambu_setting()))
        theirs = {"material": "brass"}
        assert reading.with_local_reading("get_nozzle_state", {"printer_id": "default", "printer_reading": theirs})["printer_reading"] is theirs

    def test_no_printer_id_or_no_reading_sends_nothing_extra(self, monkeypatch):
        _wire(monkeypatch, default=_Machine("bambu", serial="SN1", setting=None))
        assert reading.with_local_reading("get_nozzle_state", {}) == {}
        assert reading.with_local_reading("get_nozzle_state", {"printer_id": "default"}) == {"printer_id": "default"}

    def test_the_hosted_stub_sends_it(self, tmp_path, monkeypatch):
        """The door a free install actually goes through."""
        from kiln import server

        manifest = {"tools": [
            {"name": "get_nozzle_state", "description": "d", "tier": "free",
             "parameters": {"type": "object", "properties": {"printer_id": {"type": "string"}}, "required": ["printer_id"]}},
        ]}
        (tmp_path / "pro_tool_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        monkeypatch.setattr(server, "Path", lambda _p: tmp_path / "kiln")
        monkeypatch.setattr(server, "_PRO_TOOL_NUDGES", {})
        monkeypatch.setattr(server, "_PRO_TOOL_TIERS", {})
        monkeypatch.setattr(server, "_PRO_TOOL_QUOTA", {})
        _wire(monkeypatch, default=_Machine("bambu", serial="SN1", setting=_bambu_setting()))
        sent = {}
        monkeypatch.setattr(server, "_pro_api_call", lambda name, **kw: sent.update({name: kw}) or {"status": "ok"})

        registered = {}

        class _MCP:
            def tool(self):
                def deco(fn):
                    registered[fn.__name__] = fn
                    return fn
                return deco

        server._register_pro_tool_stubs(_MCP())
        registered["get_nozzle_state"](printer_id="default")

        assert sent["get_nozzle_state"]["printer_id"] == "default"
        assert sent["get_nozzle_state"]["printer_reading"]["read_from"] == "bambu_mqtt_report"
