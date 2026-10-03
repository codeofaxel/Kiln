"""A slice declared for a material is set for it: temperatures, melt rate, cooling.

Found 2026-10-01 in a live session on a Bambu Lab A1: ``slice_model(material=
"TPU")`` and ``slice_model(material="PLA")`` produced the same file but for two
lines -- ``filament_type`` and the density.  220 °C, a 65 °C bed and 150 mm/s
outer walls went out under a TPU label, while Kiln's own data said 225 °C, a
40-60 °C bed and "print slowly".  It got past every test because:

* the material's settings were tested as a DICTIONARY (``perimeter_speed`` in
  ``build_material_overrides`` was at most 25), never as the G-code a slice of
  it makes -- and that dictionary slowed the inner walls and left the outer
  wall at 150 mm/s;
* the doors' tests mocked the slice, so no test ever sliced with a material;
* a helper that would have applied a product's temperatures
  (``brand_overrides_for_slicer``) had tests and no caller, so it read as wired.

So these tests ask the outcome question: what the profile handed to the
slicer says, what the response says, and -- where a slicer is installed --
what the G-code does.  The resolver lives in :mod:`kiln.slicer_material`.
"""

from __future__ import annotations

import math
import re
import struct
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiln.design_intelligence import get_brand_filament_profile, get_material_profile
from kiln.slicer import MaterialNotPrintableError, SliceResult, SlicerInfo, slice_file
from kiln.slicer_filament import ensure_profile_filament
from kiln.slicer_material import (
    APPLIED,
    PROFILE,
    PROFILE_MATERIAL,
    YOURS,
    MaterialRefused,
    material_needs,
    preview_material_values,
)
from kiln.slicer_orca import ini_to_settings, settings_to_orca_presets
from kiln.slicer_profiles import (
    get_slicer_profile,
    list_slicer_profiles,
    profile_origin,
    profile_with_overrides,
    resolve_slicer_profile,
    start_floor,
)


def _cube(path: Path, size: float = 20.0) -> str:
    v = [
        (0, 0, 0), (size, 0, 0), (size, size, 0), (0, size, 0),
        (0, 0, size), (size, 0, size), (size, size, size), (0, size, size),
    ]
    faces = [
        (0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
        (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ]
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(faces)))
        for a, b, c in faces:
            fh.write(struct.pack("<3f", 0, 0, 0))
            for i in (a, b, c):
                fh.write(struct.pack("<3f", *v[i]))
            fh.write(struct.pack("<H", 0))
    return str(path)


