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
  MiB of the file; the thumbnail is read from the first MiB and, for a
  file bigger than that, the last as well.
* **OctoPrint** — the Slicer Thumbnails plugin matches
  ``^; thumbnail(?:_JPG)* begin \\d+[x ]\\d+ \\d+`` and stops reading at the
  first ``G1`` carrying an ``E`` word, so the block has to come before the
  first extrusion.  OctoPrint's own file list gets filament length and
  volume from a pass over the E moves (``util/gcodeInterpreter.py``), not
  from any comment — so no weight line is owed to it.
* **PrusaLink** — ``prusa3d/gcode-metadata`` (the Python PrusaLink on a
  Pi) matches ``; thumbnail_?(QOI|JPG|) begin (dim) (size)``, so a plain
  PNG block counts; it ignores anything smaller than 50x50, picks the image
  nearest 640x480 for the preview and nearest 100x100 for the icon, and
  reads ``filament used [g]``.  Its ``IMAGE_FORMATS = ['PNG', 'JPG']``
  (metadata.py L75, applied at L982) means a QOI block is parsed but never
  chosen, so the QOI blocks below cannot displace the PNG.  The Buddy
  printers' own web UI (``lib/WUI/link_content/previews.cpp`` L38-46 and
  ``lib/WUI/nhttp/gcode_preview.cpp`` L56, v6.10.2) serves ``/thumb/s/``
  from a PNG of exactly 16x16 and ``/thumb/l/`` from the first PNG larger
  than that — PNG both, so those printers get a 16x16 PNG beside the
  400x300.  The printer's own SCREEN reads none of these; see below.
* **Duet / RepRapFirmware** — ``src/Storage/FileInfoParser.cpp`` (3.6.3)
  L69-71 knows ``Thumbnail begin`` (PNG), ``Thumbnail_JPG begin`` and
  ``Thumbnail_QOI begin``, compared exactly after the first letter (L453);
  L747-755 take 16..500 pixels a side and a size of at least 10; L511-517
  stop parsing the header at the first G/M/T command, so every block must
  precede it; ``src/Config/Configuration.h`` L275 ``MaxThumbnails = 4``
  stores and reports only the first four.  Filament is ``ilament used`` in
  millimetres.  Duet Web Control (v3.6-dev
  ``src/components/misc/ThumbnailImg.vue`` L36-60) decodes PNG, JPEG and
  QOI alike in the browser; PanelDue draws QOI only — see below.
* **Elegoo (SDCP)** — what the Centauri Carbon's screen reads could not be
  established from a source Kiln can cite.  A preview is written anyway,
  because a comment block costs nothing, and its absence is never a
  refusal.
* **Serial / Marlin** — :meth:`SerialPrinterAdapter.upload_file` drops every
  comment line on the way to the SD card, so neither a picture nor a weight
  survives the trip.  Nothing is refused, and the completion fills the
  weight only, for the copy that stays on disk.

WHAT THE PRINTER'S OWN SCREEN READS.  Two screens read a QOI block, not a
PNG one, and each is in :data:`QOI_SCREENS` with its evidence.  Nothing
here was confirmed on hardware — the owner has a Bambu — so every size and
rule below is quoted from the firmware source, at the version named.

