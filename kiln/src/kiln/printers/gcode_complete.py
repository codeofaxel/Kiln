"""A raw G-code file leaves Kiln with its preview and its weight, or not at all.

The sibling of :mod:`kiln.printers.bambu_3mf`'s
:func:`~kiln.printers.bambu_3mf.complete_bambu_archive`.  A Bambu plate is
an archive and carries its picture as a member; every other family gets the
slicer's G-code, where the picture is a base64 comment block and the weight
is a comment line — and Kiln wrote neither.

WHAT EACH SURFACE READS, established from the surfaces themselves rather
than from habit.  Each entry in :data:`SURFACES` carries its own evidence
string; the short version:

* **Klipper / Moonraker** — Mainsail and Fluidd show what Moonraker
  extracted.  ``moonraker/components/file_manager/metadata.py`` matches
  ``(thumbnail(?:_[A-Za-z0-9]+)?) begin([;/\\+=\\w\\s]+?); \\1 end``, drops a
  block whose declared size is not the length of its base64, and makes its
  own 32x32 miniature when the file carries none.  Weight comes from
  ``total filament used [g]`` and ``filament used [g]``, read from the last
  MiB of the file; the thumbnail is read from the first.
* **OctoPrint** — the Slicer Thumbnails plugin matches
  ``^; thumbnail(?:_JPG)* begin \\d+[x ]\\d+ \\d+`` and stops reading at the
  first ``G1`` carrying an ``E`` word, so the block has to come before the
  first extrusion.  OctoPrint's own file list gets filament length and
  volume from a pass over the E moves (``util/gcodeInterpreter.py``), not
  from any comment — so no weight line is owed to it.
* **PrusaLink** — ``prusa3d/gcode-metadata`` matches
  ``; thumbnail_?(QOI|JPG|) begin (dim) (size)``, so a plain PNG block
  counts; it ignores anything smaller than 50x50, picks the image nearest
  640x480 for the preview and nearest 100x100 for the icon, and reads
  ``filament used [g]``.  (The MK4's own screen switched to QOI in
  firmware 5.1.0; Kiln writes PNG, which is what PrusaLink's web view
  reads.  The QOI slot is a known gap, not a silent one.)
* **Duet / RepRapFirmware** — ``src/Storage/FileInfoParser.cpp`` looks for
  ``; thumbnail begin 32x32 2140`` (PNG, QOI and JPG tags), ``ilament
  used`` in millimetres, and ``estimated printing time``.  PanelDue can
  only draw QOI, which Kiln does not write; Duet Web Control draws the PNG.
* **Elegoo (SDCP)** — what the Centauri Carbon's screen reads could not be
  established from a source Kiln can cite.  A preview is written anyway,
  because a comment block costs nothing, and its absence is never a
  refusal.
* **Serial / Marlin** — :meth:`SerialPrinterAdapter.upload_file` drops every
  comment line on the way to the SD card, so neither a picture nor a weight
  survives the trip.  Nothing is refused, and the completion fills the
  weight only, for the copy that stays on disk.

WHAT KILN WROTE BEFORE THIS.  Measured on this machine, 2026-09-19, with
PrusaSlicer 2.9.4: ``--thumbnails 160x120/PNG`` is accepted on the command
line and echoed into the config block, and no thumbnail block is emitted —
the renderer that draws them is not there in CLI mode.  The same slice
wrote ``; filament used [mm] = 1493.99`` and ``; total filament used [g] =
0.00``, with no ``; filament used [g]`` line at all, because Kiln's
profiles describe a printer and name no filament density (see
:func:`kiln.slicer.derive_filament_weight`).

THE FORMAT WRITTEN is PrusaSlicer's own, from its emitter
(``src/libslic3r/GCode/Thumbnails.hpp``, 2.9.4)::

    "\\n;\\n; %s begin %dx%d %d\\n"   # tag, width, height, len(base64)
    "; %s\\n"                        # up to 78 base64 characters per row
    "; %s end\\n;\\n"

Nothing but comments is ever added, changed or moved: the moves come out
byte for byte as the slicer wrote them.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: The sizes written when a file needs a picture.  ``32x32`` and ``400x300``
#: are what Mainsail's own slicer page asks for (the small one is the file
#: list tile, the large one the print-start dialog); 400x300 is also over
#: PrusaLink's 50x50 floor and shares the 4:3 aspect of the 640x480 it
#: scores previews against, so one pair serves every surface.
THUMBNAIL_SIZES: tuple[tuple[int, int], ...] = ((400, 300), (32, 32))

#: PrusaSlicer wraps the base64 at this many characters per row.
_MAX_ROW = 78

#: Extensions this check judges.  A ``.3mf`` belongs to
#: :func:`kiln.printers.bambu_3mf.bambu_archive_problems`; binary G-code
#: (``.bgcode``) carries its thumbnails in a block structure, not comments,
#: and is left alone rather than guessed at.
_GCODE_SUFFIXES = (".gcode", ".gco", ".g", ".gc")

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: How much of a file the CHECK reads.  A completion reads all of it; a
#: check runs on every upload and must not pull a 200 MB plate into memory.
#: The picture lives at the top (Moonraker reads the first MiB for it,
#: OctoPrint stops at the first extrusion) and the figures live at the
#: bottom (Moonraker's last MiB, PrusaLink's last 40 KB).
_HEAD_BYTES = 1024 * 1024
_TAIL_BYTES = 256 * 1024


@dataclass(frozen=True)
class GcodeSurface:
    """What one printer family's screen or web UI reads out of a G-code file.

    ``reads_thumbnail`` and ``weight_unit`` are what a file is REFUSED over.
    A family whose surface Kiln cannot see reads ``False`` / ``None`` and is
    never refused — a picture that surface would not draw is not a reason
    to keep a print from starting.
    """

    family: str
    #: What the person looks at, in their words, for the refusal message.
    surface: str
    reads_thumbnail: bool
    #: ``"g"``, ``"mm"``, or ``None`` when the surface works its own out.
    weight_unit: str | None
    #: Where the two answers above came from.
    evidence: str


SURFACES: dict[str, GcodeSurface] = {
    "moonraker": GcodeSurface(
        family="moonraker",
        surface="Mainsail/Fluidd's file list",
        reads_thumbnail=True,
        weight_unit="g",
        evidence=(
            "moonraker/components/file_manager/metadata.py: parse_thumbnails "
            "matches '(thumbnail(?:_[A-Za-z0-9]+)?) begin ... ; end' and checks the "
            "declared size against the base64 length; parse_filament_weight_total "
            "reads 'total filament used [g]'"
        ),
    ),
    "creality": GcodeSurface(
        family="creality",
        surface="the Creality printer's web UI",
        reads_thumbnail=True,
        weight_unit="g",
        evidence=(
            "kiln.printers.creality.CrealityAdapter talks to Moonraker (K1/K2/"
            "Ender-3 V3 run Klipper), so its surface reads what Moonraker "
            "extracts.  Creality's older non-Klipper screens read a '; jpg begin' "
            "block instead (OctoPrint-SlicerThumbnails regex_creality), which Kiln "
            "does not write and does not reach"
        ),
    ),
    "octoprint": GcodeSurface(
        family="octoprint",
        surface="OctoPrint's file list",
        reads_thumbnail=True,
        weight_unit=None,
        evidence=(
            "OctoPrint-SlicerThumbnails matches '^; thumbnail(_JPG)* begin WxH N' "
            "and stops at the first G1 with an E word; OctoPrint's own "
            "util/gcodeInterpreter.py sums the E moves for length and volume, so "
            "no weight comment is read"
        ),
    ),
    "prusalink": GcodeSurface(
        family="prusalink",
        surface="PrusaLink's file list",
        reads_thumbnail=True,
        weight_unit="g",
        evidence=(
            "prusa3d/gcode-metadata: THUMBNAIL_BEGIN_PAT accepts a plain PNG "
            "'; thumbnail begin', get_closest_image ignores anything under 50x50 "
            "and scores against 640x480 (preview) and 100x100 (icon); "
            "'filament used [g]' is one of its attributes"
        ),
    ),
    "duet": GcodeSurface(
        family="duet",
        surface="Duet Web Control's job list",
        reads_thumbnail=True,
        weight_unit="mm",
        evidence=(
            "RepRapFirmware src/Storage/FileInfoParser.cpp: FindThumbnails reads "
            "'; thumbnail begin 32x32 2140' (PNG/QOI/JPG); filament is parsed from "
            "'ilament used' in mm.  PanelDue draws QOI only, which Kiln does not "
            "write"
        ),
    ),
    "elegoo": GcodeSurface(
        family="elegoo",
        surface="the Elegoo printer's screen",
        reads_thumbnail=False,
        weight_unit=None,
        evidence=(
            "what the Centauri Carbon's SDCP screen reads out of a G-code file "
            "could not be established from a source Kiln can cite, so nothing is "
            "refused on its behalf; a preview is still written, best-effort"
        ),
    ),
    "serial": GcodeSurface(
        family="serial",
        surface="the printer's own LCD",
        reads_thumbnail=False,
        weight_unit=None,
        evidence=(
            "kiln.printers.serial_adapter.SerialPrinterAdapter.upload_file skips "
            "every line starting with ';' when writing to the SD card, so no "
            "comment — picture or weight — reaches the printer at all"
        ),
    ),
    # Bambu plates are archives, judged by bambu_archive_problems.  Present
    # so the family resolver has an answer for every shipped backend and so
    # this check knows to keep its hands off.
    "bambu": GcodeSurface(
        family="bambu",
        surface="the Bambu printer's screen",
        reads_thumbnail=False,
        weight_unit=None,
        evidence=(
            "a Bambu plate ships as a .gcode.3mf and is judged by "
            "kiln.printers.bambu_3mf.bambu_archive_problems"
        ),
    ),
}

#: The surface a file gets when nobody named a family — the slice door,
#: which knows the printer model but not the software in front of it.
#: Writes what every reader above accepts.
DEFAULT_FAMILY = "gcode"

_DEFAULT_SURFACE = GcodeSurface(
    family=DEFAULT_FAMILY,
    surface="the printer's file list",
    reads_thumbnail=True,
    weight_unit="g",
    evidence="the format every reader in SURFACES accepts",
)

#: Adapter class -> family.  Resolved by class rather than by
#: ``adapter.name`` alone because the serial adapter's name is whatever the
#: owner called their printer.
_CLASS_FAMILIES: dict[str, str] = {
    "BambuAdapter": "bambu",
    "CrealityAdapter": "creality",
    "MoonrakerAdapter": "moonraker",
    "OctoPrintAdapter": "octoprint",
    "PrusaLinkAdapter": "prusalink",
    "DuetAdapter": "duet",
    "ElegooAdapter": "elegoo",
    "SerialPrinterAdapter": "serial",
}


def family_for_adapter_class(cls: type) -> str | None:
    """The family *cls* belongs to, or ``None`` for a backend nobody mapped."""
    for base in getattr(cls, "__mro__", (cls,)):
        family = _CLASS_FAMILIES.get(base.__name__)
        if family:
            return family
    return None


def family_for_adapter(adapter: object) -> str | None:
    """The family *adapter* belongs to.

    The class comes first; a test double or a plugin-supplied backend that
    names itself after a family it speaks is taken at its word.  Anything
    else is ``None``, and an unrecognised backend is never refused — this
    gate, like the rest of the pre-upload check, soft-passes what it cannot
    establish.
    """
    family = family_for_adapter_class(type(adapter))
    if family:
        return family
    name = str(getattr(adapter, "name", "") or "").strip().lower()
    return name if name in SURFACES else None


def surface_for(family: str | None) -> GcodeSurface:
    """The surface record for *family*, falling back to the shared default."""
    if family is None:
        return _DEFAULT_SURFACE
    return SURFACES.get(family.strip().lower(), _DEFAULT_SURFACE)


# ---------------------------------------------------------------------------
# Reading what a file already carries
# ---------------------------------------------------------------------------

_THUMB_BEGIN_RE = re.compile(
    r"^;\s*thumbnail(?P<fmt>_[A-Za-z0-9]+)?\s+begin\s+(?P<w>\d+)x(?P<h>\d+)\s+(?P<size>\d+)\s*$",
    re.MULTILINE,
)


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != _PNG_MAGIC:
        return None
    return struct.unpack(">II", data[16:24])


def gcode_thumbnails(text: str) -> list[tuple[int, int]]:
    """The sizes of every thumbnail block in *text* a reader would accept.

    A block counts only when it passes what the readers themselves check:
    the declared length is the length of the base64 (Moonraker drops the
    block otherwise, logging ``Thumbnail Size Mismatch``), the base64
    decodes, and — for the PNG tag Kiln writes — the picture really is a
    PNG of the size the header claims.  A block that says 400x300 and holds
    a 32x32 is a broken tile, not a preview.
    """
    found: list[tuple[int, int]] = []
    for match in _THUMB_BEGIN_RE.finditer(text):
        tag = "thumbnail" + (match.group("fmt") or "")
        end = text.find(f"; {tag} end", match.end())
        if end < 0:
            continue
        rows = [
            line[1:].strip()
            for line in text[match.end():end].splitlines()
            if line.startswith(";")
        ]
        payload = "".join(rows)
        if len(payload) != int(match.group("size")):
            continue
        width, height = int(match.group("w")), int(match.group("h"))
        try:
            data = base64.b64decode(payload.encode("ascii"), validate=True)
        except (ValueError, UnicodeEncodeError):
            continue
        if not data:
            continue
        if match.group("fmt") is None and _png_dimensions(data) != (width, height):
            continue
        found.append((width, height))
    return found


_TOTAL_G_RE = re.compile(r"^;\s*total filament used \[g\]\s*=\s*(?P<v>[-\d., ]*)$", re.MULTILINE)
_USED_G_RE = re.compile(r"^;\s*filament used \[g\]\s*=\s*(?P<v>[-\d., ]*)$", re.MULTILINE)
_USED_MM_RE = re.compile(r"^;\s*filament used \[mm\]\s*=\s*(?P<v>[-\d., ]*)$", re.MULTILINE)
_CONFIG_BEGIN_RE = re.compile(r"^;\s*\w*slicer_config = begin\s*$", re.MULTILINE)


def _numbers(text: str | None) -> list[float]:
    if not text:
        return []
    out: list[float] = []
    for piece in re.split(r"[,;]", text):
        piece = piece.strip()
        if not piece:
            continue
        with contextlib.suppress(ValueError):
            out.append(float(piece))
    return out


def declared_grams(text: str) -> float:
    """The weight the file claims, in grams — 0.0 when it claims none."""
    for pattern in (_TOTAL_G_RE, _USED_G_RE):
        match = pattern.search(text)
        if match:
            total = sum(_numbers(match.group("v")))
            if total > 0:
                return total
    return 0.0


def declared_mm(text: str) -> float:
    """The filament length the file claims, in mm — 0.0 when it claims none."""
    match = _USED_MM_RE.search(text)
    return sum(_numbers(match.group("v"))) if match else 0.0


def _is_gcode(path: Path, name: str | None = None) -> bool:
    judged = Path(name or path.name).name.lower()
    if judged.endswith(".3mf"):
        return False
    return any(judged.endswith(suffix) for suffix in _GCODE_SUFFIXES)


def _head_and_tail(path: Path) -> str:
    """Enough of *path* for the check: the top and the bottom."""
    with open(path, "rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        fh.seek(0)
        head = fh.read(_HEAD_BYTES)
        if size > _HEAD_BYTES:
            fh.seek(max(size - _TAIL_BYTES, len(head)))
            tail = fh.read()
        else:
            tail = b""
    return head.decode("utf-8", errors="replace") + "\n" + tail.decode("utf-8", errors="replace")


def _usage(text: str):
    """What the file's own moves and comments say it consumes.

    Shared with the Bambu path on purpose: one reader decides what a plate
    weighs, whatever it is wrapped in.
    """
    from kiln.printers.bambu_3mf import filament_usage_from_gcode

    return filament_usage_from_gcode(text)


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def gcode_problems(
    path: str | os.PathLike[str],
    printer_family: str | None,
    *,
    name: str | None = None,
) -> list[str]:
    """Why *path* must not go to a *printer_family* printer — empty when it may.

    The sibling of :func:`kiln.printers.bambu_3mf.bambu_archive_problems`,
    and it answers in the same terms: what this family's surface would fail
    to show.  A family whose surface reads nothing Kiln can cite gets an
    empty list — a print is never refused over a picture the screen would
    not have drawn anyway.

    *name* is the printer-side name when *path* is a temp copy of it, so
    the extension judged is the one the printer sees.
    """
    p = Path(path)
    surface = surface_for(printer_family)
    if surface.family == "bambu":
        return []
    if not _is_gcode(p, name):
        return []
    if not surface.reads_thumbnail and surface.weight_unit is None:
        return []
    try:
        text = _head_and_tail(p)
    except OSError:
        # A file that cannot be read is the adapter's own error to raise --
        # its FileNotFoundError says more than a guess from here would.
        logger.debug("gcode completeness check could not read %s", p, exc_info=True)
        return []

    problems: list[str] = []
    # Everything below is judged against what completion WOULD write, read
    # once: a file whose real figures round to nothing is not incomplete,
    # it is empty.  A homing check or a bed-levelling macro is not a print
    # and is not held to a print's standard.
    usage = _usage(text)
    prints_something = round(usage.total_mm, 2) > 0
    if surface.reads_thumbnail and prints_something and not gcode_thumbnails(text):
        problems.append(
            f"no preview: {surface.surface} draws its tile from a "
            f"'; thumbnail begin' block and this file carries none"
        )
    if (
        surface.weight_unit == "g"
        and declared_grams(text) <= 0
        and round(usage.total_g, 2) > 0
    ):
        problems.append(
            f"no weight: {surface.surface} reads 'filament used [g]' and this "
            f"file says 0.00 though it extrudes filament"
        )
    if surface.weight_unit == "mm" and declared_mm(text) <= 0 and prints_something:
        problems.append(
            f"no length: {surface.surface} reads 'filament used [mm]' and this "
            f"file says nothing though it extrudes filament"
        )
    if problems:
        problems.append(
            "complete the file with kiln.printers.gcode_complete."
            "complete_gcode_for_printer (slice_model does this for the file it "
            "recommends); only comments change, the moves are untouched"
        )
    return problems


# ---------------------------------------------------------------------------
# The completion
# ---------------------------------------------------------------------------


def _resize_png(source: bytes, width: int, height: int) -> bytes | None:
    if _png_dimensions(source) == (width, height):
        return source
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(source)) as img:
            out = io.BytesIO()
            # RGBA throughout: a toolpath drawn on transparency sits on the
            # file list's own background, light theme or dark.  Converting
            # to RGB here would paint that transparency black.
            mode = "RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB"
            img.convert(mode).resize((width, height), Image.LANCZOS).save(out, format="PNG")
            return out.getvalue()
    except Exception:  # noqa: BLE001 — a missing size is a smaller tile, never a failure
        logger.warning("Could not resize the preview to %dx%d", width, height, exc_info=True)
        return None


#: How many extruding segments the fallback draws before it starts
#: subsampling.  A plate can be millions; a 400x300 tile cannot show them.
_MAX_SEGMENTS = 120_000

_XYZE_RE = re.compile(r"\b([XYZEF])\s*(-?\d*\.?\d+)")


def _extruding_segments(text: str) -> list[tuple[float, float, float, float, float, float]]:
    """The moves that lay filament down, as ``(x1, y1, z1, x2, y2, z2)``.

    Absolute E (``M82``) is differenced and reset by ``G92``; relative E
    (``M83``, what Kiln slices with) is read as written — the same reading
    :func:`kiln.printers.bambu_3mf.filament_usage_from_gcode` does for the
    weight, kept here because this one also needs the geometry.
    """
    segments: list[tuple[float, float, float, float, float, float]] = []
    x = y = z = 0.0
    last_e = 0.0
    relative_e = False
    absolute_xyz = True
    for line in text.splitlines():
        code = line.split(";", 1)[0].strip()
        if not code:
            continue
        word = code.split(None, 1)[0].upper()
        if word == "M83":
            relative_e = True
            continue
        if word == "M82":
            relative_e = False
            continue
        if word == "G90":
            absolute_xyz = True
            continue
        if word == "G91":
            absolute_xyz = False
            continue
        words = dict(_XYZE_RE.findall(code.upper()))
        if word == "G92":
            if "E" in words:
                last_e = float(words["E"])
            continue
        if word not in ("G0", "G1"):
            continue
        nx, ny, nz = x, y, z
        for axis, current in (("X", x), ("Y", y), ("Z", z)):
            if axis in words:
                value = float(words[axis])
                moved = value if absolute_xyz else current + value
                if axis == "X":
                    nx = moved
                elif axis == "Y":
                    ny = moved
                else:
                    nz = moved
        extruded = 0.0
        if "E" in words:
            value = float(words["E"])
            extruded = value if relative_e else value - last_e
            if not relative_e:
                last_e = value
        if extruded > 0 and (nx, ny) != (x, y):
            segments.append((x, y, z, nx, ny, nz))
        x, y, z = nx, ny, nz
    return segments


def _render_from_gcode(text: str, width: int, height: int) -> bytes | None:
    """A picture of the print drawn from the file's own extrusion moves.

    The fallback that keeps the refusal honest: a file Kiln did not slice —
    somebody's own Cura or Orca export — has no mesh to render, and without
    this the upload door would name a remedy its owner could not reach.
    What it draws is the toolpath itself, seen from the same isometric
    angle Kiln's other previews use, shaded by height so the shape reads at
    tile size.  ``None`` when the file lays nothing down.
    """
    segments = _extruding_segments(text)
    if not segments:
        return None
    if len(segments) > _MAX_SEGMENTS:
        stride = len(segments) // _MAX_SEGMENTS + 1
        segments = segments[::stride]
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.warning("Pillow unavailable — the file ships without a preview.")
        return None

    cos30, sin30 = 0.86602540378, 0.5

    def project(px: float, py: float, pz: float) -> tuple[float, float]:
        return ((px - py) * cos30, (px + py) * sin30 - pz)

    points = [project(*seg[:3]) for seg in segments] + [project(*seg[3:]) for seg in segments]
    us = [p[0] for p in points]
    vs = [p[1] for p in points]
    span_u = max(max(us) - min(us), 1e-6)
    span_v = max(max(vs) - min(vs), 1e-6)
    margin = 0.06
    scale = min(width * (1 - 2 * margin) / span_u, height * (1 - 2 * margin) / span_v)
    off_u = (width - span_u * scale) / 2 - min(us) * scale
    off_v = (height - span_v * scale) / 2 - min(vs) * scale

    z_lo = min(seg[2] for seg in segments)
    z_hi = max(max(seg[2], seg[5]) for seg in segments)
    z_span = max(z_hi - z_lo, 1e-6)

    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    stroke = max(1, round(min(width, height) / 160))
    for x1, y1, z1, x2, y2, z2 in segments:
        u1, v1 = project(x1, y1, z1)
        u2, v2 = project(x2, y2, z2)
        # Higher layers lighter, so the top surface reads as the top.
        shade = 0.35 + 0.65 * ((max(z1, z2) - z_lo) / z_span)
        colour = (round(70 + 150 * shade), round(90 + 140 * shade), round(120 + 120 * shade), 255)
        draw.line(
            (u1 * scale + off_u, height - (v1 * scale + off_v),
             u2 * scale + off_u, height - (v2 * scale + off_v)),
            fill=colour, width=stroke,
        )
    import io

    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _render_preview(
    model_path: str | None,
    colors: list[str] | None,
    width: int,
    height: int,
    *,
    gcode_text: str | None = None,
) -> bytes | None:
    """The picture of the plate, best source first.

    The mesh the file was sliced from goes through
    :func:`kiln.multicolor_3mf.render_plate_preview`, the same renderer the
    Bambu tile and the composed 3MF use, so the picture on a Klipper file
    list is the one the user already saw rather than a lesser one drawn
    just for that slot.  When there is no mesh — a file somebody sliced
    elsewhere — the toolpath in the file itself is drawn instead.  ``None``
    only when there is nothing to draw at all; a preview never fails a
    slice.
    """
    if model_path and os.path.isfile(model_path):
        try:
            from kiln.multicolor_3mf import render_plate_preview

            rendered = render_plate_preview(
                [model_path], colors=colors, width=width, height=height,
            )
            if rendered:
                return rendered
        except Exception:  # noqa: BLE001 — fall through to the toolpath
            logger.warning("Could not render a preview from %s", model_path, exc_info=True)
    if gcode_text:
        try:
            return _render_from_gcode(gcode_text, width, height)
        except Exception:  # noqa: BLE001 — see above
            logger.warning("Could not draw the toolpath preview", exc_info=True)
    return None


def _thumbnail_block(png: bytes, width: int, height: int) -> str:
    """One block, in the shape PrusaSlicer's own emitter writes."""
    encoded = base64.b64encode(png).decode("ascii")
    rows = [encoded[i:i + _MAX_ROW] for i in range(0, len(encoded), _MAX_ROW)]
    body = "".join(f"; {row}\n" for row in rows)
    return f"\n;\n; thumbnail begin {width}x{height} {len(encoded)}\n{body}; thumbnail end\n;\n"


