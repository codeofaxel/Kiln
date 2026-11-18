"""A Klipper MMU is read and said, never silently treated as "no AMS".

Kiln's multi-material surface was Bambu-AMS-shaped throughout: every door
that asked "can this printer change filament, and what is loaded?" asked
in its own spelling — ``hasattr(adapter, "get_ams_status")``, ``== "bambu"``,
``isinstance(adapter, BambuAdapter)`` — and every spelling answered
*nothing here* for a Klipper machine carrying a Happy Hare or AFC unit.
The consequences, each pinned below against the pre-fix behaviour:

* the AMS colour-mismatch refusal was unreachable for Klipper
  (``_resolve_use_ams`` returned ``use_ams=False, warnings=[]`` at the
  ``hasattr`` gate, so a four-colour file at a two-gate ERCF printed);
* ``multi_material_print`` / ``multi_color_copies`` printed every object
  in one filament and mentioned it AFTER the print had started;
* ``kiln print --ams-mapping`` at a non-Bambu exited 0 having dropped the
  flag;
* the spool advisory vanished (``None``) for every non-Bambu printer,
  which reads as "nothing to say" when it meant "never looked";
* ``pre_estimate`` estimated 90-150 minutes of ERCF / MMU tool changes at
  ``confidence="high"`` with no caution, because ``hardware_unverified``
  was a per-row flag only the CFS rows carried — and the one caution it
  did emit told a Voron owner to "verify CFS slot mapping".

The Happy Hare payloads are captured from Happy Hare 4.0.0 running in the
``mainsail-crew/virtual-klipper-printer`` simulator, after
``MMU_GATE_MAP`` / ``MMU_TTG_MAP`` seeded gates 0-2 — real Klipper, real
Moonraker, no motion.  ``TestLiveSimulator`` re-reads that rig through the
real adapter when it is up on ``localhost:7125``.
"""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import responses

from kiln import multi_material as mm
from kiln.ams_routing import Tray
from kiln.printers.base import (
    PrinterAdapter,
    PrinterCapabilities,
    PrinterError,
    PrinterState,
    PrinterStatus,
)
from kiln.printers.moonraker import MoonrakerAdapter

HOST = "http://klipper.test:7125"

# --- captured from the simulator -------------------------------------------

HH_MMU: dict[str, Any] = {
    "enabled": True,
    "num_gates": 4,
    "gate": 0,
    "tool": -1,
    "unit": 0,
    "print_state": "initialized",
    "spoolman_support": "off",
    "ttg_map": [2, 1, 2, 3],
    "gate_status": [1, 1, -1, -1],
    "gate_material": ["PLA", "PETG", "ABS", ""],
    "gate_color": ["ff0000", "00ff00", "0000ff", ""],
    "gate_color_rgb": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [0.0, 0.0, 0.0]],
    "gate_temperature": [215, 200, 200, 200],
    "gate_spool_id": [-1, -1, -1, -1],
    "gate_filament_name": ["RedPLA", "GreenPETG", "", ""],
    "gate_vendor": ["Polymaker", "", "", ""],
}
HH_MACHINE: dict[str, Any] = {
    "happy_hare_version": "4.0.0",
    "num_units": 1,
    "num_gates": 4,
    "unit_0": {
        "name": "unit0", "display_name": "unit0", "vendor": "Other",
        "version": "1.0", "num_gates": 4, "first_gate": 0,
        "selector_type": "VirtualSelector", "multi_gear": True,
    },
}
HH_OBJECTS = ["gcode", "toolhead", "extruder", "mmu", "mmu_machine",
              "mmu_stepper unit0_gear", "gcode_macro MMU__LOAD"]


class _MmuPrinter(PrinterAdapter):
    """A Klipper-shaped adapter whose only multi-material path is a read."""

    def __init__(self, status: Any = None, raise_with: Exception | None = None) -> None:
        self._status = status
        self._raise = raise_with

    name = "moonraker"

    @property
    def capabilities(self) -> PrinterCapabilities:
        return PrinterCapabilities(can_report_multi_material=True)

    def get_multi_material_status(self):
        if self._raise is not None:
            raise self._raise
        return self._status

    def get_state(self) -> PrinterState:
        return PrinterState(connected=True, state=PrinterStatus.IDLE,
                            tool_temp_actual=24.0, bed_temp_actual=23.0)

    # abstract stubs the base demands
    def _start_print_impl(self, file_name, **kwargs): raise NotImplementedError
    def upload_file(self, path): raise NotImplementedError
    def cancel_print(self): raise NotImplementedError
    def pause_print(self): raise NotImplementedError
    def resume_print(self): raise NotImplementedError
    def get_job_progress(self): raise NotImplementedError
    def list_files(self): raise NotImplementedError
    def set_temperature(self, **kw): raise NotImplementedError
    def send_gcode(self, commands): raise NotImplementedError


class _PlainPrinter(_MmuPrinter):
    """A printer with no probe at all — the pre-existing world."""

    name = "octoprint"

    @property
    def capabilities(self) -> PrinterCapabilities:
        return PrinterCapabilities()

    def get_multi_material_status(self):
        return None


def _happy_hare() -> mm.MultiMaterialStatus:
    return mm.from_happy_hare(HH_MMU, HH_MACHINE)


def _abstract_free(cls):
    """Instantiate an adapter subclass without the ABC's abstract-method gate."""
    cls.__abstractmethods__ = frozenset()
    return cls


_MmuPrinter = _abstract_free(_MmuPrinter)
_PlainPrinter = _abstract_free(_PlainPrinter)


def _gcode(tmp_path, colours: list[str], types: list[str]) -> str:
    p = tmp_path / "part.gcode"
    p.write_text(
        "; generated by PrusaSlicer\n"
        f"; filament_colour = {';'.join(colours)}\n"
        f"; filament_type = {';'.join(types)}\n"
        "G28\nG1 X10\n"
    )
    return str(p)


# ===========================================================================
# The helper: one question, one record
# ===========================================================================


