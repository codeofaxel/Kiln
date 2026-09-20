"""Every slice Kiln runs carries a filament density, so the slicer weighs the print itself.

Measured 2026-09-19 on this machine, before this change: PrusaSlicer 2.9.4
sliced a 20 mm cube through the bundled ``bambu_a1`` profile and wrote
``; filament used [mm] = 1393.81``, ``; filament used [cm3] = 3.35``,
``; total filament used [g] = 0.00`` and ``; filament_density = 0`` — no
``; filament used [g]`` line at all, because Kiln's profiles describe a
printer and named no filament.  OrcaSlicer 2.3.2 did the same
(``filament_density = 0``).  Kiln repaired the weight after the fact from
the length; that repair stays as the safety net, and these tests pin that
a Kiln-sliced file no longer needs it.

How each slicer takes the density, read off the binaries here:

* PrusaSlicer 2.9.4 ``--help-fff``: ``--filament-density N`` "(g/cm³,
  default: 0)", ``--filament-diameter N`` "(mm, default: 1.75)",
  ``--filament-type`` "(default: PLA)".  The ``--load`` INI keys are the
  same words with underscores, and the slicer echoes them back into the
  G-code footer (``; filament_density = 1.27``).
* OrcaSlicer 2.3.2: no such flag on ``--help``; the FILAMENT preset
  carries ``"filament_density": ["1.24"]``, one entry per extruder, as
  its own ``profiles/BBL/filament/fdm_filament_pla.json`` does.
"""

from __future__ import annotations

import json
import math
import os
import re
import struct
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock
from unittest.mock import MagicMock, patch

import pytest

from kiln.slicer import SlicerError, SliceResult, SlicerInfo, slice_file
from kiln.slicer_orca import ini_to_settings, settings_to_orca_presets
from kiln.slicer_profiles import resolve_slicer_profile

_AREA_MM2 = math.pi * (1.75 / 2) ** 2


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


def _footer_value(body: str, key: str) -> float | None:
    m = re.search(rf"^; {re.escape(key)}\s*=\s*([\d.]+)", body, re.M)
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# The one resolver
# ---------------------------------------------------------------------------


class TestTheResolver:
    def test_the_declared_material_wins_and_says_so(self):
        from kiln.slicer_filament import resolve_slice_filament

        f = resolve_slice_filament("petg", loaded_type="PLA", settings={"filament_type": "ABS"})
        assert (f.material, f.density_g_per_cm3, f.source) == ("PETG", 1.27, "declared")

    def test_the_loaded_spool_answers_when_nothing_was_declared(self):
        from kiln.slicer_filament import resolve_slice_filament

        f = resolve_slice_filament(None, loaded_type="ABS", settings={"filament_type": "PLA"})
        assert (f.material, f.density_g_per_cm3, f.source) == ("ABS", 1.04, "loaded")
        assert f.determined_by == "observed"
        assert "the printer reports" in f.note

    def test_a_spool_a_person_recorded_is_not_credited_to_the_printer(self):
        """"Kiln was told PETG is loaded" and "the printer reports PETG" are
        different facts; the note says which, the density is the same."""
        from kiln.slicer_filament import resolve_slice_filament

        told = resolve_slice_filament(None, loaded_type="PETG", loaded_determined_by="user_reported")
        seen = resolve_slice_filament(None, loaded_type="PETG", loaded_determined_by="observed")
        assert told.density_g_per_cm3 == seen.density_g_per_cm3 == 1.27
        assert told.source == seen.source == "loaded"
        assert "as you told it" in told.note and "printer reports" not in told.note
        assert "printer reports" in seen.note
        assert told.to_dict()["determined_by"] == "user_reported"
        assert "determined_by" not in resolve_slice_filament("PETG").to_dict()

    def test_the_profiles_own_type_answers_next(self):
        from kiln.slicer_filament import resolve_slice_filament

        f = resolve_slice_filament(None, settings={"filament_type": "ASA"})
        assert (f.material, f.density_g_per_cm3, f.source) == ("ASA", 1.07, "profile")

    def test_a_profiles_stated_density_answers_when_nothing_was_declared_or_loaded(self):
        from kiln.slicer_filament import resolve_slice_filament

        f = resolve_slice_filament(None, settings={"filament_density": "1.32", "filament_type": "PLA-SILK"})
        assert (f.density_g_per_cm3, f.source, f.material) == (1.32, "profile", "PLA-SILK")
        # Density with no type: the material is unknown, and is not called PLA.
        assert resolve_slice_filament(None, settings={"filament_density": "1.32"}).material == "unspecified"

    def test_the_declared_material_outranks_a_profiles_stated_density(self):
        """A user's exported config carries the density of whatever filament
        preset was selected when they exported it; what they declare for
        THIS slice is the stronger statement, as the ladder promises."""
        from kiln.slicer_filament import resolve_slice_filament

        f = resolve_slice_filament("PETG", settings={"filament_density": "1.04", "filament_type": "ABS"})
        assert (f.material, f.density_g_per_cm3, f.source) == ("PETG", 1.27, "declared")

    def test_the_loaded_spool_outranks_a_profiles_stated_density(self):
        from kiln.slicer_filament import resolve_slice_filament

        f = resolve_slice_filament(None, loaded_type="TPU", settings={"filament_density": "1.04", "filament_type": "ABS"})
        assert (f.material, f.density_g_per_cm3, f.source) == ("TPU", 1.21, "loaded")

    def test_a_non_string_material_does_not_raise(self):
        from kiln.slicer_filament import resolve_slice_filament

        assert resolve_slice_filament(123).source == "default"
        assert resolve_slice_filament(None, loaded_type=4.5).source == "default"

    def test_a_callers_word_is_one_bounded_ini_value(self):
        from kiln.slicer_filament import safe_filament_type

        assert safe_filament_type("PLA-CF") == "PLA-CF"
        assert safe_filament_type("PETG HF") == "PETG HF"
        assert safe_filament_type("x" * 80) == "x" * 32
        assert safe_filament_type("PETG\nlayer_height = 9") == "PETG layer_height 9"
        assert safe_filament_type("") == "unspecified"
        assert safe_filament_type(None) == "unspecified"

    def test_pla_is_the_last_rung_and_is_named_as_the_default(self):
        from kiln.slicer_filament import resolve_slice_filament

        f = resolve_slice_filament(None)
        assert (f.material, f.density_g_per_cm3, f.diameter_mm, f.source) == ("PLA", 1.24, 1.75, "default")

    def test_a_material_the_table_does_not_know_falls_through_but_keeps_its_name(self):
        from kiln.slicer_filament import resolve_slice_filament

        f = resolve_slice_filament("WOODFILL")
        assert f.source == "default"
        assert f.density_g_per_cm3 == 1.24
        assert "WOODFILL" in f.note

    @pytest.mark.parametrize(
        "spelling, key, density",
        [
            ("PLA-CF", "CF-PLA", 1.30),   # Bambu's spelling of the table's CF-PLA
            ("PETG-HF", "PETG", 1.27),   # a variant of a family the table has
            ("PA-CF", "NYLON", 1.14),    # PA is nylon
            ("pla+", "PLA+", 1.24),
            ("TPU 95A", "TPU", 1.21),
        ],
    )
    def test_vendor_spellings_land_on_the_tables_row(self, spelling, key, density):
        from kiln.slicer_filament import material_density

        assert material_density(spelling) == (key, density)

    def test_the_profiles_diameter_is_kept_and_the_tables_is_the_fallback(self):
        from kiln.slicer_filament import resolve_slice_filament

        assert resolve_slice_filament(None, settings={"filament_diameter": "2.85"}).diameter_mm == 2.85
        assert resolve_slice_filament(None, settings={"filament_diameter": "1.75;1.75"}).diameter_mm == 1.75
        assert resolve_slice_filament(None).diameter_mm == 1.75

    def test_the_answer_is_a_plain_dict_for_a_tool_response(self):
        from kiln.slicer_filament import resolve_slice_filament

        d = resolve_slice_filament("ABS").to_dict()
        assert d["material"] == "ABS"
        assert d["density_g_per_cm3"] == 1.04
        assert d["diameter_mm"] == 1.75
        assert d["source"] == "declared"
        assert isinstance(d["note"], str) and d["note"]

    def test_the_safety_net_reads_the_same_table_through_the_same_lookup(self):
        """One lookup: the after-the-fact fill and the slice-time resolver
        cannot disagree about what PLA-CF weighs."""
        from kiln.printers.bambu_3mf import _material_density
        from kiln.slicer_filament import material_density

        for spelling in ("PLA-CF", "PA-CF", "PETG", "nonsense"):
            expected = material_density(spelling)
            assert _material_density(spelling) == (expected[1] if expected else 1.24)


