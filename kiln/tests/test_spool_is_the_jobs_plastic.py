"""A loaded spool prints a job only when it is the job's plastic.

Every door that picks a spool asks one question
(:func:`kiln.ams_routing.same_plastic`) and the print gate
(``_resolve_use_ams``) decides the tray: the same plastic under any
spelling, never a family and never part of a word.  With no spool of the
plastic loaded the print is not started; the person's own ``ams_mapping``
or ``ams_slots`` still stands, and a printer with no unit still prints.
"""

from __future__ import annotations

import json
import struct
from typing import Any
from unittest import mock

import pytest

from kiln import declared_color as dc
from kiln.ams_routing import Filament, Tray, plan_ams_mapping, same_plastic

WHITE, GREEN, RED, BLUE = "FFFFFFFF", "00A000FF", "FF0000FF", "0000FFFF"


def _ams(*trays: tuple[str, str]) -> dict[str, Any]:
    """An AMS reading with one unit holding *trays* as ``(type, colour)``."""
    return {
        "tray_now": "255", "ams_exist_bits": "1", "tray_exist_bits": "f",
        "units": [{"unit_id": 0, "trays": [
            {"slot": i, "tray_type": t, "tray_color": c, "remain": 50, "remaining_known": True}
            for i, (t, c) in enumerate(trays)
        ]}],
    }


def _printer(reading: dict[str, Any]) -> mock.MagicMock:
    adapter = mock.MagicMock()
    adapter.get_ams_status.return_value = reading
    return adapter


class _SingleFeed:
    """A printer with no multi-material unit Kiln reads."""

    name = "plain"


def _stl(path: str) -> str:
    tris = [((0, 0, 0), (10, 0, 0), (10, 10, 0)), ((0, 0, 0), (10, 10, 0), (0, 10, 0)),
            ((0, 0, 5), (10, 10, 5), (10, 0, 5)), ((0, 0, 5), (0, 10, 5), (10, 10, 5))]
    with open(path, "wb") as fh:
        fh.write(b"\x00" * 80 + struct.pack("<I", len(tris)))
        for tri in tris:
            fh.write(struct.pack("<3f", 0, 0, 1))
            for v in tri:
                fh.write(struct.pack("<3f", *v))
            fh.write(struct.pack("<H", 0))
    return path


@pytest.fixture(autouse=True)
def _gates(monkeypatch):
    monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
    monkeypatch.setattr("kiln.server._check_auth", lambda *a, **k: None)
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)


