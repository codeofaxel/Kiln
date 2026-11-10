"""A raw G-code file leaves Kiln with its preview and its weight, or not at all."""

from __future__ import annotations

import base64
import contextlib
import logging
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path

from kiln.gcode import axis_word_pattern, filament_values
from kiln.printers.qoi import qoi_dimensions, qoi_encode_png

logger = logging.getLogger(__name__)

THUMBNAIL_SIZES: tuple[tuple[int, int], ...] = ((400, 300), (32, 32))

_MAX_ROW = 78

# : Extensions this check judges
_GCODE_SUFFIXES = (".gcode", ".gco", ".g", ".gc")

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# : How much of a file the CHECK reads
_HEAD_BYTES = 1024 * 1024
_TAIL_BYTES = 256 * 1024


@dataclass(frozen=True)
class GcodeSurface:
    """What one printer family's screen or web UI reads out of a G-code file."""

    family: str
    #: What the person looks at, in their words, for the refusal message.
    surface: str
    reads_thumbnail: bool
    weight_unit: str | None
    #: Where the two answers above came from.
    evidence: str
    qoi_screens: tuple[str, ...] = ()
    best_effort_thumbnail: bool = False
    screen_can_read_file: bool = True


@dataclass(frozen=True)
class QoiScreen:
    """One printer screen that draws a ``; thumbnail_QOI begin`` block."""

    key: str
    #: What the person looks at, in their words, for the refusal message.
    screen: str
    preview: tuple[int, int]
    sizes: tuple[tuple[int, int], ...]
    #: PNG sizes the same printer's own web UI reads beside the QOI.
    png_sizes: tuple[tuple[int, int], ...]
    refuses_missing: bool
    evidence: str


QOI_SCREENS: dict[str, QoiScreen] = {
    "buddy_large": QoiScreen(
        key="buddy_large",
        screen="the printer's own screen",
        preview=(313, 173),
        sizes=((313, 173), (480, 240), (440, 240)),
        png_sizes=((16, 16),),
        refuses_missing=True,
        evidence=(
            "Prusa-Firmware-Buddy v6.10.2: the large display (HAS_LARGE_DISPLAY) "
            "asks for a 313x173 preview, a 480x240 progress picture and the old "
            "440 width, all three as ImgType::QOI; it belongs to the "
            "XBUDDY/XLBUDDY boards (MK4, MK4S, MK3.5, MK3.9, XL, Core One, "
            "Core One L, iX); the web UI serves its list icon from a 16x16 PNG"
        ),
    ),
    "buddy_mini": QoiScreen(
        key="buddy_mini",
        screen="the MINI's own screen",
        preview=(220, 124),
        sizes=((220, 124), (240, 240), (200, 240)),
        png_sizes=((16, 16),),
        refuses_missing=True,
        evidence=(
            "Prusa-Firmware-Buddy v6.10.2: the MINI display (HAS_MINI_DISPLAY) "
            "asks for a 220x124 preview, a 240x240 progress picture and the old "
            "200 width, through the same readers as the large display; it "
            "belongs to the BUDDY board, which the MINI is built on; the web UI "
            "serves its list icon from a 16x16 PNG"
        ),
    ),
    "paneldue": QoiScreen(
        key="paneldue",
        screen="the PanelDue's file dialog",
        preview=(160, 120),
        sizes=((256, 192), (160, 120)),
        png_sizes=(),
        refuses_missing=False,
        evidence=(
            "PanelDueFirmware 3.7.0 reads QOI only and keeps the largest QOI that "
            "fits the file dialog's picture field, 161x161 on the 480x272 panel "
            "and 263x245 on the 800x480 panels; RepRapFirmware 3.6.3 reports the "
            "first four blocks only.  A PanelDue is optional hardware, so its "
            "blocks are written and never refused over"
        ),
    ),
}