class TestTheLoadedSpool:
    def _adapter(self, tray_now: str, trays: list[tuple[int, str]], unit2: list[tuple[int, str]] | None = None, **fields):
        """get_ams_status as kiln.printers.bambu shapes it: per-unit slot ids,
        and tray_now the GLOBAL id (unit * 4 + slot)."""
        units = [{"unit_id": 0, "trays": [
            {"slot": s, "tray_type": t, "tray_color": "FFFFFFFF", "remain": 50} for s, t in trays
        ]}]
        if unit2 is not None:
            units.append({"unit_id": 1, "trays": [
                {"slot": s, "tray_type": t, "tray_color": "FFFFFFFF", "remain": 50} for s, t in unit2
            ]})
        return SimpleNamespace(get_ams_status=lambda: {"tray_now": tray_now, "units": units, **fields})

    def test_the_active_tray_is_the_answer(self):
        from kiln.slicer_filament import loaded_filament_type

        assert loaded_filament_type(self._adapter("2", [(0, "PLA"), (2, "PETG")])) == "PETG"

    def test_a_second_units_tray_is_found_by_its_global_id(self):
        """tray_now is unit * 4 + slot: unit 1's first tray is 4, not 0."""
        from kiln.slicer_filament import loaded_filament_type

        two = dict(trays=[(0, "PLA"), (1, "PETG")], unit2=[(0, "ABS"), (1, "TPU")])
        assert loaded_filament_type(self._adapter("4", **two)) == "ABS"
        assert loaded_filament_type(self._adapter("5", **two)) == "TPU"
        assert loaded_filament_type(self._adapter("1", **two)) == "PETG"

    def test_the_external_spool_is_not_an_ams_tray(self):
        """254 is the external spool; the AMS knows nothing about it, so
        neither does the slice -- it must not credit an AMS tray."""
        from kiln.slicer_filament import loaded_filament_type

        assert loaded_filament_type(self._adapter("254", [(0, "PETG")])) is None

    def test_no_active_tray_answers_only_when_every_loaded_tray_agrees(self):
        """The A1 keeps tray_now at 255 with trays loaded.  One material
        across the loaded trays is the answer; two is a guess, and a guess
        is not something the printer reported."""
        from kiln.slicer_filament import loaded_filament_type

        assert loaded_filament_type(self._adapter("255", [(1, "PLA"), (3, "PLA")])) == "PLA"
        assert loaded_filament_type(self._adapter("255", [(0, "PETG")])) == "PETG"
        assert loaded_filament_type(self._adapter("255", [(1, "ABS"), (3, "PLA")])) is None

    def test_a_target_tray_is_not_a_feeding_tray(self):
        """tray_tar names the tray the machine is ABOUT to load; that is not
        a spool the printer reports feeding, and two materials stay a guess."""
        from kiln.slicer_filament import loaded_filament_type

        assert loaded_filament_type(self._adapter("255", [(1, "ABS"), (3, "PLA")], tray_tar="3")) is None

    def test_every_make_is_read_through_the_one_record(self):
        """A Klipper Happy Hare gate map and a Creality CFS answer through
        the same record a Bambu does -- and an unread material is honest."""
        from kiln.multi_material import from_creality_cfs, from_happy_hare
        from kiln.slicer_filament import loaded_filament_type

        def _klipper(mmu):
            return SimpleNamespace(name="moonraker", get_multi_material_status=lambda: from_happy_hare(mmu))

        # Every curated gate agrees: the MMU's spool answers.
        assert loaded_filament_type(_klipper({
            "gate_status": [1, 1, 0], "gate_material": ["PETG", "PETG", ""], "gate_color": ["ff0000", "00ff00", ""],
        })) == "PETG"
        # Two materials and no verified word on which gate feeds: no answer.
        assert loaded_filament_type(_klipper({
            "gate_status": [1, 1], "gate_material": ["PETG", "ABS"], "gate_color": ["", ""],
        })) is None
        # A loaded gate whose material was never curated is unread, not PLA.
        assert loaded_filament_type(_klipper({
            "gate_status": [1], "gate_material": [""], "gate_color": ["ff0000"],
        })) is None

        cfs = SimpleNamespace(name="creality", get_multi_material_status=lambda: from_creality_cfs({
            "detected": True, "slots": [{"slot": 0, "material": "PLA", "color": "ffffff"}], "warnings": [],
        }))
        assert loaded_filament_type(cfs) == "PLA"

    def test_a_unit_that_cannot_be_asked_is_not_an_empty_unit(self):
        """A failed probe is 'unknown', never 'nothing loaded' -- and a
        slice must not fail on it."""
        from kiln.printers.base import PrinterError
        from kiln.slicer_filament import loaded_filament_type

        def _boom():
            raise PrinterError("MQTT down")

        assert loaded_filament_type(SimpleNamespace(name="bambu", get_multi_material_status=_boom)) is None

    def test_the_record_itself_names_the_feeding_tray(self):
        from kiln.multi_material import from_bambu_ams

        info = self._adapter("5", [(0, "PLA")], unit2=[(1, "TPU")]).get_ams_status()
        status = from_bambu_ams(info, printer_model="bambu_x1c")
        assert status.feeding == (1, 1) and status.external_spool is False
        assert status.to_dict()["feeding"] == [1, 1]
        external = from_bambu_ams(self._adapter("254", [(0, "PLA")]).get_ams_status(), printer_model="bambu_a1")
        assert external.feeding is None and external.external_spool is True
        idle = from_bambu_ams(self._adapter("255", [(0, "PLA")]).get_ams_status(), printer_model="bambu_a1")
        assert idle.feeding is None and idle.external_spool is False

    def test_a_printer_without_a_unit_reports_nothing(self):
        from kiln.slicer_filament import loaded_filament_type

        assert loaded_filament_type(SimpleNamespace()) is None
        assert loaded_filament_type(None) is None

    def test_a_unit_that_cannot_be_read_reports_nothing_rather_than_raising(self):
        from kiln.slicer_filament import loaded_filament_type

        def _boom():
            raise RuntimeError("mqtt down")

        assert loaded_filament_type(SimpleNamespace(get_ams_status=_boom)) is None
        assert loaded_filament_type(self._adapter("255", [])) is None


