"""The printer's OWN screen gets the QOI block it reads, beside the web UI's PNG.

``test_every_printers_file_leaves_complete`` holds the web-UI half of the
rule: Mainsail, Fluidd, OctoPrint, PrusaLink's web view and Duet Web
Control draw a PNG ``; thumbnail begin`` block.  Two screens read something
else, established from their firmware rather than from habit:

* **Prusa MK4 / MK4S / MK3.5 / MK3.9 / XL / Core One / MINI** —
  Prusa-Firmware-Buddy 5.1.0 (2023-11-23): "QOI instead of PNG (XL, MK4,
  MINI) ... all the G-codes sliced until now, won't have a visible
  thumbnail on firmware 5.1.0 or newer."  At v6.10.2 the reader matches
  the exact prefix ``"; thumbnail_QOI begin "``, parses ``WxH N`` with
  ``%hux%hu%lu``, takes the type and BOTH dimensions exactly, searches the
  first 2048 lines, and counts only base64 characters against ``N``,
  skipping CR, LF, space and ``;``.  The 480x320 display (xBuddy/XLBuddy
  boards) reads a 313x173 preview and a 480x240 progress picture with
  440x240 as its fallback; the 240x320 MINI display reads 220x124 and
  240x240 with 200x240 as fallback.  All three are asked for as
  ``ImgType::QOI``.
* **Duet's PanelDue** — PanelDueFirmware 3.7.0 accepts only QOI and keeps
  the largest QOI that fits its file-dialog field: 161x161 on the 480x272
  panel and 263x245 on the 800x480 panels.  RepRapFirmware 3.6.3 hands it
  the first four thumbnail blocks in the file (``MaxThumbnails``), each
  16..500 pixels a side.

The block itself is PrusaSlicer's (2.9.4), tag ``thumbnail_QOI``, in the
shape its emitter writes.

NOT verified on hardware: the owner has no Prusa and no Duet.  Every
assertion here is against the readers' own source, at the versions named.
"""

from __future__ import annotations

import base64
import re
import struct
from pathlib import Path
from unittest import mock

import pytest

from kiln.printers import gcode_complete, qoi
from kiln.printers.base import PrinterError, PrinterInfo
from tests.test_every_printers_file_leaves_complete import (
    _Fake,
    _moves_of,
    png_bytes,
    raw_gcode,
)

LARGE = {(313, 173), (480, 240), (440, 240)}
MINI = {(220, 124), (240, 240), (200, 240)}
PANELDUE = {(256, 192), (160, 120)}

_QOI_BEGIN = re.compile(r"^; thumbnail_QOI begin (\d+)x(\d+) (\d+)$", re.MULTILINE)
_ANY_BEGIN = re.compile(r"^; thumbnail(?:_[A-Za-z0-9]+)? begin (\d+)x(\d+) (\d+)$", re.MULTILINE)


class _Declared(_Fake):
    """A backend whose owner wrote ``printer_model`` in config.yaml."""

    def __init__(self, family: str, model: str | None = None, *, reported: str | None = None, **kw):
        super().__init__(family, **kw)
        if model:
            self._printer_model = model
        self._reported = reported

    def get_printer_info(self):
        return PrinterInfo(model=self._reported, raw_model=self._reported, source="http") if self._reported else None


def complete(path: str, family: str | None, model: str | None = None, **kw) -> str:
    kw.setdefault("preview_png", png_bytes(400, 300))
    return gcode_complete.complete_gcode_for_printer(path, family, printer_model=model, **kw)


