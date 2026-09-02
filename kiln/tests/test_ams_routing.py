"""Tests for colour-aware AMS routing.

The failure this guards: a painted jar sliced as (white, red, black) on an
AMS loaded (white, black, grey, red) would have printed its red mark in
black and its black lid in grey, because the printer feeds extruder N from
slot N and nothing compared the file's colours to the spools.

Coverage areas:
    - reading filaments out of a sliced G-code, a Bambu .gcode.3mf, a painted 3MF
    - loaded-tray parsing, including the firmware's unread-colour sentinel
    - the planner: exact, near, no match, duplicates, material mismatch,
      more colours than spools, unread trays, the measured jar case
    - the resolver: a multi-colour file blocks on a missing colour and
      routes by colour otherwise; explicit mappings are kept and compared
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiln.ams_routing import (
    Filament,
    Tray,
    loaded_trays,
    normalize_hex,
    plan_ams_mapping,
    read_file_filaments,
)

# ---------------------------------------------------------------------------
# Fixtures: the measured 2026-09-01 case
# ---------------------------------------------------------------------------

#: What the A1's AMS reported that night, verbatim in shape.
JAR_NIGHT_AMS = {
    "success": True,
    "units": [
        {
            "unit_id": "0",
            "trays": [
                {"slot": "0", "tray_type": "PLA", "tray_color": "FFFFFFFF", "remain": 0, "remaining_known": False},
                {"slot": "1", "tray_type": "PLA", "tray_color": "161616FF", "remain": 0, "remaining_known": False},
                {"slot": "2", "tray_type": "PLA", "tray_color": "898989FF", "remain": 0, "remaining_known": False},
                {"slot": "3", "tray_type": "PLA", "tray_color": "F72323FF", "remain": 0, "remaining_known": False},
            ],
        }
    ],
}

#: The tail OrcaSlicer wrote for the painted jar: white, red, black.
JAR_GCODE_TAIL = (
    "; CONFIG_BLOCK_START\n"
    "; filament_colour = #FFFFFF;#C81E1E;#161616\n"
    "; filament_settings_id = a;b;c\n"
    "; filament_type = PLA;PLA;PLA\n"
    "; CONFIG_BLOCK_END\n"
)


def _gcode(tmp_path: Path, name: str = "jar.gcode", *, pad_lines: int = 0) -> str:
    p = tmp_path / name
    body = "G28\n" + ("G1 X1 Y1\n" * pad_lines)
    p.write_text(body + JAR_GCODE_TAIL)
    return str(p)


def _gcode_3mf(tmp_path: Path) -> str:
    p = tmp_path / "jar.gcode.3mf"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("Metadata/plate_1.gcode", "G28\n" + JAR_GCODE_TAIL)
        zf.writestr("Metadata/plate_1.json", "{}")
    return str(p)


def _trays(*hexes: str | None, material: str = "PLA") -> list[Tray]:
    return [Tray(slot=i, material=material, hex6=h) for i, h in enumerate(hexes)]


# ---------------------------------------------------------------------------
# Hex + trays
# ---------------------------------------------------------------------------


class TestNormalizeHex:
    def test_bambu_rgba_drops_alpha(self):
        assert normalize_hex("F72323FF") == "F72323"

    def test_hash_prefix_and_case(self):
        assert normalize_hex("#c81e1e") == "C81E1E"

    @pytest.mark.parametrize("bad", ["", "0x1234", "-10000", "gg0000", None, 12])
    def test_non_colours_are_none(self, bad):
        assert normalize_hex(bad) is None


class TestLoadedTrays:
    def test_reads_the_jar_night_ams(self):
        trays = loaded_trays(JAR_NIGHT_AMS)
        assert [(t.slot, t.hex6) for t in trays] == [
            (0, "FFFFFF"), (1, "161616"), (2, "898989"), (3, "F72323"),
        ]
        assert all(t.material == "PLA" for t in trays)

    def test_empty_material_is_an_empty_slot(self):
        info = {"units": [{"trays": [{"slot": "0", "tray_type": "", "tray_color": "FF0000FF"}]}]}
        assert loaded_trays(info) == []

    def test_all_zero_colour_is_unknown_not_black(self):
        info = {"units": [{"trays": [{"slot": "2", "tray_type": "PETG", "tray_color": "000000FF"}]}]}
        (tray,) = loaded_trays(info)
        assert tray.hex6 is None
        assert tray.material == "PETG"

    def test_garbage_is_empty(self):
        assert loaded_trays(None) == []
        assert loaded_trays({"units": "nope"}) == []


# ---------------------------------------------------------------------------
# Reading what a file wants
# ---------------------------------------------------------------------------


class TestReadFileFilaments:
    def test_gcode_config_block_at_the_tail(self, tmp_path):
        got = read_file_filaments(_gcode(tmp_path, pad_lines=200_000))
        assert got.source == "gcode"
        assert [f.hex6 for f in got.filaments] == ["FFFFFF", "C81E1E", "161616"]
        assert [f.material for f in got.filaments] == ["PLA", "PLA", "PLA"]
        assert got.multicolour

    def test_bambu_gcode_3mf_reads_the_gcode_inside(self, tmp_path):
        got = read_file_filaments(_gcode_3mf(tmp_path))
        assert got.source == "gcode_3mf"
        assert [f.hex6 for f in got.filaments] == ["FFFFFF", "C81E1E", "161616"]

    def test_painted_3mf_reports_its_distinct_colours(self, tmp_path):
        pytest.importorskip("kiln.multicolor_3mf")
        from kiln.multicolor_3mf import compose_painted_3mf

        tris = [
            ((0, 0, 0), (10, 0, 0), (0, 10, 0)),
            ((0, 0, 5), (10, 0, 5), (0, 10, 5)),
            ((0, 0, 10), (10, 0, 10), (0, 10, 10)),
        ]
        out = str(tmp_path / "painted.3mf")
        res = compose_painted_3mf(
            tris, ["#ffffff", "#c81e1e", "#161616"], output_path=out, name="p"
        )
        assert res.get("success"), res
        got = read_file_filaments(out)
        assert got.source == "painted_3mf"
        assert {f.hex6 for f in got.filaments} == {"FFFFFF", "C81E1E", "161616"}
        assert all(f.material is None for f in got.filaments)

    def test_single_colour_gcode_is_not_multicolour(self, tmp_path):
        p = tmp_path / "one.gcode"
        p.write_text("G28\n; filament_colour = #29B2B2\n; filament_type = PETG\n")
        got = read_file_filaments(str(p))
        assert [(f.hex6, f.material) for f in got.filaments] == [("29B2B2", "PETG")]
        assert not got.multicolour

    def test_missing_or_alien_file_is_empty(self, tmp_path):
        assert read_file_filaments(None).filaments == []
        assert read_file_filaments(str(tmp_path / "nope.gcode")).filaments == []
        p = tmp_path / "x.stl"
        p.write_bytes(b"\0" * 100)
        assert read_file_filaments(str(p)).source == "none"

    def test_corrupt_zip_is_empty_not_an_error(self, tmp_path):
        p = tmp_path / "bad.3mf"
        p.write_bytes(b"not a zip")
        assert read_file_filaments(str(p)).filaments == []


# ---------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------


class TestPlanAmsMapping:
    def test_the_jar_routes_red_to_slot_four(self):
        wanted = [Filament("FFFFFF", "PLA"), Filament("C81E1E", "PLA"), Filament("161616", "PLA")]
        plan = plan_ams_mapping(wanted, loaded_trays(JAR_NIGHT_AMS))
        assert plan.ok
        assert plan.mapping == [0, 3, 1], plan.summary
        assert plan.summary == "white PLA → slot 1, red PLA → slot 4, black PLA → slot 2"
        assert plan.warnings == []

    def test_slot_order_is_not_extruder_order(self):
        # The old behaviour, made explicit: with no planning this file feeds
        # slots 1, 2, 3 — white, black, grey.  The plan must not be that.
        wanted = [Filament("FFFFFF"), Filament("C81E1E"), Filament("161616")]
        plan = plan_ams_mapping(wanted, loaded_trays(JAR_NIGHT_AMS))
        assert plan.mapping != [0, 1, 2]

    def test_a_near_shade_matches(self):
        plan = plan_ams_mapping([Filament("D02020")], _trays("F72323"))
        assert plan.ok and plan.mapping == [0]
        assert plan.matches[0]["delta_e"] is not None

    def test_a_missing_colour_blocks_the_whole_plan(self):
        wanted = [Filament("FFFFFF"), Filament("0000FF")]  # blue: nothing loaded is blue
        plan = plan_ams_mapping(wanted, loaded_trays(JAR_NIGHT_AMS))
        assert not plan.ok
        assert plan.mapping is None
        assert plan.unmatched == [1]
        assert "no spool loaded" in plan.summary
        assert "nearest" in plan.warnings[0]

    def test_grey_is_not_red(self):
        plan = plan_ams_mapping([Filament("F72323")], _trays("898989"))
        assert not plan.ok

    def test_two_extruders_never_share_a_tray(self):
        # Two near-identical reds wanted, one red loaded: the second is
        # unmatched rather than silently doubled onto the same spool.
        plan = plan_ams_mapping([Filament("F72323"), Filament("F52525")], _trays("F72323", "FFFFFF"))
        assert not plan.ok
        assert plan.unmatched == [1]

    def test_best_pair_wins_before_a_worse_first_extruder(self):
        # Extruder 0 is a red-ish orange, extruder 1 is exactly the loaded
        # red.  Greedy-in-order would give the red spool to extruder 0 and
        # leave extruder 1 with the orange-ish one.
        plan = plan_ams_mapping([Filament("F04020"), Filament("F72323")], _trays("F72323", "F04A20"))
        assert plan.ok
        assert plan.mapping == [1, 0]

    def test_material_mismatch_matches_with_a_warning(self):
        plan = plan_ams_mapping([Filament("F72323", "PETG")], _trays("F72323", material="PLA"))
        assert plan.ok
        assert any("material differs" in w for w in plan.warnings)

    def test_same_material_is_preferred_over_a_marginally_closer_other(self):
        trays = [Tray(0, "PLA", "F52323"), Tray(1, "PETG", "F72323")]
        plan = plan_ams_mapping([Filament("F72323", "PLA")], trays)
        assert plan.ok
        assert plan.mapping == [0], plan.matches

    def test_unread_tray_matches_only_as_last_resort(self):
        trays = [Tray(0, "PLA", None), Tray(1, "PLA", "F72323")]
        plan = plan_ams_mapping([Filament("F72323", "PLA"), Filament("0000FF", "PLA")], trays)
        assert plan.ok
        assert plan.mapping == [1, 0]
        assert any("not read" in w for w in plan.warnings)

    def test_colourless_filament_matches_by_material(self):
        plan = plan_ams_mapping([Filament(None, "PETG")], [Tray(0, "PLA", "FFFFFF"), Tray(1, "PETG", "FFFFFF")])
        assert plan.ok and plan.mapping == [1]

    def test_more_colours_than_spools_blocks(self):
        wanted = [Filament(h) for h in ("FFFFFF", "161616", "898989", "F72323", "0000FF")]
        plan = plan_ams_mapping(wanted, loaded_trays(JAR_NIGHT_AMS))
        assert not plan.ok
        assert plan.unmatched == [4]

    def test_no_trays_blocks_everything(self):
        plan = plan_ams_mapping([Filament("FFFFFF")], [])
        assert not plan.ok
        assert plan.unmatched == [0]

    def test_empty_wanted_is_no_plan(self):
        assert plan_ams_mapping([], _trays("FFFFFF")).mapping is None

    def test_to_dict_round_trips_the_summary(self):
        plan = plan_ams_mapping([Filament("FFFFFF")], _trays("FFFFFF"))
        d = plan.to_dict()
        assert d["ok"] and d["mapping"] == [0] and d["summary"].endswith("slot 1")


# ---------------------------------------------------------------------------
# The resolver every print door goes through
# ---------------------------------------------------------------------------


class TestResolverRoutesByColour:
    def _adapter(self, ams=JAR_NIGHT_AMS):
        adapter = MagicMock()
        adapter.get_ams_status.return_value = ams
        return adapter

    def test_the_jar_gcode_is_routed_by_colour(self, tmp_path):
        from kiln.server import _resolve_use_ams

        got = _resolve_use_ams("auto", None, self._adapter(), file_path=_gcode(tmp_path))
        assert got["use_ams"] is True
        assert got["ams_mapping"] == [0, 3, 1]
        assert got["plan"]["summary"].startswith("white PLA → slot 1")
        assert got["selection"]["slot"] == 0

    def test_without_the_file_the_old_first_tray_pick_stands(self):
        from kiln.server import _resolve_use_ams

        got = _resolve_use_ams("auto", None, self._adapter())
        assert got["ams_mapping"] == [0]
        assert "plan" not in got

    def test_a_missing_colour_blocks_a_multicolour_print(self, tmp_path):
        from kiln.server import _resolve_use_ams

        p = tmp_path / "blue.gcode"
        p.write_text("G28\n; filament_colour = #FFFFFF;#0000FF\n; filament_type = PLA;PLA\n")
        got = _resolve_use_ams("auto", None, self._adapter(), file_path=str(p))
        assert got.get("blocked") is True
        assert got["ams_mapping"] is None
        assert "cannot supply them all" in got["warnings"][0]

    def test_a_single_unmatched_colour_falls_back_with_a_warning(self, tmp_path):
        from kiln.server import _resolve_use_ams

        p = tmp_path / "blue.gcode"
        p.write_text("G28\n; filament_colour = #0000FF\n; filament_type = PLA\n")
        got = _resolve_use_ams("auto", None, self._adapter(), file_path=str(p))
        assert got.get("blocked") is None
        assert got["use_ams"] is True
        assert got["ams_mapping"] == [0]
        assert any("no loaded spool is close to blue" in w for w in got["warnings"])

    def test_an_explicit_mapping_is_kept_and_compared(self, tmp_path):
        from kiln.server import _resolve_use_ams

        got = _resolve_use_ams("auto", [0, 1, 2], self._adapter(), file_path=_gcode(tmp_path))
        assert got["ams_mapping"] is None  # caller's mapping stands, resolver adds none
        assert any("differs from what the loaded spools suggest" in w for w in got["warnings"])

    def test_wanted_overrides_the_file(self, tmp_path):
        from kiln.server import _resolve_use_ams

        got = _resolve_use_ams(
            "auto", None, self._adapter(),
            file_path=_gcode(tmp_path),
            wanted=[Filament("F72323", "PLA")],
        )
        assert got["ams_mapping"] == [3]


class TestUploadMemory:
    def test_a_remembered_upload_is_found_by_printer_side_name(self, tmp_path):
        from kiln import server

        local = _gcode(tmp_path, "part.gcode")
        server._remember_upload("part.gcode.3mf", local)
        assert server._local_copy_of("part.gcode.3mf") == local
        assert server._local_copy_of("/cache/part.gcode.3mf") == local

    def test_a_local_path_is_its_own_copy(self, tmp_path):
        from kiln import server

        local = _gcode(tmp_path, "here.gcode")
        assert server._local_copy_of(local) == local

    def test_unknown_names_are_none(self):
        from kiln import server

        assert server._local_copy_of("never_uploaded.gcode.3mf") is None
        assert server._local_copy_of(None) is None

    def test_memory_is_bounded(self, tmp_path):
        from kiln import server

        local = _gcode(tmp_path, "x.gcode")
        for i in range(server._UPLOADED_FROM_MAX + 10):
            server._remember_upload(f"f{i}.gcode", local)
        assert len(server._UPLOADED_FROM) == server._UPLOADED_FROM_MAX


class TestApprovalDialogSaysTheRouting:
    def test_the_line_names_each_colour_and_slot(self, tmp_path, monkeypatch):
        from kiln import server

        adapter = MagicMock()
        adapter.get_ams_status.return_value = JAR_NIGHT_AMS
        monkeypatch.setattr(server, "_resolve_adapter", lambda name=None: adapter)
        local = _gcode(tmp_path)
        server._remember_upload("jar.gcode.3mf", local)
        line = server._consent_filament_line("start_print", "jar.gcode.3mf", None)
        assert line == "white PLA → slot 1, red PLA → slot 4, black PLA → slot 2"

    def test_a_missing_colour_is_said_loudly(self, tmp_path, monkeypatch):
        from kiln import server

        adapter = MagicMock()
        adapter.get_ams_status.return_value = JAR_NIGHT_AMS
        monkeypatch.setattr(server, "_resolve_adapter", lambda name=None: adapter)
        p = tmp_path / "blue.gcode"
        p.write_text("G28\n; filament_colour = #FFFFFF;#0000FF\n; filament_type = PLA;PLA\n")
        line = server._consent_filament_line("slice_and_print", str(p), None)
        assert line.startswith("MISSING COLOUR")

    def test_an_unreadable_file_adds_no_line(self, monkeypatch):
        from kiln import server

        monkeypatch.setattr(server, "_resolve_adapter", lambda name=None: MagicMock())
        assert server._consent_filament_line("start_print", "unknown.gcode.3mf", None) is None

    def test_a_failing_adapter_never_breaks_the_dialog(self, tmp_path, monkeypatch):
        from kiln import server

        def boom(name=None):
            raise RuntimeError("printer offline")

        monkeypatch.setattr(server, "_resolve_adapter", boom)
        assert server._consent_filament_line("slice_and_print", _gcode(tmp_path), None) is None

    def test_describe_print_request_carries_the_line(self):
        from kiln.print_consent import describe_print_request

        msg = describe_print_request(
            "start_print", file_name="jar.gcode.3mf", printer_name=None,
            extra={"filament slots": "white → slot 1, red → slot 4"},
        )
        assert "Filament slots: white → slot 1, red → slot 4" in msg


class TestStageShowsWhatAPreviewMade:
    def test_a_preview_mesh_beats_the_input_it_was_made_from(self):
        from kiln import stage_link

        got = stage_link.find_mesh_path(
            {"success": True, "mesh_path": "/t/in.stl", "preview_3mf_path": "/t/painted.3mf"}
        )
        assert got == "/t/painted.3mf"

    def test_a_preview_key_alone_is_found(self):
        from kiln import stage_link

        assert stage_link.find_mesh_path({"preview_3mf_path": "/t/p.3mf"}) == "/t/p.3mf"


def _unused():  # keeps io imported for readers that patch it
    return io