# ---------------------------------------------------------------------------
# The chokepoint: what slice_file hands the slicer
# ---------------------------------------------------------------------------


def _run_prusa(stl: str, seen: dict[str, Any], **kw) -> SliceResult:
    """Drive slice_file with a fake PrusaSlicer that records its argv."""
    out_dir = Path(stl).parent / "out"
    out_dir.mkdir(exist_ok=True)

    def fake_run(cmd, *args, **kwargs):
        seen["cmd"] = list(cmd)
        Path(cmd[cmd.index("--output") + 1]).write_text("; gcode\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("kiln.slicer.find_slicer", return_value=SlicerInfo(path="/usr/bin/prusa-slicer", name="prusa-slicer")), \
            patch("subprocess.run", side_effect=fake_run):
        return slice_file(stl, output_dir=str(out_dir), **kw)


def _loaded_ini(cmd: list[str]) -> dict[str, str]:
    assert "--load" in cmd, f"no profile reached the slicer: {cmd}"
    return ini_to_settings(cmd[cmd.index("--load") + 1])


class TestSliceFileCarriesTheIdentity:
    def test_a_bundled_profile_gains_density_diameter_and_type(self, tmp_path):
        stl = _cube(tmp_path / "cube.stl")
        seen: dict[str, Any] = {}
        result = _run_prusa(stl, seen, profile=resolve_slicer_profile("bambu_a1"), material="PETG")
        ini = _loaded_ini(seen["cmd"])
        assert ini["filament_density"] == "1.27"
        assert ini["filament_diameter"] == "1.75"
        assert ini["filament_type"] == "PETG"
        # Everything the printer profile said is still said.
        assert ini["use_relative_e_distances"] == "1"
        assert ini["bed_shape"].startswith("0x0")
        assert result.filament is not None
        assert result.to_dict()["filament"]["source"] == "declared"
        assert result.to_dict()["filament"]["density_g_per_cm3"] == 1.27

    def test_a_bare_slice_with_no_profile_still_carries_pla(self, tmp_path):
        """``slice_file("model.stl")`` — the documented form — used to send
        no ``--load`` at all, and PrusaSlicer's own default density is 0."""
        stl = _cube(tmp_path / "cube.stl")
        seen: dict[str, Any] = {}
        result = _run_prusa(stl, seen)
        ini = _loaded_ini(seen["cmd"])
        assert (ini["filament_density"], ini["filament_diameter"], ini["filament_type"]) == ("1.24", "1.75", "PLA")
        assert result.to_dict()["filament"]["source"] == "default"

    def test_the_loaded_spool_is_the_source_when_nothing_was_declared(self, tmp_path):
        stl = _cube(tmp_path / "cube.stl")
        seen: dict[str, Any] = {}
        result = _run_prusa(stl, seen, profile=resolve_slicer_profile("ender3"), loaded_material="ABS")
        assert _loaded_ini(seen["cmd"])["filament_density"] == "1.04"
        reported = result.to_dict()["filament"]
        assert (reported["material"], reported["source"]) == ("ABS", "loaded")

    def test_a_profile_that_already_states_a_density_goes_to_the_slicer_untouched(self, tmp_path):
        """Nothing declared, nothing loaded: a user's own filament profile
        is not rewritten, and the response says the density came from it."""
        stl = _cube(tmp_path / "cube.stl")
        own = tmp_path / "own_filament.ini"
        own.write_text("filament_density = 1.32\nfilament_diameter = 1.75\nfilament_type = PLA\nlayer_height = 0.2\n")
        seen: dict[str, Any] = {}
        result = _run_prusa(stl, seen, profile=str(own))
        assert seen["cmd"][seen["cmd"].index("--load") + 1] == str(own)
        assert result.to_dict()["filament"]["source"] == "profile"
        assert result.to_dict()["filament"]["density_g_per_cm3"] == 1.32

    def test_a_declared_material_rewrites_even_a_profile_that_states_a_density(self, tmp_path):
        stl = _cube(tmp_path / "cube.stl")
        own = tmp_path / "own_filament.ini"
        own.write_text("filament_density = 1.04\nfilament_type = ABS\nlayer_height = 0.2\n")
        seen: dict[str, Any] = {}
        result = _run_prusa(stl, seen, profile=str(own), material="PETG")
        ini = _loaded_ini(seen["cmd"])
        assert (ini["filament_density"], ini["filament_type"]) == ("1.27", "PETG")
        assert ini["layer_height"] == "0.2"
        assert result.to_dict()["filament"]["source"] == "declared"

    def test_a_callers_word_cannot_add_a_line_to_the_profile(self, tmp_path):
        """An MCP argument becomes an INI value; a newline in it must not
        become a second key.  A known material that needed cleaning is
        labelled by its table row; an unknown one keeps its cleaned word."""
        stl = _cube(tmp_path / "cube.stl")
        for word, expect_type in (("PETG\nlayer_height = 9", "PETG"), ("WOOD\nlayer_height = 9", "WOOD layer_height 9")):
            seen: dict[str, Any] = {}
            _run_prusa(stl, seen, profile=resolve_slicer_profile("ender3"), material=word)
            ini = _loaded_ini(seen["cmd"])
            assert ini["layer_height"] != "9", word
            assert ini["filament_type"] == expect_type, word

    def test_the_file_carries_the_spelling_the_printer_uses(self, tmp_path):
        """A Bambu compares the file's filament type with its tray's in its
        own vocabulary (``PLA-CF``), and the AMS load blocks read the type
        off the G-code -- so the word as given is what the slicer is handed;
        the density still comes from the table's row (``CF-PLA``)."""
        stl = _cube(tmp_path / "cube.stl")
        seen: dict[str, Any] = {}
        result = _run_prusa(stl, seen, profile=resolve_slicer_profile("bambu_a1"), loaded_material="PLA-CF")
        ini = _loaded_ini(seen["cmd"])
        assert (ini["filament_type"], ini["filament_density"]) == ("PLA-CF", "1.3")
        block = result.to_dict()["filament"]
        assert (block["material"], block["filament_type"], block["source"]) == ("CF-PLA", "PLA-CF", "loaded")
        seen = {}
        _run_prusa(stl, seen, profile=resolve_slicer_profile("bambu_a1"), material="petg")
        assert _loaded_ini(seen["cmd"])["filament_type"] == "PETG"

    def test_a_profile_kiln_cannot_read_is_handed_on_untouched_and_the_answer_says_so(self, tmp_path):
        stl = _cube(tmp_path / "cube.stl")
        own = tmp_path / "latin1.ini"
        own.write_bytes(b"layer_height = 0.2\nnotes = caf\xe9\n")
        seen: dict[str, Any] = {}
        result = _run_prusa(stl, seen, profile=str(own), material="PETG")
        assert seen["cmd"][seen["cmd"].index("--load") + 1] == str(own)
        assert "could not be read" in result.to_dict()["filament"]["note"]

    def test_a_multi_slot_profile_gets_one_value_per_slot(self, tmp_path):
        """PrusaSlicer joins floats with ',' and strings with ';'; a scalar
        would type slots 2..N as the slicer's default PLA."""
        stl = _cube(tmp_path / "cube.stl")
        four = resolve_slicer_profile("bambu_a1", overrides={
            "nozzle_diameter": "0.4,0.4,0.4,0.4", "filament_diameter": "1.75,1.75,1.75,1.75",
        })
        seen: dict[str, Any] = {}
        _run_prusa(stl, seen, profile=four, material="ABS")
        ini = _loaded_ini(seen["cmd"])
        assert ini["filament_density"] == "1.04,1.04,1.04,1.04"
        assert ini["filament_type"] == "ABS;ABS;ABS;ABS"
        assert ini["filament_diameter"] == "1.75,1.75,1.75,1.75"  # kept as stated, never shortened

    def test_a_missing_profile_is_still_the_error_it_always_was(self, tmp_path):
        stl = _cube(tmp_path / "cube.stl")
        with pytest.raises(SlicerError, match="Profile file not found"):
            _run_prusa(stl, {}, profile=str(tmp_path / "gone.ini"), material="PETG")

    def test_the_slice_is_still_counted_against_the_printer_profile(self, tmp_path):
        """Telemetry keys on the profile's stem; the derived profile must not
        turn every slice into an ``overrides_…`` row."""
        stl = _cube(tmp_path / "cube.stl")
        recorded: list[str | None] = []
        seen: dict[str, Any] = {}
        with patch("kiln.slicer._record_slice", side_effect=lambda p, *a, **k: recorded.append(p)):
            _run_prusa(stl, seen, profile=resolve_slicer_profile("bambu_a1"), material="PETG")
        assert recorded and Path(recorded[0]).name.startswith("bambu_a1_")
        # The derived file the slicer loads is named for the printer too.
        loaded = seen["cmd"][seen["cmd"].index("--load") + 1]
        assert Path(loaded).name.startswith("bambu_a1_"), loaded
        assert loaded != recorded[0]

    def test_estimate_print_hands_its_material_to_the_slice_and_reports_it(self, tmp_path):
        from kiln.slicer import estimate_print
        from kiln.slicer_filament import resolve_slice_filament

        stl = _cube(tmp_path / "cube.stl")
        gcode = tmp_path / "e.gcode"
        gcode.write_text("; filament used [mm] = 1000.00\n; filament used [g] = 2.55\n")
        with patch("kiln.slicer.slice_file") as spy:
            spy.return_value = SliceResult(
                success=True, output_path=str(gcode), filament=resolve_slice_filament("ASA"),
            )
            estimates = estimate_print(stl, material="ASA")
        assert spy.call_args.kwargs["material"] == "ASA"
        assert estimates["filament_weight_g"] == 2.55
        assert estimates["filament"]["material"] == "ASA"

    def test_slice_multicolor_copies_hands_the_identity_to_every_copy(self, tmp_path):
        from kiln.slicer import slice_multicolor_copies
        from kiln.slicer_filament import resolve_slice_filament

        stl = _cube(tmp_path / "cube.stl")
        calls: list[dict[str, Any]] = []

        def fake(path, **kw):
            calls.append(kw)
            out = Path(kw["output_dir"]) / "copy.gcode"
            out.write_text(";LAYER_CHANGE\nG1 X1 E1\n")
            return SliceResult(success=True, output_path=str(out), slicer="prusa-slicer", filament=resolve_slice_filament("ABS"))

        with patch("kiln.slicer.slice_file", side_effect=fake):
            result = slice_multicolor_copies(stl, 2, material="ABS", loaded_material=None, output_dir=str(tmp_path / "out"))
        assert len(calls) == 2 and all(c["material"] == "ABS" for c in calls)
        assert result.to_dict()["filament"]["material"] == "ABS"


class TestOrcaPresetsCarryTheIdentity:
    def test_the_filament_preset_states_density_and_type_from_the_settings(self):
        settings = ini_to_settings(resolve_slicer_profile("bambu_a1"))
        settings.update({"filament_density": "1.27", "filament_type": "PETG"})
        presets = settings_to_orca_presets(settings, name="t")
        assert presets.filament["filament_density"] == ["1.27"]
        assert presets.filament["filament_type"] == ["PETG"]
        assert presets.filament["filament_diameter"] == ["1.75"]

    def test_every_multicolor_slot_carries_it(self):
        settings = ini_to_settings(resolve_slicer_profile("bambu_a1"))
        settings.update({"filament_density": "1.04", "filament_type": "ABS"})
        presets = settings_to_orca_presets(settings, name="t", filament_colors=["#FF0000", "#00FF00", "#0000FF"])
        assert [f["filament_density"] for f in presets.filaments] == [["1.04"]] * 3
        assert [f["filament_type"] for f in presets.filaments] == [["ABS"]] * 3

    def test_a_bare_slice_on_the_orca_dialect_derives_from_kilns_default_profile(self, tmp_path):
        """With no profile Orca cannot be handed the identity alone: the
        machine and process presets must come from somewhere, and that is
        Kiln's own default profile -- resolved before the identity is written."""
        stl = _cube(tmp_path / "cube.stl")
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        seen: dict[str, Any] = {}

        def fake_run(cmd, *args, **kwargs):
            machine = cmd[cmd.index("--load-settings") + 1].split(";")[0]
            seen["machine"] = json.loads(Path(machine).read_text(encoding="utf-8"))
            seen["filament"] = json.loads(Path(cmd[cmd.index("--load-filaments") + 1]).read_text(encoding="utf-8"))
            Path(cmd[cmd.index("--outputdir") + 1], "plate_1.gcode").write_text("; gcode\n")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch("kiln.slicer.find_slicer", return_value=SlicerInfo(path="/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer", name="OrcaSlicer")), \
                patch("kiln.slicer.slicer_cli_family", return_value="bambu"), \
                patch("subprocess.run", side_effect=fake_run):
            slice_file(stl, output_dir=str(out_dir))
        assert seen["machine"].get("gcode_flavor"), "the default profile's machine settings did not reach Orca"
        assert seen["filament"].get("nozzle_temperature"), "the default profile's temperatures did not reach Orca"
        assert seen["filament"]["filament_density"] == ["1.24"]

    def test_slice_file_writes_it_into_the_preset_orca_loads(self, tmp_path):
        stl = _cube(tmp_path / "cube.stl")
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        seen: dict[str, Any] = {}

        def fake_run(cmd, *args, **kwargs):
            seen["cmd"] = list(cmd)
            fil = cmd[cmd.index("--load-filaments") + 1]
            seen["filament"] = json.loads(Path(fil).read_text(encoding="utf-8"))
            Path(cmd[cmd.index("--outputdir") + 1], "plate_1.gcode").write_text("; gcode\n")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch("kiln.slicer.find_slicer", return_value=SlicerInfo(path="/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer", name="OrcaSlicer")), \
                patch("kiln.slicer.slicer_cli_family", return_value="bambu"), \
                patch("subprocess.run", side_effect=fake_run):
            result = slice_file(stl, output_dir=str(out_dir), profile=resolve_slicer_profile("k1"), material="PETG")
        assert seen["filament"]["filament_density"] == ["1.27"]
        assert seen["filament"]["filament_type"] == ["PETG"]
        assert result.to_dict()["filament"]["source"] == "declared"


# ---------------------------------------------------------------------------
# Every door hands its material down
# ---------------------------------------------------------------------------


def _register_slicer_tools() -> dict:
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


def _fake_slice(tmp_path: Path) -> tuple[MagicMock, SliceResult]:
    from kiln.slicer_filament import resolve_slice_filament

    gcode = tmp_path / "out.gcode"
    gcode.write_text(";LAYER_CHANGE\nG1 X1 E1\n; filament used [g] = 4.26\n")
    result = SliceResult(
        success=True, output_path=str(gcode), slicer="prusa-slicer",
        message="Sliced", filament=resolve_slice_filament("PETG"),
    )
    return MagicMock(return_value=result), result


class TestTheDoors:
    @pytest.fixture(autouse=True)
    def _no_printer(self, monkeypatch):
        import kiln.server as srv

        monkeypatch.setattr(srv, "_check_auth", lambda *_a, **_k: None)
        monkeypatch.setattr(srv, "_resolve_adapter", mock.Mock(side_effect=RuntimeError("no adapters in tests")))

    def test_slice_model_takes_a_material_and_reports_the_filament(self, tmp_path):
        tools = _register_slicer_tools()
        stl = _cube(tmp_path / "cube.stl")
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            resp = tools["slice_model"](input_path=stl, printer_id="ender3", material="PETG")
        assert spy.call_args.kwargs["material"] == "PETG"
        assert resp["filament"]["material"] == "PETG"
        assert resp["filament"]["source"] == "declared"

    def test_reslice_with_overrides_takes_a_material(self, tmp_path):
        tools = _register_slicer_tools()
        stl = _cube(tmp_path / "cube.stl")
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            resp = tools["reslice_with_overrides"](
                input_path=stl, printer_id="ender3", overrides={"brim_width": "5"}, material="ABS",
            )
        assert spy.call_args.kwargs["material"] == "ABS"
        assert resp["filament"]["source"] == "declared"

    def test_slice_and_print_hands_the_declared_material_and_the_loaded_spool_apart(self, tmp_path, monkeypatch):
        """Declared and loaded are different facts; the slice is told both
        so the response can say which one the density came from."""
        import kiln.server as srv

        tools = _register_slicer_tools()
        stl = _cube(tmp_path / "cube.stl")
        spy, _ = _fake_slice(tmp_path)
        adapter = SimpleNamespace(
            get_ams_status=lambda: {"tray_now": "1", "units": [{"trays": [{"slot": 1, "tray_type": "ABS"}]}]},
        )
        monkeypatch.setattr(srv, "_resolve_adapter", lambda *_a, **_k: adapter)
        with patch("kiln.slicer.slice_file", spy), \
                patch("kiln.printers.upload_prep.prepare_upload_for_adapter", side_effect=RuntimeError("stop here")):
            tools["slice_and_print"](input_path=stl, printer_id="ender3", skip_validation=True)
        assert spy.call_args.kwargs["material"] is None
        assert spy.call_args.kwargs["loaded_material"] == "ABS"
        # Read off the machine's own unit: the default provenance stands.
        assert spy.call_args.kwargs.get("loaded_determined_by", "observed") == "observed"

        with patch("kiln.slicer.slice_file", spy), \
                patch("kiln.printers.upload_prep.prepare_upload_for_adapter", side_effect=RuntimeError("stop here")):
            tools["slice_and_print"](input_path=stl, printer_id="ender3", material="PETG", skip_validation=True)
        assert spy.call_args.kwargs["material"] == "PETG"

    def test_quick_print_pipeline_hands_its_material_down_and_records_the_filament(self, tmp_path):
        from kiln.pipelines import quick_print

        stl = _cube(tmp_path / "cube.stl")
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy), \
                patch("kiln.pipelines._resolve_pipeline_adapter", side_effect=RuntimeError("no printer")):
            result = quick_print(model_path=stl, printer_id="ender3", material="PETG", skip_validation=True)
        assert spy.call_args.kwargs["material"] == "PETG"
        step = next(s for s in result.steps if s.name == "slice")
        assert step.data["filament"]["source"] == "declared"
        assert step.data["filament"]["density_g_per_cm3"] == 1.27

    def test_reslice_and_print_pipeline_hands_its_material_down(self, tmp_path):
        from kiln.pipelines import reslice_and_print

        stl = _cube(tmp_path / "cube.stl")
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy), \
                patch("kiln.pipelines._resolve_pipeline_adapter", side_effect=RuntimeError("no printer")):
            result = reslice_and_print(
                model_path=stl, printer_id="ender3", overrides={"brim_width": "5"},
                material="ASA", skip_validation=True,
            )
        assert spy.call_args.kwargs["material"] == "ASA"
        step = next(s for s in result.steps if s.name == "slice")
        assert step.data["filament"]["material"] == "PETG"  # what the (fake) slice answered

    def test_the_pipelines_read_the_loaded_spool_from_the_adapter(self, tmp_path):
        from kiln.pipelines import quick_print, reslice_and_print

        stl = _cube(tmp_path / "cube.stl")
        adapter = SimpleNamespace(
            name="bambu",
            get_ams_status=lambda: {"tray_now": "255", "units": [{"trays": [{"slot": 0, "tray_type": "PETG"}]}]},
        )
        for door, kwargs in (
            (quick_print, {}),
            (reslice_and_print, {"overrides": {"brim_width": "5"}}),
        ):
            spy, _ = _fake_slice(tmp_path)
            with patch("kiln.slicer.slice_file", spy), \
                    patch("kiln.pipelines._resolve_pipeline_adapter", return_value=adapter), \
                    patch("kiln.printers.upload_prep.slice_overrides_for_adapter", return_value={}):
                door(model_path=stl, printer_id="ender3", skip_validation=True, **kwargs)
            assert spy.call_args.kwargs["material"] is None, door.__name__
            assert spy.call_args.kwargs["loaded_material"] == "PETG", door.__name__

    def test_the_benchmark_step_records_the_filament(self, tmp_path):
        from kiln.pipelines import benchmark

        stl = _cube(tmp_path / "cube.stl")
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy), \
                patch("kiln.pipelines._resolve_pipeline_adapter", side_effect=RuntimeError("no printer")):
            result = benchmark(model_path=stl, printer_id="ender3", skip_validation=True)
        step = next(s for s in result.steps if s.name == "slice")
        assert step.data["filament"]["material"] == "PETG"

    def test_slice_model_and_reslice_read_the_loaded_spool_when_nothing_is_declared(self, tmp_path, monkeypatch):
        import kiln.server as srv

        tools = _register_slicer_tools()
        stl = _cube(tmp_path / "cube.stl")
        ams_reads: list[bool] = []

        def _ams():
            ams_reads.append(True)
            return {"tray_now": "0", "units": [{"trays": [{"slot": 0, "tray_type": "ASA"}]}]}

        adapter = SimpleNamespace(get_ams_status=_ams)

        def adapter_ams_calls(_adapter):
            n = len(ams_reads)
            ams_reads.clear()
            return n

        asked: list[str | None] = []

        def _resolve(printer_name=None, *_a, **_k):
            asked.append(printer_name)
            return adapter

        monkeypatch.setattr(srv, "_resolve_adapter", _resolve)
        for name, kwargs in (("slice_model", {}), ("reslice_with_overrides", {"overrides": {"brim_width": "5"}})):
            spy, _ = _fake_slice(tmp_path)
            with patch("kiln.slicer.slice_file", spy):
                tools[name](input_path=stl, printer_id="ender3", **kwargs)
            assert spy.call_args.kwargs["material"] is None, name
            assert spy.call_args.kwargs["loaded_material"] == "ASA", name
            assert adapter_ams_calls(adapter) == 1, name
            # Declared: the printer is not asked for its spool.
            asked.clear()
            with patch("kiln.slicer.slice_file", spy):
                tools[name](input_path=stl, printer_id="ender3", material="PLA", **kwargs)
            assert spy.call_args.kwargs["loaded_material"] is None, name
            assert not adapter_ams_calls(adapter), name

    def test_retry_print_with_fix_reads_the_a1s_idle_unit_like_every_other_door(self, tmp_path, monkeypatch):
        """tray_now=255 with one PETG tray: the retry used to walk the trays
        only when an active tray was named, so on the A1 it weighed as PLA
        while slice_and_print on the same reading weighed as PETG."""
        import kiln.server as srv
        from kiln.plugins.smart_print_tools import _SmartPrintToolsPlugin

        tools: dict[str, Any] = {}

        class _FakeMcp:
            def tool(self, name: str | None = None, **_kwargs):
                def decorator(fn):
                    tools[name or fn.__name__] = fn
                    return fn

                return decorator

        _SmartPrintToolsPlugin().register(_FakeMcp())
        stl = _cube(tmp_path / "cube.stl")
        adapter = SimpleNamespace(
            name="bambu",
            get_ams_status=lambda: {"tray_now": "255", "units": [{"trays": [{"slot": 0, "tray_type": "PETG"}]}]},
        )
        monkeypatch.setattr(srv, "_resolve_adapter", lambda *_a, **_k: adapter)
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            tools["retry_print_with_fix"](
                model_path=stl, printer_id="ender3", skip_diagnosis=True, skip_validation=True,
            )
        assert spy.call_args.kwargs["material"] is None
        assert spy.call_args.kwargs["loaded_material"] == "PETG"

    def test_slice_and_estimate_hands_its_material_down_and_its_grams_are_the_slicers(self, tmp_path):
        from kiln.plugins.estimate_tools import _EstimateToolsPlugin

        tools: dict[str, Any] = {}

        class _FakeMcp:
            def tool(self, name: str | None = None, **_kwargs):
                def decorator(fn):
                    tools[name or fn.__name__] = fn
                    return fn

                return decorator

        _EstimateToolsPlugin().register(_FakeMcp())
        stl = _cube(tmp_path / "cube.stl")
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            resp = tools["slice_and_estimate"](input_path=stl, printer_id="ender3", material="PETG")
        assert spy.call_args.kwargs["material"] == "PETG"
        # 1393.81 mm of 1.75 mm PETG is 4.26 g, the figure the (fake) slicer wrote —
        # not 1393.81 x 0.003 = 4.2, the PLA constant this door used to apply to every material.
        assert resp["estimate"]["filament_used_grams"] == 4.26
        assert resp["estimate"]["filament"]["source"] == "declared"

    def test_generate_and_print_reads_the_loaded_spool_and_takes_a_material(self, tmp_path, monkeypatch):
        import kiln.server as srv
        from kiln.generation.base import GenerationJob, GenerationResult, GenerationStatus, MeshValidationResult
        from kiln.printers.base import UploadResult

        stl = _cube(tmp_path / "cube.stl")
        provider = MagicMock()
        provider.display_name = "OpenSCAD"
        provider.generate.return_value = GenerationJob(
            id="j", provider="meshy", prompt="a cube", status=GenerationStatus.SUCCEEDED,
            progress=100, created_at=1000.0, format="stl",
        )
        provider.download_result.return_value = GenerationResult(
            job_id="j", provider="meshy", local_path=stl, format="stl", file_size_bytes=42000, prompt="a cube",
        )
        adapter = MagicMock()
        adapter.get_ams_status.return_value = {"tray_now": "0", "units": [{"trays": [{"slot": 0, "tray_type": "ABS"}]}]}
        adapter.upload_file.return_value = UploadResult(success=True, file_name="model.gcode", message="Uploaded")
        monkeypatch.setattr(srv, "_get_generation_provider", lambda *_a, **_k: provider)
        monkeypatch.setattr(srv, "_get_adapter", lambda: adapter)
        monkeypatch.setattr(srv, "_resolve_adapter", lambda *_a, **_k: adapter)
        for declared, expect_loaded in ((None, "ABS"), ("PETG", None)):
            spy, _ = _fake_slice(tmp_path)
            with patch("kiln.slicer.slice_file", spy), \
                    patch("kiln.generation.validate_mesh", return_value=MeshValidationResult(valid=True, errors=[], warnings=[])), \
                    patch("kiln.plugins.validation_pipeline_tools.run_full_validation_pipeline", return_value={
                        "ready_to_print": True, "printability_score": 92, "validated_path": stl,
                        "summary": "ok", "next_action": None, "repaired": False, "checks": [], "status": "pass",
                        "model_info": {"dimensions_mm": {"x": 20.0, "y": 20.0, "z": 20.0}},
                    }):
                resp = srv.generate_and_print("a cube", provider="meshy", material=declared)
            assert resp.get("success") is True, resp
            assert spy.call_args.kwargs["material"] == declared
            assert spy.call_args.kwargs["loaded_material"] == expect_loaded
            assert resp["slice"]["filament"]["material"] == "PETG"  # what the (fake) slice answered

    def test_design_to_gcode_hands_its_material_to_the_slice(self, tmp_path):
        from kiln import design_reasoning

        spy, _ = _fake_slice(tmp_path)

        def fake_compile(_code, output_path):
            _cube(Path(output_path))

        with patch("kiln.slicer.slice_file", spy), \
                patch("kiln.parametric.compile_scad_code", side_effect=fake_compile):
            design_reasoning.design_to_gcode("a simple coaster", output_dir=str(tmp_path / "d"), material="ASA")
        assert spy.called, "design_to_gcode never reached the slicer"
        assert spy.call_args.kwargs["material"] == "ASA"