def text_of(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def qoi_sizes(text: str) -> set[tuple[int, int]]:
    return set(gcode_complete.qoi_thumbnails(text))


def _block_order(text: str) -> list[tuple[str, int, int]]:
    return [
        (m.group(1) or "", int(m.group(2)), int(m.group(3)))
        for m in re.finditer(r"^; thumbnail(_QOI)? begin (\d+)x(\d+) \d+$", text, re.MULTILINE)
    ]


# ---------------------------------------------------------------------------
# The block, byte for byte, against the readers that judge it
# ---------------------------------------------------------------------------


class TestQoiBlockFormat:
    def test_the_header_is_prusaslicers_own_shape(self, tmp_path):
        """``"\\n;\\n; %s begin %dx%d %d\\n"`` with tag ``thumbnail_QOI``,
        rows of ``"; "`` + up to 78 characters, ``"; %s end\\n;\\n"``."""
        path = raw_gcode(tmp_path)
        complete(path, "prusalink", "prusa_mk4")
        text = text_of(path)
        match = _QOI_BEGIN.search(text)
        assert match, "no QOI block"
        n = int(match.group(3))
        start = match.start()
        assert text[start - 3:start] == "\n;\n"
        block_end = text.index("; thumbnail_QOI end\n;\n", start)
        rows = text[match.end() + 1:block_end].split("\n")
        assert rows[-1] == ""
        rows = rows[:-1]
        assert all(row.startswith("; ") and len(row) <= 80 for row in rows), rows[:2]
        assert all(len(row) == 80 for row in rows[:-1])
        assert sum(len(row) - 2 for row in rows) == n

    def test_the_buddy_firmwares_own_reader_finds_it(self, tmp_path):
        """Exactly what ``PlainGcodeReader`` does: the literal prefix, a
        ``%hux%hu%lu`` parse, base64 read one character at a time skipping
        CR/LF/space/';', counted against the declared size."""
        path = raw_gcode(tmp_path)
        complete(path, "prusalink", "prusa_mk4")
        lines = text_of(path).splitlines()
        found: dict[tuple[int, int], bytes] = {}
        for i, line in enumerate(lines):
            if not line.startswith("; thumbnail_QOI begin "):
                continue
            rest = line[len("; thumbnail_QOI begin "):]
            m = re.fullmatch(r"(\d+)x(\d+)\s*(\d+)", rest)
            assert m, line
            width, height, declared = int(m.group(1)), int(m.group(2)), int(m.group(3))
            chars = []
            for body_line in lines[i + 1:]:
                if body_line.startswith("; thumbnail_QOI end"):
                    break
                chars.extend(c for c in body_line if c not in "\r\n ;")
            assert len(chars) == declared, (width, height, len(chars), declared)
            data = base64.b64decode("".join(chars))
            assert qoi.qoi_dimensions(data) == (width, height)
            found[(width, height)] = data
        assert set(found) == LARGE
        # bytes 4..11 of the header are all the screen reads before decoding
        for (w, h), data in found.items():
            assert struct.unpack(">II", data[4:12]) == (w, h)

    def test_reprapfirmwares_bounds_hold(self, tmp_path):
        """FileInfoParser: keyword exact after its first letter, 16..500 a
        side, size >= 10, and only the header before the first G/M/T line
        is parsed at all."""
        path = raw_gcode(tmp_path)
        complete(path, "duet")
        lines = text_of(path).splitlines()
        first_command = next(
            i for i, ln in enumerate(lines) if ln[:1] in ("G", "M", "T")
        )
        headers = [(i, ln) for i, ln in enumerate(lines) if _ANY_BEGIN.match(ln)]
        assert headers
        for i, ln in headers:
            assert i < first_command
            m = _ANY_BEGIN.match(ln)
            w, h, n = (int(g) for g in m.groups())
            assert 16 <= w <= 500 and 16 <= h <= 500 and n >= 10, ln
            assert ln.startswith("; thumbnail begin ") or ln.startswith("; thumbnail_QOI begin ")

    def test_duet_never_carries_more_blocks_than_the_firmware_stores(self, tmp_path):
        path = raw_gcode(tmp_path)
        complete(path, "duet")
        assert len(_ANY_BEGIN.findall(text_of(path))) <= 4

    def test_every_header_is_within_the_screens_2048_lines(self, tmp_path):
        path = raw_gcode(tmp_path)
        complete(path, "prusalink", "prusa_mk4")
        lines = text_of(path).splitlines()
        header_lines = [i for i, ln in enumerate(lines) if _ANY_BEGIN.match(ln)]
        assert header_lines and max(header_lines) < 2048

    def test_the_blocks_come_in_the_order_the_readers_budgets_dictate(self, tmp_path):
        """The screen's preview first (it is what the file is refused
        without), the web icons next, the large web PNG before the 32x32
        (Buddy's ``/thumb/l/`` takes the first PNG bigger than 16x16), and
        the progress pictures last, the 440x240 fallback last of all."""
        path = raw_gcode(tmp_path)
        complete(path, "prusalink", "prusa_mk4")
        assert _block_order(text_of(path)) == [
            ("_QOI", 313, 173), ("", 16, 16), ("", 400, 300), ("", 32, 32),
            ("_QOI", 480, 240), ("_QOI", 440, 240),
        ]

    def test_a_dense_toolpath_keeps_every_reader_inside_its_window(self, tmp_path):
        """A file nobody sliced through Kiln gets its picture drawn from its
        own moves, and a dense print draws a picture QOI compresses badly —
        several hundred lines a block.  The screen's own 2048-line search
        must still reach the preview
        the screen is refused without and the icons its web UI serves."""
        moves = ["M83\n"]
        for i in range(6000):
            moves.append(f"G1 X{10 + (i * 37) % 200:.2f} Y{10 + (i * 53) % 200:.2f} Z{0.2 + (i // 300) * 0.2:.2f} E0.8 F1800\n")
        path = tmp_path / "dense.gcode"
        path.write_text(
            "; generated by OrcaSlicer 2.3\n" + "".join(moves)
            + "; filament used [mm] = 4800.00\n; prusaslicer_config = begin\n; prusaslicer_config = end\n",
            encoding="utf-8",
        )
        gcode_complete.complete_gcode_for_printer(str(path), "prusalink", printer_model="prusa_mk4")
        counted = 0
        at: dict[tuple[str, int, int], int] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue  # stream_get_line skips an empty line without counting it
            counted += 1
            m = re.match(r"^; thumbnail(_QOI)? begin (\d+)x(\d+) \d+$", line)
            if m:
                at[(m.group(1) or "", int(m.group(2)), int(m.group(3)))] = counted
        assert at[("_QOI", 313, 173)] <= 2048
        assert at[("", 16, 16)] <= 2048
        assert at[("", 400, 300)] <= 2048
        assert at[("", 32, 32)] <= 2048
        assert at[("_QOI", 313, 173)] < at[("", 16, 16)] < at[("", 400, 300)] < at[("", 32, 32)]
        assert at[("_QOI", 440, 240)] == max(at.values())

    def test_a_qoi_block_is_rgba_srgb_like_prusaslicers(self, tmp_path):
        """``compress_thumbnail_qoi``: channels 4, colorspace QOI_SRGB."""
        path = raw_gcode(tmp_path)
        complete(path, "prusalink", "prusa_mini")
        text = text_of(path)
        m = _QOI_BEGIN.search(text)
        end = text.index("; thumbnail_QOI end", m.end())
        payload = "".join(ln[2:] for ln in text[m.end():end].splitlines() if ln.startswith("; "))
        header = base64.b64decode(payload)[:14]
        assert header[:4] == b"qoif"
        assert header[12:14] == b"\x04\x00"


# ---------------------------------------------------------------------------
# Each model's screen, by size
# ---------------------------------------------------------------------------


class TestEachPrusaModelsSizes:
    @pytest.mark.parametrize(
        "model", [
            "prusa_mk4", "prusa_mk4s", "prusa_mk3_5", "prusa_mk3_5s", "prusa_mk3_9",
            "prusa_mk3_9s", "prusa_xl", "prusa_ix", "prusa_core_one", "prusa_core_one_l",
        ],
    )
    def test_the_large_display_gets_313x173_and_both_progress_widths(self, tmp_path, model):
        path = raw_gcode(tmp_path)
        complete(path, "prusalink", model)
        assert qoi_sizes(text_of(path)) == LARGE

    def test_the_mini_display_gets_220x124_and_both_progress_widths(self, tmp_path):
        path = raw_gcode(tmp_path)
        complete(path, "prusalink", "prusa_mini")
        assert qoi_sizes(text_of(path)) == MINI

    def test_the_buddy_web_ui_gets_its_16x16_png_icon(self, tmp_path):
        """The Buddy web UI's file list takes its small icon from a PNG of
        exactly 16x16, and its large one from the first PNG bigger than
        that."""
        path = raw_gcode(tmp_path)
        complete(path, "prusalink", "prusa_mk4")
        sizes = gcode_complete.gcode_thumbnails(text_of(path))
        assert (16, 16) in sizes and (400, 300) in sizes

    @pytest.mark.parametrize("family", ["serial", "octoprint"])
    def test_a_transport_the_screen_never_sees_gets_no_screen_block(self, tmp_path, family):
        """A Buddy printer on USB (the serial adapter strips every comment)
        or behind OctoPrint (the file stays on the host and is streamed):
        the printer's screen never opens the file, so nothing is written
        for it and nothing is refused over it."""
        path = raw_gcode(tmp_path)
        complete(path, family, "prusa_mk4")
        assert "thumbnail_QOI" not in text_of(path)
        assert not any(
            "screen" in p for p in gcode_complete.gcode_problems(path, family, printer_model="prusa_mk4")
        )

    @pytest.mark.parametrize("model", ["prusa_mk3s", "prusa_mk3", "prusa_mk2_5", "prusa_mk2_5s"])
    def test_the_8bit_printers_have_no_qoi_screen(self, tmp_path, model):
        """PrusaSlicer's own MK3 profile writes ``160x120`` PNG and no QOI;
        nothing is owed and nothing is written."""
        path = raw_gcode(tmp_path)
        complete(path, "prusalink", model)
        assert qoi_sizes(text_of(path)) == set()

    @pytest.mark.parametrize(
        "written,screen", [
            ("Prusa MK4S", "buddy_large"), ("MK4", "buddy_large"), ("mk3.9s", "buddy_large"),
            ("Original Prusa MK3.5", "buddy_large"), ("prusa-mini", "buddy_mini"),
            ("Prusa MINI+", "buddy_mini"), ("Original Prusa XL", "buddy_large"),
            ("Prusa CORE One", "buddy_large"), ("CORE One L", "buddy_large"),
            ("Prusa iX", "buddy_large"), ("MK3S+", None), ("prusa_mk3s", None),
            ("Original Prusa i3 MK3S", None), ("voron_2", None), ("k1", None),
            ("Rat Rig V-Core 3", None), ("bambu_a1_mini", None), ("Neptune 4 XL", None),
            ("Ender 3 XL", None), ("xl", None), ("mini", None), ("MK4S 0.4 nozzle", "buddy_large"),
            ("prusa_xl_5t", "buddy_large"), ("visionminer_22idex_v4", None),
            ("", None), (None, None),
        ],
    )
    def test_the_model_as_people_write_it(self, written, screen):
        got = gcode_complete.screen_for_model(written)
        assert (got.key if got else None) == screen

    def test_the_slice_door_needs_only_the_model(self, tmp_path):
        """``slice_model`` knows the printer, not the software in front of
        it; the screen belongs to the machine, so the model alone is enough."""
        path = raw_gcode(tmp_path)
        complete(path, None, "prusa_mini")
        assert qoi_sizes(text_of(path)) == MINI

    def test_the_slice_door_serves_a_duet_machines_paneldue_from_the_catalogue(self, tmp_path):
        """A Duet-driven machine may carry a PanelDue whatever adapter is in
        front of it; the catalogue says which machines those are."""
        path = raw_gcode(tmp_path)
        complete(path, None, "visionminer_22idex_v4")
        assert qoi_sizes(text_of(path)) == PANELDUE
        assert gcode_complete.gcode_problems(path, None, printer_model="visionminer_22idex_v4") == []


class TestDuet:
    def test_the_paneldue_gets_a_picture_that_fits_every_panel(self, tmp_path):
        path = raw_gcode(tmp_path)
        complete(path, "duet")
        sizes = qoi_sizes(text_of(path))
        assert sizes == PANELDUE
        assert any(w <= 161 and h <= 161 for w, h in sizes), "the 4.3-inch panel gets nothing"
        assert all(w <= 263 and h <= 245 for w, h in sizes), "a block no PanelDue can draw"

    def test_paneldues_own_pick_lands_on_each_panel(self, tmp_path):
        """PanelDue's own pick, in the order RRF reports the blocks: a
        candidate replaces the pick when it is larger in both dimensions and
        still fits the field."""
        path = raw_gcode(tmp_path)
        complete(path, "duet")
        blocks = [
            (m.group(1) or "", int(m.group(2)), int(m.group(3)))
            for m in re.finditer(
                r"^; thumbnail(_QOI)? begin (\d+)x(\d+) \d+$", text_of(path), re.MULTILINE,
            )
        ][:4]
        for field, expected in (((161, 161), (160, 120)), ((263, 245), (256, 192))):
            pick = (0, 0)
            for tag, w, h in blocks:
                if tag != "_QOI":
                    continue
                if pick[1] < h <= field[1] and pick[0] < w <= field[0]:
                    pick = (w, h)
            assert pick == expected, (field, blocks)


# ---------------------------------------------------------------------------
# The check: a missing QOI is named only where a screen certainly reads it
# ---------------------------------------------------------------------------


def _png_only(tmp_path, family="prusalink") -> str:
    """A file with the web UI's PNG and nothing for the screen — what a
    PrusaSlicer export for the MK3, or Kiln's own output before this
    change, looks like."""
    path = raw_gcode(tmp_path)
    gcode_complete.complete_gcode_for_printer(path, family, preview_png=png_bytes(400, 300))
    assert gcode_complete.gcode_problems(path, family) == []
    return path


class TestTheCheck:
    def test_a_declared_buddy_model_names_the_missing_qoi_by_size(self, tmp_path):
        path = _png_only(tmp_path)
        problems = gcode_complete.gcode_problems(path, "prusalink", printer_model="prusa_mk4")
        assert any("thumbnail_QOI" in p and "313x173" in p for p in problems), problems
        problems = gcode_complete.gcode_problems(path, "prusalink", printer_model="prusa_mini")
        assert any("thumbnail_QOI" in p and "220x124" in p for p in problems), problems

    def test_the_other_displays_picture_does_not_count(self, tmp_path):
        """A MINI file on an MK4: the screen asks for 313x173 exactly."""
        path = raw_gcode(tmp_path)
        complete(path, "prusalink", "prusa_mini")
        assert gcode_complete.gcode_problems(path, "prusalink", printer_model="prusa_mini") == []
        problems = gcode_complete.gcode_problems(path, "prusalink", printer_model="prusa_mk4")
        assert any("313x173" in p for p in problems), problems

    def test_an_undeclared_model_is_not_refused_over_a_screen_kiln_cannot_see(self, tmp_path):
        path = _png_only(tmp_path)
        assert gcode_complete.gcode_problems(path, "prusalink") == []
        assert gcode_complete.gcode_problems(path, "prusalink", printer_model="") == []

    def test_the_8bit_printers_are_not_refused(self, tmp_path):
        path = _png_only(tmp_path)
        assert gcode_complete.gcode_problems(path, "prusalink", printer_model="prusa_mk3s") == []

    def test_a_paneldue_is_optional_so_duet_is_never_refused_over_it(self, tmp_path):
        path = _png_only(tmp_path, "duet")
        assert gcode_complete.gcode_problems(path, "duet") == []
        assert gcode_complete.gcode_problems(path, "duet", printer_model="visionminer_22idex_v4") == []

    def test_completion_clears_the_check(self, tmp_path):
        path = raw_gcode(tmp_path)
        assert gcode_complete.gcode_problems(path, "prusalink", printer_model="prusa_xl")
        complete(path, "prusalink", "prusa_xl")
        assert gcode_complete.gcode_problems(path, "prusalink", printer_model="prusa_xl") == []

    def test_a_file_that_prints_nothing_is_not_asked_for_a_screen_picture(self, tmp_path):
        path = tmp_path / "level.gcode"
        path.write_text("; generated by PrusaSlicer\nG28\nG29\n", encoding="utf-8")
        assert gcode_complete.gcode_problems(str(path), "prusalink", printer_model="prusa_mk4") == []

    def test_the_screen_entries_say_where_they_came_from(self):
        """Each screen names its reader and the version it was read at --
        the source by type, never the file and line it was read from."""
        for key, screen in gcode_complete.QOI_SCREENS.items():
            assert screen.key == key
            assert re.search(r"\b(?:Prusa-Firmware-Buddy|PanelDueFirmware) v?\d+\.\d+\.\d+\b", screen.evidence), key
            assert not re.search(r"\.(?:cpp|hpp|h)\b|\bL\d+", screen.evidence), key
            assert screen.preview in screen.sizes
        assert gcode_complete.QOI_SCREENS["paneldue"].refuses_missing is False
        assert gcode_complete.QOI_SCREENS["buddy_large"].refuses_missing is True


# ---------------------------------------------------------------------------
# Families with no QOI screen are untouched; so are the moves; so is Bambu
# ---------------------------------------------------------------------------


class TestNothingElseChanges:
    @pytest.mark.parametrize(
        "family,model", [
            ("moonraker", "voron_2"), ("moonraker", None), ("creality", "k1"),
            ("octoprint", "ender3"), ("elegoo", "elegoo_centauri_carbon"), (None, None),
        ],
    )
    def test_a_family_without_a_qoi_screen_gets_no_qoi_block(self, tmp_path, family, model):
        path = raw_gcode(tmp_path)
        complete(path, family, model)
        assert "thumbnail_QOI" not in text_of(path)
        assert gcode_complete.gcode_problems(path, family, printer_model=model) == []

    def test_every_non_comment_line_is_byte_identical(self, tmp_path):
        path = raw_gcode(tmp_path)
        before = text_of(path)
        complete(path, "prusalink", "prusa_mk4")
        assert _moves_of(text_of(path)) == _moves_of(before)

    def test_completion_is_idempotent(self, tmp_path):
        path = raw_gcode(tmp_path)
        complete(path, "prusalink", "prusa_mk4")
        once = Path(path).read_bytes()
        complete(path, "prusalink", "prusa_mk4")
        assert Path(path).read_bytes() == once

    def test_a_second_pass_adds_only_what_the_screen_still_lacks(self, tmp_path):
        """A file completed for the web UIs before this change, or exported
        by PrusaSlicer for the MK3, gets the screen's blocks and keeps
        every block and every move it already had."""
        path = _png_only(tmp_path)
        before = text_of(path)
        complete(path, "prusalink", "prusa_mk4")
        after = text_of(path)
        assert qoi_sizes(after) == LARGE
        assert after.count("; thumbnail begin 400x300") == 1
        assert _moves_of(after) == _moves_of(before)
        for block in re.findall(r"; thumbnail begin .*?; thumbnail end\n", before, re.DOTALL):
            assert block in after

    def test_a_block_already_on_line_one_is_never_written_into(self, tmp_path):
        """A file whose first line opens a thumbnail block gets the new
        blocks in front of it — every block it had still decodes."""
        path = _png_only(tmp_path)
        lines = text_of(path).splitlines(keepends=True)
        first_block = next(i for i, ln in enumerate(lines) if ln.startswith("; thumbnail begin"))
        Path(path).write_text("".join(lines[first_block:]), encoding="utf-8")
        before = set(gcode_complete.gcode_thumbnails(text_of(path)))
        complete(path, "prusalink", "prusa_mk4")
        after = text_of(path)
        assert set(gcode_complete.gcode_thumbnails(after)) == before | {(16, 16)} | LARGE
        assert qoi_sizes(after) == LARGE
        assert after.startswith("\n;\n; thumbnail_QOI begin 313x173 ")

    def test_the_bambu_family_is_left_to_its_own_check(self, tmp_path):
        path = raw_gcode(tmp_path)
        before = Path(path).read_bytes()
        assert gcode_complete.gcode_problems(path, "bambu", printer_model="bambu_a1") == []
        gcode_complete.complete_gcode_for_printer(path, "bambu", printer_model="bambu_a1")
        assert Path(path).read_bytes() == before


# ---------------------------------------------------------------------------
# Every door: upload, start by name, the slice steer, the three print doors
# ---------------------------------------------------------------------------


class TestEveryDoor:
    def test_the_upload_door_refuses_for_a_declared_buddy_model(self, tmp_path):
        path = _png_only(tmp_path)
        adapter = _Declared("prusalink", "prusa_mk4")
        with pytest.raises(PrinterError) as exc:
            adapter.upload_file(path)
        assert "thumbnail_QOI" in str(exc.value) and "313x173" in str(exc.value)
        assert adapter.uploaded == []

    def test_the_upload_door_reads_the_declared_model_not_the_self_report(self, tmp_path):
        """A refusal keys off config alone; a self-report only fills in
        where config is silent, and never for a refusal."""
        path = _png_only(tmp_path)
        assert _Declared("prusalink", None, reported="prusa_mk4").upload_file(path).success

    def test_a_completed_file_leaves(self, tmp_path):
        path = raw_gcode(tmp_path)
        complete(path, "prusalink", "prusa_mk4")
        adapter = _Declared("prusalink", "prusa_mk4")
        assert adapter.upload_file(path).success

    def test_the_start_by_name_door_reads_the_printers_copy(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
        raw = Path(_png_only(tmp_path)).read_bytes()
        adapter = _Declared("prusalink", "prusa_mini", printer_copy=raw)
        result = adapter.start_print("part.gcode")
        assert result.success is False, result.message
        assert "220x124" in result.message
        assert adapter.started == []

    def test_the_slice_steer_serves_the_screen_of_the_printer_it_sliced_for(self, tmp_path):
        from kiln.plugins import slicer_tools

        path = raw_gcode(tmp_path)
        response: dict = {"output_path": path}
        with mock.patch.object(gcode_complete, "_render_preview", return_value=png_bytes(400, 300)):
            slicer_tools._steer_to_complete_gcode(response, path, "prusa_mk4", None)
        assert response["recommended_upload_path"] == path
        assert qoi_sizes(text_of(path)) == LARGE
        assert gcode_complete.gcode_problems(path, "prusalink", printer_model="prusa_mk4") == []

    def test_the_print_doors_use_the_declared_model(self, tmp_path):
        from kiln.printers.upload_prep import prepare_upload_for_adapter

        path = raw_gcode(tmp_path)
        adapter = _Declared("prusalink", "prusa_mini")
        with mock.patch.object(gcode_complete, "_render_preview", return_value=png_bytes(400, 300)):
            upload_path, wrapped = prepare_upload_for_adapter(adapter, path)
        assert (upload_path, wrapped) == (path, False)
        assert qoi_sizes(text_of(path)) == MINI
        assert adapter.upload_file(upload_path).success

    def test_the_print_doors_let_the_self_report_fill_a_silent_config(self, tmp_path):
        from kiln.printers.upload_prep import prepare_upload_for_adapter

        path = raw_gcode(tmp_path)
        adapter = _Declared("prusalink", None, reported="prusa_xl")
        with mock.patch.object(gcode_complete, "_render_preview", return_value=png_bytes(400, 300)):
            prepare_upload_for_adapter(adapter, path)
        assert qoi_sizes(text_of(path)) == LARGE

    def test_the_declared_model_wins_over_the_self_report(self, tmp_path):
        adapter = _Declared("prusalink", "prusa_mini", reported="prusa_mk4")
        assert gcode_complete.printer_model_for_adapter(adapter) == "prusa_mini"
        assert gcode_complete.printer_model_for_adapter(_Declared("prusalink")) is None

    def test_the_bambu_wrap_path_is_untouched(self, tmp_path):
        from kiln.printers.upload_prep import prepare_upload_for_adapter

        path = raw_gcode(tmp_path)
        before = Path(path).read_bytes()

        class _Wrapping(_Declared):
            def wrap_gcode_as_3mf(self, gcode_path, **kwargs):
                return gcode_path + ".3mf"

        upload_path, wrapped = prepare_upload_for_adapter(_Wrapping("bambu", "bambu_a1"), path)
        assert wrapped is True and upload_path.endswith(".3mf")
        assert Path(path).read_bytes() == before