class TestHelper:
    def test_adapter_without_probe_is_none(self):
        st = mm.multi_material_status(object())
        assert st.kind == "none" and not st.detected and not st.driven_by_kiln

    def test_probe_returning_none_is_none(self):
        st = mm.multi_material_status(_PlainPrinter())
        assert st.kind == "none"
        assert "no_multi_material_path" in st.source

    def test_probe_that_raises_is_unknown_not_none(self):
        """A failed read is not an empty printer.  That conflation IS the bug class."""
        st = mm.multi_material_status(_MmuPrinter(raise_with=PrinterError("boom")))
        assert st.kind == "unknown"
        assert not st.detected
        assert "boom" in st.warnings[0]
        assert "could not read" in st.describe().lower()

    def test_happy_hare_gate_map_becomes_loaded_trays(self):
        st = _happy_hare()
        assert st.kind == "happy_hare"
        assert st.driven_by_kiln is False
        assert st.num_slots == 4
        assert st.tool_map == (2, 1, 2, 3)
        assert st.version == "4.0.0" and st.unit_name == "unit0"
        # gate 2 carries ABS but its status is -1 (unknown): NOT loaded.
        assert [t.slot for t in st.slots] == [0, 1]
        assert st.slots[0] == Tray(slot=0, material="PLA", hex6="FF0000")
        assert st.slots[1].material == "PETG" and st.slots[1].hex6 == "00FF00"
        assert st.warnings == []

    def test_happy_hare_all_unknown_is_said_not_read_as_empty(self):
        fresh = dict(HH_MMU, gate_status=[-1, -1, -1, -1])
        st = mm.from_happy_hare(fresh, HH_MACHINE)
        assert st.slots == ()
        assert any("not reported which gates" in w for w in st.warnings)

    def test_happy_hare_disabled_is_said(self):
        st = mm.from_happy_hare(dict(HH_MMU, enabled=False), HH_MACHINE)
        assert any("disabled" in w for w in st.warnings)

    def test_afc_lanes_parse_defensively_and_say_they_are_untested(self):
        afc = {
            "lanes": {
                "lane1": {"lane": 1, "map": "T0", "load": True, "prep": True,
                          "material": "PLA", "color": "#FF0000"},
                "lane2": {"lane": 2, "map": "T1", "load": False, "prep": False,
                          "material": "PETG", "color": "#00FF00"},
                "lane3": {"lane": 3, "map": "T2", "prep": True, "material": ""},
            },
        }
        st = mm.from_afc(afc)
        assert st.kind == "afc" and st.driven_by_kiln is False
        assert st.num_slots == 3
        assert [(t.slot, t.material, t.hex6) for t in st.slots] == [
            (0, "PLA", "FF0000"), (2, "UNKNOWN", None),
        ]
        assert any("not been verified" in w for w in st.warnings)

    def test_bambu_ams_kind_follows_the_model(self):
        info = {"ams_exist_bits": "1", "tray_exist_bits": "f", "units": [
            {"unit_id": 0, "trays": [
                {"slot": 0, "tray_type": "PLA", "tray_color": "FF0000FF"},
                {"slot": 1, "tray_type": "", "tray_color": ""},
            ]},
        ]}
        lite = mm.from_bambu_ams(info, printer_model="bambu_a1_mini")
        full = mm.from_bambu_ams(info, printer_model="bambu_x1c")
        assert lite.kind == "ams_lite" and full.kind == "ams"
        assert lite.driven_by_kiln and full.driven_by_kiln
        assert lite.num_slots == 2 and [t.slot for t in lite.slots] == [0]

    def test_bambu_without_ams_hardware_is_none(self):
        st = mm.from_bambu_ams({"ams_exist_bits": "0", "tray_exist_bits": "0", "units": []},
                               printer_model="bambu_p1s")
        assert st.kind == "none"

    def test_the_fact_kiln_drives_only_bambu_is_written_once(self):
        """Every door and the estimator read this set; it must say exactly AMS."""
        assert frozenset({"ams", "ams_lite"}) == mm.KILN_DRIVEN_CHANGERS
        assert _happy_hare().driven_by_kiln is False
        assert mm.from_afc({"lanes": {}}).driven_by_kiln is False

    def test_to_dict_carries_a_summary_sentence(self):
        d = _happy_hare().to_dict()
        assert d["detected"] is True and d["driven_by_kiln"] is False
        # ``tray_id`` is the printer's own id (unit * 4 + slot), the one the
        # print command takes; a one-unit changer's ids are its slots.
        assert d["loaded_slots"][0] == {"slot": 0, "unit": 0, "tray_id": 0, "name": None, "material": "PLA", "color": "FF0000"}
        assert "does not drive" in d["summary"]

    def test_capabilities_default_cannot_report(self):
        assert PrinterCapabilities().can_report_multi_material is False


# ===========================================================================
# The Moonraker probe — object list, then the gate map
# ===========================================================================


class TestMoonrakerProbe:
    @staticmethod
    def _listing(objects: list[str]) -> None:
        responses.add(responses.GET, f"{HOST}/printer/objects/list",
                      json={"result": {"objects": objects}}, status=200)

    @responses.activate
    def test_happy_hare_is_detected_and_read(self):
        self._listing(HH_OBJECTS)
        responses.add(responses.GET, f"{HOST}/printer/objects/query",
                      json={"result": {"eventtime": 1.0,
                                       "status": {"mmu": HH_MMU, "mmu_machine": HH_MACHINE}}},
                      status=200)
        adapter = MoonrakerAdapter(host=HOST, retries=1)
        assert adapter.capabilities.can_report_multi_material is True
        st = adapter.get_multi_material_status()
        assert st.kind == "happy_hare" and st.num_slots == 4
        assert [t.slot for t in st.slots] == [0, 1]
        query = responses.calls[1].request
        assert "mmu=" in query.url and "mmu_machine=" in query.url

    @responses.activate
    def test_afc_is_detected_by_object_name(self):
        self._listing(["gcode", "toolhead", "AFC", "AFC_stepper lane1"])
        responses.add(responses.GET, f"{HOST}/printer/objects/query",
                      json={"result": {"status": {"AFC": {"lanes": {
                          "lane1": {"lane": 1, "map": "T0", "load": True, "material": "PLA"}}}}}},
                      status=200)
        st = MoonrakerAdapter(host=HOST, retries=1).get_multi_material_status()
        assert st.kind == "afc" and [t.slot for t in st.slots] == [0]

    @responses.activate
    def test_no_mmu_object_is_none(self):
        self._listing(["gcode", "toolhead", "extruder", "filament_switch_sensor runout"])
        st = MoonrakerAdapter(host=HOST, retries=1).get_multi_material_status()
        assert st.kind == "none" and st.source == "moonraker:no_mmu_object"

    @responses.activate
    def test_transport_failure_is_unknown_through_the_helper(self):
        responses.add(responses.GET, f"{HOST}/printer/objects/list",
                      body=ConnectionError("down"))
        adapter = MoonrakerAdapter(host=HOST, retries=1)
        with pytest.raises(PrinterError):
            adapter.get_multi_material_status()
        assert mm.multi_material_status(adapter).kind == "unknown"

    @responses.activate
    def test_mmu_listed_but_no_status_is_an_error_not_none(self):
        self._listing(HH_OBJECTS)
        responses.add(responses.GET, f"{HOST}/printer/objects/query",
                      json={"result": {"status": {}}}, status=200)
        with pytest.raises(PrinterError):
            MoonrakerAdapter(host=HOST, retries=1).get_multi_material_status()


# ===========================================================================
# The print gate: the colour-mismatch refusal is reachable for Klipper
# ===========================================================================