class TestTheCliDoor:
    @pytest.fixture(autouse=True)
    def _gate_off(self, monkeypatch):
        monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")

    def _invoke(
        self, tmp_path, args: list[str], tracker_material: str | None = None,
        determined_by: str | None = None, env: dict[str, str] | None = None,
    ):
        from click.testing import CliRunner

        from kiln.cli.main import cli
        from kiln.materials import LoadedMaterial

        stl = tmp_path / "cube.stl"
        stl.write_text("solid\nendsolid\n")
        spy, _ = _fake_slice(tmp_path)
        tracker = MagicMock()
        if tracker_material:
            row = {"printer_name": "default", "material_type": tracker_material}
            if determined_by:
                row["determined_by"] = determined_by
            tracker.get_material.return_value = LoadedMaterial(**row)
        else:
            tracker.get_material.return_value = None
        with patch("kiln.cli.main._autodetect_printer_profile_id", return_value=None), \
                patch("kiln.materials.MaterialTracker", return_value=tracker), \
                patch("kiln.persistence.get_db"), \
                patch.dict(os.environ, {"KILN_MATERIAL": "", "KILN_DEFAULT_MATERIAL": "", "KILN_FILAMENT": "", **(env or {})}), \
                patch("kiln.slicer.slice_file", spy):
            result = CliRunner().invoke(cli, ["slice", str(stl), "--json", *args])
        assert result.exit_code == 0, result.output
        return spy.call_args.kwargs, json.loads(result.output)["data"]

    def test_an_explicit_material_is_declared(self, tmp_path):
        kwargs, data = self._invoke(tmp_path, ["--material", "PETG"])
        assert kwargs["material"] == "PETG"
        assert kwargs["loaded_material"] is None
        assert data["filament"]["source"] == "declared"

    def test_the_tracked_spool_is_the_loaded_one_and_says_a_person_recorded_it(self, tmp_path):
        kwargs, _ = self._invoke(tmp_path, [], tracker_material="ABS")
        assert kwargs["material"] is None
        assert kwargs["loaded_material"] == "ABS"
        # The tracker row is what a person typed (its own default provenance),
        # and the CLI never asks the printer: the slice must not credit the
        # printer with reporting it.
        assert kwargs["loaded_determined_by"] == "user_reported"

    def test_a_tracked_row_a_machine_wrote_keeps_its_provenance(self, tmp_path):
        kwargs, _ = self._invoke(tmp_path, [], tracker_material="ABS", determined_by="observed")
        assert kwargs["loaded_determined_by"] == "observed"

    def test_nothing_known_hands_nothing_down_and_the_slice_defaults(self, tmp_path):
        kwargs, _ = self._invoke(tmp_path, [])
        assert kwargs["material"] is None
        assert kwargs["loaded_material"] is None

    def test_an_environment_default_is_a_declaration(self, tmp_path):
        kwargs, _ = self._invoke(tmp_path, [], env={"KILN_MATERIAL": "ABS"})
        assert kwargs["material"] == "ABS"
        assert kwargs["loaded_material"] is None