* **Prusa MK4 / MK4S / MK3.5 / MK3.9 / XL / Core One / iX / MINI**
  (Prusa-Firmware-Buddy).  The 5.1.0 release notes (2023-11-23): "QOI
  instead of PNG (XL, MK4, MINI) ... all the G-codes sliced until now,
  won't have a visible thumbnail on firmware 5.1.0 or newer."  At v6.10.2
  (2026-09-16; identical on master as of 2026-09-19):

  - ``src/common/gcode/gcode_reader_plaintext.cpp`` L197-198 match the
    literal prefixes ``"; thumbnail begin "`` (PNG) and ``"; thumbnail_QOI
    begin "`` (QOI); L208 reads ``WxH N`` with ``%hux%hu%lu``; L223 refuses
    the wrong type and L227-228 the wrong width or height (exact, unless
    the caller allows larger, which the screen never does); L36 searches
    only the first 2048 lines; L166-169 read the base64 one character at a
    time, skipping CR, LF, space and ``;``, and count only the base64
    characters against ``N``.
  - ``src/common/thumbnail_sizes.hpp`` L10-21: on the 480x320 display
    (``HAS_LARGE_DISPLAY``) the preview is 313x173 and the progress
    picture 480x240 with an ``old_progress_thumbnail_width`` of 440; on the
    240x320 MINI display the preview is 220x124 and the progress picture
    240x240 with an old width of 200.  ``include/guiconfig/guiconfig.h``
    L18-21 give the large display to the XBUDDY and XLBUDDY boards and the
    MINI display to BUDDY; ``CMakePresets.json`` builds MINI on BUDDY,
    MK4 (the build the MK4S and MK3.9 run), MK3.5, iX, COREONE and COREONEL
    on XBUDDY, XL on XLBUDDY.
  - ``src/gui/window_thumbnail.cpp`` L27 asks for the preview at the
    window's own size as ``ImgType::QOI``; L52-57 ask for the progress
    picture at the full width, then at the old width drawn centred.
    ``src/common/gcode/gcode_info.cpp`` L96-99 index exactly those three
    sizes, all QOI, and L494-502 set ``has_preview_thumbnail_`` and
    ``has_progress_thumbnail_`` only for a QOI block at one of them.
  - ``src/gui/qoi_decoder.hpp`` L47-67 read width and height from header
    bytes 4..11 and nothing else; ``src/guiapi/src/ili9488.cpp`` L677 mixes
    each pixel's alpha against the back colour, which for a G-code
    thumbnail is black (``src/guiapi/src/display_ex.cpp`` L530-531).
  - Prusa's own profiles agree: ``resources/profiles/PrusaResearch.ini``
    in PrusaSlicer 2.9.4 writes ``16x16/QOI, 313x173/QOI, 480x240/QOI,
    380x285/PNG`` for the MK4 family (L39046, inherited by MK4S, MK3.9 and
    MK3.5), the XL (L38344), the Core One (L39520) and the Core One L
    (L40186), and
    ``16x16/QOI, 220x124/QOI, 200x240/QOI, 380x285/PNG`` for the MINI
    (L38152).  2.7.0 (the release that went with firmware 5.1.0) wrote
    440x240 for the large display; 2.7.4 through 2.9.0 wrote 440x240 and
    480x240 both.  Kiln writes both progress widths, so a screen on either
    side of that change finds its picture.  The MK3 and MK3S profiles write
    ``160x120`` PNG and no QOI (L37830): those printers have no screen
    that reads one, and nothing is written or refused for them.
  - UNKNOWN: who reads the ``16x16/QOI`` in those profiles.  The firmware
    at v6.10.2 asks for no 16x16 QOI anywhere, its web UI wants a 16x16
    PNG, and gcode-metadata drops anything under 50x50.  Not written.

* **Duet's PanelDue** (PanelDueFirmware 3.7.0; 3.5.2 reads the same way).
  ``src/Library/Thumbnail.cpp`` L16-21: only ``ImageFormat::Qoi`` is valid.
  ``src/PanelDue.cpp`` L2076 takes the format from RepRapFirmware's M36
  report as ``"qoi"``, and L2194-2205 keep, in the order RepRapFirmware
  lists them, the largest QOI whose height and width are both no more than
  the file dialog's picture field and both strictly more than the previous
  pick's.  ``src/UI/UserInterface.cpp`` L628-633 size that field at
  ``7 * rowTextHeight + 2 * rowTextHeight / 3`` by ``fileInfoPopupWidth / 3
  + 5``; with ``src/UI/UserInterfaceConstants.hpp`` (margin 2 and row 21
  for the 480-wide panel, L26/L34; margin 4 and row 32 for the 800-wide
  panels, L72/L80; ``fileInfoPopupWidth`` L156) that is 161x161 on the
  4.3-inch 480x272 panel and 263x245 on the 5- and 7-inch 800x480 panels.
  ``src/UI/Display.cpp`` L1112-1119 draw a smaller picture right-aligned
  and vertically centred; ``src/Hardware/UTFT.cpp`` L2248 discards alpha,
  so a transparent pixel shows the colour underneath it.  Its decoder
  (Duet3D/qoi ``qoi.h`` at ef59be0, L575-583) refuses a header without
  the magic, with a zero dimension, with channels outside 3..4 or a
  colorspace above 1.  Kiln writes 160x120 (fits every panel) and 256x192
  (the 800-wide panels pick it over the smaller one), 4:3 like the render.
  A PanelDue is optional hardware Kiln cannot see, so its blocks are
  written and their absence is never a refusal.  They are written for the
  Duet family and for any model the catalogue lists as RepRapFirmware
  (``firmware_family`` in ``printer_intelligence.json``), so the slice
  door serves them without knowing the adapter.

