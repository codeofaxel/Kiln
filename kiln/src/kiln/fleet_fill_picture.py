"""The picture of a copies plan across printers: one top-down plate per printer.

Drawn from the plan itself (``fleet_fill/1``) and what this install knows of
each printer: its bed, and the parts its plate record says are on it.  Each
plate is to scale, front edge at the bottom; parts already there are solid
grey, the copies that print now are outlined at the spots the plan gives, and
the line under each plate says how many print now and how many follow.
Printers that need the person come first (a plate nobody has looked at, a
machine marked for repair), then the ones printing, then the ones the run does
not need.  Nothing is drawn of why a spot was not used: the picture shows
where copies go, never where they cannot.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)

# The stage's own backdrop (kiln.stage_paint), so the plan reads as Kiln's.
_BG = (0x1A, 0x22, 0x2D)
_BED = (0x26, 0x30, 0x3D)
_BED_EDGE = (0x45, 0x52, 0x63)
_PART_ON_PLATE = (0x8A, 0x94, 0xA3)
_COPY_EDGE = (0xF2, 0x9A, 0x3A)
_COPY_FILL = (0x5A, 0x3E, 0x24)
_TEXT = (0xEE, 0xF1, 0xF5)
_TEXT_DIM = (0xA4, 0xAE, 0xBB)
_NEEDS_YOU = (0xF2, 0xC1, 0x4E)

_TILE_W, _BED_BOX, _LABEL_H, _NOTE_H = 280, 220, 44, 104
_NOTE_LINES = 5
_TILE_H = _LABEL_H + _BED_BOX + _NOTE_H
_GAP, _MARGIN, _HEADER_H, _SECTION_H = 18, 28, 98, 40
_MAX_COLUMNS = 6

#: What an unrecorded plate says on the picture.
_NO_RECORD = (
    "Kiln has no record of what is on this plate. Take anything off and run kiln plate clear, "
    "or tell your assistant the plate is clear."
)

#: A reason the run did not use a printer that is not the person's to fix.
_NOT_NEEDED = "the other printers take every copy"


def draw(
    plan: Mapping[str, Any],
    *,
    part_size_mm: Sequence[float] | None,
    beds: Mapping[str, Sequence[float] | None],
    on_plate: Mapping[str, Sequence[Sequence[float]]] | None = None,
    models: Mapping[str, str] | None = None,
    printing_seconds: Mapping[str, float] | None = None,
    finish: str = "",
    title: str = "",
    out_path: str | None = None,
) -> str:
    """Draw *plan* and return the PNG's path.

    *beds* is each printer's bed ``[width, depth]`` in mm (``None`` when Kiln
    does not know it: the plate is drawn as a square and labelled so);
    *on_plate* each printer's parts already there, as footprints
    ``[x0, y0, x1, y1]``; *part_size_mm* the part's ``[x, y, ...]``; *models*
    the model each printer is declared as; *printing_seconds* each
    printer's rough printing time for the run.  Raises only what Pillow raises.
    """
    from PIL import Image, ImageDraw

    from kiln._fonts import load_font

    sections = _sections(plan)
    tiles = sum(len(rows) for _name, rows in sections)
    longest = max((len(rows) for _n, rows in sections), default=1)
    columns = max(1, -(-longest // -(-longest // _MAX_COLUMNS)))  # the fewest rows, then balanced
    width = _MARGIN * 2 + columns * _TILE_W + (columns - 1) * _GAP
    height = _MARGIN * 2 + _HEADER_H
    for _name, rows in sections:
        lines = -(-len(rows) // columns)
        height += _SECTION_H + lines * _TILE_H + max(0, lines - 1) * _GAP + _GAP
    img = Image.new("RGB", (width, max(height, 200)), _BG)
    d = ImageDraw.Draw(img)
    big, mid, small = load_font(26, bold=True), load_font(17, bold=True), load_font(15)

    y = _MARGIN
    d.text((_MARGIN, y), title or "Copies across your printers", font=big, fill=_TEXT)
    y += 36
    for line in [str(plan.get("summary") or ""), finish][: 2 if finish else 1]:
        d.text((_MARGIN, y), _fit(d, line, small, width - 2 * _MARGIN), font=small, fill=_TEXT_DIM)
        y += 22
    y = _MARGIN + _HEADER_H

    for name, rows in sections:
        d.text((_MARGIN, y + 8), name, font=mid, fill=_NEEDS_YOU if name == "Needs you" else _TEXT)
        y += _SECTION_H
        for i, row in enumerate(rows):
            col, line = i % columns, i // columns
            x0 = _MARGIN + col * (_TILE_W + _GAP)
            y0 = y + line * (_TILE_H + _GAP)
            _tile(d, x0, y0, row, part_size_mm, beds, on_plate or {}, models or {}, mid, small, printing_seconds or {})
        lines = -(-len(rows) // columns)
        y += lines * _TILE_H + max(0, lines - 1) * _GAP + _GAP

    if out_path is None:
        fd, out_path = tempfile.mkstemp(prefix="kiln_copies_plan_", suffix=".png")
        os.close(fd)
    img.save(out_path, "PNG")
    logger.debug("copies plan picture: %d plates -> %s", tiles, out_path)
    return out_path


def _sections(plan: Mapping[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    """``[(heading, rows)]``: the printers that need the person, those that
    print, those the run does not need; empty sections left out."""
    printing = [{**p, "kind": "printing"} for p in plan.get("printers") or []]
    needs, idle = [], []
    for p in plan.get("not_used") or []:
        why = [str(w) for w in p.get("why") or []]
        (idle if why == [_NOT_NEEDED] else needs).append({**p, "kind": "needs_you" if why != [_NOT_NEEDED] else "idle"})
    out = [("Needs you", needs), ("Printing", printing), ("Not needed for this run", idle)]
    return [(name, rows) for name, rows in out if rows]


def _tile(
    d: Any, x0: int, y0: int, row: Mapping[str, Any], part: Sequence[float] | None,
    beds: Mapping[str, Sequence[float] | None], on_plate: Mapping[str, Sequence[Sequence[float]]],
    models: Mapping[str, str], mid: Any, small: Any, printing_seconds: Mapping[str, float],
) -> None:
    name = str(row.get("printer") or "")
    d.text((x0, y0), _fit(d, name, mid, _TILE_W), font=mid, fill=_TEXT)
    d.text((x0, y0 + 22), _fit(d, _model_words(models.get(name) or ""), small, _TILE_W), font=small, fill=_TEXT_DIM)

    bed = beds.get(name)
    bw, bd = (float(bed[0]), float(bed[1])) if bed and len(bed) >= 2 and bed[0] and bed[1] else (1.0, 1.0)
    scale = min(_TILE_W / bw, _BED_BOX / bd)
    pw, ph = bw * scale, bd * scale
    ox, oy = x0 + (_TILE_W - pw) / 2, y0 + _LABEL_H + (_BED_BOX - ph) / 2
    d.rectangle([ox, oy, ox + pw, oy + ph], fill=_BED, outline=_BED_EDGE, width=2)

    def box(rect: Sequence[float]) -> list[float]:
        # Top-down, the printer's front edge at the bottom of the picture.
        rx0, ry0, rx1, ry1 = (float(v) for v in rect)
        return [ox + rx0 * scale, oy + (bd - ry1) * scale, ox + rx1 * scale, oy + (bd - ry0) * scale]

    if bed:
        for rect in on_plate.get(name) or []:
            if len(rect) == 4:
                d.rectangle(box(rect), fill=_PART_ON_PLATE)
        if part and len(part) >= 2:
            for spot in row.get("spots") or []:
                rect = [spot[0], spot[1], spot[0] + float(part[0]), spot[1] + float(part[1])]
                d.rectangle(box(rect), fill=_COPY_FILL, outline=_COPY_EDGE, width=2)

    note_y = y0 + _LABEL_H + _BED_BOX + 8
    if row.get("kind") == "printing":
        now, later, plates = int(row.get("now") or 0), int(row.get("later") or 0), int(row.get("later_plates") or 0)
        first = f"{now} now" + (" beside what is there" if row.get("status") == "occupied" else "")
        d.text((x0, note_y), _fit(d, first, small, _TILE_W), font=small, fill=_COPY_EDGE)
        lines = []
        if later:
            lines.append(f"{later} later, in {plates} more plate{'s' if plates != 1 else ''}")
        seconds = printing_seconds.get(name)
        if seconds:
            lines.append(f"about {_hours(seconds)} of printing")
        if not bed:
            lines.append("bed size not known to Kiln")
        for i, line in enumerate(lines):
            d.text((x0, note_y + 20 * (i + 1)), line, font=small, fill=_TEXT_DIM)
        return
    why = " ".join(str(w) for w in row.get("why") or [])
    if row.get("status") == "unknown":
        # The full sentence is in the plan for the agent; the plate says the one thing to do.
        why = _NO_RECORD
    colour = _NEEDS_YOU if row.get("kind") == "needs_you" else _TEXT_DIM
    for i, line in enumerate(_wrap(d, why[:1].upper() + why[1:], small, _TILE_W)):
        d.text((x0, note_y + i * 18), line, font=small, fill=colour)


def _wrap(d: Any, text: str, font: Any, width: float) -> list[str]:
    words, lines, line = text.split(), [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if d.textlength(trial, font=font) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    if len(lines) > _NOTE_LINES:
        lines = lines[:_NOTE_LINES]
        lines[-1] = _fit(d, lines[-1] + " ...", font, width)
    return lines


def _hours(seconds: float) -> str:
    """The same words the plan's sentence uses for a duration."""
    from kiln.generation.validation import _human_duration

    return _human_duration(seconds)


def _model_words(model: str) -> str:
    """``bambu_p1s`` as a person reads it: ``Bambu P1S``."""
    return " ".join(w.upper() if any(c.isdigit() for c in w) else w.capitalize() for w in model.split("_") if w)


def _fit(d: Any, text: str, font: Any, width: float) -> str:
    if d.textlength(text, font=font) <= width:
        return text
    while text and d.textlength(text + "...", font=font) > width:
        text = text[:-1]
    return text + "..."