class TestResolveUseAms:
    def test_three_colour_file_at_two_loaded_gates_is_blocked(self, tmp_path):
        """Pre-fix: use_ams=False, warnings=[] — the four-colour-file-at-an-ERCF print."""
        from kiln.server import _resolve_use_ams

        path = _gcode(tmp_path, ["#ff0000", "#00ff00", "#0000ff"], ["PLA", "PETG", "ABS"])
        decision = _resolve_use_ams("auto", None, _MmuPrinter(_happy_hare()), file_path=path)
        assert decision["use_ams"] is False, "Kiln does not drive an MMU"
        assert decision.get("blocked") is True
        assert decision["multi_material"]["kind"] == "happy_hare"
        text = " ".join(decision["warnings"])
        assert "3 filaments" in text and "Happy Hare" in text
        assert "cannot substitute" in text

    def test_matching_file_is_allowed_with_the_unit_named(self, tmp_path):
        from kiln.server import _resolve_use_ams

        path = _gcode(tmp_path, ["#ff0000", "#00ff00"], ["PLA", "PETG"])
        decision = _resolve_use_ams("auto", None, _MmuPrinter(_happy_hare()), file_path=path)
        assert decision["use_ams"] is False and not decision.get("blocked")
        assert decision["plan"]["ok"] is True
        assert "tool map" in " ".join(decision["warnings"])

    def test_explicit_mapping_is_reported_ignored_not_dropped(self):
        from kiln.server import _resolve_use_ams

        decision = _resolve_use_ams("auto", [0, 1], _MmuPrinter(_happy_hare()))
        assert decision["ams_mapping"] is None
        assert any("ignored" in w and "[0, 1]" in w for w in decision["warnings"])

    def test_plain_printer_keeps_its_old_shape_exactly(self):
        from kiln.server import _resolve_use_ams

        assert _resolve_use_ams("auto", None, _PlainPrinter()) == {
            "use_ams": False, "ams_mapping": None, "warnings": [],
        }

    def test_probe_failure_is_said(self):
        from kiln.server import _resolve_use_ams

        decision = _resolve_use_ams("auto", None, _MmuPrinter(raise_with=PrinterError("offline")))
        assert decision["use_ams"] is False
        assert decision["multi_material"]["kind"] == "unknown"
        assert any("could not read" in w.lower() for w in decision["warnings"])

    def test_unreported_gates_do_not_block_but_are_said(self, tmp_path):
        from kiln.server import _resolve_use_ams

        empty = mm.from_happy_hare(dict(HH_MMU, gate_status=[-1, -1, -1, -1]), HH_MACHINE)
        path = _gcode(tmp_path, ["#ff0000", "#00ff00"], ["PLA", "PETG"])
        decision = _resolve_use_ams("auto", None, _MmuPrinter(empty), file_path=path)
        assert not decision.get("blocked")
        assert any("could not be checked" in w for w in decision["warnings"])


# ===========================================================================
# The advisory the colouring tools carry
# ===========================================================================


class TestSpoolAdvisory:
    def _advise(self, adapter, colours):
        from kiln import server

        with patch.object(server, "_resolve_adapter", return_value=adapter), \
                patch.object(server, "_resolve_effective_printer_name", return_value="voron"):
            return server._spool_advisory(colours)

    def test_mmu_gate_map_judges_the_colours(self):
        """Pre-fix: None — the advisory vanished for every non-Bambu printer."""
        out = self._advise(_MmuPrinter(_happy_hare()), ["#ff0000", "#0000ff"])
        assert out is not None
        assert out["verdict"] == "mismatch"
        assert out["missing"][0]["color"] == "#0000FF"
        assert out["multi_material"]["kind"] == "happy_hare"
        assert "does not drive" in out["message"]

    def test_probe_failure_is_an_unknown_verdict_not_silence(self):
        out = self._advise(_MmuPrinter(raise_with=PrinterError("offline")), ["#ff0000"])
        assert out is not None and out["verdict"] == "unknown"
        assert "offline" in out["message"]

    def test_plain_printer_still_says_nothing(self):
        assert self._advise(_PlainPrinter(), ["#ff0000"]) is None

    def test_detected_unit_with_no_loaded_gates_says_so(self):
        empty = mm.from_happy_hare(dict(HH_MMU, gate_status=[-1, -1, -1, -1]), HH_MACHINE)
        out = self._advise(_MmuPrinter(empty), ["#ff0000"])
        assert out is not None and out["verdict"] == "empty"
        assert "Happy Hare" in out["message"]


# ===========================================================================
# Per-object filament prints refuse BEFORE slicing
# ===========================================================================


def _no_auth(*_a, **_k):
    return None


class TestPerObjectPrintsRefuse:

    @pytest.fixture(autouse=True)
    def _preview_gate_bypassed(self, monkeypatch):
        monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")  # this door now gates on a preview like start_print; these tests are about the rest

    def _stl(self, tmp_path) -> str:
        p = tmp_path / "cube.stl"
        # one triangle is enough: arrangement only needs a bounding box
        p.write_text(
            "solid cube\n facet normal 0 0 1\n  outer loop\n"
            "   vertex 0 0 0\n   vertex 10 0 0\n   vertex 0 10 0\n"
            "  endloop\n endfacet\nendsolid cube\n"
        )
        return str(p)

    def test_multi_material_print_refuses_at_an_undriven_unit(self, tmp_path):
        """Pre-fix: sliced and printed, then attached ams_warning to the result."""
        from kiln import server

        stl = self._stl(tmp_path)
        sliced: list[dict] = []
        with patch.object(server, "_check_auth", side_effect=_no_auth), \
                patch.object(server, "_resolve_adapter", return_value=_MmuPrinter(_happy_hare())), \
                patch.object(server, "run_reslice_and_print",
                             side_effect=lambda **kw: sliced.append(kw) or {"success": True}):
            result = server.multi_material_print(
                objects_json=json.dumps([
                    {"file_path": stl, "material_id": "pla", "color": "#ff0000"},
                    {"file_path": stl, "material_id": "pla_plus", "color": "#00ff00"},
                    {"file_path": stl, "material_id": "pla_matte", "color": "#0000ff"},
                ]),
                printer_id="voron_2",
            )
        assert result["success"] is False
        assert result["error"]["code"] == "MULTI_MATERIAL_NOT_DRIVEN"
        assert sliced == [], "the print step must not run"
        assert result["multi_material"]["kind"] == "happy_hare"
        assert result["multi_material_3mf"].endswith(".3mf")
        assert result["filaments_needed"] == 3
        assert "Happy Hare" in result["error"]["message"] and "OrcaSlicer" in result["error"]["message"]

    def test_multi_material_print_refuses_at_a_single_feed_printer(self, tmp_path):
        from kiln import server

        stl = self._stl(tmp_path)
        with patch.object(server, "_check_auth", side_effect=_no_auth), \
                patch.object(server, "_resolve_adapter", return_value=_PlainPrinter()), \
                patch.object(server, "run_reslice_and_print") as reslice:
            result = server.multi_material_print(
                objects_json=json.dumps([
                    {"file_path": stl, "material_id": "pla", "color": "#ff0000"},
                    {"file_path": stl, "material_id": "pla_plus", "color": "#00ff00"},
                ]),
            )
        assert result["error"]["code"] == "MULTI_MATERIAL_NOT_DRIVEN"
        assert "no multi-material unit" in result["error"]["message"]
        reslice.assert_not_called()

    def test_multi_material_print_proceeds_where_kiln_drives(self, tmp_path):
        from kiln import server

        class _Ams(_MmuPrinter):
            name = "bambu"

        driven = mm.from_bambu_ams(
            {"ams_exist_bits": "1", "tray_exist_bits": "3", "units": [{"unit_id": 0, "trays": [
                {"slot": 0, "tray_type": "PLA", "tray_color": "FF0000FF"},
                {"slot": 1, "tray_type": "PETG", "tray_color": "00FF00FF"}]}]},
            printer_model="bambu_a1",
        )
        stl = self._stl(tmp_path)
        with patch.object(server, "_check_auth", side_effect=_no_auth), \
                patch.object(server, "_resolve_adapter", return_value=_Ams(driven)), \
                patch.object(server, "ams_status", return_value={"success": False}), \
                patch.object(server, "run_reslice_and_print", return_value={"success": True}) as reslice:
            result = server.multi_material_print(
                objects_json=json.dumps([
                    {"file_path": stl, "material_id": "pla", "color": "#ff0000"},
                    {"file_path": stl, "material_id": "pla_plus", "color": "#00ff00"},
                ]),
            )
        assert result["success"] is True
        reslice.assert_called_once()

    def test_multi_color_copies_refuses_before_the_bambu_slot_map_is_sent(self, tmp_path):
        """Pre-fix: use_ams=True + the slot map went to the adapter, which dropped it."""
        from kiln import server

        stl = self._stl(tmp_path)
        with patch.object(server, "_check_auth", side_effect=_no_auth), \
                patch.object(server, "_resolve_adapter", return_value=_MmuPrinter(_happy_hare())), \
                patch.object(server, "ams_status", return_value={"success": False, "error": "UNSUPPORTED"}), \
                patch.object(server, "run_reslice_and_print") as reslice:
            result = server.multi_color_copies(
                model_path=stl, copies=2, ams_slots=[0, 1],
                colors=["#ff0000", "#00ff00"], printer_id="voron_2",
            )
        assert result["success"] is False
        assert result["error"]["code"] == "MULTI_MATERIAL_NOT_DRIVEN"
        reslice.assert_not_called()
        assert result["filaments_needed"] == 2

    def test_no_printer_at_all_leaves_the_old_failure_path_alone(self, tmp_path):
        """No adapter → the refusal stays out of the way; the print step fails as before."""
        from kiln import server

        stl = self._stl(tmp_path)
        with patch.object(server, "_check_auth", side_effect=_no_auth), \
                patch.object(server, "_resolve_adapter", side_effect=RuntimeError("no printer")), \
                patch.object(server, "run_reslice_and_print", return_value={"success": False, "error": "x"}) as reslice:
            result = server.multi_material_print(
                objects_json=json.dumps([
                    {"file_path": stl, "material_id": "pla", "color": "#ff0000"},
                    {"file_path": stl, "material_id": "pla_plus", "color": "#00ff00"},
                ]),
            )
        err = result.get("error")
        assert not (isinstance(err, dict) and err.get("code") == "MULTI_MATERIAL_NOT_DRIVEN")
        reslice.assert_called_once()


