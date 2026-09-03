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
``mainsail-crew/virtual-klipper-printer`` simulator (2026-09-03), after
``MMU_GATE_MAP`` / ``MMU_TTG_MAP`` seeded gates 0-2 — real Klipper, real
Moonraker, no motion.  ``TestLiveSimulator`` re-reads that rig through the
real adapter when it is up on ``localhost:7125``.
"""

from __future__ import annotations

import json
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
        assert d["loaded_slots"][0] == {"slot": 0, "material": "PLA", "color": "FF0000"}
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
    @pytest.mark.parametrize("printer_id,changer", [
        ("voron_2", "ercf"), ("voron_0", "ercf"), ("prusa_mk3s", "mmu2s"),
        ("prusa_mk4", "mmu3"), ("prusa_xl", "tool_changer"),
        ("visionminer_22idex_v4", "idex"),
    ])
    def test_undriven_changers_are_hardware_unverified(self, printer_id, changer):
        """Pre-fix: False for every one of these — only the seven CFS rows said True."""
        from kiln.pre_estimate import _get_printer_tool_change

        tc = _get_printer_tool_change(printer_id)
        assert tc["tool_changer"] == changer
        assert tc["hardware_unverified"] is True

    @pytest.mark.parametrize("printer_id", ["bambu_a1", "bambu_x1c", "bambu_p1s"])
    def test_bambu_ams_stays_verified(self, printer_id):
        from kiln.pre_estimate import _get_printer_tool_change

        assert _get_printer_tool_change(printer_id)["hardware_unverified"] is False

    def test_no_changer_is_not_unverified(self):
        from kiln.pre_estimate import _get_printer_tool_change

        assert _get_printer_tool_change("ender3")["hardware_unverified"] is False

    def test_addons_kiln_does_not_drive_are_unverified(self):
        from kiln.pre_estimate import _get_printer_tool_change, list_addons

        assert _get_printer_tool_change("ender3", tool_changer_addon="mosaic_palette3")["hardware_unverified"] is True
        listed = {a["id"]: a for a in list_addons()}
        assert all(a["hardware_unverified"] is True for a in listed.values())

    def test_the_caution_names_the_users_changer_not_cfs(self):
        """Pre-fix: silence for the Voron; and the only text that existed said 'CFS'."""
        from kiln.pre_estimate import estimate_from_dimensions

        est = estimate_from_dimensions(100, 100, 15, materials=["PLA", "PLA"], printer_id="voron_2")
        cautions = [w for w in est.warnings if "unverified" in w]
        assert len(cautions) == 1
        assert "ERCF" in cautions[0]
        assert "CFS" not in cautions[0]

    def test_prusa_mmu_gets_its_own_name(self):
        from kiln.pre_estimate import estimate_from_dimensions

        est = estimate_from_dimensions(100, 100, 15, materials=["PLA", "PLA"], printer_id="prusa_mk4")
        assert any("Prusa MMU3" in w and "unverified" in w for w in est.warnings)

    def test_cfs_says_it_once(self):
        """Pre-fix: the row warning AND the generic CFS line — twice, two wordings."""
        from kiln.pre_estimate import estimate_from_dimensions

        est = estimate_from_dimensions(100, 100, 15, materials=["PLA", "PLA"], printer_id="k2")
        assert sum("unverified" in w.lower() for w in est.warnings) == 1

    def test_bambu_gets_no_caution(self):
        from kiln.pre_estimate import estimate_from_dimensions

        est = estimate_from_dimensions(100, 100, 15, materials=["PLA", "PLA"], printer_id="bambu_a1")
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