def _run_prusa(stl: str, seen: dict[str, Any], **kw: Any) -> SliceResult:
    """Drive slice_file with a fake PrusaSlicer that records the profile it was handed."""
    out_dir = Path(stl).parent / "out"
    out_dir.mkdir(exist_ok=True)

    def fake_run(cmd, *args, **kwargs):
        seen["cmd"] = list(cmd)
        Path(cmd[cmd.index("--output") + 1]).write_text("; gcode\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("kiln.slicer.find_slicer", return_value=SlicerInfo(path="/usr/bin/prusa-slicer", name="prusa-slicer")), \
            patch("subprocess.run", side_effect=fake_run):
        return slice_file(stl, output_dir=str(out_dir), **kw)


def _handed(seen: dict[str, Any]) -> dict[str, str]:
    cmd = seen["cmd"]
    return ini_to_settings(cmd[cmd.index("--load") + 1])


def _middle(span: list[int]) -> str:
    return str((span[0] + span[1]) // 2)


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


class TestTheResolver:
    def test_a_material_on_a_printer_takes_kilns_settings_for_that_printer(self):
        from kiln.printer_intelligence import get_material_settings

        tuned = get_material_settings("bambu_a1", "TPU")
        values = material_needs("TPU", printer_id="bambu_a1").values()
        assert values["temperature"] == values["first_layer_temperature"] == str(tuned.hotend)
        assert values["bed_temperature"] == values["first_layer_bed_temperature"] == str(tuned.bed)
        assert values["max_fan_speed"] == str(tuned.fan)

    def test_without_a_printer_the_middle_of_the_materials_range(self):
        thermal = get_material_profile("petg").thermal
        values = material_needs("PETG").values()
        assert values["temperature"] == _middle(thermal["print_temp_range_c"])
        assert values["bed_temperature"] == _middle(thermal["bed_temp_range_c"])

    def test_a_product_uses_its_own_figures(self):
        product = get_brand_filament_profile("prusament_tpu_95a")
        needs = material_needs("prusament_tpu_95a", printer_id="bambu_a1")
        assert needs.product is True
        assert needs.values()["temperature"] == str(product.nozzle_temp_optimal_c)
        assert needs.values()["bed_temperature"] == str(product.bed_temp_optimal_c)
        assert needs.label == f"{product.brand} {product.product_name}"

    @pytest.mark.parametrize(
        ("spelled", "catalog_id"),
        [
            ("PA-CF", "cf_nylon"),
            ("PA6-CF", "cf_nylon"),
            ("PC", "polycarbonate"),
            ("PC-ABS", "pc_abs"),
            ("PA6-GF", "pa6_gf"),
            ("PLA Silk", "silk_pla"),
            ("PETG HF", "petg_hf"),
        ],
    )
    def test_a_printers_spelling_reaches_the_catalog(self, spelled, catalog_id):
        """The spellings a printer's own unit uses.  "PA-CF" and "PC" used to
        name nothing, so a spool the printer reported had no settings."""
        assert material_needs(spelled).material_id == catalog_id

    def test_a_temperature_is_never_set_above_the_printers_rating(self, monkeypatch):
        import kiln.slicer_material as sm

        monkeypatch.setattr(sm, "_limits", lambda printer_id: {"hotend": 230.0, "bed": 60.0})
        needs = material_needs("PETG", printer_id="bambu_a1")
        assert needs.values()["temperature"] == "230"
        assert needs.values()["bed_temperature"] == "60"
        assert all(n.held for n in needs.needs if n.concept in ("nozzle", "bed"))

    def test_a_printer_that_cannot_melt_it_refuses(self):
        needs = material_needs("pei_1010", printer_id="bambu_a1")
        assert needs.refusal is not None
        assert needs.refusal["code"] == "MATERIAL_EXCEEDS_HOTEND"

    def test_an_unknown_word_has_no_settings(self):
        assert material_needs("unobtainium") is None
        assert material_needs("") is None
        assert material_needs(None) is None


# ---------------------------------------------------------------------------
# The chokepoint writes them
# ---------------------------------------------------------------------------


class TestTheSliceIsSetForTheMaterial:
    def test_a_tpu_slice_is_a_tpu_slice(self, tmp_path):
        """The incident, at the profile the slicer is handed."""
        seen: dict[str, Any] = {}
        result = _run_prusa(_cube(tmp_path / "c.stl"), seen, profile=resolve_slicer_profile("bambu_a1"), material="TPU")
        want = material_needs("TPU", printer_id="bambu_a1").values()
        ini = _handed(seen)
        for key, value in want.items():
            assert ini[key] == value, key
        assert ini["filament_type"] == "TPU"
        settings = result.to_dict()["filament"]["settings"]
        assert settings["outcome"] == APPLIED
        assert "TPU" in settings["note"]
        assert {c["setting"] for c in settings["changed"]} >= {"temperature", "bed_temperature"}

    def test_the_profiles_own_material_keeps_the_profiles_tuning(self, tmp_path):
        bundled = get_slicer_profile("bambu_a1").settings
        for material in ("PLA", None):
            seen: dict[str, Any] = {}
            result = _run_prusa(_cube(tmp_path / "c.stl"), seen, profile=resolve_slicer_profile("bambu_a1"), material=material)
            ini = _handed(seen)
            for key in ("temperature", "first_layer_temperature", "bed_temperature", "max_fan_speed"):
                assert ini[key] == bundled[key], (material, key)
            # The one thing the profile leaves to the slicer -- whose default
            # is no limit at all -- is the material's melt rate on this printer.
            assert "filament_max_volumetric_speed" not in bundled
            want = material_needs("PLA", printer_id="bambu_a1").values()["filament_max_volumetric_speed"]
            assert ini["filament_max_volumetric_speed"] == want
            report = result.to_dict()["filament"]["settings"]
            assert report["outcome"] == PROFILE
            assert "where it leaves the rest to the slicer, at most" in report["note"]

    def test_a_stated_setting_is_kept_and_named(self, tmp_path):
        seen: dict[str, Any] = {}
        profile = resolve_slicer_profile("bambu_a1", overrides={"temperature": "235"})
        result = _run_prusa(_cube(tmp_path / "c.stl"), seen, profile=profile, material="TPU")
        ini = _handed(seen)
        assert ini["temperature"] == "235"
        assert ini["bed_temperature"] == material_needs("TPU", printer_id="bambu_a1").values()["bed_temperature"]
        settings = result.to_dict()["filament"]["settings"]
        assert {k["setting"] for k in settings["kept"]} == {"temperature"}
        assert "temperature 235" in settings["note"]

    def test_a_callers_own_profile_is_compared_not_edited(self, tmp_path):
        own = tmp_path / "mine.ini"
        own.write_text("temperature = 220\nfirst_layer_temperature = 220\nbed_temperature = 65\nfirst_layer_bed_temperature = 65\n")
        seen: dict[str, Any] = {}
        result = _run_prusa(_cube(tmp_path / "c.stl"), seen, profile=str(own), material="TPU")
        ini = _handed(seen)
        assert ini["temperature"] == "220"
        assert ini["bed_temperature"] == "65"
        settings = result.to_dict()["filament"]["settings"]
        assert settings["outcome"] == YOURS
        assert any(d["setting"] == "bed" for d in settings["differs"])

    def test_the_warm_up_floor_heats_for_the_material(self, tmp_path):
        """A profile with no start routine gets Kiln's floor, which quotes the
        temperatures literally: written for PLA and left, it heats a PETG
        print to PLA's temperatures before the first layer asks for more."""
        seen: dict[str, Any] = {}
        _run_prusa(_cube(tmp_path / "c.stl"), seen, profile=resolve_slicer_profile("ender3"), material="PETG")
        ini = _handed(seen)
        assert ini["start_gcode"] == start_floor(ini)
        assert f"M109 S{ini['first_layer_temperature']}" in ini["start_gcode"]
        assert f"M190 S{ini['first_layer_bed_temperature']}" in ini["start_gcode"]

    def test_every_default_slice_suits_the_material_it_says_it_is(self, tmp_path):
        """A slice that names no material says it is PLA, so its temperatures
        are PLA's.  ``visionminer_22idex_v4`` printed a "PLA" slice at 240 °C
        and an 80 °C bed."""
        thermal = get_material_profile(PROFILE_MATERIAL).thermal
        lo, hi = thermal["print_temp_range_c"]
        bed_lo, bed_hi = thermal["bed_temp_range_c"]
        wrong: list[str] = []
        for pid in list_slicer_profiles():
            path, filament = ensure_profile_filament(resolve_slicer_profile(pid))
            ini = ini_to_settings(path)
            nozzle, bed = float(ini["temperature"]), float(ini["bed_temperature"])
            if not (lo <= nozzle <= hi and bed_lo <= bed <= bed_hi):
                wrong.append(f"{pid}: {nozzle:g}/{bed:g}")
        assert not wrong, wrong

    def test_a_bare_slice_gets_the_materials_temperatures(self, tmp_path):
        """No profile at all used to leave the slicer's own defaults: 200 °C
        and an unheated bed, whatever the material."""
        seen: dict[str, Any] = {}
        _run_prusa(_cube(tmp_path / "c.stl"), seen, material="PETG")
        thermal = get_material_profile("petg").thermal
        ini = _handed(seen)
        assert ini["temperature"] == _middle(thermal["print_temp_range_c"])
        assert ini["bed_temperature"] == _middle(thermal["bed_temp_range_c"])

    def test_a_material_the_printer_cannot_melt_is_refused_before_the_slicer_runs(self, tmp_path):
        seen: dict[str, Any] = {}
        with pytest.raises(MaterialNotPrintableError) as exc:
            _run_prusa(_cube(tmp_path / "c.stl"), seen, profile=resolve_slicer_profile("bambu_a1"), material="pei_1010")
        assert exc.value.code == "MATERIAL_EXCEEDS_HOTEND"
        assert "cmd" not in seen
        with pytest.raises(MaterialRefused):
            ensure_profile_filament(resolve_slicer_profile("bambu_a1"), material="pei_1010")

    def test_a_multi_extruder_profile_keeps_one_value_per_slot(self):
        """A scalar over a per-extruder vector would leave the other slots
        to the slicer's own default."""
        from kiln.slicer_profiles import resolve_multiextruder_profile

        path, _ = ensure_profile_filament(resolve_multiextruder_profile("bambu_a1", 4), material="TPU")
        ini = ini_to_settings(path)
        want = material_needs("TPU", printer_id="bambu_a1").values()["temperature"]
        assert ini["temperature"].replace(",", ";").split(";") == [want] * 4

    def test_the_next_material_replaces_the_last(self, tmp_path):
        """Kiln's own layers are not the caller's: a file set for TPU and
        sliced again for PETG prints at PETG's temperatures."""
        tpu, _ = ensure_profile_filament(resolve_slicer_profile("bambu_a1"), material="TPU")
        assert profile_origin(tpu).stated == frozenset()
        petg, _ = ensure_profile_filament(tpu, material="PETG")
        assert ini_to_settings(petg)["temperature"] == material_needs("PETG", printer_id="bambu_a1").values()["temperature"]


# ---------------------------------------------------------------------------
# Where a profile came from
# ---------------------------------------------------------------------------


class TestProfileOrigin:
    def test_a_bundled_profile_names_its_printer_and_states_nothing(self):
        origin = profile_origin(resolve_slicer_profile("bambu_a1"))
        assert (origin.kiln, origin.printer_id, origin.stated) == (True, "bambu_a1", frozenset())

    def test_overrides_are_recorded_as_stated(self):
        origin = profile_origin(resolve_slicer_profile("bambu_a1", overrides={"retract_length": "1.0"}))
        assert origin.stated == {"retract_length"}

    def test_an_override_equal_to_the_printers_value_is_still_stated(self):
        same = get_slicer_profile("bambu_a1").settings["temperature"]
        assert profile_origin(resolve_slicer_profile("bambu_a1", overrides={"temperature": same})).stated == {"temperature"}

    def test_a_derived_file_adds_a_callers_keys_but_not_kilns(self):
        base = resolve_slicer_profile("bambu_a1", overrides={"retract_length": "1.0"})
        theirs = profile_with_overrides(base, {"brim_width": "5"})
        ours = profile_with_overrides(base, {"filament_type": "PETG"}, stated=False)
        assert profile_origin(theirs).stated == {"retract_length", "brim_width"}
        assert profile_origin(ours).stated == {"retract_length"}
        assert profile_origin(theirs).printer_id == "bambu_a1"

    def test_a_file_kiln_did_not_write_is_the_callers(self, tmp_path):
        own = tmp_path / "mine.ini"
        own.write_text("temperature = 210\n")
        assert profile_origin(str(own)).kiln is False
        assert profile_origin(profile_with_overrides(str(own), {"brim_width": "5"})).kiln is False


# ---------------------------------------------------------------------------
# Every door gives the same answer
# ---------------------------------------------------------------------------


def _no_auth(_scope: str) -> None:
    return None


def _slicer_tools() -> dict[str, Any]:
    from kiln.plugins.slicer_tools import _SlicerToolsPlugin

    tools: dict[str, Any] = {}

    class _FakeMcp:
        def tool(self, name: str | None = None, **_kwargs):
            def decorator(fn):
                tools[name or fn.__name__] = fn
                return fn

            return decorator

    _SlicerToolsPlugin().register(_FakeMcp())
    return tools


class TestEveryDoorAgrees:
    @pytest.mark.parametrize(
        ("material", "printer_id"),
        [("tpu", "bambu_a1"), ("petg", "bambu_a1"), ("pla", "bambu_a1"), ("abs", "bambu_x1c"), ("petg", "ender3")],
    )
    def test_build_material_overrides_is_what_the_slice_writes(self, material, printer_id):
        from kiln.server import build_material_overrides

        with patch("kiln.server._check_auth", side_effect=_no_auth):
            shown = build_material_overrides(material, printer_id)
        assert shown["success"] is True, shown
        path, _ = ensure_profile_filament(resolve_slicer_profile(printer_id), material=material)
        ini = ini_to_settings(path)
        assert shown["overrides"] == {k: ini[k] for k in shown["overrides"]}

    def test_build_material_overrides_states_no_speed_of_its_own(self):
        """The hand-kept table that slowed TPU's inner walls and left its
        outer wall at 150 mm/s is gone: a material slows a print through its
        melt rate, every feature at once."""
        from kiln.server import build_material_overrides

        with patch("kiln.server._check_auth", side_effect=_no_auth):
            shown = build_material_overrides("tpu", "bambu_a1")
        assert not any(key.endswith("_speed") and "fan" not in key and "volumetric" not in key for key in shown["overrides"])

    def test_slice_model_reports_what_the_slice_was_set_to(self, tmp_path):
        stl = _cube(tmp_path / "c.stl")
        seen: dict[str, Any] = {}

        def fake_run(cmd, *args, **kwargs):
            if "--output" not in cmd:  # a preview render, not the slicer
                return SimpleNamespace(returncode=1, stdout="", stderr="not the slicer")
            seen["cmd"] = list(cmd)
            Path(cmd[cmd.index("--output") + 1]).write_text("; gcode\n")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        tools = _slicer_tools()
        with patch("kiln.server._check_auth", side_effect=_no_auth), \
                patch("kiln.slicer.find_slicer", return_value=SlicerInfo(path="/usr/bin/prusa-slicer", name="prusa-slicer")), \
                patch("subprocess.run", side_effect=fake_run):
            out = tools["slice_model"](input_path=stl, printer_id="ender3", material="TPU", output_dir=str(tmp_path))
        assert out["success"] is True, out
        assert out["filament"]["settings"]["outcome"] == APPLIED
        assert _handed(seen)["temperature"] == material_needs("TPU", printer_id="ender3").values()["temperature"]

    def test_reprint_tells_the_slice_the_material(self):
        """It used to pass the settings as overrides and no material, so the
        file was labelled and weighed as whatever spool was loaded."""
        from kiln.server import reprint_with_material

        with patch("kiln.server._check_auth", side_effect=_no_auth), \
                patch("kiln.server.run_reslice_and_print", return_value={"success": True}) as reslice:
            out = reprint_with_material("/tmp/part.stl", "tpu", printer_id="bambu_a1")
        assert out["success"] is True
        assert reslice.call_args.kwargs["material"] == "tpu"
        assert reslice.call_args.kwargs["overrides"] is None

    def test_the_start_macro_is_handed_the_materials_temperatures(self):
        """A printer's own start macro takes its heat-up temperatures as
        arguments, decided before the slice: it must be handed the
        temperatures the slice prints at, not the printer profile's."""
        from kiln.slicer_profiles import start_gcode_override_from_printer

        bridge = MagicMock()
        bridge.pro_features.start_gcode_override.return_value = (None, "no-macro")
        with patch.dict("sys.modules", {"kiln_pro.bridge": bridge}):
            start_gcode_override_from_printer(object(), "voron_2", None, material="TPU")
        handed = bridge.pro_features.start_gcode_override.call_args.args[2]
        assert handed["first_layer_temperature"] == preview_material_values("voron_2", "TPU")["first_layer_temperature"]
        assert handed["first_layer_temperature"] == material_needs("TPU", printer_id="voron_2").values()["temperature"]

    def test_the_cli_no_longer_writes_a_temperature_table_of_its_own(self):
        """``kiln slice --material`` wrote a seven-material table into the
        profile as stated keys, so the slice kept it over Kiln's settings."""
        from kiln.cli.main import _resolve_slice_plan

        ctx = SimpleNamespace(obj={})
        plan = _resolve_slice_plan(
            ctx, input_file="part.stl", profile=None, printer_id="bambu_a1", material="PETG", support_mode="off",
        )
        stated = profile_origin(plan["profile_path"]).stated
        assert not stated & {"temperature", "first_layer_temperature", "bed_temperature", "first_layer_bed_temperature"}
        assert plan["declared_material"] == "PETG"


# ---------------------------------------------------------------------------
# Orca is handed the same file
# ---------------------------------------------------------------------------


class TestOrcaGetsTheSameSettings:
    def test_the_filament_preset_carries_the_materials_settings(self):
        path, _ = ensure_profile_filament(resolve_slicer_profile("bambu_a1"), material="TPU")
        ini = ini_to_settings(path)
        filament = settings_to_orca_presets(ini, name="t").filament
        assert filament["nozzle_temperature"] == [ini["temperature"]]
        assert filament["textured_plate_temp"] == [ini["bed_temperature"]]
        assert filament["fan_max_speed"] == [ini["max_fan_speed"]]
        limits = [float(v) for k in ("filament_max_volumetric_speed", "max_volumetric_speed") if (v := ini.get(k))]
        assert float(filament["filament_max_volumetric_speed"][0]) == pytest.approx(min(limits))


# ---------------------------------------------------------------------------
# The G-code itself (where a slicer is installed)
# ---------------------------------------------------------------------------

_G1 = re.compile(r"^G1\b([^;]*)")
_WORD = re.compile(r"([XYEF])(-?\d*\.?\d+)")


def _peak_flow(gcode: str, filament_diameter: float = 1.75) -> float:
    """The most plastic any extruding move asks for, in mm³/s, from its own E."""
    area = math.pi * filament_diameter**2 / 4
    x = y = e = feed = 0.0
    relative_e = False
    peak = 0.0
    for line in gcode.splitlines():
        if line.startswith("M83"):
            relative_e = True
        elif line.startswith("M82"):
            relative_e = False
        m = _G1.match(line)
        if not m:
            continue
        words = dict(_WORD.findall(m.group(1)))
        nx, ny = float(words.get("X", x)), float(words.get("Y", y))
        if "F" in words:
            feed = float(words["F"]) / 60.0
        delta_e = 0.0
        if "E" in words:
            ne = float(words["E"])
            delta_e = ne if relative_e else ne - e
            e = e + ne if relative_e else ne
        dist = ((nx - x) ** 2 + (ny - y) ** 2) ** 0.5
        x, y = nx, ny
        if delta_e > 0 and dist > 0.5 and feed > 0:
            peak = max(peak, delta_e * area * feed / dist)
    return peak


def _have_prusaslicer() -> bool:
    try:
        from kiln.slicer import find_slicer, slicer_cli_family

        return slicer_cli_family(find_slicer()) == "prusa"
    except Exception:
        return False


@pytest.mark.skipif(not _have_prusaslicer(), reason="PrusaSlicer not installed")
class TestTheGcode:
    def test_a_tpu_slice_prints_at_tpus_temperature_and_melt_rate(self, tmp_path):
        """Before: 220 °C, and outer walls asking for 12 mm³/s of a filament
        rated well under a third of that."""
        needs = material_needs("TPU", printer_id="bambu_a1")
        assert needs.flow_ceiling, "TPU has no melt rate in Kiln's material table"
        result = slice_file(
            _cube(tmp_path / "c.stl", 30.0),
            profile=resolve_slicer_profile("bambu_a1"),
            material="TPU",
            output_dir=str(tmp_path),
        )
        gcode = Path(result.output_path).read_text(errors="replace")
        assert re.search(rf"^M109 S{needs.values()['first_layer_temperature']}\b", gcode, re.M)
        # 2% for E's own rounding on the shortest moves counted.
        assert _peak_flow(gcode) <= needs.flow_ceiling * 1.02