# ===========================================================================
# The doors that compose AND print say what the composer says about the
# plate's materials: the hardware each needs, and a check made without Kiln's
# answer, before and after the print.
# ===========================================================================


class TestPrintingDoorsSayTheComposersSafety:
    @pytest.fixture(autouse=True)
    def _signed_out_plain_install(self, monkeypatch):
        import kiln.server as srv
        from kiln import _pro_material_pairing_bridge as pairing

        monkeypatch.setattr(srv, "_served_bearer", lambda: ("", None))
        monkeypatch.setattr(pairing, "_local_mix_check", lambda *a, **k: pairing._NOT_HERE)

    @staticmethod
    def _driven():
        class _Ams(_MmuPrinter):
            name = "bambu"

        return _Ams(mm.from_bambu_ams(
            {"ams_exist_bits": "1", "tray_exist_bits": "3", "units": [{"unit_id": 0, "trays": [
                {"slot": 0, "tray_type": "PETG-CF", "tray_color": "FF0000FF"},
                {"slot": 1, "tray_type": "PETG", "tray_color": "00FF00FF"}]}]},
            printer_model="bambu_a1",
        ))

    def _assert_said(self, reply):
        assert any("PETG-CF" in w and "hardened" in w for w in reply.get("hardware_warnings") or []), reply
        assert reply.get("safety_basic") is True, reply
        assert "signed out" in reply.get("safety_caution", ""), reply

    def test_multi_material_print_says_it_after_the_print(self, tmp_path, monkeypatch):
        from kiln import server

        monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
        stl = TestPerObjectPrintsRefuse()._stl(tmp_path)
        with patch.object(server, "_check_auth", side_effect=_no_auth), \
                patch.object(server, "_resolve_adapter", return_value=self._driven()), \
                patch.object(server, "ams_status", return_value={"success": False}), \
                patch.object(server, "run_reslice_and_print", return_value={"success": True}):
            result = server.multi_material_print(objects_json=json.dumps([
                {"file_path": stl, "material_id": "PETG-CF", "color": "#ff0000"},
                {"file_path": stl, "material_id": "PETG", "color": "#00ff00"},
            ]))
        assert result["success"] is True
        self._assert_said(result)

    def test_multi_material_print_says_it_beside_the_plate_it_shows_first(self, tmp_path, monkeypatch):
        from kiln import server

        monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
        stl = TestPerObjectPrintsRefuse()._stl(tmp_path)
        with patch.object(server, "_check_auth", side_effect=_no_auth), \
                patch.object(server, "_resolve_adapter", return_value=self._driven()), \
                patch.object(server, "ams_status", return_value={"success": False}), \
                patch.object(server, "run_reslice_and_print") as reslice:
            result = server.multi_material_print(objects_json=json.dumps([
                {"file_path": stl, "material_id": "PETG-CF", "color": "#ff0000"},
                {"file_path": stl, "material_id": "PETG", "color": "#00ff00"},
            ]))
        assert result["error"]["code"] == "PREVIEW_NOT_CONFIRMED", result
        reslice.assert_not_called()
        self._assert_said(result)

    def test_multi_material_print_prints_the_plate_it_showed_and_says_it_again(self, tmp_path, monkeypatch):
        """The two documented calls through the real preview gate: the first
        builds the plate and hands it back, a real token is issued on that
        plate, and the second call, with the person's go, prints it and says
        what the first said."""
        from kiln import server
        from kiln.preview_gate import get_preview_gate
        from kiln.print_consent import reset_consent

        monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
        stl = TestPerObjectPrintsRefuse()._stl(tmp_path)
        objects = json.dumps([
            {"file_path": stl, "material_id": "PETG-CF", "color": "#ff0000"},
            {"file_path": stl, "material_id": "PETG", "color": "#00ff00"},
        ])
        with patch.object(server, "_check_auth", side_effect=_no_auth), \
                patch.object(server, "_resolve_adapter", return_value=self._driven()), \
                patch.object(server, "ams_status", return_value={"success": False}), \
                patch.object(server, "run_reslice_and_print", return_value={"success": True}) as reslice:
            shown = server.multi_material_print(objects_json=objects)
            assert shown["error"]["code"] == "PREVIEW_NOT_CONFIRMED", shown
            reslice.assert_not_called()
            token = get_preview_gate().issue(shown["plate_path"]).token
            said_go = TestMultiMaterialPrintWaitsForTheGo._said_go(shown["plate_path"])
            try:
                printed = server.multi_material_print(objects_json=objects, preview_token=token)
            finally:
                reset_consent(said_go)

        assert printed["success"] is True, printed
        reslice.assert_called_once()
        assert reslice.call_args.kwargs["model_path"] == shown["plate_path"]
        self._assert_said(printed)
        assert printed["hardware_warnings"] == shown["hardware_warnings"]
        assert printed["safety_caution"] == shown["safety_caution"]
        spots = {(o["x_mm"], o["y_mm"]) for o in printed["objects"]}
        assert len(spots) == 2, printed["objects"]

    def test_multi_color_copies_prints_with_a_real_token_and_says_it(self, tmp_path, monkeypatch):
        """multi_color_copies takes its token on the model it copies: the call
        carrying it and the person's yes prints the plate and says the
        hardware the plastic needs."""
        from kiln import server
        from kiln.preview_gate import get_preview_gate
        from kiln.print_consent import PrintConsent, reset_consent, set_consent

        monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
        stl = TestPerObjectPrintsRefuse()._stl(tmp_path)
        kwargs = dict(
            model_path=stl, copies=2, ams_slots=[0, 1], material="PETG-CF",
            colors=["#ff0000", "#00ff00"], printer_id="bambu_a1",
        )
        with patch.object(server, "_check_auth", side_effect=_no_auth), \
                patch.object(server, "_resolve_adapter", return_value=self._driven()), \
                patch.object(server, "ams_status", return_value={"success": False}), \
                patch.object(server, "run_reslice_and_print", return_value={"success": True}) as reslice:
            refused = server.multi_color_copies(**kwargs)
            assert refused["error"]["code"] == "PREVIEW_NOT_CONFIRMED", refused
            reslice.assert_not_called()
            token = get_preview_gate().issue(stl).token
            said_go = set_consent(PrintConsent(tool="multi_color_copies", file_name=stl, printer_name=None))
            try:
                printed = server.multi_color_copies(**kwargs, preview_token=token)
            finally:
                reset_consent(said_go)

        assert printed["success"] is True, printed
        reslice.assert_called_once()
        assert any("PETG-CF" in w and "hardened" in w for w in printed.get("hardware_warnings") or []), printed
        assert printed.get("safety_basic") is True and printed.get("safety_caution"), printed

    def test_multi_color_copies_says_it_after_the_print(self, tmp_path, monkeypatch):
        from kiln import server

        monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
        stl = TestPerObjectPrintsRefuse()._stl(tmp_path)
        with patch.object(server, "_check_auth", side_effect=_no_auth), \
                patch.object(server, "_resolve_adapter", return_value=self._driven()), \
                patch.object(server, "ams_status", return_value={"success": False}), \
                patch.object(server, "run_reslice_and_print", return_value={"success": True}):
            result = server.multi_color_copies(
                model_path=stl, copies=2, ams_slots=[0, 1], material="PETG-CF",
                colors=["#ff0000", "#00ff00"], printer_id="bambu_a1",
            )
        assert result["success"] is True, result
        assert any("PETG-CF" in w and "hardened" in w for w in result.get("hardware_warnings") or []), result
        assert result.get("safety_basic") is True and result.get("safety_caution"), result


