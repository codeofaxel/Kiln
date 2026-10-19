"""The colour a make was shown in is kept at the print step.

A part shown in teal on the stage used to print in whatever spool happened
to be active, with nothing said.  Now the colour travels as a note beside
the file (``kiln.declared_color``), the slicer and the 3MF wrap carry it,
and the one AMS resolver every print door goes through reads it: a loaded
spool of that colour is used and named; none loaded and the print is HELD
with the nearest spool named; a printer with no unit Kiln can read prints
whatever is loaded and the result says the colour was how the part was shown.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiln import declared_color as dc

AMS = {
    "success": True,
    "units": [
        {
            "unit_id": "0",
            "trays": [
                {"slot": "0", "tray_type": "PLA", "tray_color": "FFFFFFFF"},
                {"slot": "1", "tray_type": "PLA", "tray_color": "00A000FF"},
                {"slot": "2", "tray_type": "PLA", "tray_color": "008080FF"},
            ],
        }
    ],
}
AMS_NO_TEAL = {
    "success": True,
    "units": [
        {
            "unit_id": "0",
            "trays": [
                {"slot": "0", "tray_type": "PLA", "tray_color": "FFFFFFFF"},
                {"slot": "1", "tray_type": "PLA", "tray_color": "00A000FF"},
            ],
        }
    ],
}
# A one-filament slice as PrusaSlicer writes it: the slicer's default white.
SINGLE_TAIL = (
    "; CONFIG_BLOCK_START\n"
    "; filament_colour = #FFFFFF\n"
    "; filament_type = PLA\n"
    "; CONFIG_BLOCK_END\n"
)


def _gcode(tmp_path: Path, name: str = "part.gcode") -> str:
    p = tmp_path / name
    p.write_text("G28\nG1 X1 Y1\n" + SINGLE_TAIL)
    return str(p)


def _adapter(ams):
    adapter = MagicMock()
    adapter.get_ams_status.return_value = ams
    return adapter


# ---------------------------------------------------------------------------
# Names and the note
# ---------------------------------------------------------------------------


class TestColourWords:
    def test_css_names_and_hex_resolve_and_nonsense_does_not(self):
        assert dc.hex_for("teal") == "#008080"
        assert dc.hex_for("Dark Slate Gray") == "#2f4f4f"
        assert dc.hex_for("#ABC") == "#aabbcc"
        assert dc.hex_for("2aa198") == "#2aa198"
        assert dc.hex_for("sparkly unicorn") is None
        assert dc.hex_for("") is None
        assert len(dc.CSS_NAMED_COLOURS) == 148


class TestTheNoteBesideTheFile:
    def test_record_read_carry_and_forget(self, tmp_path):
        part = tmp_path / "part.stl"
        part.write_bytes(b"\x00" * 100)
        assert dc.record(str(part), dc.ShownColour("teal", "#008080")) is not None
        got = dc.read(str(part))
        assert got == dc.ShownColour("teal", "#008080", "caller")
        sliced = tmp_path / "part.gcode"
        sliced.write_text("G28\n")
        dc.carry(str(part), str(sliced))
        assert dc.read(str(sliced)).hex6 == "#008080"
        dc.forget(str(sliced))
        assert dc.read(str(sliced)) is None

    def test_a_note_for_other_bytes_is_not_believed(self, tmp_path):
        part = tmp_path / "part.stl"
        part.write_bytes(b"\x00" * 100)
        dc.record(str(part), dc.ShownColour("teal", "#008080"))
        part.write_bytes(b"\x01" * 100)  # the part was remade
        assert dc.read(str(part)) is None

    def test_attach_records_the_note_when_the_colour_resolves(self, tmp_path):
        part = tmp_path / "part.stl"
        part.write_bytes(b"\x00" * 100)
        response: dict = {}
        assert dc.attach_declared_color(response, "teal", file_path=str(part)) == "teal"
        assert response["color"]["hex"] == "#008080"
        assert dc.read(str(part)).shown == "teal"
        # A word Kiln cannot resolve is still recorded on the result, as a
        # word, but leaves no note: there is no hex to keep a promise with.
        other = tmp_path / "other.stl"
        other.write_bytes(b"\x00" * 100)
        response2: dict = {}
        dc.attach_declared_color(response2, "sparkly unicorn", file_path=str(other))
        assert response2["color"]["shown"] == "sparkly unicorn"
        assert "hex" not in response2["color"]
        assert dc.read(str(other)) is None


# ---------------------------------------------------------------------------
# The resolver every print door goes through
# ---------------------------------------------------------------------------


class TestTheResolverKeepsThePromise:
    def test_a_loaded_spool_of_the_asked_colour_is_used_and_named(self, tmp_path):
        from kiln.server import _resolve_use_ams

        gcode = _gcode(tmp_path)
        dc.record(gcode, dc.ShownColour("teal", "#008080"))
        got = _resolve_use_ams("auto", None, _adapter(AMS), material="PLA", file_path=gcode)
        assert got["use_ams"] is True
        assert got["ams_mapping"] == [2]
        assert got["colour"]["outcome"] == "loaded"
        assert got["colour"]["asked"] == "teal"
        assert "Printing in teal" in got["colour"]["message"]
        assert "blocked" not in got

    def test_no_spool_of_that_colour_holds_the_print_and_names_the_nearest(self, tmp_path):
        from kiln.server import _resolve_use_ams

        gcode = _gcode(tmp_path)
        dc.record(gcode, dc.ShownColour("teal", "#008080"))
        got = _resolve_use_ams("auto", None, _adapter(AMS_NO_TEAL), material="PLA", file_path=gcode)
        assert got["blocked"] is True
        assert got["code"] == "COLOR_NOT_LOADED"
        block = got["colour"]
        assert block["outcome"] == "held"
        assert block["nearest"]  # the green spool, by the same matcher the gate uses
        assert "No teal spool is loaded" in block["message"]
        assert 'color="any"' in block["message"]
        assert got["warnings"][0] == block["message"]

    def test_without_a_note_a_single_colour_file_still_prints_as_before(self, tmp_path):
        from kiln.server import _resolve_use_ams

        got = _resolve_use_ams("auto", None, _adapter(AMS_NO_TEAL), material="PLA", file_path=_gcode(tmp_path))
        # The slicer's default white matches the white spool; nothing is held
        # and no colour block is claimed, because nobody asked for a colour.
        assert "blocked" not in got
        assert "colour" not in got

    def test_a_printer_with_no_unit_says_the_colour_was_a_picture(self, tmp_path):
        from kiln.server import _resolve_use_ams

        gcode = _gcode(tmp_path)
        dc.record(gcode, dc.ShownColour("teal", "#008080"))
        no_ams = _adapter({"success": True, "units": [], "ams_exist_bits": "0", "tray_exist_bits": "0"})
        got = _resolve_use_ams("auto", None, no_ams, material="PLA", file_path=gcode)
        assert got["use_ams"] is False
        assert got["colour"]["outcome"] == "no_unit"
        assert "how the part was shown" in got["colour"]["message"]
        assert got["warnings"] == [got["colour"]["message"]]


# ---------------------------------------------------------------------------
# The wrap says the asked colour, and the note follows it
# ---------------------------------------------------------------------------


class TestTheWrapCarriesTheColour:
    def test_the_3mf_declares_the_asked_colour_over_the_loaded_one(self, tmp_path):
        from kiln.plugins import slicer_tools as st

        gcode = _gcode(tmp_path)
        dc.record(gcode, dc.ShownColour("teal", "#008080"))
        seen: dict = {}

        def fake_build(gcode_body, threemf_path, *, settings, **kwargs):
            seen["colors"] = settings.filament_colors
            Path(threemf_path).write_bytes(b"PK")
            return {"start_gcode_source": "kiln"}

        trimesh = pytest.importorskip("trimesh")
        stl = tmp_path / "part.stl"
        trimesh.creation.box(extents=(10, 10, 10)).export(stl)
        loaded = MagicMock()
        loaded.active_filament_color.return_value = "#FF0000"
        with patch("kiln.printers.bambu_3mf.build_bambu_3mf", side_effect=fake_build), \
             patch("kiln.server._get_adapter", return_value=loaded):
            threemf, _note = st._auto_wrap_bambu_3mf(gcode, "bambu_a1", str(stl))
        assert seen["colors"] == ["#008080"]
        assert threemf and dc.read(threemf).hex6 == "#008080"