* **Klipper / Moonraker** would convert a QOI block to PNG through Pillow
  (``SUPPORTED_THUMB_FORMATS`` and ``FMT_CONV_MAP``, metadata.py L42-45),
  which needs a Pillow with a QOI reader (9.5 or later).  No Klipper screen
  asks for one, so none is written for that family.

* The QOI encoder is :mod:`kiln.printers.qoi`, pinned byte for byte
  against the reference implementation.  Blocks are written RGBA with
  colorspace sRGB, as PrusaSlicer's ``compress_thumbnail_qoi`` does
  (``Thumbnails.cpp`` L101-107); the tag is ``thumbnail_QOI``
  (``Thumbnails.cpp`` L43; ``thumbnails_format`` in ``PrintConfig.cpp``
  L399-404 offers PNG, JPG and QOI).  The screen's blocks go first in the
  file, preview first, because the screen's reader has a line budget and
  the web readers have byte budgets a whole MiB wide.

WHAT KILN WROTE BEFORE THIS.  Measured on this machine, 2026-09-19, with
PrusaSlicer 2.9.4: ``--thumbnails 160x120/PNG`` is accepted on the command
line and echoed into the config block, and no thumbnail block is emitted —
the renderer that draws them is not there in CLI mode.  The same slice
wrote ``; filament used [mm] = 1493.99`` and ``; total filament used [g] =
0.00``, with no ``; filament used [g]`` line at all, because Kiln's
profiles describe a printer and named no filament density.  Since then
every slice is handed one (:mod:`kiln.slicer_filament`) and the slicer
writes both lines itself; the weight fill below stays as the safety net for
G-code sliced elsewhere, and a Kiln slice leaves it untouched.

THE FORMAT WRITTEN is PrusaSlicer's own, from its emitter
(``src/libslic3r/GCode/Thumbnails.hpp`` L72-82, 2.9.4)::

    "\\n;\\n; %s begin %dx%d %d\\n"   # tag, width, height, len(base64)
    "; %s\\n"                        # up to 78 base64 characters per row
    "; %s end\\n;\\n"

with the tag ``thumbnail`` for PNG and ``thumbnail_QOI`` for QOI.

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

from kiln.printers.qoi import qoi_dimensions, qoi_encode_png

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
    #: Keys into :data:`QOI_SCREENS` for a screen that belongs to the
    #: family's CONTROLLER rather than to a printer model — Duet's optional
    #: PanelDue.  A screen that belongs to the machine (the Prusa Buddy
    #: display) is reached through :func:`screen_for_model` instead.
    qoi_screens: tuple[str, ...] = ()
    #: A preview is written even though no reader of it can be cited — the
    #: block costs nothing and the screen may well draw it — while its
    #: absence is still never a refusal.  Elegoo.
    best_effort_thumbnail: bool = False
    #: Whether the uploaded file lands where a screen ON THE PRINTER can
    #: open it.  ``False`` for a transport that keeps the file on a host and
    #: streams it (OctoPrint) or strips every comment on the way (serial):
    #: no screen block is written or owed there, whatever model is declared.
    screen_can_read_file: bool = True