_MODEL_SCREENS: dict[str, str | None] = {
    "prusa_mini": "buddy_mini",
    "prusa_mk4": "buddy_large",
    "prusa_mk4s": "buddy_large",
    "prusa_mk3_5": "buddy_large",
    "prusa_mk3_5s": "buddy_large",
    "prusa_mk3_9": "buddy_large",
    "prusa_mk3_9s": "buddy_large",
    "prusa_xl": "buddy_large",
    "prusa_ix": "buddy_large",
    "prusa_core_one": "buddy_large",
    "prusa_core_one_l": "buddy_large",
    "prusa_mk3s": None,
    "prusa_mk3": None,
    "prusa_mk2_5": None,
    "prusa_mk2_5s": None,
}

_PRESET_TAIL = re.compile(r"(?:_(?:hf)?\d+_\d+_nozzle|_\d+t)+$")


def _normalise_model(printer_model: str | None) -> str:
    hint = str(printer_model or "").strip().lower()
    for ch in ("-", " ", ".", "+", "/"):
        hint = hint.replace(ch, "_")
    return re.sub(r"_+", "_", hint).strip("_")


def screen_for_model(printer_model: str | None) -> QoiScreen | None:
    """The screen a declared *printer_model* carries, or ``None``."""
    hint = _normalise_model(printer_model)
    machine = _PRESET_TAIL.sub("", hint)
    if machine != hint:
        printer_model, hint = machine, machine
    if not hint:
        return None
    if hint in _MODEL_SCREENS:
        key = _MODEL_SCREENS[hint]
        return QOI_SCREENS[key] if key else None
    is_prusa = "prusa" in hint
    if "mk3_5" in hint or "mk3_9" in hint or hint.startswith(("core_one", "coreone")):
        return QOI_SCREENS["buddy_large"]
    if is_prusa and ("core" in hint or re.search(r"(^|_)ix($|_)", hint)):
        return QOI_SCREENS["buddy_large"]
    from kiln.printer_profile_ids import map_printer_hint_to_profile_id

    mapped = map_printer_hint_to_profile_id(printer_model)
    if mapped == "prusa_xl" and not is_prusa:
        # The mapper files any "
        return None
    key = _MODEL_SCREENS.get(mapped or "")
    return QOI_SCREENS[key] if key else None


def _controller_screens(printer_model: str | None) -> tuple[str, ...]:
    """Screens the declared model's CONTROLLER may carry, from the catalogue."""
    if not printer_model:
        return ()
    try:
        from kiln.motion_facts import motion_facts_for

        facts = motion_facts_for(printer_model)
    except Exception:  # noqa: BLE001 — no catalogue answer is no screen
        return ()
    if facts is not None and getattr(facts, "firmware_family", None) == "reprapfirmware":
        return ("paneldue",)
    return ()


def declared_model_for_adapter(adapter: object) -> str | None:
    """The config-declared model *adapter* was built with, or ``None``."""
    with contextlib.suppress(Exception):
        declared = str(adapter.declared_printer_model() or "").strip()  # type: ignore[attr-defined]
        return declared or None
    return None


def printer_model_for_adapter(adapter: object) -> str | None:
    """The model to write a screen's blocks for: declared first, then reported."""
    declared = declared_model_for_adapter(adapter)
    if declared:
        return declared
    try:
        info = adapter.get_printer_info()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — a probe that fails is "not reported"
        return None
    model = str(getattr(info, "model", "") or "").strip()
    return model or None


def _screens_for(surface: GcodeSurface, printer_model: str | None) -> list[QoiScreen]:
    """Every screen a file for *surface* on *printer_model* should carry."""
    screens: list[QoiScreen] = []
    if not surface.screen_can_read_file:
        return screens
    by_model = screen_for_model(printer_model)
    if by_model:
        screens.append(by_model)
    for key in surface.qoi_screens + _controller_screens(printer_model):
        screen = QOI_SCREENS[key]
        if screen not in screens:
            screens.append(screen)
    return screens