def _insert_thumbnails(text: str, blocks: str) -> str:
    """Put *blocks* where every reader looks: the top of the file.

    After the generator line, which is how PrusaSlicer orders its own
    output, and before the first extrusion, which is where OctoPrint's
    reader stops.
    """
    lines = text.splitlines(keepends=True)
    at = 1 if lines and lines[0].lstrip().startswith(";") else 0
    return "".join(lines[:at]) + blocks + "".join(lines[at:])


def _format_numbers(values: tuple[float, ...] | list[float]) -> str:
    return ", ".join(f"{v:.2f}" for v in values) or "0.00"


def _fill_weight(text: str, usage) -> str:
    """Write the weight where each surface reads it, touching nothing else.

    ``total filament used [g]`` is what Moonraker reads, ``filament used
    [g]`` is what PrusaLink reads, and PrusaSlicer writes the second one
    only for multi-material — so a single-extruder Kiln slice has to have
    it added.  A figure the slicer worked out for itself is kept.
    """
    grams = _format_numbers(usage.grams)
    total = f"{usage.total_g:.2f}"
    lines: list[str] = []

    if _TOTAL_G_RE.search(text):
        text = _TOTAL_G_RE.sub(lambda m: f"; total filament used [g] = {total}", text, count=1)
    else:
        lines.append(f"; total filament used [g] = {total}")

    if _USED_G_RE.search(text):
        text = _USED_G_RE.sub(lambda m: f"; filament used [g] = {grams}", text, count=1)
    else:
        lines.append(f"; filament used [g] = {grams}")

    if not _USED_MM_RE.search(text) and usage.total_mm > 0:
        lines.append(f"; filament used [mm] = {_format_numbers(usage.mm)}")

    if not lines:
        return text
    added = "".join(f"{line}\n" for line in lines)

    # Above the slicer's config block when there is one, so the figures sit
    # with the other estimates and inside the window every reader scans;
    # otherwise at the end, which is the same window.
    config = _CONFIG_BEGIN_RE.search(text)
    if config:
        return text[:config.start()] + added + text[config.start():]
    if text and not text.endswith("\n"):
        text += "\n"
    return text + added