class _Doors:
    """The three doors, run against one fake printer; ``calls`` is what
    reached the slice-and-print step."""

    def __init__(self, reading: dict[str, Any] | None, *, adapter: Any = None) -> None:
        self.adapter = adapter if adapter is not None else _printer(reading or {})
        self.reading = reading
        self.calls: list[dict[str, Any]] = []

    def _run(self, fn_name: str, **kwargs: Any) -> dict[str, Any]:
        import kiln.server as srv

        tool_reply = {"success": True, **(self.reading or {})} if self.reading else {"success": False}
        with mock.patch.object(srv, "_resolve_adapter", return_value=self.adapter), \
                mock.patch.object(srv, "_get_adapter", return_value=self.adapter), \
                mock.patch.object(srv, "ams_status", return_value=tool_reply), \
                mock.patch.object(srv, "_refuse_undriven_multi_material", return_value=None), \
                mock.patch.object(srv, "run_reslice_and_print",
                                  side_effect=lambda **kw: self.calls.append(kw) or {"success": True}):
            return getattr(srv, fn_name)(**kwargs)

    def reprint(self, model: str, material: str, **kw: Any) -> dict[str, Any]:
        return self._run("smart_reprint", file_name=model, material_id=material, **kw)

    def materials(self, objects: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
        return self._run("multi_material_print", objects_json=json.dumps(objects), **kw)

    def copies(self, model: str, **kw: Any) -> dict[str, Any]:
        return self._run("multi_color_copies", model_path=model, **kw)


def _resolve(reading: dict[str, Any], mapping: list[int] | None = None, **kwargs: Any) -> dict[str, Any]:
    from kiln.server import _resolve_use_ams

    return _resolve_use_ams("auto", mapping, _printer(reading), **kwargs)


class TestSamePlastic:
    @pytest.mark.parametrize(("wanted", "loaded"), [
        ("nylon", "PA"), ("polycarbonate", "PC"), ("cf_pla", "PLA-CF"), ("PLA-CF", "pla_cf"),
        ("cf_nylon", "PA-CF"), ("pa6_gf", "PA-GF"), ("pla_plus", "PLA"), ("silk_pla", "PLA"),
        ("petg_hf", "PETG"), ("tpu_95a", "TPU"), ("Carbon Fiber PLA", "PLA-CF"),
    ])
    def test_one_plastic_under_two_spellings(self, wanted, loaded):
        assert same_plastic(wanted, loaded) is True

    @pytest.mark.parametrize(("wanted", "loaded"), [
        ("PLA", "PLA-CF"), ("cf_pla", "PLA"), ("PC", "PLA-CF"), ("PC", "PCTG"), ("PA", "PPA-CF"),
        ("nylon", "PLA"), ("cf_nylon", "PA"), ("PETG", "PETG-CF"), ("ABS", "ABS-GF"), ("ASA", "ASA-AERO"),
        ("pla_plus", "PLA-CF"), ("pa6_gf", "PA"),
    ])
    def test_a_different_plastic_is_never_a_family_or_part_of_a_word(self, wanted, loaded):
        assert same_plastic(wanted, loaded) is False

    @pytest.mark.parametrize(("wanted", "loaded"), [
        ("PLA", ""), ("", "PLA"), (None, "PLA"), ("PLA", "UNKNOWN"), ("sparkle", "PLA"), ("pc_abs", "PC"),
    ])
    def test_nothing_to_judge_is_said_as_such(self, wanted, loaded):
        assert same_plastic(wanted, loaded) is None

    def test_a_spool_that_is_a_job_s_plastic_is_abrasive_only_when_the_job_is(self):
        from kiln.design_intelligence import list_public_material_profiles
        from kiln.material_safety import is_abrasive_filament
        from kiln.printers.bambu_3mf import BAMBU_FILAMENT_TYPES

        jobs = [*(p.material_id for p in list_public_material_profiles()), *BAMBU_FILAMENT_TYPES]
        assert len(jobs) > 40
        wrong = [
            (job, spool) for job in jobs for spool in BAMBU_FILAMENT_TYPES
            if same_plastic(job, spool) and is_abrasive_filament(spool) and not is_abrasive_filament(job)
        ]
        assert wrong == []


class TestTheRouter:
    def test_a_spool_of_another_plastic_is_not_a_match_even_in_the_right_colour(self):
        plan = plan_ams_mapping([Filament("0000FF", "PC")], [Tray(0, "PLA-CF", "0000FF"), Tray(1, "PLA", "FFFFFF")])
        assert not plan.ok
        assert plan.warnings == ["no loaded spool is PC"]

    def test_the_spool_s_spelling_of_the_plastic_matches(self):
        plan = plan_ams_mapping([Filament("0000FF", "POLYCARBONATE")], [Tray(0, "PLA", "0000FF"), Tray(1, "PC", "0000FF")])
        assert plan.mapping == [1] and plan.warnings == []


class TestThePrintGate:
    def test_a_nylon_job_takes_the_pa_spool(self):
        out = _resolve(_ams(("PLA", WHITE), ("PA", WHITE)), material="nylon")
        assert out["ams_mapping"] == [1] and out["warnings"] == []
        assert out["selection"]["type"] == "PA"

    def test_a_pc_job_with_no_pc_loaded_is_not_started(self):
        out = _resolve(_ams(("PLA-CF", WHITE), ("PLA", WHITE)), material="PC")
        assert out["blocked"] is True and out["ams_mapping"] is None
        assert out["code"] == "AMS_MATERIAL_NOT_LOADED"
        said = out["warnings"][0]
        assert said.startswith("This print needs PC, and none of your spools are PC (you have white PLA-CF and white PLA).")
        assert "Load a PC spool and I'll start it." in said

    def test_the_asked_colour_loaded_in_another_plastic_is_said_as_that(self, tmp_path):
        gcode = tmp_path / "part.gcode"
        gcode.write_text("G28\n; filament_colour = #FFFFFF\n; filament_type = PLA\n")
        dc.record(str(gcode), dc.ShownColour("teal", "#008080"))
        out = _resolve(_ams(("PETG", "008080FF"), ("PLA", WHITE)), material="PLA", file_path=str(gcode))
        assert out["blocked"] is True and out["code"] == "COLOR_NOT_LOADED"
        assert "re-slice it for the teal PETG in slot A1" in out["warnings"][0]

    def test_a_cf_pla_job_does_not_take_plain_pla(self):
        assert _resolve(_ams(("PLA", WHITE)), material="cf_pla")["blocked"] is True

    def test_a_pla_job_passes_over_the_carbon_fibre_spool(self):
        out = _resolve(_ams(("PLA-CF", WHITE), ("PLA", WHITE)), material="PLA")
        assert out["ams_mapping"] == [1]

    def test_the_fuller_spool_of_the_plastic_is_used(self):
        reading = _ams(("PETG", WHITE), ("PETG", GREEN))
        reading["units"][0]["trays"][1]["remain"] = 90
        assert _resolve(reading, material="petg")["ams_mapping"] == [1]

    def test_the_person_s_own_mapping_stands_and_is_said(self):
        out = _resolve(_ams(("PLA-CF", WHITE), ("PLA", WHITE)), [0], material="PC")
        assert "blocked" not in out and out["use_ams"] is True and out["ams_mapping"] is None
        assert out["selection"]["slot"] == 0
        assert any("ams_mapping names it" in w for w in out["warnings"])

    def test_no_material_named_keeps_the_first_loaded_tray(self):
        assert _resolve(_ams(("PETG", WHITE), ("PLA", WHITE)))["ams_mapping"] == [0]

    def test_a_printer_with_no_unit_prints_from_its_one_feed(self):
        out = _resolve({"ams_exist_bits": "0", "tray_exist_bits": "0", "units": []}, material="nylon")
        assert out == {"use_ams": False, "ams_mapping": None, "warnings": []}

    def test_a_unit_whose_trays_are_not_yet_reported_is_not_a_refusal(self):
        out = _resolve({"ams_exist_bits": "1", "tray_exist_bits": "f", "units": []}, material="nylon")
        assert "blocked" not in out and out["ambiguous"] is True


class TestSmartReprint:
    def test_a_pc_job_with_no_pc_loaded_is_refused_before_slicing(self, tmp_path):
        doors = _Doors(_ams(("PLA-CF", WHITE), ("PLA", WHITE)))
        result = doors.reprint(_stl(str(tmp_path / "part.stl")), "polycarbonate")
        assert result["success"] is False and doors.calls == []
        assert result["error"]["code"] == "AMS_MATERIAL_NOT_LOADED"
        assert "(you have white PLA-CF and white PLA)" in result["error"]["message"]

    def test_a_cf_pla_job_does_not_print_from_plain_pla(self, tmp_path):
        # An abrasive filament's own refusal comes first, at the slice; the
        # print step then holds it to the spool of its plastic.
        doors = _Doors(_ams(("PLA", WHITE)))
        doors.reprint(_stl(str(tmp_path / "part.stl")), "cf_pla")
        assert len(doors.calls) == 1
        assert doors.calls[0]["material"] == "cf_pla" and doors.calls[0]["ams_mapping"] is None

    def test_a_nylon_job_prints_from_the_pa_spool(self, tmp_path):
        doors = _Doors(_ams(("PLA", WHITE), ("PA", WHITE)))
        result = doors.reprint(_stl(str(tmp_path / "part.stl")), "nylon")
        assert doors.calls[0]["ams_mapping"] == "[1]" and doors.calls[0]["use_ams"] is True
        assert result["ams_slot_selected"]["slot"] == 1

    def test_a_colour_that_is_not_loaded_holds_the_print(self, tmp_path):
        model = _stl(str(tmp_path / "part.stl"))
        dc.record(model, dc.ShownColour("teal", "#008080"))
        doors = _Doors(_ams(("PLA", WHITE), ("PLA", GREEN)))
        result = doors.reprint(model, "pla")
        assert result["success"] is False and doors.calls == []
        assert result["error"]["code"] == "COLOR_NOT_LOADED"

    def test_a_printer_with_no_unit_still_prints(self, tmp_path):
        doors = _Doors(None, adapter=_SingleFeed())
        doors.reprint(_stl(str(tmp_path / "part.stl")), "nylon")
        assert len(doors.calls) == 1
        assert doors.calls[0]["use_ams"] is None and doors.calls[0]["ams_mapping"] is None


class TestMultiMaterialPrint:
    def test_a_pc_object_with_no_pc_loaded_is_refused(self, tmp_path):
        doors = _Doors(_ams(("PLA-CF", BLUE), ("PLA", WHITE)))
        result = doors.materials([{"file_path": _stl(str(tmp_path / "a.stl")), "material_id": "polycarbonate", "color": "#0000FF"}])
        assert result["success"] is False and doors.calls == []
        assert result["error"]["code"] == "AMS_MATERIAL_NOT_LOADED"

    def test_each_material_prints_from_its_own_spool(self, tmp_path):
        doors = _Doors(_ams(("PLA", WHITE), ("PA", WHITE), ("PA-CF", "111111FF")))
        result = doors.materials([
            {"file_path": _stl(str(tmp_path / "a.stl")), "material_id": "nylon", "color": "#FFFFFF"},
            {"file_path": _stl(str(tmp_path / "b.stl")), "material_id": "cf_nylon", "color": "#111111"},
        ])
        assert doors.calls[0]["ams_mapping"] == "[1, 2]"
        assert [m["name"] for m in result["ams_mapping"]] == ["A2", "A3"]
        assert "ams_warning" not in result

    def test_a_colour_that_is_not_loaded_is_refused(self, tmp_path):
        doors = _Doors(_ams(("PLA", WHITE), ("PLA", GREEN)))
        result = doors.materials([
            {"file_path": _stl(str(tmp_path / "a.stl")), "material_id": "pla", "color": "#FF0000"},
            {"file_path": _stl(str(tmp_path / "b.stl")), "material_id": "pla", "color": "#0000FF"},
        ])
        assert result["success"] is False and doors.calls == []
        assert result["error"]["code"] == "AMS_COLOR_MISMATCH"

    def test_a_reply_never_blames_the_printer_for_a_spool_that_is_not_loaded(self, tmp_path):
        doors = _Doors(None, adapter=_SingleFeed())
        result = doors.materials([
            {"file_path": _stl(str(tmp_path / "a.stl")), "material_id": "pla", "color": "#FF0000"},
            {"file_path": _stl(str(tmp_path / "b.stl")), "material_id": "pla", "color": "#0000FF"},
        ])
        assert len(doors.calls) == 1
        assert "requires a Bambu" not in json.dumps(result)


class TestMultiColorCopies:
    def test_a_pla_job_leaves_the_carbon_fibre_spool_out(self, tmp_path):
        doors = _Doors(_ams(("PLA-CF", "111111FF"), ("PLA", WHITE), ("PLA", GREEN)))
        doors.copies(_stl(str(tmp_path / "part.stl")))
        assert doors.calls[0]["ams_mapping"] == "[1, 2]"

    def test_pc_does_not_take_pctg_and_pa_does_not_take_ppa(self, tmp_path):
        model = _stl(str(tmp_path / "part.stl"))
        for material, other in (("PC", "PCTG"), ("PA", "PPA-CF")):
            doors = _Doors(_ams((other, WHITE), (other, GREEN)))
            result = doors.copies(model, material=material)
            assert result["error"]["code"] == "NO_MATERIAL" and doors.calls == []
            assert result["error"]["message"].startswith(
                f"This print needs {material}, and none of your spools are {material} (you have white and green {other})."
            )

    def test_nylon_copies_print_from_the_pa_spools(self, tmp_path):
        doors = _Doors(_ams(("PLA", WHITE), ("PA", WHITE), ("PA", GREEN)))
        doors.copies(_stl(str(tmp_path / "part.stl")), material="nylon")
        assert doors.calls[0]["ams_mapping"] == "[1, 2]"

    def test_named_colours_that_are_not_loaded_are_refused(self, tmp_path):
        doors = _Doors(_ams(("PLA", WHITE), ("PLA", GREEN)))
        result = doors.copies(_stl(str(tmp_path / "part.stl")), colors=["#FF0000", "#0000FF"])
        assert result["success"] is False and doors.calls == []
        assert result["error"]["code"] == "AMS_COLOR_MISMATCH"

    def test_named_colours_print_from_the_spools_of_those_colours(self, tmp_path):
        doors = _Doors(_ams(("PLA", WHITE), ("PLA", GREEN), ("PLA", RED)))
        result = doors.copies(_stl(str(tmp_path / "part.stl")), copies=2, colors=["#FF0000", "#FFFFFF"])
        assert doors.calls[0]["ams_mapping"] == "[2, 0]"
        assert [m["name"] for m in result["ams_mapping"]] == ["A3", "A1"]

    def test_the_person_s_own_trays_are_printed_as_named(self, tmp_path):
        doors = _Doors(_ams(("PLA-CF", "111111FF"), ("PLA", WHITE)))
        doors.copies(_stl(str(tmp_path / "part.stl")), ams_slots=[0, 1])
        assert doors.calls[0]["ams_mapping"] == "[0, 1]" and doors.calls[0]["use_ams"] is True


class TestTheTrackedSpool:
    def test_two_spellings_of_one_plastic_are_not_a_mismatch(self, tmp_path):
        from kiln.materials import LoadedMaterial, MaterialTracker

        tracker = MaterialTracker()
        loaded = LoadedMaterial(printer_name="p", material_type="PA")
        assert tracker._mismatch_warning("p", "nylon", loaded) is None
        assert tracker._mismatch_warning("p", "PLA", loaded) is not None