# ===========================================================================
# multi_material_print starts the plate it showed, and only on the go
# ===========================================================================


class TestMultiMaterialPrintWaitsForTheGo:
    """The first call builds the plate and hands it back; the call carrying
    its preview token prints it.  That call is a print like any other: the
    person's go is needed beside the token, and it prints only the plate
    the same objects made.  multi_color_copies, whose yes is given on the
    model it copies, is walked the same way.  The preview token, the door's
    gate and the inner start's gate are all real; only the slice and the
    printer are stood in."""

    @pytest.fixture(autouse=True)
    def _real_gates(self, monkeypatch):
        import kiln.server as srv
        from kiln import _pro_material_pairing_bridge as pairing
        from kiln import print_signoff

        monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
        monkeypatch.setattr(srv, "_served_bearer", lambda: ("", None))
        monkeypatch.setattr(pairing, "_local_mix_check", lambda *a, **k: pairing._NOT_HERE)
        print_signoff.clear()
        yield
        print_signoff.clear()

    @staticmethod
    def _objects(stl: str, first: str = "PETG-CF", first_colour: str = "#ff0000") -> str:
        return json.dumps([
            {"file_path": stl, "material_id": first, "color": first_colour},
            {"file_path": stl, "material_id": "PETG", "color": "#00ff00"},
        ])

    @contextlib.contextmanager
    def _printer(self):
        """A Bambu with an AMS, and a slice-and-start step that records what
        it was handed instead of slicing."""
        from kiln import server

        started: list[dict[str, Any]] = []

        def _slice_and_start(**kw):
            started.append(kw)
            return SimpleNamespace(success=True, steps=[], to_dict=lambda: {})

        with patch.object(server, "_check_auth", side_effect=_no_auth), \
                patch.object(server, "_resolve_adapter", return_value=TestPrintingDoorsSayTheComposersSafety._driven()), \
                patch.object(server, "ams_status", return_value={"success": False}), \
                patch.object(server, "_pipeline_reslice_and_print", side_effect=_slice_and_start):
            yield started

    @staticmethod
    def _said_go(plate: str):
        """The yes the approval dialog records for the plate."""
        from kiln.print_consent import PrintConsent, set_consent

        return set_consent(PrintConsent(tool="multi_material_print", file_name=plate, printer_name=None))

    def test_the_token_alone_starts_nothing_and_the_go_prints_the_plate(self, tmp_path):
        from kiln import server
        from kiln.preview_gate import get_preview_gate
        from kiln.print_consent import reset_consent

        objects = self._objects(TestPerObjectPrintsRefuse()._stl(tmp_path))
        with self._printer() as started:
            shown = server.multi_material_print(objects_json=objects)
            assert shown["error"]["code"] == "PREVIEW_NOT_CONFIRMED", shown
            plate = shown["plate_path"]
            token = get_preview_gate().issue(plate, door="stage").token

            unasked = server.multi_material_print(objects_json=objects, preview_token=token)
            assert started == [], "the plate started with nobody's go"
            assert unasked["error"]["code"] == "PREVIEW_NOT_CONFIRMED", unasked

            # The refusal leaves the token good for the call that carries the yes.
            said_go = self._said_go(plate)
            try:
                printed = server.multi_material_print(objects_json=objects, preview_token=token)
            finally:
                reset_consent(said_go)

        assert printed["success"] is True, printed
        assert [s["model_path"] for s in started] == [plate]
        # The start rides the person's yes, through the door they saw it in.
        assert started[0]["signoff"]["source"] == "derived", started[0]["signoff"]
        assert started[0]["signoff"]["door"] == "stage", started[0]["signoff"]

    @pytest.mark.parametrize("changed", [{"first": "PETG"}, {"first_colour": "#0000ff"}], ids=["material", "colour"])
    def test_the_token_prints_only_the_plate_those_objects_made(self, tmp_path, changed):
        """The plate on disk is what the person saw; other objects with its
        token would print it at their temperatures and through their trays."""
        from kiln import server
        from kiln.preview_gate import get_preview_gate
        from kiln.print_consent import reset_consent

        stl = TestPerObjectPrintsRefuse()._stl(tmp_path)
        objects = self._objects(stl)
        with self._printer() as started:
            plate = server.multi_material_print(objects_json=objects)["plate_path"]
            token = get_preview_gate().issue(plate, door="stage").token
            said_go = self._said_go(plate)
            try:
                other = server.multi_material_print(objects_json=self._objects(stl, **changed), preview_token=token)
                assert started == [], "a plate printed for objects it was not built from"
                assert other["error"]["code"] == "PREVIEW_TOKEN_INVALID", other
                same = server.multi_material_print(objects_json=objects, preview_token=token)
            finally:
                reset_consent(said_go)

        assert same["success"] is True, same
        assert [s["model_path"] for s in started] == [plate]

    def test_multi_color_copies_starts_its_plate_on_the_yes_its_model_got(self, tmp_path):
        """The yes and the door the model was shown through reach the plate
        of copies, and the inner start's own gate accepts them."""
        from kiln import server
        from kiln.preview_gate import get_preview_gate
        from kiln.print_consent import PrintConsent, reset_consent, set_consent

        stl = TestPerObjectPrintsRefuse()._stl(tmp_path)
        with self._printer() as started:
            token = get_preview_gate().issue(stl, door="stage").token
            said_go = set_consent(PrintConsent(tool="multi_color_copies", file_name=stl, printer_name=None))
            try:
                printed = server.multi_color_copies(
                    model_path=stl, copies=2, ams_slots=[0, 1], material="PETG",
                    colors=["#ff0000", "#00ff00"], printer_id="bambu_a1", preview_token=token,
                )
            finally:
                reset_consent(said_go)

        assert printed["success"] is True, printed
        assert len(started) == 1 and started[0]["model_path"] == printed["multi_color_3mf"], started
        assert started[0]["signoff"]["source"] == "derived", started[0]["signoff"]
        assert started[0]["signoff"]["door"] == "stage", started[0]["signoff"]