SURFACES: dict[str, GcodeSurface] = {
    "moonraker": GcodeSurface(
        family="moonraker",
        surface="Mainsail/Fluidd's file list",
        reads_thumbnail=True,
        weight_unit="g",
        evidence=(
            "Moonraker's metadata parser matches "
            "'(thumbnail(?:_[A-Za-z0-9]+)?) begin ... ; end', checks the declared "
            "size against the base64 length, and reads 'total filament used [g]'"
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
            "and stops at the first G1 with an E word; OctoPrint's own G-code "
            "analysis sums the E moves for length and volume, so no weight "
            "comment is read.  Kiln uploads to /api/files/local, the "
            "host's own storage, and OctoPrint streams the moves from there, so "
            "no screen on the printer ever opens the file"
        ),
        screen_can_read_file=False,
    ),
    "prusalink": GcodeSurface(
        family="prusalink",
        surface="PrusaLink's file list",
        reads_thumbnail=True,
        weight_unit="g",
        evidence=(
            "Prusa's G-code metadata library accepts a plain PNG "
            "'; thumbnail begin', ignores anything under 50x50 and scores "
            "against 640x480 (preview) and 100x100 (icon), PNG and JPG only; "
            "'filament used [g]' is one of its attributes.  The Buddy printers' own screen reads a QOI "
            "block instead, resolved per model through screen_for_model"
        ),
    ),
    "duet": GcodeSurface(
        family="duet",
        surface="Duet Web Control's job list",
        reads_thumbnail=True,
        weight_unit="mm",
        evidence=(
            "RepRapFirmware 3.6.3 reads '; thumbnail begin 32x32 2140' (PNG), "
            "'_JPG' and '_QOI', the first four in the file; filament is parsed from "
            "'ilament used' in mm.  Duet Web Control decodes all three formats; "
            "PanelDue draws QOI only, and gets its blocks through qoi_screens"
        ),
        qoi_screens=("paneldue",),
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
        best_effort_thumbnail=True,
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
        screen_can_read_file=False,
    ),
    # Bambu plates are archives, judged by bambu_archive_problems
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

DEFAULT_FAMILY = "gcode"

_DEFAULT_SURFACE = GcodeSurface(
    family=DEFAULT_FAMILY,
    surface="the printer's file list",
    reads_thumbnail=True,
    weight_unit="g",
    evidence="the format every reader in SURFACES accepts",
)

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
    """The family *adapter* belongs to."""
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


def _thumbnail_blocks(text: str) -> list[tuple[str, int, int]]:
    """``(tag suffix, width, height)`` of every block a reader would accept."""
    found: list[tuple[str, int, int]] = []
    for match in _THUMB_BEGIN_RE.finditer(text):
        suffix = match.group("fmt") or ""
        tag = "thumbnail" + suffix
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
        if suffix == "" and _png_dimensions(data) != (width, height):
            continue
        if suffix.upper() == "_QOI" and qoi_dimensions(data) != (width, height):
            continue
        found.append((suffix, width, height))
    return found


def gcode_thumbnails(text: str) -> list[tuple[int, int]]:
    """The sizes of every thumbnail block in *text* a reader would accept."""
    return [(w, h) for _, w, h in _thumbnail_blocks(text)]


def qoi_thumbnails(text: str) -> list[tuple[int, int]]:
    """The sizes of every ``; thumbnail_QOI begin`` block a screen would draw."""
    return [(w, h) for suffix, w, h in _thumbnail_blocks(text) if suffix == "_QOI"]


_TOTAL_G_RE = re.compile(r"^;\s*total filament used \[g\]\s*=\s*(?P<v>[-\d., ]*)$", re.MULTILINE)
_USED_G_RE = re.compile(r"^;\s*filament used \[g\]\s*=\s*(?P<v>[-\d., ]*)$", re.MULTILINE)
_USED_MM_RE = re.compile(r"^;\s*filament used \[mm\]\s*=\s*(?P<v>[-\d., ]*)$", re.MULTILINE)
_CONFIG_BEGIN_RE = re.compile(r"^;\s*\w*slicer_config = begin\s*$", re.MULTILINE)


def _claimed(pattern: re.Pattern[str], text: str) -> float:
    match = pattern.search(text)
    return float(sum(filament_values(match.group("v"))[0])) if match else 0.0


def declared_grams(text: str) -> float:
    """The weight the file claims in the lines Moonraker and PrusaLink read, in grams — 0.0 when it claims none."""
    for pattern in (_TOTAL_G_RE, _USED_G_RE):
        total = _claimed(pattern, text)
        if total > 0:
            return total
    return 0.0


def declared_mm(text: str) -> float:
    """The filament length the file claims in ``; filament used [mm]``, every extruder summed — 0.0 when it claims none."""
    return _claimed(_USED_MM_RE, text)


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
    """What the file's own moves and comments say it consumes."""
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
    printer_model: str | None = None,
) -> list[str]:
    """Why *path* must not go to a *printer_family* printer — empty when it may."""
    p = Path(path)
    surface = surface_for(printer_family)
    if surface.family == "bambu":
        return []
    if not _is_gcode(p, name):
        return []
    screen = screen_for_model(printer_model) if surface.screen_can_read_file else None
    if screen is not None and not screen.refuses_missing:
        screen = None
    if not surface.reads_thumbnail and surface.weight_unit is None and screen is None:
        return []
    try:
        text = _head_and_tail(p)
    except OSError:
        logger.debug("gcode completeness check could not read %s", p, exc_info=True)
        return []

    problems: list[str] = []
    usage = _usage(text)
    prints_something = round(usage.total_mm, 2) > 0
    if surface.reads_thumbnail and prints_something and not gcode_thumbnails(text):
        problems.append(
            f"no preview: {surface.surface} draws its tile from a "
            f"'; thumbnail begin' block and this file carries none"
        )
    if screen is not None and prints_something and screen.preview not in qoi_thumbnails(text):
        width, height = screen.preview
        problems.append(
            f"no screen preview: {screen.screen} draws its print preview from a "
            f"'; thumbnail_QOI begin {width}x{height}' block (firmware 5.1.0 and later) "
            f"and this file carries none"
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
            mode = "RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB"
            img.convert(mode).resize((width, height), Image.LANCZOS).save(out, format="PNG")
            return out.getvalue()
    except Exception:  # noqa: BLE001 — a missing size is a smaller tile, never a failure
        logger.warning("Could not resize the preview to %dx%d", width, height, exc_info=True)
        return None


_MAX_SEGMENTS = 120_000

_XYZE_RE = axis_word_pattern("XYZEF")


def _extruding_segments(text: str) -> list[tuple[float, float, float, float, float, float]]:
    """The moves that lay filament down, as ``(x1, y1, z1, x2, y2, z2)``."""
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
    """A picture of the print drawn from the file's own extrusion moves."""
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
    """The picture of the plate, best source first."""
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


def _fit_png(source: bytes, width: int, height: int) -> bytes | None:
    """*source* scaled to fit inside *width* x *height*, centred, padded."""
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(source)) as img:
            rgba = img.convert("RGBA")
            scale = min(width / rgba.width, height / rgba.height)
            fitted_w = max(1, round(rgba.width * scale))
            fitted_h = max(1, round(rgba.height * scale))
            fitted = rgba.resize((fitted_w, fitted_h), Image.LANCZOS)
            corners = [
                rgba.getpixel((x, y))
                for x in (0, rgba.width - 1) for y in (0, rgba.height - 1)
            ]
            backdrop = max(set(corners), key=corners.count)
            canvas = Image.new("RGBA", (width, height), backdrop)
            canvas.paste(fitted, ((width - fitted_w) // 2, (height - fitted_h) // 2))
            out = io.BytesIO()
            canvas.save(out, format="PNG")
            return out.getvalue()
    except Exception:  # noqa: BLE001 — a missing size is a smaller tile, never a failure
        logger.warning("Could not fit the preview into %dx%d", width, height, exc_info=True)
        return None


def _thumbnail_block(data: bytes, width: int, height: int, tag: str = "thumbnail") -> str:
    """One block, in the shape PrusaSlicer's own emitter writes."""
    encoded = base64.b64encode(data).decode("ascii")
    rows = [encoded[i:i + _MAX_ROW] for i in range(0, len(encoded), _MAX_ROW)]
    body = "".join(f"; {row}\n" for row in rows)
    return f"\n;\n; {tag} begin {width}x{height} {len(encoded)}\n{body}; {tag} end\n;\n"


def _qoi_block(source: bytes, width: int, height: int) -> str:
    """The screen's block at one of its sizes, or ``""`` when it cannot be drawn."""
    fitted = _fit_png(source, width, height)
    if not fitted:
        return ""
    try:
        encoded = qoi_encode_png(fitted)
    except Exception:  # noqa: BLE001 — see _fit_png
        logger.warning("Could not encode the %dx%d preview as QOI", width, height, exc_info=True)
        return ""
    if not encoded:
        return ""
    return _thumbnail_block(encoded, width, height, tag="thumbnail_QOI")


def _insert_thumbnails(text: str, blocks: str) -> str:
    """Put *blocks* where every reader looks: the top of the file."""
    lines = text.splitlines(keepends=True)
    first = lines[0].lstrip() if lines else ""
    at = 1 if first.startswith(";") and not _THUMB_BEGIN_RE.match(first) else 0
    return "".join(lines[:at]) + blocks + "".join(lines[at:])


def _format_numbers(values: tuple[float, ...] | list[float]) -> str:
    return ", ".join(f"{v:.2f}" for v in values) or "0.00"


def _fill_weight(text: str, usage) -> str:
    """Write the weight where each surface reads it, touching nothing else."""
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
    printer_model: str | None = None,
    model_path: str | None = None,
    colors: list[str] | None = None,
    preview_png: bytes | None = None,
    output_path: str | os.PathLike[str] | None = None,
) -> str:
    """Give a raw G-code file the preview and the weight its surface reads."""
    src = Path(path)
    dst = Path(output_path) if output_path else src
    surface = surface_for(printer_family)
    if surface.family == "bambu":
        # A Bambu plate is completed as an archive, by complete_bambu_archive
        return str(dst)
    text = src.read_text(encoding="utf-8", errors="replace")
    original = text

    present = _thumbnail_blocks(text)
    png_have = {(w, h) for suffix, w, h in present if suffix == ""}
    qoi_have = {(w, h) for suffix, w, h in present if suffix == "_QOI"}
    screens = _screens_for(surface, printer_model)

    # The order is the order the readers' budgets dictate
    wanted: list[tuple[str, int, int]] = []

    def want(kind: str, size: tuple[int, int]) -> None:
        have = png_have if kind == "png" else qoi_have
        if size not in have and (kind, *size) not in wanted:
            wanted.append((kind, *size))

    for screen in screens:
        want("qoi", screen.preview)
    for screen in screens:
        for size in screen.png_sizes:
            want("png", size)
    if (surface.reads_thumbnail or surface.best_effort_thumbnail) and not present:
        for size in THUMBNAIL_SIZES:
            want("png", size)
    for screen in screens:
        for size in screen.sizes:
            want("qoi", size)

    if wanted:
        largest = max(THUMBNAIL_SIZES, key=lambda size: size[0] * size[1])
        source = preview_png or _render_preview(
            model_path, colors, *largest, gcode_text=text,
        )
        if source:
            blocks = ""
            for kind, width, height in wanted:
                if kind == "qoi":
                    blocks += _qoi_block(source, width, height)
                else:
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
        "Completed %s for %s: %d preview slot(s), %d screen slot(s), weight %s",
        dst.name, surface.family, len(gcode_thumbnails(text)), len(qoi_thumbnails(text)),
        f"{declared_grams(text):.2f} g",
    )
    return str(dst)