def complete_gcode_for_printer(
    path: str | os.PathLike[str],
    printer_family: str | None = None,
    *,
    model_path: str | None = None,
    colors: list[str] | None = None,
    preview_png: bytes | None = None,
    output_path: str | os.PathLike[str] | None = None,
) -> str:
    """Give a raw G-code file the preview and the weight its surface reads.

    Header comments only.  Every move comes out byte for byte as the slicer
    wrote it — this is the promise that lets the completion run on a file
    that is already on its way to a printer.

    The picture is rendered from *model_path* (the mesh the file was sliced
    from) unless *preview_png* is handed in already drawn, is written at
    :data:`THUMBNAIL_SIZES`, and is skipped entirely for a family whose
    surface draws none.  The weight is read from the file's own moves by
    :func:`kiln.printers.bambu_3mf.filament_usage_from_gcode` and written
    only where the slicer left a zero or nothing.

    Idempotent: a second call finds the check satisfied and changes nothing.
    Rewrites in place unless *output_path* is given.  Returns the path
    written.
    """
    src = Path(path)
    dst = Path(output_path) if output_path else src
    surface = surface_for(printer_family)
    if surface.family == "bambu":
        # A Bambu plate is completed as an archive, by complete_bambu_archive.
        # Nothing here belongs anywhere near that path.
        return str(dst)
    text = src.read_text(encoding="utf-8", errors="replace")
    original = text

    if surface.reads_thumbnail and not gcode_thumbnails(text):
        largest = max(THUMBNAIL_SIZES, key=lambda size: size[0] * size[1])
        source = preview_png or _render_preview(
            model_path, colors, *largest, gcode_text=text,
        )
        if source:
            blocks = ""
            for width, height in THUMBNAIL_SIZES:
                fitted = _resize_png(source, width, height)
                if fitted:
                    blocks += _thumbnail_block(fitted, width, height)
            if blocks:
                text = _insert_thumbnails(text, blocks)
        else:
            logger.warning(
                "No preview could be drawn for %s — it ships without one, and the "
                "upload door will say so.", src.name,
            )

    if declared_grams(text) <= 0:
        usage = _usage(text)
        if round(usage.total_g, 2) > 0:
            text = _fill_weight(text, usage)

    if text == original and dst == src:
        return str(dst)
    tmp = dst.with_name(dst.name + ".completing")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(dst))
    logger.info(
        "Completed %s for %s: %d preview slot(s), weight %s",
        dst.name, surface.family, len(gcode_thumbnails(text)),
        f"{declared_grams(text):.2f} g",
    )
    return str(dst)