# ---------------------------------------------------------------------------
# End to end: the real slicer writes the real figure
# ---------------------------------------------------------------------------


def _real_prusaslicer() -> str | None:
    import shutil

    for name in ("prusa-slicer", "PrusaSlicer", "prusaslicer"):
        found = shutil.which(name)
        if found:
            return found
    mac = "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"
    return mac if os.path.isfile(mac) and os.access(mac, os.X_OK) else None


def _real_orca() -> str | None:
    mac = "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"
    return mac if os.path.isfile(mac) and os.access(mac, os.X_OK) else None


@pytest.mark.skipif(_real_prusaslicer() is None, reason="needs a real PrusaSlicer")
class TestPrusaSlicerWritesTheWeightItself:
    def _slice(self, tmp_path: Path, **kw) -> str:
        stl = _cube(tmp_path / "cube.stl")
        result = slice_file(
            stl, profile=resolve_slicer_profile("bambu_a1"), slicer_path=_real_prusaslicer(),
            output_dir=str(tmp_path / "out"), timeout=300, **kw,
        )
        return Path(result.output_path).read_text(encoding="utf-8", errors="replace")

    def test_the_cube_leaves_with_the_grams_the_declared_density_implies(self, tmp_path):
        body = self._slice(tmp_path, material="PETG")
        mm = _footer_value(body, "filament used [mm]")
        grams = _footer_value(body, "filament used [g]")
        total = _footer_value(body, "total filament used [g]")
        assert mm and mm > 1000, body[-2000:]
        assert grams is not None, "PrusaSlicer omitted 'filament used [g]' — no density reached it"
        assert grams == pytest.approx(mm * _AREA_MM2 * 1.27 / 1000.0, abs=0.01)
        assert total == pytest.approx(grams)
        assert _footer_value(body, "filament_density") == 1.27
        assert re.search(r"^; filament_type = PETG$", body, re.M)

    def test_with_nothing_declared_the_cube_weighs_as_pla(self, tmp_path):
        body = self._slice(tmp_path)
        mm = _footer_value(body, "filament used [mm]")
        assert _footer_value(body, "filament used [g]") == pytest.approx(mm * _AREA_MM2 * 1.24 / 1000.0, abs=0.01)
        assert _footer_value(body, "filament_density") == 1.24

    def test_the_after_the_fact_fill_is_no_longer_needed_on_a_kiln_slice(self, tmp_path):
        """The safety net stays; a Kiln-sliced file never reaches it.  The
        usage reader takes the slicer's grams as written, the Bambu wrap
        writes those same grams to the tile, and completion changes nothing."""
        import zipfile

        from kiln.printers import bambu_3mf, gcode_complete

        body = self._slice(tmp_path, material="PETG")
        usage = bambu_3mf.filament_usage_from_gcode(body)
        assert usage.source == "slicer_grams"
        assert usage.total_g == pytest.approx(_footer_value(body, "filament used [g]"))

        assert gcode_complete.declared_grams(body) == pytest.approx(usage.total_g)
        raw = tmp_path / "raw.gcode"
        raw.write_text(body, encoding="utf-8")
        with patch("kiln.printers.gcode_complete._fill_weight", side_effect=AssertionError("the fill ran")):
            gcode_complete.complete_gcode_for_printer(str(raw), "moonraker")

        # Wrapped with nothing declared: the tile's type and the start
        # sequence's filament come off the G-code the slicer wrote.
        out = str(tmp_path / "cube.gcode.3mf")
        wrapped = bambu_3mf.build_bambu_3mf(body, out)
        with zipfile.ZipFile(out) as zf:
            info = zf.read("Metadata/slice_info.config").decode("utf-8")
            plate = zf.read("Metadata/plate_1.gcode").decode("utf-8", errors="replace")
        assert wrapped.filament_type == "PETG"
        assert 'type="PETG"' in info
        assert "set_filament_type:PETG" in plate
        weight = re.search(r'<metadata key="weight" value="([^"]*)"', info).group(1)
        assert float(weight) == pytest.approx(usage.total_g, abs=0.01)
        bambu_3mf.complete_bambu_archive(out)
        with zipfile.ZipFile(out) as zf:
            assert zf.read("Metadata/slice_info.config").decode("utf-8") == info

    def test_a_four_extruder_profile_weighs_and_names_every_slot(self, tmp_path):
        """A real comma-vector four-extruder profile (PrusaSlicer's own vector
        spelling): every slot carries the declared density and type, and the
        diameter vector it stated is echoed back unshortened."""
        stl = _cube(tmp_path / "cube.stl")
        four = resolve_slicer_profile("bambu_a1", overrides={
            "nozzle_diameter": "0.4,0.4,0.4,0.4", "filament_diameter": "1.75,1.75,1.75,1.75",
            "temperature": "220,220,220,220", "first_layer_temperature": "220,220,220,220",
            "retract_length": "0.8,0.8,0.8,0.8", "retract_speed": "30,30,30,30", "retract_lift": "0.2,0.2,0.2,0.2",
        })
        result = slice_file(
            stl, profile=four, slicer_path=_real_prusaslicer(),
            output_dir=str(tmp_path / "out"), timeout=300, material="ABS",
        )
        body = Path(result.output_path).read_text(encoding="utf-8", errors="replace")
        assert re.search(r"^; nozzle_diameter = 0\.4,0\.4,0\.4,0\.4$", body, re.M), "not a four-extruder slice"
        assert re.search(r"^; filament_density = 1\.04,1\.04,1\.04,1\.04$", body, re.M)
        assert re.search(r"^; filament_type = ABS;ABS;ABS;ABS$", body, re.M)
        assert re.search(r"^; filament_diameter = 1\.75,1\.75,1\.75,1\.75$", body, re.M)
        mm = _footer_value(body, "filament used [mm]")
        assert _footer_value(body, "filament used [g]") == pytest.approx(mm * _AREA_MM2 * 1.04 / 1000.0, abs=0.01)


@pytest.mark.skipif(_real_orca() is None, reason="needs a real OrcaSlicer")
class TestOrcaWritesTheWeightItself:
    def test_the_cube_leaves_with_the_grams_the_declared_density_implies(self, tmp_path):
        stl = _cube(tmp_path / "cube.stl")
        result = slice_file(
            stl, profile=resolve_slicer_profile("k1"), slicer_path=_real_orca(),
            output_dir=str(tmp_path / "out"), timeout=300, material="PETG",
        )
        body = Path(result.output_path).read_text(encoding="utf-8", errors="replace")
        mm = _footer_value(body, "filament used [mm]")
        grams = _footer_value(body, "filament used [g]")
        assert mm and grams is not None, body[-2000:]
        assert grams == pytest.approx(mm * _AREA_MM2 * 1.27 / 1000.0, abs=0.01)
        assert _footer_value(body, "total filament used [g]") == pytest.approx(grams)
        assert result.to_dict()["filament"]["source"] == "declared"