# ===========================================================================
# preflight_check says what the printer carries
# ===========================================================================


class TestPreflight:
    def _run(self, adapter, **kw):
        from kiln import server

        with patch.object(server, "_resolve_control_target", return_value=(adapter, "voron")), \
                patch.object(server, "_get_registry") as reg:
            reg.return_value.count = 0
            return server.preflight_check(printer_name="voron", **kw)

    def _check(self, result):
        hits = [c for c in result["checks"] if c["name"] == "multi_material"]
        return hits[0] if hits else None

    def test_single_feed_printer_has_no_multi_material_line(self):
        assert self._check(self._run(_PlainPrinter())) is None

    def test_detected_unit_is_described_as_an_advisory(self):
        check = self._check(self._run(_MmuPrinter(_happy_hare())))
        assert check is not None and check["passed"] is True and check["advisory"] is True
        assert "Happy Hare" in check["message"]
        assert check["multi_material"]["num_slots"] == 4

    def test_file_needing_more_colours_than_loaded_fails_the_check(self, tmp_path):
        """Pre-fix: no such check existed at any tier — absence read as a pass."""
        path = _gcode(tmp_path, ["#ff0000", "#00ff00", "#0000ff"], ["PLA", "PETG", "ABS"])
        result = self._run(_MmuPrinter(_happy_hare()), file_path=path)
        check = self._check(result)
        assert check is not None and check["passed"] is False
        assert result["ready"] is False
        assert "needs 3 filaments" in check["message"] and "only 2 loaded" in check["message"]

    def test_file_that_fits_passes_with_the_counts_said(self, tmp_path):
        path = _gcode(tmp_path, ["#ff0000", "#00ff00"], ["PLA", "PETG"])
        check = self._check(self._run(_MmuPrinter(_happy_hare()), file_path=path))
        assert check["passed"] is True
        assert "needs 2 filaments" in check["message"]

    def test_probe_failure_is_an_advisory_not_a_pass_by_absence(self):
        check = self._check(self._run(_MmuPrinter(raise_with=PrinterError("offline"))))
        assert check is not None and check["advisory"] is True
        assert "could not read" in check["message"].lower()


# ===========================================================================
# pre_estimate: hardware_unverified is derived, and the caution names the changer
# ===========================================================================


class TestPreEstimateDerived:
    """The derivation, on the session's synthetic rows (a real printer's
    changer is its served record, pinned in kiln-pro's
    ``tests/test_printer_catalogue_numbers.py``)."""

    @pytest.mark.parametrize("printer_id,changer", [
        ("kilntest_variants_se", "ercf"), ("kilntest_big", "cfs"),
    ])
    def test_undriven_changers_are_hardware_unverified(self, printer_id, changer):
        """Pre-fix: False for every undriven changer — only the CFS rows said True."""
        from kiln.pre_estimate import _get_printer_tool_change

        tc = _get_printer_tool_change(printer_id)
        assert tc["tool_changer"] == changer
        assert tc["hardware_unverified"] is True

    def test_a_changer_kiln_drives_stays_verified(self):
        from kiln.pre_estimate import _get_printer_tool_change

        assert _get_printer_tool_change("kilntest_enclosed")["tool_changer"] == "ams_lite"
        assert _get_printer_tool_change("kilntest_enclosed")["hardware_unverified"] is False

    def test_no_changer_is_not_unverified(self):
        from kiln.pre_estimate import _get_printer_tool_change

        assert _get_printer_tool_change("kilntest_open")["hardware_unverified"] is False

    def test_addons_kiln_does_not_drive_are_unverified(self):
        from kiln.pre_estimate import _get_printer_tool_change, list_addons

        assert _get_printer_tool_change("ender3", tool_changer_addon="mosaic_palette3")["hardware_unverified"] is True
        listed = {a["id"]: a for a in list_addons()}
        assert all(a["hardware_unverified"] is True for a in listed.values())

    def test_the_caution_names_the_users_changer_not_cfs(self):
        """Pre-fix: silence for an ERCF row; and the only text that existed said 'CFS'."""
        from kiln.pre_estimate import estimate_from_dimensions

        est = estimate_from_dimensions(100, 100, 15, materials=["PLA", "PLA"], printer_id="kilntest_variants_se")
        cautions = [w for w in est.warnings if "unverified" in w]
        assert len(cautions) == 1
        assert "ERCF" in cautions[0]
        assert "CFS" not in cautions[0]

    def test_a_row_with_its_own_wording_says_it_once(self):
        """Pre-fix: the row warning AND the generic CFS line — twice, two wordings."""
        from kiln.pre_estimate import estimate_from_dimensions

        est = estimate_from_dimensions(100, 100, 15, materials=["PLA", "PLA"], printer_id="kilntest_big")
        assert sum("unverified" in w.lower() for w in est.warnings) == 1

    def test_a_changer_kiln_drives_gets_no_caution(self):
        from kiln.pre_estimate import estimate_from_dimensions

        est = estimate_from_dimensions(100, 100, 15, materials=["PLA", "PLA"], printer_id="kilntest_enclosed")
        assert not any("unverified" in w for w in est.warnings)


# ===========================================================================
# The CLI stops dropping flags
# ===========================================================================