@dataclass(frozen=True)
class QoiScreen:
    """One printer screen that draws a ``; thumbnail_QOI begin`` block.

    ``sizes`` is everything the completion writes for it, ``preview`` the
    one size the check demands — the picture the person sees when they
    pick the file, which every reader of this screen since it first read
    QOI has asked for by exactly that size.  ``refuses_missing`` is
    ``False`` for a screen Kiln cannot know is there.
    """

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
            "Prusa-Firmware-Buddy v6.10.2 src/common/thumbnail_sizes.hpp L16-21 "
            "(HAS_LARGE_DISPLAY: preview 313x173, progress 480x240, old width 440); "
            "src/gui/window_thumbnail.cpp L27, L52-57 and src/common/gcode/"
            "gcode_info.cpp L96-99, L494-502 ask for all three as ImgType::QOI; "
            "guiconfig.h L18-19 gives the display to the XBUDDY/XLBUDDY boards "
            "(MK4, MK4S, MK3.5, MK3.9, XL, Core One, Core One L, iX per "
            "CMakePresets.json); "
            "lib/WUI/link_content/previews.cpp L38-41 serves the web list icon "
            "from a 16x16 PNG"
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
            "Prusa-Firmware-Buddy v6.10.2 src/common/thumbnail_sizes.hpp L10-15 "
            "(HAS_MINI_DISPLAY: preview 220x124, progress 240x240, old width 200); "
            "the same readers as the large display; guiconfig.h L20-21 gives the "
            "display to the BUDDY board, which CMakePresets.json builds the MINI on; "
            "lib/WUI/link_content/previews.cpp L38-41 serves the web list icon "
            "from a 16x16 PNG"
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
            "PanelDueFirmware 3.7.0 src/Library/Thumbnail.cpp L16-21 (QOI only); "
            "src/PanelDue.cpp L2194-2205 keeps the largest QOI that fits the file "
            "dialog's picture field, sized by src/UI/UserInterface.cpp L628-633 to "
            "161x161 on the 480x272 panel and 263x245 on the 800x480 panels; "
            "RepRapFirmware 3.6.3 src/Config/Configuration.h L275 reports the "
            "first four blocks only.  A PanelDue is optional hardware, so its "
            "blocks are written and never refused over"
        ),
    ),
}

#: Kiln printer id -> the screen it carries, or ``None`` for a model whose
#: screen reads no QOI.  The ids are the ones
#: :mod:`kiln.printers.prusalink` resolves a Buddy printer's ``/api/version``
#: type code to, plus the catalogue's own.  Models the PrusaLink adapter
#: names but which run the 8-bit Prusa-Firmware (MK2.5, MK3, MK3S) are here
#: as ``None`` on purpose, so the general hint mapper's ``"mk3"`` bucket is
#: never mistaken for a screen.
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

def _normalise_model(printer_model: str | None) -> str:
    hint = str(printer_model or "").strip().lower()
    for ch in ("-", " ", ".", "+", "/"):
        hint = hint.replace(ch, "_")
    return re.sub(r"_+", "_", hint).strip("_")


def screen_for_model(printer_model: str | None) -> QoiScreen | None:
    """The screen a declared *printer_model* carries, or ``None``.

    ``None`` covers both "this model's screen reads no QOI" and "Kiln does
    not know this model" — neither is a reason to write a block or to
    refuse a file.  The spelling is taken as people write it (``"Prusa
    MK4S"``, ``"mk3.9"``, ``"prusa-mini"``): normalised, looked up, then
    handed to the shared hint mapper the rest of Kiln resolves a model
    with, minus the two places that mapper is looser than a screen can be.
    """
    hint = _normalise_model(printer_model)
    if not hint:
        return None
    if hint in _MODEL_SCREENS:
        key = _MODEL_SCREENS[hint]
        return QOI_SCREENS[key] if key else None
    is_prusa = "prusa" in hint
    # The xBuddy upgrades, which the shared mapper files under the 8-bit
    # MK3S ("mk3" is in "mk3.5"), and the Core One and iX, which it does
    # not know.
    if "mk3_5" in hint or "mk3_9" in hint or hint.startswith(("core_one", "coreone")):
        return QOI_SCREENS["buddy_large"]
    if is_prusa and ("core" in hint or re.search(r"(^|_)ix($|_)", hint)):
        return QOI_SCREENS["buddy_large"]
    from kiln.printer_profile_ids import map_printer_hint_to_profile_id

    mapped = map_printer_hint_to_profile_id(printer_model)
    if mapped == "prusa_xl" and not is_prusa:
        # The mapper files any "... XL" here; other makers sell XLs too, and
        # a Neptune 4 XL on Klipper must not get a Prusa screen and a
        # refusal to go with it.
        return None
    key = _MODEL_SCREENS.get(mapped or "")
    return QOI_SCREENS[key] if key else None


def _controller_screens(printer_model: str | None) -> tuple[str, ...]:
    """Screens the declared model's CONTROLLER may carry, from the catalogue.

    A Duet-driven machine (``firmware_family == "reprapfirmware"`` in
    ``printer_intelligence.json``) may have a PanelDue whatever software is
    in front of it, so the slice door — which knows the model and not the
    adapter — writes its blocks too.  Written, never refused over.
    """
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
    """The config-declared model *adapter* was built with, or ``None``.

    :meth:`PrinterAdapter.declared_printer_model` and nothing looser —
    never the global resolver, which answers for the default printer, and
    never a self-report.  This is what a REFUSAL may key off.
    """
    with contextlib.suppress(Exception):
        declared = str(adapter.declared_printer_model() or "").strip()  # type: ignore[attr-defined]
        return declared or None
    return None


def printer_model_for_adapter(adapter: object) -> str | None:
    """The model to write a screen's blocks for: declared first, then reported.

    The config-declared ``printer_model`` is the one accessor every
    behaviour reads.  Where config is silent, the adapter's own
    self-report (``get_printer_info``, a bounded and cached probe) fills
    in — for PICKING PICTURE SIZES, which is display; a refusal keys off
    the declared model alone (:func:`declared_model_for_adapter`).
    """
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
            "no weight comment is read.  Kiln uploads to /api/files/local, the "
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
            "prusa3d/gcode-metadata: THUMBNAIL_BEGIN_PAT accepts a plain PNG "
            "'; thumbnail begin', get_closest_image ignores anything under 50x50 "
            "and scores against 640x480 (preview) and 100x100 (icon), PNG and "
            "JPG only (IMAGE_FORMATS, metadata.py L75); 'filament used [g]' is "
            "one of its attributes.  The Buddy printers' own screen reads a QOI "
            "block instead, resolved per model through screen_for_model"
        ),
    ),
    "duet": GcodeSurface(
        family="duet",
        surface="Duet Web Control's job list",
        reads_thumbnail=True,
        weight_unit="mm",
        evidence=(
            "RepRapFirmware 3.6.3 src/Storage/FileInfoParser.cpp L69-71 reads "
            "'; thumbnail begin 32x32 2140' (PNG), '_JPG' and '_QOI', the first "
            "four in the file (Configuration.h L275); filament is parsed from "
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


def _thumbnail_blocks(text: str) -> list[tuple[str, int, int]]:
    """``(tag suffix, width, height)`` of every block a reader would accept.

    A block counts only when it passes what the readers themselves check:
    the declared length is the length of the base64 (Moonraker drops the
    block otherwise, logging ``Thumbnail Size Mismatch``; the Buddy screen
    and gcode-metadata count the same way), the base64 decodes, and — for
    the two tags Kiln writes — the picture really is a PNG or a QOI of the
    size the header claims.  A block that says 400x300 and holds a 32x32
    is a broken tile, not a preview.
    """
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
    """The sizes of every thumbnail block in *text* a reader would accept.

    Every tag, as before: the web UIs between them read PNG, JPG and (for
    Moonraker, through Pillow) QOI.  See :func:`_thumbnail_blocks` for
    what "accept" means.
    """
    return [(w, h) for _, w, h in _thumbnail_blocks(text)]


def qoi_thumbnails(text: str) -> list[tuple[int, int]]:
    """The sizes of every ``; thumbnail_QOI begin`` block a screen would draw.

    Exact case: the Buddy reader matches the literal ``thumbnail_QOI`` and
    RepRapFirmware compares the keyword exactly after its first letter, so
    a ``thumbnail_qoi`` block is a picture for Moonraker and nobody else.
    """
    return [(w, h) for suffix, w, h in _thumbnail_blocks(text) if suffix == "_QOI"]


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
    printer_model: str | None = None,
) -> list[str]:
    """Why *path* must not go to a *printer_family* printer — empty when it may.

    The sibling of :func:`kiln.printers.bambu_3mf.bambu_archive_problems`,
    and it answers in the same terms: what this family's surface would fail
    to show.  A family whose surface reads nothing Kiln can cite gets an
    empty list — a print is never refused over a picture the screen would
    not have drawn anyway.

    *name* is the printer-side name when *path* is a temp copy of it, so
    the extension judged is the one the printer sees.  *printer_model* is
    the CONFIG-DECLARED model and nothing looser: a missing screen block is
    named only for a screen that model certainly carries.  With no model,
    or a model whose screen reads no QOI, or a screen that is optional
    hardware (PanelDue), the screen's block is written but never refused.
    """
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


def _fit_png(source: bytes, width: int, height: int) -> bytes | None:
    """*source* scaled to fit inside *width* x *height*, centred, padded.

    For the screen's frames, which are screen-shaped (2:1 for the MK4's
    progress picture, square for the MINI's) while the render is 4:3: a
    plain resize would squash the model, so the picture keeps its
    proportions and the frame is filled out with the render's own
    background — the colour most of its corners share, which is transparent
    for a picture drawn on transparency and the backdrop for an opaque one.
    """
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
    """One block, in the shape PrusaSlicer's own emitter writes.

    *tag* is ``thumbnail`` for a PNG and ``thumbnail_QOI`` for a QOI —
    PrusaSlicer's own tags, and the exact spellings the Buddy screen and
    RepRapFirmware match.
    """
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
    """Put *blocks* where every reader looks: the top of the file.

    After the generator line, which is how PrusaSlicer orders its own
    output, and before the first extrusion, which is where OctoPrint's
    reader stops.  A file whose first line already opens a thumbnail block
    gets the new blocks in front of it, never inside it.
    """
    lines = text.splitlines(keepends=True)
    first = lines[0].lstrip() if lines else ""
    at = 1 if first.startswith(";") and not _THUMB_BEGIN_RE.match(first) else 0
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
    printer_model: str | None = None,
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
    :data:`THUMBNAIL_SIZES` for the web UIs, and is skipped entirely for a
    family whose surface draws none.  *printer_model* names the machine —
    the config-declared model, or the slice door's printer id — and a model
    whose own screen reads a QOI block (:func:`screen_for_model`) gets that
    screen's blocks too, at every size it asks for, ahead of the PNGs; a
    controller that may carry such a screen (Duet's PanelDue) gets them
    through :attr:`GcodeSurface.qoi_screens` or, at the slice door, the
    catalogue's firmware family for the model.  The weight is read from
    the file's own moves by
    :func:`kiln.printers.bambu_3mf.filament_usage_from_gcode` and written
    only where the slicer left a zero or nothing.

    Idempotent: a second call finds the check satisfied and changes nothing.
    A file that already carries the web UIs' PNG (an earlier completion, a
    PrusaSlicer export) gets only the screen blocks it still lacks.
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

    present = _thumbnail_blocks(text)
    png_have = {(w, h) for suffix, w, h in present if suffix == ""}
    qoi_have = {(w, h) for suffix, w, h in present if suffix == "_QOI"}
    screens = _screens_for(surface, printer_model)

    # The order is the order the readers' budgets dictate.  The Buddy
    # screen and its web UI both look through the first 2048 lines only
    # (gcode_reader_plaintext.cpp L36), and a toolpath drawn from a dense
    # print costs several hundred lines a block; so: the preview the screen
    # refuses without, then the small icons the web lists want, then the
    # large web PNG (before the 32x32, since Buddy's /thumb/l/ takes the
    # first PNG bigger than 16x16), and the screen's progress pictures last
    # — the 440x240 fallback last of all, since a screen with a 480x240 in
    # hand never opens it.  Moonraker and the Python PrusaLink read a whole
    # MiB; OctoPrint reads up to the first extrusion; RepRapFirmware keeps
    # the first four blocks it meets.
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