class TestCli:
    @pytest.fixture
    def runner(self):
        from click.testing import CliRunner

        return CliRunner()

    def _patches(self, adapter):
        return (
            patch("kiln.cli.main._make_adapter", return_value=adapter),
            patch("kiln.cli.main.load_printer_config", return_value={
                "type": "moonraker", "host": "http://test.local:7125",
                "timeout": 30, "retries": 3}),
            patch("kiln.cli.main.validate_printer_config", return_value=(True, None)),
        )

    def test_print_with_ams_mapping_at_a_klipper_mmu_exits_nonzero(self, runner, tmp_path):
        """Pre-fix: exit 0, flag dropped, print started."""
        from kiln.cli.main import cli

        gcode = tmp_path / "part.gcode"
        gcode.write_text("G28\n")
        adapter = _MmuPrinter(_happy_hare())
        started: list = []
        adapter.upload_file = lambda p: (_ for _ in ()).throw(AssertionError("must not upload"))
        adapter.start_print = lambda *a, **k: started.append(k)
        p1, p2, p3 = self._patches(adapter)
        with p1, p2, p3:
            result = runner.invoke(cli, ["print", str(gcode), "--ams-mapping", "0,1", "--json"])
        assert result.exit_code == 1, result.output
        assert "AMS_UNSUPPORTED_ON_PRINTER" in result.output
        assert "Happy Hare" in result.output and "MMU_TTG_MAP" in result.output
        assert started == []

    def test_print_with_use_ams_at_a_plain_printer_exits_nonzero(self, runner, tmp_path):
        from kiln.cli.main import cli

        gcode = tmp_path / "part.gcode"
        gcode.write_text("G28\n")
        p1, p2, p3 = self._patches(_PlainPrinter())
        with p1, p2, p3:
            result = runner.invoke(cli, ["print", str(gcode), "--use-ams", "--json"])
        assert result.exit_code == 1
        assert "no multi-material unit" in result.output

    def test_print_without_the_flags_is_untouched(self, runner, tmp_path):
        from kiln.cli.main import cli
        from kiln.printers.base import PrintResult, UploadResult

        gcode = tmp_path / "part.gcode"
        gcode.write_text("G28\n")
        adapter = _MmuPrinter(_happy_hare())
        adapter.upload_file = lambda p: UploadResult(success=True, message="ok", file_name="part.gcode")
        adapter.start_print = lambda *a, **k: PrintResult(success=True, message="started")
        p1, p2, p3 = self._patches(adapter)
        with p1, p2, p3, patch("kiln.cli.main._run_cli_preflight", return_value=None, create=True):
            result = runner.invoke(cli, ["print", str(gcode), "--json", "--skip-preflight"])
        assert "AMS_UNSUPPORTED_ON_PRINTER" not in result.output


# ===========================================================================
# The instrument: the heartbeat can finally count MMUs in the field
# ===========================================================================


class TestInstrument:
    def test_a_sighting_is_recorded_by_kind(self, tmp_path, monkeypatch):
        from kiln import daily_stats

        monkeypatch.setattr(daily_stats, "_STATS_PATH", tmp_path / "stats.json")
        monkeypatch.setattr(daily_stats, "_recording_suppressed", lambda: False)
        mm.multi_material_status(_MmuPrinter(_happy_hare()))
        mm.multi_material_status(_PlainPrinter())
        mm.multi_material_status(_MmuPrinter(raise_with=PrinterError("x")))
        seen = daily_stats.get_daily_stats()["multi_material_seen"]
        assert seen == {"happy_hare": 1, "none": 1}, "unknown is never counted"

    def test_the_map_survives_midnight_and_leaves_the_machine(self):
        import inspect

        from kiln import daily_stats, heartbeat

        assert "multi_material_seen" in daily_stats._ROLLOVER_MAPS
        assert '"multi_material_seen"' in inspect.getsource(heartbeat)


# ===========================================================================
# Live: the simulator, when it is up
# ===========================================================================


def _sim_up() -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen("http://localhost:7125/printer/objects/list", timeout=2) as r:
            return "mmu" in json.load(r)["result"]["objects"]
    except Exception:
        return False


@pytest.mark.slow
@pytest.mark.skipif(not _sim_up(), reason="virtual-klipper-printer with Happy Hare not on :7125")
class TestLiveSimulator:
    """The real adapter against real Klipper + Moonraker + Happy Hare 4.0.0.

    Recipe: kiln-pro/.local/klipper_mmu_2026-09-03/sim/setup_happy_hare_sim.sh
    """

    def test_gate_map_reads_through_the_real_adapter(self):
        adapter = MoonrakerAdapter("http://localhost:7125", timeout=8, retries=1)
        st = mm.multi_material_status(adapter)
        assert st.kind == "happy_hare"
        assert st.num_slots == 4 and st.version is not None
        assert st.driven_by_kiln is False

    def test_the_gate_map_is_writable_and_read_back(self):
        """Config-and-state, no motion: the sim stays ready."""
        import urllib.request

        req = urllib.request.Request(
            "http://localhost:7125/printer/gcode/script",
            data=json.dumps({"script": "MMU_GATE_MAP GATE=3 MATERIAL=TPU COLOR=ffff00 AVAILABLE=1"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=30).read()
        st = MoonrakerAdapter("http://localhost:7125", timeout=8, retries=1).get_multi_material_status()
        gate3 = next(t for t in st.slots if t.slot == 3)
        assert gate3.material == "TPU" and gate3.hex6 == "FFFF00"
        with urllib.request.urlopen("http://localhost:7125/printer/info", timeout=5) as r:
            assert json.load(r)["result"]["state"] == "ready"


# ===========================================================================
# Second pass: defects the first pass shipped, each verified
# before it was fixed.  Kept as regressions because every one of them is the
# SAME failure the module exists to end, reappearing one layer in.
# ===========================================================================


class TestSpoolNameIsNotAMaterial:
    """A gate's filament NAME is a label, never a material spec.

    Pre-fix, a gate with no ``gate_material`` but a ``gate_filament_name``
    of "Polymaker Galaxy Black" produced ``material='POLYMAKER GALAXY
    BLACK'``.  That reached the colour matcher, which then warned "colour
    agrees, material differs" against a real PLA — a mismatch Kiln invented
    out of a brand string, in the one module built to stop Kiln inventing
    filament facts.
    """

    _NAMED = {
        "enabled": True, "num_gates": 2, "gate_status": [1, 1],
        "gate_material": ["", ""], "gate_color": ["ff0000", "00ff00"],
        "gate_filament_name": ["Polymaker Galaxy Black", "RedPLA"],
    }

    def test_an_uncurated_gate_is_unknown_not_the_spool_name(self):
        st = mm.from_happy_hare(self._NAMED, {"num_gates": 2})
        from kiln.ams_routing import UNREAD_MATERIAL

        assert [t.material for t in st.slots] == [UNREAD_MATERIAL] * 2
        assert "POLYMAKER" not in st.slots[0].label.upper()

    def test_no_invented_material_mismatch_reaches_the_matcher(self):
        from kiln.ams_routing import Filament, plan_ams_mapping

        st = mm.from_happy_hare(self._NAMED, {"num_gates": 2})
        plan = plan_ams_mapping([Filament(hex6="FF0000", material="PLA")], list(st.slots))
        assert plan.ok
        assert not any("material differs" in w for w in plan.warnings), (
            "Kiln invented a material from a spool name and then warned "
            "about the mismatch it invented"
        )

    def test_a_real_material_still_wins(self):
        st = mm.from_happy_hare(dict(self._NAMED, gate_material=["PLA", "PETG"]),
                                {"num_gates": 2})
        assert [t.material for t in st.slots] == ["PLA", "PETG"]


class TestCrealityCfsIsSeen:
    """A CFS is a real unit and Kiln has always been able to read it.

    Pre-fix the shared reader knew only Bambu and Klipper, so a K2 with a
    CFS answered ``kind="none"`` — "no multi-material unit" — to an owner
    looking straight at one.  Exactly the lie the module exists to end,
    left standing in the module itself.
    """

    _CFS = {
        "detected": True, "hardware_unverified": True,
        "slots": [
            {"slot": 0, "material": "PLA", "color": "#FF0000"},
            {"slot": 1, "material": "PETG", "color": "#00FF00"},
            {"slot": 2},
        ],
        "warnings": ["Creality CFS active slot control is hardware-unverified in Kiln."],
    }

    def test_a_loaded_cfs_reads_as_a_unit(self):
        st = mm.from_creality_cfs(self._CFS)
        assert st.kind == "cfs" and st.detected
        assert st.driven_by_kiln is False
        assert [(t.slot, t.material, t.hex6) for t in st.slots] == [
            (0, "PLA", "FF0000"), (1, "PETG", "00FF00"),
        ]
        assert st.num_slots == 3
        assert any("hardware-unverified" in w for w in st.warnings)

    def test_no_cfs_discovered_is_none(self):
        assert mm.from_creality_cfs({"detected": False, "slots": []}).kind == "none"

    def test_the_adapter_answers_the_shared_reader(self):
        from kiln.printers.creality import CrealityAdapter

        assert hasattr(CrealityAdapter, "get_multi_material_status")
        adapter = CrealityAdapter.__new__(CrealityAdapter)
        with patch.object(CrealityAdapter, "get_cfs_status", return_value=self._CFS):
            assert mm.multi_material_status(adapter).kind == "cfs"

    def test_cfs_colours_are_judged_like_any_other_unit(self):
        from kiln.ams_routing import advise_colours

        st = mm.from_creality_cfs(self._CFS)
        advice = advise_colours(["#ff0000", "#0000ff"], list(st.slots), printer="k2")
        assert advice.verdict == "mismatch"


class TestAmsStatusStopsDenyingAVisibleUnit:
    """The door a user knocks on to ask "what is loaded?".

    Pre-fix it told a Voron owner with a populated gate map: "AMS status is
    only available on Bambu Lab printers with AMS." Kiln could read that
    exact map by then. The dump stays Bambu-shaped — that part is honest —
    but the refusal now says what IS there rather than naming a brand the
    user did not buy.
    """

    def _ams_status(self, adapter):
        from kiln import server

        with patch.object(server, "_check_auth", return_value=None), \
                patch.object(server, "_check_rate_limit", return_value=None), \
                patch.object(server, "_get_adapter", return_value=adapter):
            return server.ams_status()

    def test_a_visible_mmu_is_named_in_the_refusal(self):
        out = self._ams_status(_MmuPrinter(_happy_hare()))
        assert out["success"] is False
        assert out["error"]["code"] == "UNSUPPORTED"
        assert "Happy Hare" in out["error"]["message"]
        assert out["multi_material"]["num_slots"] == 4
        assert len(out["multi_material"]["loaded_slots"]) == 2

    def test_a_failed_read_does_not_claim_the_printer_has_nothing(self):
        out = self._ams_status(_MmuPrinter(raise_with=PrinterError("offline")))
        assert "not saying it has none" in out["error"]["message"]
        assert out["multi_material"]["kind"] == "unknown"

    def test_a_single_feed_printer_still_says_so_plainly(self):
        out = self._ams_status(_PlainPrinter())
        assert "no multi-material unit of any kind" in out["error"]["message"]
        assert out["multi_material"]["kind"] == "none"


def test_the_afc_slot_has_no_dead_assignment():
    """Every line earns its place — the loop overwrote `slot` on both paths."""
    import inspect

    src = inspect.getsource(mm.from_afc)
    assert "slot = index\n" not in src


def test_the_legacy_fallback_reads_no_private_attribute():
    """A shared module has no business reading BambuAdapter's privates."""
    import inspect

    src = inspect.getsource(mm._legacy_ams_status)
    assert 'getattr(adapter, "_printer_model"' not in src
    assert "printer_model=None" in src


class TestUnreadMaterialIsNotADisagreement:
    """An absence is not a mismatch — on the tray side as on the file side.

    The file's side has always worked this way (``f.material is None`` means
    "cannot judge").  The tray's side did not, so the first fix for the
    spool-name defect only moved it: the invented brand became ``UNKNOWN``
    and the matcher went on reporting "colour agrees, material differs" —
    a disagreement built out of an absence.
    """

    def test_an_unread_tray_material_never_reports_a_mismatch(self):
        from kiln.ams_routing import UNREAD_MATERIAL, Filament, Tray, plan_ams_mapping

        trays = [Tray(slot=0, material=UNREAD_MATERIAL, hex6="FF0000")]
        plan = plan_ams_mapping([Filament(hex6="FF0000", material="PLA")], trays)
        assert plan.ok
        assert not any("material differs" in w for w in plan.warnings)

    def test_a_real_material_disagreement_is_still_reported(self):
        from kiln.ams_routing import Filament, Tray, plan_ams_mapping

        trays = [Tray(slot=0, material="PETG", hex6="FF0000")]
        plan = plan_ams_mapping([Filament(hex6="FF0000", material="PLA")], trays)
        assert any("material differs" in w for w in plan.warnings), (
            "loosening the unread case must not blind the real one"
        )

    def test_an_unread_material_reads_as_filament_not_as_a_material_name(self):
        from kiln.ams_routing import UNREAD_MATERIAL, Tray

        assert Tray(slot=0, material=UNREAD_MATERIAL, hex6="FF0000").label == (
            "red filament in slot 1"
        )

    def test_a_gate_with_nothing_read_matches_nothing(self):
        """The hole the FIRST version of this fix opened, caught in review.

        Treating an unread tray material as "cannot judge" is right, but it
        must not turn a slot Kiln knows nothing about — neither colour nor
        material — into a slot that matches everything.  The first cut did
        exactly that, and reported it as "matched on material only": a
        claim about a material that was never read.
        """
        from kiln.ams_routing import UNREAD_MATERIAL, Filament, Tray, plan_ams_mapping

        trays = [Tray(slot=0, material=UNREAD_MATERIAL, hex6=None)]
        plan = plan_ams_mapping([Filament(hex6="FF0000", material="PLA")], trays)
        assert not plan.ok
        assert not any("matched on material" in w for w in plan.warnings)

    def test_an_unread_colour_still_matches_on_a_material_that_was_read(self):
        """The legitimate half of that branch must survive the fix."""
        from kiln.ams_routing import Filament, Tray, plan_ams_mapping

        trays = [Tray(slot=0, material="PLA", hex6=None)]
        plan = plan_ams_mapping([Filament(hex6="FF0000", material="PLA")], trays)
        assert plan.ok
        assert any("matched on material only" in w for w in plan.warnings)
