"""Bambu Lab 3MF packaging for PrusaSlicer gcode.

Wraps PrusaSlicer-generated gcode with BambuStudio's proprietary
start/end gcode and packages everything as a Bambu-compatible 3MF
file ready for upload and printing.

The Bambu A1 (and other Bambu printers) require a specific proprietary
initialization sequence in the start gcode for the extruder motor to
respond to E commands.  Without it, ``G1 E`` commands are silently
ignored — the head moves but nothing extrudes.  This module provides
that sequence.

The proven pipeline:
    1. PrusaSlicer slices the model with ``--use-relative-e-distances``
       and empty start/end gcode.
    2. This module wraps the gcode body with the BambuStudio A1 start
       gcode (~620 lines, including M620 M motor enable, AMS load,
       nozzle flush, extrusion calibration, bed leveling) and end gcode
       (~150 lines, AMS retract, cooldown, finish sound).
    3. PrusaSlicer's native ``M73 P R`` progress commands are stripped
       (they lack the ``L`` parameter and override layer tracking) and
       replaced with Bambu-compatible ``M73 L``, ``M991 S0 P0``, and
       ``M73 P R`` at each PrusaSlicer ``;LAYER_CHANGE`` marker.
    4. Everything is packaged as a Bambu 3MF with proper metadata.

Tested and verified on the Bambu Lab A1 Combo (firmware 01.08.03.00).

Where the sequences come from, and why start and end differ
-----------------------------------------------------------
Every sequence here is Bambu Lab's own, as the maker's slicer ships it for
that machine.  The end sequences are the maker's TEMPLATES, copied verbatim:
they carry the slicer's own expression language ---
``[bed_temperature_initial_layer_single]``,
``{nozzle_temperature_initial_layer[initial_extruder]}``, and
``{if ...}{else}{endif}`` blocks --- which this module resolves at build time.

``bambu_a1_start_gcode.gcode`` and ``bambu_a1_end_gcode.gcode`` are NOT
templates.  They are what a real slice with the maker's own profile wrote,
which is why they hold literal values and no placeholders, and why they are
20-odd lines longer than the templates: they also contain the slicer's
injected preamble (``M201``/``M203`` machine limits, ``M73`` progress) and
postamble (``; MACHINE_END_GCODE_END``, spaghetti detector).  They are the
A1-proven artifacts and are left exactly as they are.

The end capture is no longer what an A1 receives, though.  A capture holds
the numbers of the one print it was taken from, and two of the end block's
lifts are the part's own height: this one froze "100 mm above the part" as
``Z165``/``Z163``, so after any A1 print taller than about 188 mm the end
sequence lifted clear and then drove the gantry back down, and the X rail --
25 mm above the nozzle, across the plate -- came down on the part.  The A1
is now served ``bambu_a1_end_template.gcode``: Bambu's own A1 end template,
the same version as the capture, inside the capture's own slicer lines.
Filled in at 65 mm it is the capture byte for byte; at every other height it
is Bambu's own routine.  The capture stays, as the reference that pins
exactly that.

That is what splits start from end here:

* **End G-code is per model.**  Across every model's end template the only
  variable is ``max_layer_z`` --- a value this module already computes from
  the G-code body --- plus, on A1 and A1 mini, the bed centre and two
  slicing flags that are constants for Kiln.  Nothing has to be guessed, so
  each supported model ships its own end template and
  :func:`_resolve_end_gcode` expands it at build time.
* **Start G-code is per model too, and is captured rather than resolved.**
  The start templates depend on values the slicer works out while it slices
  and stores nowhere, and those pick the flush and the chamber, levelling
  and heating branches.  So each model's start is the sequence the maker's
  own slicer wrote for it: the slicer itself picks the flush values and the
  branches, and no number here was chosen by hand.

  What that buys is not cosmetic.  The A1 and A1 mini are bed-slingers whose
  startup drives X negative --- 54 and 33 such moves --- with ``M211 X0 Y0 Z0``
  disabling the soft endstops first.  The enclosed models make **no** negative-X
  move at all, and their bed centres differ (the H2S homes around X170 Y160,
  the P1P/P1S around X65 Y230).  Wrapping a P2S print in the A1 sequence sent
  a CoreXY machine off the front of its own bed with the endstops off.

Nozzle size is where start and end differ.  The maker publishes end G-code at
the 0.4 nozzle only --- one file per model, no per-nozzle variant --- so the end
template a 0.6 owner gets is the only one that exists rather than a 0.4 file
standing in for theirs.  Start G-code is the opposite: P1P, P1S, X1 Carbon and
X1E each publish four start templates (0.2 / 0.4 / 0.6 / 0.8) whose contents
genuinely differ, and ``[nozzle_diameter]`` appears in their conditionals.
``_MODEL_START_GCODE_FILES`` is therefore keyed on ``(model, nozzle)`` and every
capture so far is 0.4.  Any other nozzle gets its own machine's capture and says
so: across the vendor's own nozzle variants the head visits the same box and
treats the soft endstops the same, so another size can mis-tune the purge and
the flow calibration but cannot send the head anywhere this machine does not
already go -- which the A1's sequence, the old fallback, did.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import hashlib
import json
import logging
import math
import os
import re
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Estimated Bambu startup overhead in seconds (homing, AMS load, bed
# leveling, calibration).  Varies by printer and settings but 7 minutes
# is typical for the A1 with AMS.  Added to the slicer's pure-printing
# estimate so the firmware's remaining-time display is accurate from the
# very first second of the print.
_BAMBU_STARTUP_OVERHEAD_SEC = 420  # 7 minutes

# ---------------------------------------------------------------------------
# Data file paths
# ---------------------------------------------------------------------------

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_A1_START_GCODE_PATH = _DATA_DIR / "bambu_a1_start_gcode.gcode"
_A1_END_GCODE_PATH = _DATA_DIR / "bambu_a1_end_gcode.gcode"

# Lazy-loaded singletons for gcode templates.
_a1_start_gcode: str | None = None
_a1_end_gcode: str | None = None

# ---------------------------------------------------------------------------
# Per-model template registry
# ---------------------------------------------------------------------------
#
# Keyed on the printer model the OWNER DECLARED (``printer_model`` in
# ``~/.kiln/config.yaml``, or the profile id a caller passed to the slicer).
# Never on a model inferred from a serial prefix or a firmware string: a table
# that guessed wrong in five of six rows once named the wrong printer
# confidently, so the probes behind ``get_printer_info`` are telemetry only.
# See ``BambuAdapter._build_print_url`` for the same rule on job URLs.
#
# A model that is absent here, and any printer whose owner never declared a
# model, gets the A1 files --- byte-for-byte what every Bambu print has been
# wrapped in until now.  An unknown model is never an error: people are
# printing successfully on this fallback right now.

# Start G-code, keyed on (model, nozzle diameter) --- never model alone.
#
# The vendor publishes four start templates per model for P1P, P1S, X1 Carbon
# and X1E (0.2 / 0.4 / 0.6 / 0.8) whose contents genuinely differ, and
# ``[nozzle_diameter]`` appears inside their conditionals.  Handing a 0.6 owner
# the 0.4 sequence would be the same class of defect this table exists to end,
# so a nozzle with no template of its own is a miss, not a near-enough match.
#
# Every file here is a post-expansion CAPTURE of a real BambuStudio slice, the
# same way the A1 files were produced --- not a template this module resolved.
# That distinction is the whole reason these can ship: BambuStudio computed the
# AMS flush temperature and volumetric speed, chose the chamber-cooling and
# vitrification branches, and evaluated the bed-obstacle probe itself.  Nothing
# in them was picked by hand.  Re-capture, never hand-edit.
_MODEL_START_GCODE_FILES: dict[tuple[str, str], str] = {
    ("bambu_a1", "0.4"): "bambu_a1_start_gcode.gcode",
    ("bambu_a1_mini", "0.4"): "bambu_a1_mini_start_gcode.gcode",
    ("bambu_p1p", "0.4"): "bambu_p1p_start_gcode.gcode",
    ("bambu_p1s", "0.4"): "bambu_p1s_start_gcode.gcode",
    ("bambu_p2s", "0.4"): "bambu_p2s_start_gcode.gcode",
    ("bambu_x1c", "0.4"): "bambu_x1c_start_gcode.gcode",
    ("bambu_x1e", "0.4"): "bambu_x1e_start_gcode.gcode",
    ("bambu_h2s", "0.4"): "bambu_h2s_start_gcode.gcode",
    ("bambu_a2l", "0.4"): "bambu_a2l_start_gcode.gcode",
    ("bambu_h2d", "0.4"): "bambu_h2d_start_gcode.gcode",
    ("bambu_h2d_pro", "0.4"): "bambu_h2d_pro_start_gcode.gcode",
    ("bambu_h2c", "0.4"): "bambu_h2c_start_gcode.gcode",
    ("bambu_x2d", "0.4"): "bambu_x2d_start_gcode.gcode",
}

# End G-code, one file per model we ship a template for.
# ---------------------------------------------------------------------------
# Where the AMS block flushes
# ---------------------------------------------------------------------------
#
# Kiln slices with PrusaSlicer, which emits a bare ``T0``/``T1``; the Bambu
# firmware wants that wrapped in an M620/M621 AMS block, and the block has to
# take the head somewhere off the plate before it purges 50 mm of filament.
# That somewhere is a PER-MODEL FACT and it is not the same on two machines:
# the A1 cuts and drops into a chute off the plate's LEFT edge at X-48.2, the
# A1 mini's is at X-13.5, and the X1/P1 family has no single point at all --
# the vendor's own colour change purges at points only its slicer works out.
#
# WHY THERE IS ONE ENTRY.  Kiln's block is not the vendor's block.  The A1's
# vendor block purges on the RIGHT, at X267 Y128, and only then travels to the
# cutter at X-48.2; Kiln's block purges AT the cutter, which is a
# simplification that was run on the owner's own A1 and watched.  That makes
# it a bench fact about one machine, not a rule about Bambu printers -- so it
# cannot be carried to a sibling by analogy, however similar the frame looks.
# A model earns a line here when its own flush position has been run and
# watched.  Until then :func:`_wrap_tool_changes` refuses to write a
# multi-colour file for a model Kiln KNOWS is not an A1, rather than send its
# head to the A1's chute; a printer nobody declared keeps the A1 default that
# its warm-up and end sequence take too.
_MODEL_FLUSH_STATION: dict[str, tuple[float | None, float | None]] = {
    # Run on the owner's A1 Combo; ``None`` in Y keeps the head's own row.
    "bambu_a1": (-48.2, None),
}


_MODEL_END_GCODE_FILES: dict[str, str] = {
    # The A1's end is Bambu's own template -- the one stamped 20231229 that the
    # hardware-proven bambu_a1_end_gcode.gcode was captured from -- inside that
    # capture's own slicer wrapper (power-loss recovery off, the 0.04 mm
    # retract, fan and spaghetti detector off, and the closing M73).  Filled in
    # at the print's real height it is that capture byte for byte at 65 mm,
    # the height it was taken at, and Bambu's own routine at every other: the
    # capture froze "lift 100 mm above the part" as Z165, which after a print
    # taller than about 188 mm lowered the X rail onto the part.
    "bambu_a1": "bambu_a1_end_template.gcode",
    "bambu_a1_mini": "bambu_a1_mini_end_gcode.gcode",
    "bambu_p1p": "bambu_p1p_end_gcode.gcode",
    "bambu_p1s": "bambu_p1s_end_gcode.gcode",
    "bambu_p2s": "bambu_p2s_end_gcode.gcode",
    "bambu_x1c": "bambu_x1c_end_gcode.gcode",
    "bambu_x1e": "bambu_x1e_end_gcode.gcode",
    "bambu_h2s": "bambu_h2s_end_gcode.gcode",
    "bambu_a2l": "bambu_a2l_end_gcode.gcode",
    "bambu_h2d": "bambu_h2d_end_gcode.gcode",
    "bambu_h2d_pro": "bambu_h2d_pro_end_gcode.gcode",
    "bambu_x2d": "bambu_x2d_end_gcode.gcode",
    # The H2C's template also reads the AMS values in _MODEL_END_VALUES.
    "bambu_h2c": "bambu_h2c_end_gcode.gcode",
}

#: Values an end template reads besides the part's height, per model.
#:
#: The H2C's end pulls the filament back into the AMS, and its template asks
#: how: which filament and hotend, how far, how fast.  None of these moves the
#: head -- every X, Y and Z in the block is the part's own height, which Kiln
#: already has.  They are what the maker's own slicer writes for the H2C with
#: the same filament as its start sequence, and expanding the template with
#: them reproduces that slicer's own end block line for line, which
#: ``kiln/tests/data`` pins.  Like the flush values inside every start
#: sequence, they are Generic PLA's: filament 0 because a multi-colour H2C
#: file is refused before it is built, hotend -1 because that is what the
#: slicer writes for one filament, and the 12 mm3/s flush speed and 14 mm cut
#: retraction the Generic PLA preset gives the H2C.
_MODEL_END_VALUES: dict[str, dict[str, Any]] = {
    "bambu_h2c": {
        "current_filament_id": 0,
        "current_hotend": -1,
        "long_retraction_when_cut": True,
        "retraction_distance_when_cut": 14,
        "long_retraction_when_ec": False,
        "retraction_distance_when_ec": 0,
        "flush_volumetric_speeds": [12.0],
    },
}

# Lazy cache for the per-model files, keyed by filename.
_model_gcode_cache: dict[str, str] = {}

# Fallbacks already reported, so each is logged once per process rather than
# once per print.  Keyed by ``(kind, model)``: the start-gcode gap and the
# end-gcode gap are different facts about a model, and one must not silence
# the other.
_fallback_warned: set[tuple[str, str]] = set()


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class BambuPrintSettings:
    """Print-specific settings for Bambu 3MF building.

    All temperatures are in degrees Celsius.  ``hotend_temp``, ``bed_temp``
    and ``filament_type`` left ``None`` mean "the caller did not say": the
    build reads them off the G-code being wrapped -- the temperatures its
    first heating commands ask for and the ``filament_type`` its slicer
    wrote (every Kiln slice carries one, see :mod:`kiln.slicer_filament`)
    -- and falls back to PLA on the A1 (220 / 65 / ``PLA``) only for a body
    that says nothing.  A value the caller states always wins.  Whatever
    the type's origin, it reaches the printer in Bambu's own vocabulary
    (:func:`bambu_filament_type`).

    For multi-color prints, set ``num_filaments`` > 1 and provide
    ``filament_colors`` / ``filament_types`` lists with that many entries.
    """

    hotend_temp: int | None = None
    bed_temp: int | None = None
    filament_type: str | None = None
    filament_color: str = "#FFFFFF"
    nozzle_diameter: float = 0.4
    layer_height: float = 0.2
    bed_type: str = "textured_plate"
    model_name: str = "model"
    # Multi-filament (set num_filaments > 1 for multi-color)
    num_filaments: int = 1
    filament_colors: list[str] | None = None
    filament_types: list[str] | None = None

    def get_filament_colors(self) -> list[str]:
        """Return the filament color list, generating from defaults if needed."""
        if self.filament_colors and len(self.filament_colors) >= self.num_filaments:
            return self.filament_colors[: self.num_filaments]
        # Default: repeat the single color
        return [self.filament_color] * self.num_filaments

    def get_filament_types(self) -> list[str]:
        """Return the filament type list, generating from defaults if needed."""
        if self.filament_types and len(self.filament_types) >= self.num_filaments:
            return self.filament_types[: self.num_filaments]
        return [self.filament_type or _FALLBACK_FILAMENT_TYPE] * self.num_filaments

    def to_dict(self) -> dict[str, Any]:
        d = {
            "hotend_temp": self.hotend_temp,
            "bed_temp": self.bed_temp,
            "filament_type": self.filament_type,
            "filament_color": self.filament_color,
            "nozzle_diameter": self.nozzle_diameter,
            "layer_height": self.layer_height,
            "bed_type": self.bed_type,
            "model_name": self.model_name,
        }
        if self.num_filaments > 1:
            d["num_filaments"] = self.num_filaments
            d["filament_colors"] = self.get_filament_colors()
            d["filament_types"] = self.get_filament_types()
        return d


@dataclass
class Bambu3MFResult:
    """Result of building a Bambu 3MF."""

    output_path: str
    total_layers: int
    max_z: float
    file_size: int
    md5: str
    est_print_time_sec: int
    # The model whose start sequence this file actually carries, and the one
    # the caller asked for.  They differ whenever a template is missing, which
    # today is every model but the A1.
    start_gcode_model: str = "bambu_a1"
    #: The nozzle size that start sequence was captured for, and the size the
    #: print was sliced for.  They differ when this machine has no capture at
    #: the fitted size and its own capture at another size was used.
    start_gcode_nozzle: str = "0.4"
    requested_nozzle: str | None = None
    #: The model whose END sequence this file carries.  Separate from the
    #: start: a model can have one capture and not the other, and the end
    #: block is the one that parks the head over a finished plate.
    end_gcode_model: str = "bambu_a1"
    requested_model: str | None = None
    #: A quiet start: the file opens with Kiln's own prologue instead of
    #: the vendor's start, and every Kiln-owned lift in it rises to the floor.
    quiet_start: bool = False
    lift_floor_mm: float | None = None
    #: What the printer is told -- the filament type in Bambu's vocabulary
    #: and the temperatures the start sequence heats to -- after the
    #: caller's word, the G-code's own, and the fallback were reconciled.
    filament_type: str = "PLA"
    hotend_temp: int = 220
    bed_temp: int = 65

    @property
    def start_gcode_warning(self) -> str | None:
        """Say so when the start sequence is not this machine's own.

        The fallback itself is deliberate and long-standing --- see
        :data:`_MODEL_START_GCODE_FILES`.  What was missing is that nobody
        downstream could tell it had happened: the substitution was a log line
        on a server the operator is not reading.  This is the same fact on the
        object every caller already gets back, so a tool response can carry it
        to the person deciding whether to press print.
        """
        requested = _normalize_model(self.requested_model)
        if not requested or requested == self.start_gcode_model:
            if self.requested_nozzle and self.requested_nozzle != self.start_gcode_nozzle:
                return (
                    f"This file's warm-up is {self.start_gcode_model}'s own, captured for a "
                    f"{self.start_gcode_nozzle} mm nozzle, not the {self.requested_nozzle} mm one this "
                    f"print is sliced for: Kiln has no {self.requested_nozzle} mm capture of it. The "
                    f"head goes only where this machine's own warm-up goes, but the purge and the "
                    f"flow calibration are tuned for {self.start_gcode_nozzle} mm. Watch the purge "
                    f"line and the first layer."
                )
            return None
        return (
            f"This file carries the {self.start_gcode_model} startup sequence, not "
            f"{requested}'s: Kiln ships no validated start G-code for {requested}. "
            f"The print will start, and prints are running on this fallback today, "
            f"but the homing, purge and bed-levelling moves are the A1's — it drives "
            f"X negative and disables soft endstops. Watch the first layer."
        )

    @property
    def end_gcode_warning(self) -> str | None:
        """Say so when the END sequence is not this machine's own.

        The start's substitution has been carried to the caller since it was
        found; the end's was logged and nowhere else, which is the same gap
        one door along.  The end block is the one that runs with a finished
        part on the plate -- it wipes, parks and travels the bed's full
        length -- so a machine printing another model's end moves is a fact
        the person pressing print is entitled to.
        """
        requested = _normalize_model(self.requested_model)
        if not requested or requested == self.end_gcode_model:
            return None
        return (
            f"This file carries the {self.end_gcode_model} end sequence, not "
            f"{requested}'s: Kiln ships no validated end G-code for {requested}. "
            f"The print will finish, but the wipe, park and presenting moves are "
            f"the {self.end_gcode_model}'s and are aimed at its plate, not this one's. "
            f"Watch the head after the last layer."
        )

    def to_dict(self) -> dict[str, Any]:
        d = {
            "output_path": self.output_path,
            "total_layers": self.total_layers,
            "max_z": self.max_z,
            "file_size": self.file_size,
            "md5": self.md5,
            "est_print_time_sec": self.est_print_time_sec,
            "start_gcode_model": self.start_gcode_model,
            "start_gcode_nozzle": self.start_gcode_nozzle,
            "end_gcode_model": self.end_gcode_model,
        }
        if self.start_gcode_warning:
            d["start_gcode_warning"] = self.start_gcode_warning
        if self.end_gcode_warning:
            d["end_gcode_warning"] = self.end_gcode_warning
        return d


# ---------------------------------------------------------------------------
# Template loading (lazy singletons)
# ---------------------------------------------------------------------------


def _load_a1_start_gcode() -> str:
    """Load the A1 start gcode template."""
    global _a1_start_gcode  # noqa: PLW0603
    if _a1_start_gcode is None:
        if not _A1_START_GCODE_PATH.is_file():
            msg = f"Bambu A1 start gcode not found: {_A1_START_GCODE_PATH}"
            raise FileNotFoundError(msg)
        _a1_start_gcode = _A1_START_GCODE_PATH.read_text(encoding="utf-8")
    return _a1_start_gcode


def _load_a1_end_gcode() -> str:
    """The hardware-proven A1 end capture, as captured: frozen at a 65 mm
    print.  Kept as the reference the A1's end template is checked against,
    never sent to a printer -- :func:`_select_end_gcode` serves the template."""
    global _a1_end_gcode  # noqa: PLW0603
    if _a1_end_gcode is None:
        if not _A1_END_GCODE_PATH.is_file():
            msg = f"Bambu A1 end gcode not found: {_A1_END_GCODE_PATH}"
            raise FileNotFoundError(msg)
        _a1_end_gcode = _A1_END_GCODE_PATH.read_text(encoding="utf-8")
    return _a1_end_gcode


def _load_model_template(filename: str) -> str:
    """Load a per-model gcode template from ``kiln/data``, cached by name."""
    cached = _model_gcode_cache.get(filename)
    if cached is None:
        path = _DATA_DIR / filename
        if not path.is_file():
            msg = f"Bambu gcode template not found: {path}"
            raise FileNotFoundError(msg)
        cached = path.read_text(encoding="utf-8")
        _model_gcode_cache[filename] = cached
    return cached


def _normalize_model(printer_model: str | None) -> str:
    """Normalize a declared printer model id for registry lookup."""
    return (printer_model or "").strip().lower()


def _nozzle_key(nozzle_diameter: float | str | None) -> str:
    """Normalize a nozzle diameter to the registry's key form ("0.4")."""
    try:
        return f"{float(nozzle_diameter):.1f}"
    except (TypeError, ValueError):
        return ""


def _start_gcode_choice(
    printer_model: str | None,
    nozzle_diameter: float | str | None = 0.4,
) -> tuple[str, str, str]:
    """Pick the start gcode for a DECLARED printer model and nozzle.

    Returns ``(gcode_text, source_model, source_nozzle)`` -- the machine and
    the nozzle size the sequence was actually captured for, which is what the
    caller should believe the file is flavoured for.

    Three cases, in order:

    1. **This machine, this nozzle** -- a capture of its own.
    2. **This machine, another nozzle** -- the machine's OWN capture at the
       size it was taken.  The vendor's start templates differ by nozzle size,
       but only in how much they extrude: across Bambu's own 0.2/0.4/0.6/0.8
       variants of the X1C and P1S the head visits the same box (X 18-240,
       Y -3 to 265) and treats the soft endstops the same, and every waypoint
       one size adds lies inside the box another size already visits.  So a
       borrowed size can mis-tune the purge and the flow calibration; it
       cannot send the head anywhere this machine's own warm-up does not go.
       The A1's sequence -- the fallback this case used to take -- disables
       the soft endstops and drives X to -48.2, which on an enclosed CoreXY
       is the frame.  The substitution is logged once and carried to the
       caller by :attr:`Bambu3MFResult.start_gcode_warning`.
    3. **A machine with no capture at all** -- the A1's, as it always has
       been.  It is a real substitution rather than a near-miss (the A1 and
       A1 mini are bed-slingers whose startup drives X negative with the soft
       endstops disabled, while every enclosed model here makes no
       negative-X move at all), so it is logged once per model per process
       and :attr:`Bambu3MFResult.start_gcode_warning` carries it too.
    """
    model = _normalize_model(printer_model)
    nozzle = _nozzle_key(nozzle_diameter)
    filename = _MODEL_START_GCODE_FILES.get((model, nozzle))
    if filename is not None:
        return _load_model_template(filename), model, nozzle

    own = sorted(n for m, n in _MODEL_START_GCODE_FILES if m == model)
    if own:
        captured = "0.4" if "0.4" in own else own[0]
        if ("start", model, nozzle) not in _fallback_warned:
            _fallback_warned.add(("start", model, nozzle))
            logger.warning(
                "No start gcode for %s at a %s nozzle — using %s's own %s sequence.  The head "
                "goes only where this machine's own warm-up goes; the purge and the flow "
                "calibration are tuned for a %s nozzle.",
                model, nozzle or "unknown", model, captured, captured,
            )
        return _load_model_template(_MODEL_START_GCODE_FILES[(model, captured)]), model, captured

    if model and ("start", model, nozzle) not in _fallback_warned:
        _fallback_warned.add(("start", model, nozzle))
        logger.warning(
            "No start gcode for %s at a %s nozzle (no start sequence is captured for %s) — "
            "using the Bambu A1 sequence, which is flavoured for the A1: its bed coordinates, "
            "purge and calibration moves, including moves to negative X with the soft "
            "endstops disabled.  The print will be wrapped and can start, but the startup "
            "sequence is not this model's own.",
            model, nozzle or "unknown", model,
        )
    return _load_a1_start_gcode(), "bambu_a1", "0.4"


def _select_start_gcode(
    printer_model: str | None,
    nozzle_diameter: float | str | None = 0.4,
) -> tuple[str, str]:
    """``(gcode_text, source_model)`` from :func:`_start_gcode_choice`."""
    text, source_model, _source_nozzle = _start_gcode_choice(printer_model, nozzle_diameter)
    return text, source_model


def flush_station_for(printer_model: str | None) -> tuple[float | None, float | None] | None:
    """Where this model's AMS block takes the head to flush.

    A model with a station on record gets it.  A model Kiln KNOWS -- one with
    its own captured warm-up -- but whose chute is not on record gets
    ``None``: a refusal the caller must act on, never a cue to reach for a
    sibling's figure, because Kiln knows that machine is not an A1.  A
    printer nobody declared, or one Kiln has never heard of, gets the A1's,
    the same long-standing default its warm-up and end sequence take:
    people print on that path, and nothing says the machine is not an A1.
    """
    model = _normalize_model(printer_model)
    if model in _MODEL_FLUSH_STATION:
        return _MODEL_FLUSH_STATION[model]
    if any(known == model for known, _nozzle in _MODEL_START_GCODE_FILES):
        return None
    return _MODEL_FLUSH_STATION["bambu_a1"]


def _select_end_gcode(printer_model: str | None) -> tuple[str, str]:
    """Pick the end gcode template for a DECLARED printer model.

    Returns ``(template_text, source_model)``.  Falls back to the A1 end
    sequence for a model with no template and for a printer whose owner never
    declared one.
    """
    model = _normalize_model(printer_model)
    filename = _MODEL_END_GCODE_FILES.get(model)
    if filename is not None:
        return _load_model_template(filename), model

    if model and ("end", model) not in _fallback_warned:
        _fallback_warned.add(("end", model))
        logger.warning(
            "No end gcode template for %s — using the Bambu A1 end sequence.",
            model,
        )
    return _load_model_template(_MODEL_END_GCODE_FILES["bambu_a1"]), "bambu_a1"


# ---------------------------------------------------------------------------
# Template resolution
# ---------------------------------------------------------------------------

# A placeholder that survives resolution would be sent to the printer
# verbatim, so resolution fails loudly instead.  Both shapes BambuStudio uses:
# ``[option_name]`` and ``{expression}``.  The proven A1 files contain neither
# character, and this check only ever runs over our own templates --- never
# over the slicer's gcode body, whose comments legitimately carry brackets.
_BRACKET_PLACEHOLDER_RE = re.compile(r"\[[a-z_][a-z0-9_\[\]]*\]", re.IGNORECASE)
_BRACE_PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")


def _assert_fully_resolved(gcode: str, *, source: str) -> None:
    """Refuse to emit a template that still carries a placeholder.

    :param gcode: Resolved template text.
    :param source: Human-readable description for the error message.
    :raises ValueError: If any unresolved placeholder remains.
    """
    for pattern in (_BRACE_PLACEHOLDER_RE, _BRACKET_PLACEHOLDER_RE):
        match = pattern.search(gcode)
        if match:
            line = gcode.count("\n", 0, match.start()) + 1
            msg = (
                f"Unresolved gcode placeholder {match.group(0)!r} in {source} "
                f"at line {line}.  Refusing to build a 3MF: this text is sent "
                f"to the printer verbatim."
            )
            raise ValueError(msg)

# Fixed temperatures in the A1 start gcode that must NOT be replaced:
#   140°C — initial nozzle preheat for bed leveling
#   170°C — nozzle wipe temperature
#   250°C — filament flush temperature
#   25°C  — cooldown check
# Only 220°C (PLA print temp) and 65°C (PLA bed temp) are parametric.


_CAPTURE_HOTEND_TEMP = 220  # every start sequence here holds PLA's 220C

#: What a settings field falls back to when neither the caller nor the
#: G-code says: PLA on the A1, the values every capture was taken with.
_FALLBACK_HOTEND_TEMP = 220
_FALLBACK_BED_TEMP = 65
_FALLBACK_FILAMENT_TYPE = "PLA"

#: The filament types Bambu's firmware is written to -- every distinct
#: ``filament_type`` across the filament presets the maker's own slicer
#: ships.  ``M1002 set_filament_type:`` in the start sequence
#: and ``<filament type="…">`` in ``slice_info.config`` are read by the
#: machine, so a word outside this list is mapped onto it or, failing that,
#: sent as ``PLA`` -- what every Kiln wrap told the printer before the type
#: was read off the G-code at all, so an exotic word changes nothing about
#: what the machine hears.  (``UNKNOWN`` is a value Studio writes only
#: transiently, before the real type; what the firmware makes of it for a
#: whole job is unverified, so it is not used.)
BAMBU_FILAMENT_TYPES: frozenset[str] = frozenset({
    "ABS", "ABS-GF", "ASA", "ASA-AERO", "ASA-CF", "BVOH", "EVA", "HIPS",
    "PA", "PA-CF", "PA-GF", "PA6-CF", "PC", "PCTG", "PE", "PE-CF", "PET-CF",
    "PETG", "PETG-CF", "PHA", "PLA", "PLA-AERO", "PLA-CF", "PP", "PP-CF",
    "PP-GF", "PPA-CF", "PPA-GF", "PPS", "PPS-CF", "PVA", "TPU", "TPU-AMS",
})
#: Kiln's material-table rows spelled the way Bambu spells them.
_TABLE_ROW_TO_BAMBU: dict[str, str] = {
    "CF-PLA": "PLA-CF",
    "NYLON": "PA",
    "PLA+": "PLA",
    "SILK-PLA": "PLA",
}


def bambu_filament_type(word: str | None) -> str:
    """*word* as the filament type a Bambu understands.

    The word itself when it is one of Bambu's (``PLA-CF``, ``PETG``, in
    any case); else Kiln's material row for it, spelled Bambu's way
    (``CF-PLA`` -> ``PLA-CF``, ``NYLON`` -> ``PA``); else the family it
    starts with when Bambu has that (``PETG-HF`` -> ``PETG``); else
    ``PLA``, the historical fallback.  Never raises.
    """
    text = " ".join(str(word or "").split()).upper()
    if not text:
        return _FALLBACK_FILAMENT_TYPE
    if text in BAMBU_FILAMENT_TYPES:
        return text
    try:
        from kiln.slicer_filament import material_density

        row = material_density(text)
    except Exception:  # noqa: BLE001 -- the table is a lookup, never a failure
        row = None
    if row is not None:
        spelled = _TABLE_ROW_TO_BAMBU.get(row[0], row[0])
        if spelled in BAMBU_FILAMENT_TYPES:
            return spelled
    family = re.match(r"[A-Z]+", text)
    if family and family.group(0) in BAMBU_FILAMENT_TYPES:
        return family.group(0)
    return _FALLBACK_FILAMENT_TYPE


#: The temperatures a body prints its first layer at.  The command the
#: print WAITS on (``M109`` / ``M190``) is read before a set-only one
#: (``M104`` / ``M140``): a body that opens with a 140 °C preheat, as
#: Bambu-style start blocks do, still heats the start sequence to the
#: temperature it printed at.  Both slicers write these at the top of the
#: body when the profile's start block is empty, as Kiln's Bambu profiles
#: leave it: PrusaSlicer ``M190 S65`` / ``M104 S220`` / ``M109 S220``, Orca
#: ``M190 S60`` / ``M109 S225`` (measured 2026-09-20 on a 20 mm cube).  A
#: body with no such command is read from its footer's first-layer keys.
_GCODE_HOTEND_WAIT_RE = re.compile(r"^\s*M109\s+(?:T\d+\s+)?S(\d+(?:\.\d+)?)", re.MULTILINE)
_GCODE_HOTEND_SET_RE = re.compile(r"^\s*M104\s+(?:T\d+\s+)?S(\d+(?:\.\d+)?)", re.MULTILINE)
_GCODE_BED_WAIT_RE = re.compile(r"^\s*M190\s+S(\d+(?:\.\d+)?)", re.MULTILINE)
_GCODE_BED_SET_RE = re.compile(r"^\s*M140\s+S(\d+(?:\.\d+)?)", re.MULTILINE)
_GCODE_FOOTER_HOTEND_RE = re.compile(
    r"^;\s*(?:first_layer_temperature|nozzle_temperature_initial_layer|temperature|nozzle_temperature)\s*=\s*(\d+)",
    re.MULTILINE,
)
_GCODE_FOOTER_BED_RE = re.compile(
    r"^;\s*(?:first_layer_bed_temperature|bed_temperature)\s*=\s*(\d+)", re.MULTILINE,
)


def _print_temperatures(gcode_body: str) -> tuple[int | None, int | None]:
    """``(hotend, bed)`` the body prints at, or ``None`` where it never says."""

    def _first(*patterns: re.Pattern[str]) -> int | None:
        for pattern in patterns:
            for match in pattern.finditer(gcode_body):
                value = int(float(match.group(1)))
                if value > 0:
                    return value
        return None

    return (
        _first(_GCODE_HOTEND_WAIT_RE, _GCODE_HOTEND_SET_RE, _GCODE_FOOTER_HOTEND_RE),
        _first(_GCODE_BED_WAIT_RE, _GCODE_BED_SET_RE, _GCODE_FOOTER_BED_RE),
    )


def resolve_settings_from_gcode(settings: BambuPrintSettings, gcode_body: str) -> BambuPrintSettings:
    """The settings the build runs with: the caller's word, else the G-code's own, else the fallback.

    The G-code is the artifact that knows what the slice was for: the type
    its slicer wrote (``; filament_type = PETG``, the resolved material of
    a Kiln slice) and the temperatures it heats to.  A caller that stated a
    value keeps it.  Every type -- stated, read, or fallen back to -- is
    then put into Bambu's vocabulary, so nothing outside it reaches the
    machine.
    """
    hotend, bed = _print_temperatures(gcode_body)
    filament_type = settings.filament_type
    if not filament_type:
        match = _GCODE_FILAMENT_TYPE_RE.search(gcode_body)
        if match:
            filament_type = match.group(1).split(";")[0].split(",")[0].strip()
    filament_types = (
        [bambu_filament_type(t) for t in settings.filament_types]
        if settings.filament_types else None
    )
    return replace(
        settings,
        hotend_temp=settings.hotend_temp if settings.hotend_temp is not None else (hotend or _FALLBACK_HOTEND_TEMP),
        bed_temp=settings.bed_temp if settings.bed_temp is not None else (bed or _FALLBACK_BED_TEMP),
        filament_type=bambu_filament_type(filament_type or _FALLBACK_FILAMENT_TYPE),
        filament_types=filament_types,
    )


def _capture_bed_temp(template: str) -> int | None:
    """The bed temperature a captured start sequence was taken at.

    Read from the capture instead of assumed, because it is not one number
    across the bundle: the A1 and P1P captured at 65C and the P2S, P1S, X1
    Carbon, X1E and H2S at 55C, all for the same Generic PLA.  Assuming the
    A1's 65 left the substitution below silently doing nothing on five of
    eight machines, which pins a print to the capture's bed temperature no
    matter what the caller asked for.
    """
    values = [int(v) for v in re.findall(r"^\s*M1[49]0 S(\d+)", template, re.M)]
    values = [v for v in values if v > 0]
    if not values:
        return None
    return max(set(values), key=values.count)


def _resolve_start_gcode(
    template: str,
    *,
    hotend_temp: int = 220,
    bed_temp: int = 65,
    filament_type: str = "PLA",
) -> str:
    """Put this print's temperatures into a captured start sequence.

    The captures are post-expansion G-code, so this is a substitution of the
    values the capture was taken with --- not template resolution.  Fixed init
    temperatures (140C preheat, 250C flush, 170C wipe) are left alone, and so
    is anything at a temperature the capture did not use for the print itself.
    """
    capture_bed = _capture_bed_temp(template)
    lines = template.split("\n")
    resolved: list[str] = []

    for line in lines:
        stripped = line.strip()

        # Replace hotend temp: M104/M109 S220 → S{hotend_temp}
        if hotend_temp != _CAPTURE_HOTEND_TEMP and (
            stripped.startswith(f"M104 S{_CAPTURE_HOTEND_TEMP}")
            or stripped.startswith(f"M109 S{_CAPTURE_HOTEND_TEMP}")
        ):
            line = line.replace(f"S{_CAPTURE_HOTEND_TEMP}", f"S{hotend_temp}")

        # Replace bed temp: M140/M190 S{capture} → S{bed_temp}
        elif (
            capture_bed is not None
            and bed_temp != capture_bed
            and (
                stripped.startswith(f"M140 S{capture_bed}")
                or stripped.startswith(f"M190 S{capture_bed}")
            )
        ):
            line = line.replace(f"S{capture_bed}", f"S{bed_temp}")

        # Replace filament type (skip UNKNOWN lines — fixed for AMS switching)
        elif filament_type != "PLA" and "set_filament_type:PLA" in line:
            line = line.replace("set_filament_type:PLA", f"set_filament_type:{filament_type}")

        resolved.append(line)

    return "\n".join(resolved)


# ---------------------------------------------------------------------------
# BambuStudio end-template expansion
# ---------------------------------------------------------------------------
#
# The per-model end templates are shipped verbatim from BambuStudio's bundle so
# they stay diffable against it, which means they arrive carrying that tool's
# own expression syntax.  Expanding it needs a tiny evaluator, and this one is
# deliberately tiny: it covers exactly the surface the end templates use and
# refuses everything else.  Every value it needs is a value Kiln really has.
#
# Validated against ground truth: expanding the bundle's A1 end template with
# max_layer_z=65 and bed centre 128 reproduces the hardware-proven
# bambu_a1_end_gcode.gcode capture, guard lines and blank lines included.
# See test_bundle_a1_template_expands_to_the_proven_capture.

# `{if cond}` / `{else}` / `{endif}` occupy whole lines, nested up to two deep.
_TPL_IF_RE = re.compile(r"^\s*\{if\s+(?P<cond>.+)\}\s*$")
_TPL_ELSE_RE = re.compile(r"^\s*\{else\}\s*$")
_TPL_ENDIF_RE = re.compile(r"^\s*\{endif\}\s*$")
_TPL_EXPR_RE = re.compile(r"\{(?P<expr>[^{}]*)\}")
#: BambuStudio's plain-variable placeholder, ``[current_hotend]``.  Only a
#: lower-case name is one: the end templates' comments carry no brackets.
_TPL_VAR_RE = re.compile(r"\[(?P<name>[a-z_][a-z0-9_]*)\]")

# Slicing flags the templates branch on.  Constants for Kiln: this pipeline
# slices one plate, layer by layer, and never in vase mode.  Both readings are
# what BambuStudio itself resolved them to in the proven A1 capture.
_KILN_SPIRAL_MODE = False
_KILN_PRINT_SEQUENCE = "by layer"

_PRINTER_INTEL_PATH = _DATA_DIR / "printer_intelligence.json"
_printer_intel_raw: dict[str, Any] | None = None


def _bed_center(printer_model: str) -> tuple[float, float] | None:
    """Bed centre ``(x, y)`` in mm for a DECLARED model, or ``None`` if unknown.

    Read from ``printer_intelligence.json`` — the same spec sheet the rest of
    Kiln reads — rather than copied into a table here, because a second copy
    of a machine's bed size drifts silently and keeps answering confidently.

    Exact key only: no fuzzy prefix match and no ``"default"`` profile.  This
    number becomes a travel move, so an unrecognised model gets ``None`` and
    the caller refuses instead of parking the head somewhere plausible.
    """
    global _printer_intel_raw  # noqa: PLW0603
    if _printer_intel_raw is None:
        try:
            _printer_intel_raw = json.loads(
                _PRINTER_INTEL_PATH.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            _printer_intel_raw = {}
    entry = _printer_intel_raw.get(_normalize_model(printer_model))
    if not isinstance(entry, dict):
        return None
    volume = entry.get("build_volume_mm")
    if isinstance(volume, dict):
        width, depth = volume.get("x"), volume.get("y")
    elif isinstance(volume, (list, tuple)) and len(volume) >= 2:
        width, depth = volume[0], volume[1]
    else:
        return None
    try:
        return float(width) / 2.0, float(depth) / 2.0
    except (TypeError, ValueError):
        return None


def _format_template_number(value: float) -> str:
    """Format a resolved number the way BambuStudio writes it.

    An integral result loses its decimal point (``165.0`` → ``165``), matching
    the proven A1 capture; a fractional one keeps only the digits it needs.
    """
    if isinstance(value, bool):  # bool is an int subclass — not a coordinate
        msg = f"Boolean {value!r} used where a number was expected"
        raise ValueError(msg)
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def _eval_template_expr(expr: str, variables: dict[str, Any]) -> Any:
    """Evaluate one BambuStudio template expression.

    Supports only what the end templates contain: arithmetic, comparison,
    ``&&`` / ``||`` / ``!``, string equality, indexing a known list, and
    ``max``/``min`` of plain numbers (the H2C's end works out its AMS
    retraction speed as ``max(<flush speed>/2.4053*60, 200)``).  A
    name it was not given, or any other syntax, raises — this text ends up on
    a printer, so an expression we do not fully understand must not produce a
    number anyway.

    :raises ValueError: On unknown names or unsupported syntax.
    """
    # BambuStudio spells the boolean operators in C.  Rewrite `!` only when it
    # is negation, never when it is the `!=` in `print_sequence != "by object"`.
    py_expr = expr.replace("&&", " and ").replace("||", " or ")
    # Strip afterwards: rewriting a leading `!` leaves whitespace that
    # `ast.parse` in eval mode reads as an indent.
    py_expr = re.sub(r"!(?!=)", " not ", py_expr).strip()

    try:
        tree = ast.parse(py_expr, mode="eval")
    except SyntaxError as exc:
        msg = f"Cannot parse gcode template expression {expr!r}: {exc}"
        raise ValueError(msg) from exc

    def visit(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in variables:
                msg = (
                    f"Gcode template expression {expr!r} needs {node.id!r}, "
                    f"which Kiln has no value for."
                )
                raise ValueError(msg)
            return variables[node.id]
        if isinstance(node, ast.Subscript):
            container = visit(node.value)
            index = visit(node.slice)
            if not isinstance(container, (list, tuple)) or not isinstance(index, int):
                msg = f"Unsupported subscript in gcode template expression {expr!r}"
                raise ValueError(msg)
            return container[index]
        if isinstance(node, ast.UnaryOp):
            operand = visit(node.operand)
            if isinstance(node.op, ast.Not):
                return not operand
            if isinstance(node.op, ast.USub):
                return -operand
            if isinstance(node.op, ast.UAdd):
                return +operand
        elif isinstance(node, ast.BoolOp):
            values = [visit(v) for v in node.values]
            if isinstance(node.op, ast.And):
                return all(values)
            if isinstance(node.op, ast.Or):
                return any(values)
        elif isinstance(node, ast.BinOp):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                # BambuStudio divides two integers as integers — measured on
                # the A1 start capture, where `{...\/(24\/20) * 60}` came out
                # 720 and not 600.  Nothing in the end templates does that, so
                # rather than guess which semantics a future template wants,
                # refuse the ambiguous case.
                if isinstance(left, int) and isinstance(right, int):
                    msg = (
                        f"Integer division in gcode template expression {expr!r}: "
                        f"BambuStudio truncates here and Kiln will not guess."
                    )
                    raise ValueError(msg)
                return left / right
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("max", "min")
            and len(node.args) >= 2
            and not node.keywords
        ):
            args = [visit(a) for a in node.args]
            if not all(isinstance(a, (int, float)) and not isinstance(a, bool) for a in args):
                msg = f"{node.func.id}() of something other than numbers in gcode template expression {expr!r}"
                raise ValueError(msg)
            return max(args) if node.func.id == "max" else min(args)
        elif isinstance(node, ast.Compare) and len(node.ops) == 1:
            left = visit(node.left)
            right = visit(node.comparators[0])
            op = node.ops[0]
            if isinstance(op, ast.Lt):
                return left < right
            if isinstance(op, ast.LtE):
                return left <= right
            if isinstance(op, ast.Gt):
                return left > right
            if isinstance(op, ast.GtE):
                return left >= right
            if isinstance(op, ast.Eq):
                return left == right
            if isinstance(op, ast.NotEq):
                return left != right
        msg = (
            f"Unsupported syntax {type(node).__name__} in gcode template "
            f"expression {expr!r}"
        )
        raise ValueError(msg)

    return visit(tree)


def _indent_of(line: str) -> str:
    """The leading whitespace BambuStudio leaves where a guard line was."""
    return line[: len(line) - len(line.lstrip())]


def _template_variable(name: str, variables: dict[str, Any], line: str) -> str:
    """A ``[name]`` placeholder's value, or a refusal naming it."""
    if name not in variables:
        msg = f"Gcode template line {line.strip()!r} needs {name!r}, which Kiln has no value for."
        raise ValueError(msg)
    return _format_template_number(variables[name])


def _expand_end_template(template: str, variables: dict[str, Any]) -> str:
    """Expand a BambuStudio end-gcode template's conditionals and expressions.

    Whitespace follows BambuStudio's own output, which the A1 and H2C
    ground-truth diffs pin exactly: an ``{if}`` or ``{endif}`` guard becomes a
    blank line that keeps the guard's own indentation, an ``{else}`` and every
    line of the branch not taken disappear, and the branch that is taken keeps
    its original indentation.  ``[name]`` is a plain variable.

    A template with no braces — the proven A1 capture — comes back unchanged.

    :raises ValueError: On unbalanced conditionals or an expression that
        cannot be resolved.
    """
    out: list[str] = []
    # One frame per open `{if}`: (branch_active, parent_was_emitting).
    stack: list[tuple[bool, bool]] = []

    def emitting() -> bool:
        return all(active for active, _ in stack)

    for lineno, line in enumerate(template.split("\n"), 1):
        if_match = _TPL_IF_RE.match(line)
        if if_match:
            parent = emitting()
            active = bool(_eval_template_expr(if_match.group("cond"), variables)) if parent else False
            stack.append((active, parent))
            if parent:
                out.append(_indent_of(line))
            continue
        if _TPL_ELSE_RE.match(line):
            if not stack:
                msg = f"Gcode template has {{else}} with no {{if}} at line {lineno}"
                raise ValueError(msg)
            active, parent = stack[-1]
            stack[-1] = ((not active) if parent else False, parent)
            continue
        if _TPL_ENDIF_RE.match(line):
            if not stack:
                msg = f"Gcode template has {{endif}} with no {{if}} at line {lineno}"
                raise ValueError(msg)
            _, parent = stack.pop()
            if parent:
                out.append(_indent_of(line))
            continue
        if not emitting():
            continue
        expanded = _TPL_EXPR_RE.sub(
            lambda m: _format_template_number(
                _eval_template_expr(m.group("expr"), variables)
            ),
            line,
        )
        out.append(_TPL_VAR_RE.sub(
            lambda m, whole=line: _template_variable(m.group("name"), variables, whole), expanded,
        ))

    if stack:
        msg = f"Gcode template has {len(stack)} unclosed {{if}} block(s)"
        raise ValueError(msg)
    return "\n".join(out)


def _machine_top_mm(printer_model: str | None) -> float | None:
    """How high this machine's Z goes, from the public catalogue: the
    firmware's own travel limit where it is on record, else the build
    volume's height.  ``None`` for a machine the catalogue does not know --
    no ceiling is invented for it, least of all the A1's, whose end sequence
    an unknown machine borrows but whose height it need not share."""
    if not printer_model:
        return None
    from kiln.printers.bed_fit import _load_printer_intelligence, _printer_id_candidates, get_build_volume

    catalogue = _load_printer_intelligence()
    for candidate in _printer_id_candidates(_normalize_model(printer_model)):
        entry = catalogue.get(candidate)
        if not isinstance(entry, dict):
            continue
        motion = entry.get("motion")
        travel = motion.get("z_travel_limit_mm") if isinstance(motion, dict) else None
        with contextlib.suppress(TypeError, ValueError):
            if travel is not None and float(travel) > 0:
                return float(travel)
        volume = get_build_volume(candidate)
        return float(volume[2]) if volume else None
    return None


def _end_template_variables(max_z: float, printer_model: str | None) -> dict[str, Any]:
    """Everything an end template may read, for one print on one model: the
    part's height, the two slicing flags that are constants for Kiln, the bed
    centre where the model has one on record, and the model's own entries in
    :data:`_MODEL_END_VALUES`."""
    variables: dict[str, Any] = {
        "max_layer_z": float(max_z),
        "spiral_mode": _KILN_SPIRAL_MODE,
        "print_sequence": _KILN_PRINT_SEQUENCE,
        **_MODEL_END_VALUES.get(_normalize_model(printer_model), {}),
    }
    center = _bed_center(printer_model) if printer_model else None
    if center is not None:
        # BambuStudio indexes this as a point; the templates only read [1].
        variables["first_layer_center_no_wipe_tower"] = [center[0], center[1]]
    return variables


def _resolve_end_gcode(
    template: str,
    *,
    max_z: float = 65.0,
    printer_model: str | None = None,
    lift_floor_mm: float | None = None,
    z_top_mm: float | None = None,
) -> str:
    """Resolve an end gcode template with print-specific values.

    Two steps, in this order:

    1. Expand BambuStudio's template syntax against the real print height.
       A template with no expressions passes through untouched.
    2. Adjust the safe Z-move height.  The first ``G1 Z... F900`` command is
       the safe-move after the last layer — it needs to clear the print.
       Kiln lifts ``max_z + 5.0`` where Bambu's own template asks for
       ``max_layer_z + 0.5``; the larger clearance is the A1-proven behaviour
       and is applied to every model so there is one rule, not eight.

    With a *lift_floor_mm* -- the plate holds OTHER parts, and the floor is
    above the tallest of them -- every absolute Z the block commands is
    raised to at least the floor, and none is lowered: the vendor's lines
    stay in the vendor's order, only the heights change.  The end block
    travels to the plate's centre and rolls the bed to park, and a part
    beside the finished one is in that path at the vendor's own height.

    :param printer_model: Declared model, used only to look up the bed centre
        for templates that park on it.
    :param z_top_mm: How high the machine the file is for can go
        (:func:`_machine_top_mm` of the DECLARED model, not of the model whose
        template this is): the first lift is ``max_z + 5`` but never past it.
        ``None`` leaves the lift uncapped.
    """
    expanded = _expand_end_template(template, _end_template_variables(max_z, printer_model))

    safe_z = max_z + 5.0
    if z_top_mm is not None:
        # Never past the machine's own top: the A1, H2 and P2S warm-ups leave
        # the soft endstops off, so nothing else would stop an over-travel.
        # Never below the part either, whatever the catalogue says.
        safe_z = min(safe_z, max(float(z_top_mm), max_z))
    resolved = re.sub(
        r"(G1 Z)\d+\.?\d*( F900)",
        rf"\g<1>{safe_z:.1f}\2",
        expanded,
        count=1,
    )
    if lift_floor_mm is None:
        return resolved
    return _raise_absolute_z_to_floor(resolved, float(lift_floor_mm))


_Z_ONLY_MOVE_RE = re.compile(r"^(\s*G[01]\s+)Z(-?\d+\.?\d*)(\b.*)$")


def _raise_absolute_z_to_floor(gcode: str, floor_mm: float) -> str:
    """Every absolute ``G0``/``G1`` line that moves only Z, raised to at
    least *floor_mm*; nothing lowered, nothing else touched.  Relative
    stretches (``G91`` .. ``G90``) are left alone: a relative Z is a
    distance, and a floor is a height."""
    out: list[str] = []
    absolute = True
    for line in gcode.split("\n"):
        code = line.split(";", 1)[0].strip().upper()
        if code == "G91":
            absolute = False
        elif code == "G90":
            absolute = True
        if absolute:
            m = _Z_ONLY_MOVE_RE.match(line)
            if m and not re.search(r"[XYE]-?\d", m.group(3).split(";", 1)[0]):
                z = float(m.group(2))
                if z < floor_mm:
                    line = f"{m.group(1)}Z{floor_mm:.2f}{m.group(3)}"
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Gcode post-processing
# ---------------------------------------------------------------------------


def _extract_slicer_time_estimate(gcode_body: str) -> int:
    """Extract the slicer's own print time estimate from gcode comments.

    PrusaSlicer writes lines like::

        ; estimated printing time (normal mode) = 1h 23m 45s

    OrcaSlicer uses a similar format.  For merged multi-part gcodes that
    contain multiple "normal mode" estimates (one per sliced part), all
    estimates are summed to produce the total print time.

    Returns seconds, or 0 if no estimate is found.
    """
    total_seconds = 0

    for m in re.finditer(
        r"estimated printing time \(normal mode\).*?=\s*"
        r"(?:(\d+)d\s*)?(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?",
        gcode_body,
        re.IGNORECASE,
    ):
        d = int(m.group(1) or 0)
        h = int(m.group(2) or 0)
        mins = int(m.group(3) or 0)
        s = int(m.group(4) or 0)
        total_seconds += d * 86400 + h * 3600 + mins * 60 + s

    return total_seconds


def _count_layers(gcode_body: str) -> int:
    """Count ``;LAYER_CHANGE`` markers in PrusaSlicer gcode."""
    return len(re.findall(r"^;LAYER_CHANGE", gcode_body, re.MULTILINE))


#: A tool select at line start — the form every slicer in this family
#: emits per filament change.  ``M104 T0 S200`` mid-line is heater
#: targeting, not a tool change, and correctly does not match.
_GCODE_TOOL_SELECT_RE = re.compile(r"^T(\d+)\b", re.MULTILINE)

#: The per-filament settings blocks these slicers write into the gcode
#: footer, semicolon-separated, one entry per loaded filament.
_GCODE_FILAMENT_COLOUR_RE = re.compile(
    r"^;\s*filament_colour\s*=\s*(.+)$", re.MULTILINE,
)
_GCODE_FILAMENT_TYPE_RE = re.compile(
    r"^;\s*filament_type\s*=\s*(.+)$", re.MULTILINE,
)

#: What each slicer writes about filament consumed, measured 2026-09-18.
#: PrusaSlicer 2.9.4 and OrcaSlicer 2.3.2 footer: ``; filament used [mm] =
#: 11035.45, 584.76`` and, only when the profile carried a density,
#: ``; filament used [g] = 1.27``.  Bambu Studio 02.05 header:
#: ``; total filament length [mm] : 5127.93,2704.70`` and ``; total
#: filament weight [g] : 16.28,8.13``.  All of them list one value per USED
#: extruder, comma separated.  Kiln's own profiles describe a printer and
#: no filament, so the slicer's grams used to read ``0.00`` and the length
#: was the number that survived.  A slice through ``kiln.slicer`` now
#: carries a density (:mod:`kiln.slicer_filament`) and the grams are the
#: slicer's own; the length path remains for files sliced elsewhere.
_GCODE_USED_MM_RE = re.compile(
    r"^;\s*(?:filament used \[mm\]\s*=|total filament length \[mm\]\s*:)\s*(.+)$",
    re.MULTILINE | re.IGNORECASE,
)
_GCODE_USED_G_RE = re.compile(
    r"^;\s*(?:filament used \[g\]\s*=|total filament weight \[g\]\s*:)\s*(.+)$",
    re.MULTILINE | re.IGNORECASE,
)
_GCODE_FILAMENT_DENSITY_RE = re.compile(
    r"^;\s*filament_density\s*[:=]\s*(.+)$", re.MULTILINE | re.IGNORECASE,
)
_GCODE_FILAMENT_DIAMETER_RE = re.compile(
    r"^;\s*filament_diameter\s*[:=]\s*(.+)$", re.MULTILINE | re.IGNORECASE,
)
#: Tool selects wherever they sit on the line: Bambu's AMS blocks indent
#: the real ``T0`` / ``T1``.  Tools from 255 up are the start sequence's
#: pseudo-tools (T255, T1000), not trays.
_GCODE_ANY_TOOL_SELECT_RE = re.compile(r"^\s*T(\d+)\b", re.MULTILINE)
_BAMBU_FIRST_PSEUDO_TOOL = 255
_GCODE_E_WORD_RE = re.compile(r"(?:^|\s)E(-?\d*\.?\d+)")
_FILAMENT_DIAMETER_MM = 1.75
_DEFAULT_FILAMENT_DENSITY = 1.24  # PLA, the table's own figure


@dataclass(frozen=True)
class FilamentUsage:
    """Filament a G-code body consumes, per extruder index (0-based).

    ``source`` says where the grams came from: ``slicer_grams`` (the
    slicer wrote them), ``slicer_length`` (the slicer's length times the
    filament cross-section times the material density), ``e_moves`` (no
    slicer comment at all — the E words were summed), or ``none`` (nothing
    is extruded, and the zeros are the truth).
    """

    mm: tuple[float, ...]
    grams: tuple[float, ...]
    source: str

    @property
    def total_mm(self) -> float:
        return float(sum(self.mm))

    @property
    def total_g(self) -> float:
        return float(sum(self.grams))


def _number_list(text: str) -> list[float]:
    out: list[float] = []
    for raw in re.split(r"[,;]", text):
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(float(raw))
        except ValueError:
            return []
    return out


def _material_density(filament_type: str | None) -> float:
    """The family's nominal density from Kiln's material table, or PLA's.

    The same lookup the slice-time resolver uses
    (:func:`kiln.slicer_filament.material_density`), so the safety net and
    the slicer can never disagree about what a spool weighs.
    """
    from kiln.slicer_filament import material_density

    row = material_density(filament_type)
    return row[1] if row else _DEFAULT_FILAMENT_DENSITY


def _real_tools_used(gcode_body: str) -> list[int]:
    return sorted(
        {
            int(t)
            for t in _GCODE_ANY_TOOL_SELECT_RE.findall(gcode_body)
            if int(t) < _BAMBU_FIRST_PSEUDO_TOOL
        }
    )


def _place_on_used_tools(values: list[float], tools: list[int]) -> list[float]:
    """A slicer's comma list is one value per USED extruder: a print on T0
    and T2 writes two numbers and the second is tray 3's."""
    if not values or len(values) != len(tools) or tools == list(range(len(values))):
        return values
    out = [0.0] * (max(tools) + 1)
    for tool, value in zip(tools, values, strict=True):
        out[tool] = value
    return out


def _sum_e_moves(gcode_body: str) -> list[float]:
    """Net E per extruder from the moves themselves.

    Retract and unretract cancel because deltas are signed.  Absolute E
    (``M82``, the G-code default) is differenced and reset by ``G92``;
    relative E (``M83``, what Kiln slices with) is summed as written.  Moves
    under a pseudo-tool (Bambu's ``T1000`` unload / ``T255``) belong to no
    tray and are not counted.
    """
    totals: dict[int, float] = {}
    tool: int | None = 0
    relative = False
    last_e = 0.0
    for line in gcode_body.splitlines():
        code = line.split(";", 1)[0].strip()
        if not code:
            continue
        if code[0] in "Tt" and code[1:2].isdigit():
            number = int(re.match(r"\d+", code[1:]).group(0))
            tool = number if number < _BAMBU_FIRST_PSEUDO_TOOL else None
            continue
        word = code.split(None, 1)[0].upper()
        if word == "M82" or word == "G90":
            relative = False
            continue
        if word == "M83" or word == "G91":
            relative = True
            continue
        if word == "G92":
            e_word = _GCODE_E_WORD_RE.search(code)
            if e_word:
                last_e = float(e_word.group(1))
            continue
        if word not in ("G0", "G1", "G2", "G3"):
            continue
        e_word = _GCODE_E_WORD_RE.search(code)
        if not e_word:
            continue
        value = float(e_word.group(1))
        if relative:
            delta = value
        else:
            delta = value - last_e
            last_e = value
        if tool is not None:
            totals[tool] = totals.get(tool, 0.0) + delta
    if not totals:
        return []
    out = [0.0] * (max(totals) + 1)
    for index, total in totals.items():
        out[index] = max(total, 0.0)
    return out


def filament_usage_from_gcode(
    gcode_body: str,
    *,
    filament_types: list[str] | None = None,
) -> FilamentUsage:
    """How much filament *gcode_body* consumes, per extruder and in total.

    The slicer's own grams win when it wrote them (``filament used [g]``,
    Studio's ``total filament weight [g]``).  Otherwise its length is
    turned into grams: length x the 1.75 mm cross-section x density, where
    density is the slicer's own ``filament_density`` when it is not zero,
    else Kiln's table for the material — *filament_types* (what the caller
    declared and the AMS will be told) first, the body's ``filament_type``
    line second, PLA last.  A body with no slicer comment at all has its E
    words summed.  Never raises.
    """
    tools = _real_tools_used(gcode_body)
    mm_match = _GCODE_USED_MM_RE.search(gcode_body)
    mm = _place_on_used_tools(_number_list(mm_match.group(1)), tools) if mm_match else []
    g_match = _GCODE_USED_G_RE.search(gcode_body)
    grams = _place_on_used_tools(_number_list(g_match.group(1)), tools) if g_match else []

    if mm and grams and any(g > 0 for g in grams) and len(grams) == len(mm):
        source = "slicer_grams"
    else:
        source = "slicer_length" if mm else "e_moves"
        if not mm:
            mm = _sum_e_moves(gcode_body)
        densities = _number_list(
            (_GCODE_FILAMENT_DENSITY_RE.search(gcode_body) or [None, ""])[1]
        )
        diameters = _number_list(
            (_GCODE_FILAMENT_DIAMETER_RE.search(gcode_body) or [None, ""])[1]
        )
        type_match = _GCODE_FILAMENT_TYPE_RE.search(gcode_body)
        body_types = [t.strip() for t in type_match.group(1).split(";")] if type_match else []

        def _pick(values: list[float], index: int) -> float | None:
            if index < len(values) and values[index] > 0:
                return values[index]
            if len(values) == 1 and values[0] > 0:
                return values[0]
            return None

        grams = []
        for index, length in enumerate(mm):
            declared = filament_types[index] if filament_types and index < len(filament_types) else None
            density = _pick(densities, index)
            if density is None:
                density = _material_density(
                    declared or (body_types[index] if index < len(body_types) else None)
                )
            diameter = _pick(diameters, index) or _FILAMENT_DIAMETER_MM
            area_mm2 = math.pi * (diameter / 2.0) ** 2
            grams.append(length * area_mm2 * density / 1000.0)

    if not mm or sum(mm) <= 0:
        return FilamentUsage(mm=tuple(mm), grams=tuple(0.0 for _ in mm), source="none")
    return FilamentUsage(mm=tuple(mm), grams=tuple(grams), source=source)


_SLICE_INFO_WEIGHT_RE = re.compile(r'(<metadata\s+key="weight"\s+value=")([^"]*)(")')
_SLICE_INFO_FILAMENT_TAG_RE = re.compile(r"<filament\b[^>]*>")


def _slice_info_knows_its_weight(slice_info: str) -> bool:
    match = _SLICE_INFO_WEIGHT_RE.search(slice_info)
    if not match:
        return False
    try:
        return float(match.group(2)) > 0
    except ValueError:
        return False


def _fill_slice_info_usage(slice_info: str, usage: FilamentUsage) -> str:
    """Write *usage* where the printer's screen reads it, touching nothing else.

    The plate's ``weight`` is the sum of the per-filament grams; each
    ``<filament>`` gets ``used_m`` (metres) and ``used_g`` (grams), two
    decimals, exactly as Bambu Studio writes them.  Filament tags map to
    extruders in order when the counts agree, by ``id`` otherwise.
    """
    out = _SLICE_INFO_WEIGHT_RE.sub(
        lambda m: f"{m.group(1)}{usage.total_g:.2f}{m.group(3)}", slice_info, count=1,
    )
    tags = list(_SLICE_INFO_FILAMENT_TAG_RE.finditer(out))
    if not tags:
        return out
    pieces: list[str] = []
    cursor = 0
    for position, tag in enumerate(tags):
        if len(tags) == len(usage.mm):
            index = position
        else:
            id_match = re.search(r'\bid="(\d+)"', tag.group(0))
            index = int(id_match.group(1)) - 1 if id_match else position
        mm = usage.mm[index] if 0 <= index < len(usage.mm) else 0.0
        grams = usage.grams[index] if 0 <= index < len(usage.grams) else 0.0
        text = re.sub(r'\bused_m="[^"]*"', f'used_m="{mm / 1000.0:.2f}"', tag.group(0), count=1)
        text = re.sub(r'\bused_g="[^"]*"', f'used_g="{grams:.2f}"', text, count=1)
        pieces.append(out[cursor:tag.start()])
        pieces.append(text)
        cursor = tag.end()
    pieces.append(out[cursor:])
    return "".join(pieces)


#: The generator stamp these slicers write into their G-code header.
#: OrcaSlicer and BambuStudio share the fork; PrusaSlicer and the other
#: Slic3r derivatives write their own names.  Read from the head of the
#: file only — a body can mention anything in a comment.
_BAMBU_DIALECT_GENERATORS = ("orcaslicer", "bambustudio", "bambu studio")

_GCODE_HEAD_CHARS = 4096


def _gcode_is_bambu_dialect(gcode_body: str) -> bool:
    """Whether this G-code came from OrcaSlicer / BambuStudio.

    Used to decide whether a PrusaSlicer-calibrated time correction
    applies.  An unrecognised generator reads as NOT the Bambu dialect,
    which keeps the historical behaviour for every gcode Kiln was
    already wrapping.
    """
    head = gcode_body[:_GCODE_HEAD_CHARS].lower()
    return any(name in head for name in _BAMBU_DIALECT_GENERATORS)


def _declared_filaments_in_gcode(
    gcode_body: str,
) -> tuple[int, list[str], list[str]] | None:
    """What the gcode itself says about its filaments, or ``None``.

    Returns ``(count, colors, types)`` when the gcode uses more than one
    filament, so a wrapper can declare the same thing to the printer
    instead of flattening a multicolor toolpath into a one-color file.

    The count is a highest-tool-index, not a distinct count: a print
    using T0 and T2 needs three trays declared, or T2 maps to nothing.
    Colors and types come from the slicer's own ``; filament_colour =``
    / ``; filament_type =`` footer lines when present, trimmed or padded
    to the count so the caller always gets one entry per slot.

    Never raises — a gcode this cannot read reads as single-filament,
    which is exactly the behaviour every caller had before.
    """
    tools = {int(t) for t in _GCODE_TOOL_SELECT_RE.findall(gcode_body)}
    if not tools:
        return None
    count = max(tools) + 1  # tools are 0-based
    if count < 2:
        return None

    def _declared(pattern: re.Pattern[str]) -> list[str]:
        match = pattern.search(gcode_body)
        if not match:
            return []
        return [v.strip() for v in match.group(1).split(";") if v.strip()]

    def _fit(values: list[str], filler: str) -> list[str]:
        """Exactly *count* entries: declared ones kept, the rest filled.

        A short declaration is the case that matters — the consumer
        drops the list wholesale when it is shorter than the slot count,
        which would throw away the real colors it DID state.
        """
        if not values:
            return []
        return (values + [filler] * count)[:count]

    colors = _fit(_declared(_GCODE_FILAMENT_COLOUR_RE), "#FFFFFF")
    types = _fit(_declared(_GCODE_FILAMENT_TYPE_RE), "PLA")
    return count, colors, types


def _find_max_z(gcode_body: str) -> float:
    """Find the maximum Z height from PrusaSlicer ``;Z:`` comments."""
    z_heights = re.findall(r";Z:(\d+\.?\d*)", gcode_body)
    return max(float(z) for z in z_heights) if z_heights else 10.0


def _postprocess_prusa_body(
    gcode_body: str,
    *,
    total_layers: int,
    est_time_sec: int,
) -> str:
    """Post-process PrusaSlicer gcode body for Bambu firmware compatibility.

    1. Strips PrusaSlicer's own init commands (M83, G28, M104, etc.)
       since the BambuStudio start gcode handles machine initialization.
    2. Strips PrusaSlicer's native ``M73 P{pct} R{min}`` progress commands
       which lack the ``L`` parameter and would override our layer tracking,
       causing the printer display to show stale progress.
    3. Injects Bambu-specific layer tracking at each ``;LAYER_CHANGE``:
       - ``M73 L{n}`` — layer number for firmware display
       - ``M991 S0 P0`` — notify firmware of layer change
       - ``M73 P{pct} R{min}`` — progress percentage and remaining time
    """
    body_lines = gcode_body.split("\n")

    # Strip PrusaSlicer init commands before the first layer.
    _skip_prefixes = (
        "M83", "M82", "G21", "G90", "G92", "M107",
        "M104", "M140", "M190", "M109", "G28",
    )
    cleaned: list[str] = []
    in_header = True
    for line in body_lines:
        stripped = line.strip()
        if in_header:
            if stripped.startswith((";BEFORE_LAYER_CHANGE", ";LAYER_CHANGE")):
                in_header = False
                cleaned.append(line)
            elif stripped.startswith(";") or stripped == "":
                cleaned.append(line)
            elif stripped.startswith(_skip_prefixes):
                continue  # Skip — Bambu start gcode handles these
            else:
                in_header = False
                cleaned.append(line)
        else:
            cleaned.append(line)

    # Inject Bambu layer tracking at each ;LAYER_CHANGE and strip
    # PrusaSlicer's own M73 commands.  PrusaSlicer emits M73 P{pct} R{min}
    # frequently throughout the gcode (often every few lines).  These lack
    # the L parameter that Bambu firmware needs for layer counting, and they
    # override the progress values we inject at each layer boundary — causing
    # the printer display to show stale progress (e.g. stuck at "5% / layer 1").
    # We replace them with our own M73 commands that include L for layer
    # tracking alongside correct P/R values.
    layer_num = 0
    processed: list[str] = []
    for line in cleaned:
        stripped = line.strip()
        if stripped == ";LAYER_CHANGE":
            layer_num += 1
            processed.append(line)
            processed.append(
                f"; layer num/total_layer_count: {layer_num}/{total_layers}"
            )
            processed.append("; update layer progress")
            processed.append(f"M73 L{layer_num}")
            processed.append("M991 S0 P0 ;notify layer change")
            pct = min(int(layer_num * 100 / total_layers), 99)
            remaining_sec = max(
                60, int((total_layers - layer_num) * est_time_sec / total_layers)
            )
            remaining_min = max(1, remaining_sec // 60)
            processed.append(f"M73 P{pct} R{remaining_min}")
            continue
        # Strip PrusaSlicer's native M73 lines — we inject our own above.
        if stripped.startswith("M73 ") or stripped == "M73":
            continue
        processed.append(line)

    return "\n".join(processed)


# ---------------------------------------------------------------------------
# Gcode assembly
# ---------------------------------------------------------------------------


def _wrap_tool_changes(
    gcode: str,
    *,
    printer_model: str | None = None,
    hotend_temp: int = 220,
    filament_type: str = "PLA",
    lift_floor_mm: float | None = None,
) -> str:
    """Wrap PrusaSlicer ``T`` commands in Bambu M620/M621 AMS load blocks.

    PrusaSlicer multi-material gcode uses bare ``T0``, ``T1``, etc. to
    switch tools.  Bambu firmware requires these to be wrapped in
    ``M620 S{n}A`` / ``M621 S{n}A`` blocks for the AMS to load the
    correct filament.

    Only wraps T0–T15 (real extruder indices).  Leaves T255 (retract)
    and T1000 (virtual tool) untouched.

    The block takes the head to THIS MODEL'S chute before it flushes, from
    :data:`_MODEL_FLUSH_STATION`.  A model with no chute on record raises:
    the position is what keeps 50 mm of purged filament off the plate, and
    one machine's chute is another machine's frame.

    With a *lift_floor_mm* (other parts on the plate) the block is Kiln's
    lifted one: before the head goes to the chute it rises to the floor --
    never less than 3 mm above the layer, the vendor's own lift -- and
    after the flush it travels back to where it left from at that height
    and only then descends to the layer.  The plate is crossed above
    everything on it, and the descent is over the part.

    :raises ValueError: if *gcode* changes tool on a model whose chute is
        not on record.
    """
    from kiln.printers.safe_motion import chute_move

    station = flush_station_for(printer_model)
    to_chute = chute_move(station, feedrate=3000) if station else None
    lines = gcode.split("\n")
    result: list[str] = []
    # Track the initial T0 from start gcode — don't double-wrap it
    saw_m620 = False
    layer_z: float | None = None
    last_z: float | None = None
    last_xy: tuple[float | None, float | None] = (None, None)

    for line in lines:
        stripped = line.strip()
        if lift_floor_mm is not None:
            if stripped.startswith(";Z:"):
                with contextlib.suppress(ValueError):
                    layer_z = float(stripped[3:])
            elif stripped.startswith(("G0", "G1")):
                code = stripped.split(";", 1)[0]
                mz = re.search(r"\bZ(-?\d+\.?\d*)", code)
                if mz:
                    last_z = float(mz.group(1))
                mx = re.search(r"\bX(-?\d+\.?\d*)", code)
                my = re.search(r"\bY(-?\d+\.?\d*)", code)
                if mx or my:
                    last_xy = (
                        float(mx.group(1)) if mx else last_xy[0],
                        float(my.group(1)) if my else last_xy[1],
                    )
        # Track if we're inside an M620/M621 block already
        if stripped.startswith("M620 "):
            saw_m620 = True
            result.append(line)
            continue
        if stripped.startswith("M621 "):
            saw_m620 = False
            result.append(line)
            continue

        # Match standalone T commands (T0, T1, ..., T15)
        m = re.match(r"^T(\d+)$", stripped)
        if m and not saw_m620:
            n = int(m.group(1))
            if 0 <= n < 16:
                flush_temp = min(hotend_temp + 30, 260)
                here_z = last_z if last_z is not None else layer_z
                if lift_floor_mm is not None and here_z is not None:
                    lifted = max(float(lift_floor_mm), here_z + 3.0)
                    result.append(f"G1 Z{lifted:.2f} F600  ; Kiln: lift clear of everything on the plate before the cutter")
                result.append(f"M620 S{n}A   ; AMS switch to filament {n}")
                result.append("    M1002 gcode_claim_action : 4")
                result.append("    M400")
                result.append("    M1002 set_filament_type:UNKNOWN")
                result.append(f"    M109 S{hotend_temp}")
                result.append(f"    M104 S{flush_temp}")
                result.append("    M400")
                if to_chute is None:
                    raise ValueError(
                        f"this print changes filament and Kiln has no waste chute on record for "
                        f"{_normalize_model(printer_model)}, so it will not write the file: the AMS block has "
                        "to purge somewhere off the plate, and sending the head to another model's chute is "
                        "how a head meets a frame. Slice this one in a single colour, or print it on a model "
                        f"whose chute has been run and watched ({', '.join(sorted(_MODEL_FLUSH_STATION))}).",
                    )
                result.append(f"    T{n}")
                result.append(f"    {to_chute}")
                result.append("    M400")
                result.append(f"    M620.1 E F299.339 T{flush_temp}")
                result.append(f"    M109 S{flush_temp}")
                result.append("    M106 P1 S0")
                result.append("    G92 E0")
                result.append("    G1 E50 F200")
                result.append("    M400")
                result.append(f"    M1002 set_filament_type:{filament_type}")
                result.append(f"M621 S{n}A")
                if lift_floor_mm is not None and here_z is not None:
                    lx, ly = last_xy
                    if lx is not None and ly is not None:
                        result.append(f"G1 X{lx:.3f} Y{ly:.3f} F6000  ; Kiln: back over the part, still lifted")
                    result.append(f"G1 Z{here_z:.2f} F600  ; Kiln: down to the layer, over the part")
                continue
        result.append(line)

    return "\n".join(result)


def _build_gcode_header(
    *,
    total_layers: int,
    max_z: float,
    est_print_time_sec: int,
    filament_type: str = "PLA",
    nozzle_diameter: float = 0.4,
    hotend_temp: int = 220,
    bed_temp: int = 65,
    num_filaments: int = 1,
    filament_types: list[str] | None = None,
) -> str:
    """Build the Bambu-compatible gcode header block."""
    est_h = est_print_time_sec // 3600
    est_m = (est_print_time_sec % 3600) // 60
    est_s = est_print_time_sec % 60

    types = filament_types or [filament_type] * num_filaments
    type_str = ";".join(types)

    return (
        f"; HEADER_BLOCK_START\n"
        f"; BambuStudio 02.05.00.66\n"
        f"; model printing time: {est_h}h {est_m}m {est_s}s; "
        f"total estimated time: {est_h}h {est_m + 5}m 0s\n"
        f"; total layer number: {total_layers}\n"
        f"; filament_density: 1.24\n"
        f"; filament_diameter: 1.75\n"
        f"; max_z_height: {max_z:.2f}\n"
        f"; filament: {num_filaments}\n"
        f"; HEADER_BLOCK_END\n"
        f"\n"
        f"; CONFIG_BLOCK_START\n"
        f"; filament_type = {type_str}\n"
        f"; nozzle_diameter = {nozzle_diameter}\n"
        f"; bed_temperature = {bed_temp}\n"
        f"; temperature = {hotend_temp}\n"
        f"; CONFIG_BLOCK_END\n"
        f"\n"
    )


# ---------------------------------------------------------------------------
# 3MF metadata builders
# ---------------------------------------------------------------------------


def _build_slice_info(
    *,
    total_layers: int,
    est_print_time_sec: int,
    filament_type: str = "PLA",
    filament_color: str = "#FFFFFF",
    nozzle_diameter: float = 0.4,
    model_name: str = "model",
    first_layer_time: float = 60.0,
    num_filaments: int = 1,
    filament_colors: list[str] | None = None,
    filament_types: list[str] | None = None,
    usage: FilamentUsage | None = None,
) -> str:
    """Build the ``slice_info.config`` XML for the 3MF.

    Supports multi-filament: set ``num_filaments`` > 1 and provide
    ``filament_colors`` / ``filament_types`` lists.

    *usage* is what the printer's screen shows as the print's weight: the
    plate ``weight`` (grams, the sum) and each filament's ``used_m`` /
    ``used_g`` — the fields Bambu Studio writes and the A1's tile reads.
    Without it every one of them is ``0.00``, which is what the tile showed
    for every Kiln print until the builder started passing it.
    """
    colors = filament_colors or [filament_color] * num_filaments
    types = filament_types or [filament_type] * num_filaments
    used_mm = list(usage.mm) if usage else []
    used_g = list(usage.grams) if usage else []

    # Build filament entries
    filament_entries: list[str] = []
    for i in range(num_filaments):
        ftype = types[i] if i < len(types) else filament_type
        fcolor = colors[i] if i < len(colors) else filament_color
        metres = (used_mm[i] if i < len(used_mm) else 0.0) / 1000.0
        grams = used_g[i] if i < len(used_g) else 0.0
        filament_entries.append(
            f'    <filament id="{i + 1}" tray_info_idx="GFL99" type="{ftype}" '
            f'color="{fcolor}" used_m="{metres:.2f}" used_g="{grams:.2f}" '
            f'used_for_object="true" used_for_support="false" group_id="0" '
            f'nozzle_diameter="{nozzle_diameter:.2f}" volume_type="Standard"/>'
        )
    weight = sum(used_g[:num_filaments])

    # Build object entries (one per filament for multi-color copies)
    object_entries: list[str] = []
    for i in range(num_filaments):
        obj_name = model_name if num_filaments == 1 else f"{model_name}_{i + 1}"
        object_entries.append(
            f'    <object identify_id="{i + 1}" name="{obj_name}" skipped="false" />'
        )

    filament_map_val = ";".join(str(i) for i in range(num_filaments))

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<config>\n"
        "  <header>\n"
        '    <header_item key="X-BBL-Client-Type" value="slicer"/>\n'
        '    <header_item key="X-BBL-Client-Version" value="02.05.00.66"/>\n'
        "  </header>\n"
        "  <plate>\n"
        '    <metadata key="index" value="1"/>\n'
        '    <metadata key="extruder_type" value="0"/>\n'
        '    <metadata key="nozzle_volume_type" value="0"/>\n'
        '    <metadata key="printer_model_id" value="N2S"/>\n'
        f'    <metadata key="nozzle_diameters" value="{nozzle_diameter}"/>\n'
        '    <metadata key="timelapse_type" value="0"/>\n'
        f'    <metadata key="prediction" value="{est_print_time_sec}"/>\n'
        f'    <metadata key="weight" value="{weight:.2f}"/>\n'
        f'    <metadata key="first_layer_time" value="{first_layer_time:.1f}"/>\n'
        '    <metadata key="outside" value="false"/>\n'
        '    <metadata key="support_used" value="false"/>\n'
        '    <metadata key="label_object_enabled" value="false"/>\n'
        f'    <metadata key="filament_maps" value="{filament_map_val}"/>\n'
        '    <metadata key="limit_filament_maps" value="0"/>\n'
        + "\n".join(object_entries) + "\n"
        + "\n".join(filament_entries) + "\n"
        "    <layer_filament_lists>\n"
        f'      <layer_filament_list filament_list="0" '
        f'layer_ranges="0 {total_layers - 1}" />\n'
        "    </layer_filament_lists>\n"
        "  </plate>\n"
        "</config>"
    )


def _build_plate_json(
    *,
    filament_color: str = "#FFFFFF",
    nozzle_diameter: float = 0.4,
    bed_type: str = "textured_plate",
    first_layer_time: float = 60.0,
    num_filaments: int = 1,
    filament_colors: list[str] | None = None,
) -> str:
    """Build the ``plate_1.json`` metadata.

    Supports multi-filament: set ``num_filaments`` > 1 and provide
    ``filament_colors`` list.
    """
    colors = filament_colors or [filament_color] * num_filaments
    ids = list(range(num_filaments))

    data = {
        "bbox_all": [78, 78, 178, 178],
        "bbox_objects": [],
        "bed_type": bed_type,
        "filament_colors": colors[:num_filaments],
        "filament_ids": ids,
        "first_extruder": 0,
        "first_layer_time": first_layer_time,
        "is_seq_print": False,
        "nozzle_diameter": nozzle_diameter,
        "version": 2,
    }
    return json.dumps(data)


# ---------------------------------------------------------------------------
# Static 3MF boilerplate
# ---------------------------------------------------------------------------

_CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
    '  <Default Extension="gcode" ContentType="text/x.gcode"/>\n'
    '  <Default Extension="model" ContentType='
    '"application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
    '  <Default Extension="png" ContentType="image/png"/>\n'
    '  <Default Extension="config" ContentType="text/xml"/>\n'
    '  <Default Extension="json" ContentType="application/json"/>\n'
    "</Types>"
)

_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
    '  <Relationship Target="/3D/3dmodel.model" Id="rel-1" '
    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
    '  <Relationship Target="/Metadata/plate_1.gcode" Id="rel-2" '
    'Type="http://schemas.bambulab.com/package/2021/gcode"/>\n'
    '  <Relationship Target="/Metadata/slice_info.config" Id="rel-3" '
    'Type="http://schemas.bambulab.com/package/2021/slice-info"/>\n'
    "</Relationships>"
)

_MODEL_SETTINGS_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
    "</Relationships>"
)

# Minimal 3D model placeholder — a 1 mm cube at origin.
# The printer only reads the gcode; geometry is for BambuStudio UI only.
_MINIMAL_3D_MODEL = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<model unit="millimeter" '
    'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
    '  <metadata name="Application">Kiln</metadata>\n'
    '  <metadata name="CreatedBy">Kiln — kiln3d.com</metadata>\n'
    "  <resources>\n"
    '    <object id="1" type="model">\n'
    "      <mesh>\n"
    "        <vertices>\n"
    '          <vertex x="0" y="0" z="0"/>\n'
    '          <vertex x="1" y="0" z="0"/>\n'
    '          <vertex x="1" y="1" z="0"/>\n'
    '          <vertex x="0" y="1" z="0"/>\n'
    '          <vertex x="0" y="0" z="1"/>\n'
    '          <vertex x="1" y="0" z="1"/>\n'
    '          <vertex x="1" y="1" z="1"/>\n'
    '          <vertex x="0" y="1" z="1"/>\n'
    "        </vertices>\n"
    "        <triangles>\n"
    '          <triangle v1="0" v2="1" v3="2"/>\n'
    '          <triangle v1="0" v2="2" v3="3"/>\n'
    '          <triangle v1="4" v2="6" v3="5"/>\n'
    '          <triangle v1="4" v2="7" v3="6"/>\n'
    '          <triangle v1="0" v2="4" v3="5"/>\n'
    '          <triangle v1="0" v2="5" v3="1"/>\n'
    '          <triangle v1="2" v2="6" v3="7"/>\n'
    '          <triangle v1="2" v2="7" v3="3"/>\n'
    '          <triangle v1="0" v2="7" v3="4"/>\n'
    '          <triangle v1="0" v2="3" v3="7"/>\n'
    '          <triangle v1="1" v2="5" v3="6"/>\n'
    '          <triangle v1="1" v2="6" v3="2"/>\n'
    "        </triangles>\n"
    "      </mesh>\n"
    "    </object>\n"
    "  </resources>\n"
    "  <build>\n"
    '    <item objectid="1"/>\n'
    "  </build>\n"
    "</model>"
)


#: BambuStudio's thumbnail set: archive path -> (width, height).  Firmware
#: and Studio each pick a different entry by path, so the whole set travels
#: together or some surface shows a blank tile.
_BAMBU_THUMBNAIL_SPECS: dict[str, tuple[int, int]] = {
    "Metadata/plate_1.png": (512, 512),
    "Metadata/plate_1_small.png": (128, 128),
    "Metadata/top_1.png": (512, 512),
    "Metadata/pick_1.png": (512, 512),
    "Auxiliaries/.thumbnails/thumbnail_3mf.png": (240, 180),
    "Auxiliaries/.thumbnails/thumbnail_middle.png": (680, 510),
    "Auxiliaries/.thumbnails/thumbnail_small.png": (251, 188),
}


def _declared_filament_colors(plate_json: str | None) -> list[str] | None:
    """The filament colors THIS archive declares, read back from its own JSON.

    The preview has one job — say what the print will look like — so the
    only defensible source for its color is the file's own claim, and the
    file makes that claim in the ``Metadata/plate_1.json`` written beside
    the thumbnail.  Reading it back from that exact string is what keeps
    the two in agreement: pass the JSON the archive gets, and the preview
    cannot show a color the file does not declare.

    Deliberately NOT a caller-supplied parameter.  A parameter is a
    promise every call site has to keep, and the wrap that copies its
    metadata from a source 3MF has no colors of its own to pass — so a
    parameter would have gone unfilled there and quietly fallen back to a
    default, which is exactly the failure this is written to prevent.

    :param plate_json: The ``Metadata/plate_1.json`` text bound for the
        archive, or ``None`` when the archive will declare no plate at all.
    :returns: The declared ``#RRGGBB`` list, or ``None`` when the file
        declares nothing usable — never a substituted default.
    """
    if not plate_json:
        return None
    try:
        declared = json.loads(plate_json).get("filament_colors")
    except (ValueError, AttributeError):
        logger.warning(
            "plate_1.json is not readable JSON — the preview renders "
            "neutral rather than guessing a filament color.",
            exc_info=True,
        )
        return None
    if not isinstance(declared, list):
        return None
    colors = [c for c in declared if isinstance(c, str) and c.strip()]
    return colors or None


#: Kiln's witness that the thumbnail family was rendered from the archive's
#: own model in the colours the archive declares.  Written by
#: :func:`complete_bambu_archive` and by the builder when it renders; read
#: by :func:`bambu_archive_problems`.  A slicer's own full set carries
#: ``plate_no_light_1.png`` instead, which Bambu Studio writes only when it
#: drew the pictures itself.
KILN_PREVIEW_MARKER = "Metadata/kiln_preview.json"

#: The quiet-start plan a file was built to, kept in the archive beside the
#: G-code that carries the same numbers in its contract header.
KILN_QUIET_START_MARKER = "Metadata/kiln_quiet_start.json"
_QUIET_PLAN_KEYS = ("clear_z_mm", "lift_floor_mm", "travel_to_mm", "first_layer_z_mm", "planned_for_machine", "plate_fingerprint")


def _check_quiet_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """The plan with every number it must carry, read as numbers; a plan
    missing one is refused here, before any line is written."""
    if not isinstance(plan, dict):
        msg = "quiet_start must be the placement verdict's start plan"
        raise ValueError(msg)
    missing = [k for k in _QUIET_PLAN_KEYS if plan.get(k) in (None, "")]
    if missing:
        msg = f"quiet_start plan is missing {', '.join(missing)}"
        raise ValueError(msg)
    out = dict(plan)
    for key in ("clear_z_mm", "lift_floor_mm", "first_layer_z_mm"):
        out[key] = float(plan[key])
    travel = plan["travel_to_mm"]
    if not isinstance(travel, (list, tuple)) or len(travel) != 2:
        msg = "quiet_start.travel_to_mm must be [x, y]"
        raise ValueError(msg)
    out["travel_to_mm"] = [float(travel[0]), float(travel[1])]
    if out["clear_z_mm"] > out["lift_floor_mm"] + 1e-6:
        msg = "quiet_start.clear_z_mm cannot be above lift_floor_mm: the floor includes what is there now"
        raise ValueError(msg)
    return out


def _purge_at(value: Any) -> tuple[float | None, float | None] | None:
    """The plan's chute as ``(x, y)`` with ``None`` for a kept axis, or
    ``None`` when the plan names no chute."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    x, y = value
    try:
        return (None if x is None else float(x), None if y is None else float(y))
    except (TypeError, ValueError):
        return None


def _quiet_start_header(plan: dict[str, Any]) -> str:
    """The contract the pre-print gate reads back from the file
    (:func:`kiln.printers.print_gate.read_quiet_start_contract`): which
    machine and which plate it was planned for, the two heights, and every
    start switch that must be off."""
    from kiln.printers.print_gate import (
        QUIET_START_HEADER_END,
        QUIET_START_HEADER_START,
        QUIET_START_MOTION_CONTRACT,
        QUIET_START_POLICY_VERSION,
    )

    off = list(plan.get("flags") or {}) if isinstance(plan.get("flags"), dict) else []
    for name, how in (plan.get("switched_off") or {}).items():
        flag = str(how).split("=", 1)[0].strip() if "=" in str(how) else str(name)
        if flag and flag not in off:
            off.append(flag)
    lines = [
        QUIET_START_HEADER_START,
        f"; quiet_start_policy_version: {QUIET_START_POLICY_VERSION}",
        f"; motion_contract: {QUIET_START_MOTION_CONTRACT}",
        f"; planned_for_machine: {plan['planned_for_machine']}",
        f"; planned_for_plate: {plan['plate_fingerprint']}",
        f"; clear_z_mm: {float(plan['clear_z_mm']):.2f}",
        f"; lift_floor_mm: {float(plan['lift_floor_mm']):.2f}",
        f"; switched_off: {','.join(off)}",
        "; no Z home, no bed probe and no purge line is emitted by this file; the lift below is absolute",
        QUIET_START_HEADER_END,
    ]
    return "\n".join(lines) + "\n"


_FORBIDDEN_IN_QUIET = ("M620 M", "G29", "G380", "G28 Z")


def _assert_quiet_start_file(gcode: str, plan: dict[str, Any]) -> None:
    """Refuse this build's own output if a vendor motion slipped in: the
    only homing is the X/Y home, no probe, no motor init, and the first
    motion after the header is the absolute lift to the clear height."""
    from kiln.printers.print_gate import QUIET_START_HEADER_END

    home = str(plan.get("home_xy_gcode") or "G28 X Y").strip().upper()
    seen_end = False
    first_motion: str | None = None
    for raw in gcode.split("\n"):
        stripped = raw.strip()
        if stripped == QUIET_START_HEADER_END:
            seen_end = True
            continue
        code = stripped.split(";", 1)[0].strip()
        upper = code.upper()
        if not upper:
            continue
        for bad in _FORBIDDEN_IN_QUIET:
            if upper.startswith(bad.upper()):
                msg = f"quiet-start build carries the vendor motion {bad!r}; refusing to write it"
                raise ValueError(msg)
        if upper.startswith("G28") and upper != home:
            msg = f"quiet-start build carries a homing line other than {home!r}: {code!r}; refusing to write it"
            raise ValueError(msg)
        if seen_end and first_motion is None and upper.startswith(("G0", "G1")) and re.search(r"[XYZ]-?\d", upper):
            first_motion = code
    expected = f"G1 Z{float(plan['clear_z_mm']):.2f}"
    if first_motion is None or not first_motion.upper().startswith(expected.upper()):
        msg = f"quiet-start build's first motion is {first_motion!r}, not the absolute lift {expected!r}; refusing to write it"
        raise ValueError(msg)

#: The slots the printer and Studio draw from.  Measured on the A1
#: (firmware 01.07.02.00, 2026-09-19): the file-list tile is
#: ``plate_1_small.png``; an archive with only ``plate_1.png`` shows the
#: broken-image placeholder.  The Auxiliaries set is Studio's and optional.
_REQUIRED_TILE_SLOTS: tuple[str, ...] = (
    "Metadata/plate_1.png",
    "Metadata/plate_1_small.png",
    "Metadata/top_1.png",
    "Metadata/pick_1.png",
)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != _PNG_MAGIC:
        return None
    import struct

    return struct.unpack(">II", data[16:24])


def _declared_plate_colors(zf: zipfile.ZipFile) -> list[str]:
    try:
        plate_json = zf.read("Metadata/plate_1.json").decode("utf-8")
    except KeyError:
        return []
    return _declared_filament_colors(plate_json) or []


def _archive_is_painted(zf: zipfile.ZipFile) -> bool:
    """Whether the model itself carries colour: a palette or a paint channel."""
    try:
        from kiln.threemf_parser import _scan_color_constructs

        has_palette, has_paint = _scan_color_constructs(zf)
        return bool(has_palette or has_paint)
    except Exception:  # noqa: BLE001 — an unreadable model reads as uncoloured
        return False


def bambu_archive_problems(path: str | os.PathLike[str], *, name: str | None = None) -> list[str]:
    """Why *path* must not go to a Bambu printer — empty when it may.

    Reads the archive the printer would receive and says, in the printer's
    terms, what its screen would fail to show: a sliced plate must carry
    every tile slot at its size, and a plate that declares more than one
    colour (or whose model is painted) must carry a witness that the
    picture was drawn in those colours — the slicer's own no-light slot,
    or Kiln's marker with a hash of the picture it wrote.  Files that are
    not 3MF archives are not this check's business.  *name* is the
    printer-side name when *path* is a temp copy of it, so the extension
    judged is the one the printer sees.
    """
    p = Path(path)
    if Path(name or p.name).suffix.lower() != ".3mf":
        return []
    try:
        zf = zipfile.ZipFile(str(p))
    except (OSError, zipfile.BadZipFile) as exc:
        return [f"{p.name} is not a readable 3MF archive ({exc})"]
    with zf:
        names = set(zf.namelist())
        if "Metadata/plate_1.gcode" not in names:
            return [
                f"{p.name} is not a sliced plate (no Metadata/plate_1.gcode): a printer "
                "cannot start a project file — slice it first (slice_model) and upload "
                "the .gcode.3mf it recommends"
            ]
        problems: list[str] = []
        for slot in _REQUIRED_TILE_SLOTS:
            want = _BAMBU_THUMBNAIL_SPECS[slot]
            short = slot.rsplit("/", 1)[-1]
            if slot not in names:
                problems.append(f"missing {short} ({want[0]}x{want[1]})")
                continue
            got = _png_dimensions(zf.read(slot))
            if got is None:
                problems.append(f"{short} is not a PNG")
            elif tuple(got) != tuple(want):
                problems.append(f"{short} is {got[0]}x{got[1]}, the printer wants {want[0]}x{want[1]}")
        colors = _declared_plate_colors(zf)
        # Who drew the picture, and in what: the slicer's own full set (it
        # writes plate_no_light_1.png only when it rendered the plate in
        # its filament colours), or Kiln's marker naming a stage look, the
        # declared colours, and the hash of the picture it wrote.
        witnessed = "Metadata/plate_no_light_1.png" in names
        if not witnessed and KILN_PREVIEW_MARKER in names and "Metadata/plate_1.png" in names:
            try:
                marker = json.loads(zf.read(KILN_PREVIEW_MARKER).decode("utf-8"))
                digest = hashlib.sha256(zf.read("Metadata/plate_1.png")).hexdigest()[:32]
                witnessed = (
                    marker.get("sha") == digest
                    and [c.upper() for c in marker.get("colors", [])] == [c.upper() for c in colors]
                )
            except (KeyError, ValueError, AttributeError):
                witnessed = False
        if not witnessed:
            problems.append(
                "the preview is not on record as drawn in the plate's declared colours "
                f"({', '.join(colors) or 'none declared'})"
            )
        # The tile also shows the print's weight.  A plate that lays down
        # filament and claims 0.00 g is incomplete (Orca's density-less
        # profile and Kiln's old packager both wrote it); a plate that
        # extrudes nothing may say so honestly.
        if "Metadata/slice_info.config" in names:
            info = zf.read("Metadata/slice_info.config").decode("utf-8", errors="replace")
            if not _slice_info_knows_its_weight(info):
                body = zf.read("Metadata/plate_1.gcode").decode("utf-8", errors="replace")
                # Judged by what completion would write: a plate whose real
                # weight rounds to 0.00 g (a purge line and nothing else)
                # is not incomplete, it is light.
                if round(sum(filament_usage_from_gcode(body).grams), 2) > 0:
                    problems.append(
                        "the plate's weight reads 0.00 g though it extrudes filament — the "
                        "printer's screen would show no weight"
                    )
    if problems:
        problems.append(
            "the printer's screen would show a broken tile; complete the archive with "
            "kiln.printers.bambu_3mf.complete_bambu_archive (slice_model does this for the "
            "file it recommends)"
        )
    return problems


def _archive_triangles(path: str, colors: list[str]) -> list[Any]:
    """The archive's own model, coloured as the archive declares."""
    from kiln.threemf_parser import parse_colored_3mf

    mesh = parse_colored_3mf(path)
    triangles = list(mesh.triangles)
    if not mesh.colors_found and colors:
        # One declared colour and an unpainted model: the whole plate is it.
        h = colors[0].lstrip("#")
        rgb = tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
        triangles = [dataclasses.replace(t, color=rgb) for t in triangles]
    return triangles


def complete_bambu_archive(
    path: str | os.PathLike[str], *, output_path: str | os.PathLike[str] | None = None,
) -> str:
    """Give a sliced Bambu archive the preview family its screen needs.

    Every thumbnail slot is rendered from the archive's OWN model in the
    colours its plate declares (a painted plate keeps its paint) — the
    plain coloured render the printer's screen shows, not Kiln's stage,
    which belongs to the previews shown to a person — scaled to each
    slot's size, and written beside the marker that vouches for them.
    A ``slice_info.config`` whose ``weight`` the slicer left at ``0.00``
    (Orca through Kiln's density-less profile did, for the painted jar)
    gets the weight and per-filament ``used_m`` / ``used_g`` the screen
    shows, read from the plate's own G-code; a weight the slicer knew is
    kept.  Every other member — the G-code above all — is copied byte for
    byte: an archive a slicer already wrapped must never be wrapped again
    (measured: re-wrapping Orca's plate doubled its start sequence).
    Rewrites in place unless *output_path* is given.  Idempotent.

    Raises ``ValueError`` for an archive that is not a sliced plate.
    """
    src = Path(path)
    dst = Path(output_path) if output_path else src
    slice_info: str | None = None
    with zipfile.ZipFile(str(src)) as zf:
        if "Metadata/plate_1.gcode" not in zf.namelist():
            raise ValueError(f"{src.name} is not a sliced plate (no Metadata/plate_1.gcode)")
        colors = _declared_plate_colors(zf)
        if "Metadata/slice_info.config" in zf.namelist():
            existing = zf.read("Metadata/slice_info.config").decode("utf-8", errors="replace")
            if not _slice_info_knows_its_weight(existing):
                plate_gcode = zf.read("Metadata/plate_1.gcode").decode("utf-8", errors="replace")
                slice_info = _fill_slice_info_usage(existing, filament_usage_from_gcode(plate_gcode))
    from kiln.colored_renderer import render_colored_mesh

    triangles = _archive_triangles(str(src), colors)
    if not triangles:
        raise ValueError(f"{src.name} carries no model to draw a preview from")
    rendered: dict[str, bytes] = {}
    for slot_names in _thumbnail_aspect_groups().values():
        width, height = max(
            (_BAMBU_THUMBNAIL_SPECS[n] for n in slot_names), key=lambda size: size[0] * size[1],
        )
        result = render_colored_mesh(triangles, width=width, height=height)
        try:
            source = Path(result.path).read_bytes()
        finally:
            with contextlib.suppress(OSError):
                os.remove(result.path)
        rendered.update(_fit_to_specs(source, slot_names))
    marker = json.dumps(
        {
            "colors": colors,
            "renderer": "colored_mesh",
            "from": "3D/3dmodel.model",
            "sha": hashlib.sha256(rendered["Metadata/plate_1.png"]).hexdigest()[:32],
        }
    )
    replaced = set(rendered) | {KILN_PREVIEW_MARKER}
    tmp = dst.with_name(dst.name + ".completing")
    with zipfile.ZipFile(str(src)) as src_zf, zipfile.ZipFile(str(tmp), "w", zipfile.ZIP_DEFLATED) as dst_zf:
        for item in src_zf.infolist():
            if item.filename in replaced:
                continue
            if item.filename == "Metadata/slice_info.config" and slice_info is not None:
                dst_zf.writestr(item, slice_info)
                continue
            dst_zf.writestr(item, src_zf.read(item.filename))
        for name, data in rendered.items():
            dst_zf.writestr(name, data)
        dst_zf.writestr(KILN_PREVIEW_MARKER, marker)
    os.replace(str(tmp), str(dst))
    logger.info("Completed Bambu archive %s: %d preview slots in %s", dst.name, len(rendered), colors or "neutral")
    return str(dst)


def thumbnail_inputs_for_model(
    model_path: str | None,
) -> tuple[list[str] | None, str | None]:
    """Route a model file to the two thumbnail inputs of the wrap functions.

    A wrap can learn what the part looks like two ways — by rendering a
    mesh (``stl_paths``) or by copying the preview a 3MF already carries
    (``source_3mf_path``) — and which one applies is decided by the file
    extension.  Every door that wraps gcode has to make that same
    decision, so it is made once here: a per-door copy is how one door
    ends up routing a format the others drop on the floor.

    Any other extension yields ``(None, None)`` — the wrap still
    succeeds, without a preview.

    :param model_path: The model the gcode was sliced from, if known.
    :returns: ``(stl_paths, source_3mf_path)`` for the wrap call.
    """
    if not model_path or not os.path.isfile(model_path):
        return (None, None)
    ext = os.path.splitext(model_path)[1].lower()
    if ext in (".stl", ".obj", ".glb"):
        return ([model_path], None)
    if ext == ".3mf":
        return (None, model_path)
    if ext in (".step", ".stp"):
        # PrusaSlicer reads STEP natively, so a CAD file can be sliced
        # without ever becoming a mesh — but the LCD thumbnail is
        # rendered FROM a mesh, so the printer's screen went blank for
        # exactly the CAD-first users this path exists to serve.  Convert
        # for the preview only.  No converter installed just means no
        # thumbnail, which is where this started, so it degrades to the
        # old behavior rather than costing anyone a print.
        #
        # DELIBERATELY drops the conversion record, unlike every other
        # caller of this door.  The gcode was sliced from the STEP itself,
        # so this mesh is a picture of the part and not the part:
        # recording its fidelity here would attach an accuracy figure to
        # geometry that never reached the printer, and would attach it to
        # the one output whose real accuracy came from PrusaSlicer's own
        # tessellation instead.  A thumbnail's fidelity is nobody's
        # question.  Pinned by
        # test_the_thumbnail_path_deliberately_keeps_no_record.
        try:
            from kiln.step_import import ensure_mesh_path

            return ([ensure_mesh_path(model_path)[0]], None)
        except Exception as exc:  # noqa: BLE001 — no preview beats no print
            logger.info(
                "No LCD thumbnail for %s (%s)",
                os.path.basename(model_path), exc,
            )
    return (None, None)


def _thumbnail_aspect_groups() -> dict[float, list[str]]:
    """The thumbnail paths, grouped by the shape of the image they want.

    Rendering once and stretching that image into every slot turns a round
    coaster into an ellipse in the 4:3 ones, so each SHAPE gets its own
    render.  Ratios bucket to one decimal, which puts 251x188 (1.335) in
    with 4:3 (1.333): a tenth of a percent of stretch nobody can see, and
    one render saved.
    """
    groups: dict[float, list[str]] = {}
    for name, (width, height) in _BAMBU_THUMBNAIL_SPECS.items():
        groups.setdefault(round(width / height, 1), []).append(name)
    return groups


def _fit_to_specs(source: bytes, names: list[str]) -> dict[str, bytes]:
    """*source* scaled to each of *names*' declared sizes."""
    try:
        from PIL import Image
    except ImportError:
        # Pillow absent: ship the render at its native size under every
        # path.  The display scales it itself — a wrong-sized preview
        # still beats a blank one.
        logger.warning("Pillow unavailable — embedding the thumbnail unresized.")
        return dict.fromkeys(names, source)

    from io import BytesIO

    resample = getattr(Image, "Resampling", Image).LANCZOS
    fitted: dict[str, bytes] = {}
    with Image.open(BytesIO(source)) as img:
        img.load()
        for name in names:
            buf = BytesIO()
            img.resize(_BAMBU_THUMBNAIL_SPECS[name], resample).save(buf, format="PNG")
            fitted[name] = buf.getvalue()
    return fitted


def _render_plate_preview(
    stl_paths: list[str],
    colors: list[str] | None,
    width: int,
    height: int,
) -> bytes | None:
    """A picture of the part, at the size a thumbnail slot wants.

    Thin seam over :func:`kiln.multicolor_3mf.render_plate_preview` —
    the shared canonical-preview door every 3MF-emitting path calls, so
    the Bambu slots and the generic exporter cannot drift apart.  Kept
    as a module-level name so tests can fail this door in isolation.
    """
    from kiln.multicolor_3mf import render_plate_preview

    return render_plate_preview(
        stl_paths, colors=colors, width=width, height=height,
    )


def _stl_thumbnail_set(
    stl_paths: list[str],
    plate_json: str | None,
) -> dict[str, bytes]:
    """Every BambuStudio thumbnail entry, rendered from *stl_paths*.

    Both wraps in this module need the identical set, and each carried its
    own copy of the code that built it.  That is how both came to hand a
    list of PATHS to a renderer that takes parsed geometry, and how both
    stayed broken: every 3MF shipped with no preview at all.  One helper,
    one call per door.

    Best-effort by contract — a missing thumbnail must never fail the
    wrap.  It must not fail SILENTLY either: the defect above hid behind a
    bare ``except Exception: pass``, so every unhappy exit here logs.

    :param plate_json: The plate metadata this archive will carry, read
        for the colors it declares.  See :func:`_declared_filament_colors`.
    """
    if not stl_paths:
        return {}
    thumbnails: dict[str, bytes] = {}
    try:
        colors = _declared_filament_colors(plate_json)
        for names in _thumbnail_aspect_groups().values():
            # Render the largest slot in the group and scale down into the
            # rest — downsampling is free of the softness upscaling adds.
            width, height = max(
                (_BAMBU_THUMBNAIL_SPECS[n] for n in names),
                key=lambda size: size[0] * size[1],
            )
            source = _render_plate_preview(stl_paths, colors, width, height)
            if not source:
                logger.warning(
                    "No %dx%d thumbnail could be rendered from %s.",
                    width, height, stl_paths,
                )
                continue
            thumbnails.update(_fit_to_specs(source, names))
    except Exception:  # noqa: BLE001 — a preview never blocks a print
        logger.warning(
            "STL thumbnail generation failed for %s — the 3MF ships without "
            "a printer preview.",
            stl_paths,
            exc_info=True,
        )
        return {}
    if not thumbnails:
        logger.warning(
            "No thumbnail could be rendered from %s — the 3MF ships without "
            "a printer preview.",
            stl_paths,
        )
    return thumbnails


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_bambu_3mf(
    gcode_body: str,
    output_path: str,
    *,
    settings: BambuPrintSettings | None = None,
    source_3mf_path: str | None = None,
    stl_paths: list[str] | None = None,
    resume_mode: bool = False,
    printer_model: str | None = None,
    quiet_start: dict[str, Any] | None = None,
    lift_floor_mm: float | None = None,
) -> Bambu3MFResult:
    """Build a Bambu-compatible 3MF from PrusaSlicer gcode body.

    Wraps the raw PrusaSlicer gcode with BambuStudio's proprietary
    start/end gcode and packages everything as a 3MF file.

    :param gcode_body: Raw gcode from PrusaSlicer (sliced with
        ``--use-relative-e-distances`` and empty start/end gcode).
    :param output_path: Path for the output 3MF file.
    :param settings: Print settings (temps, filament, etc.).
    :param source_3mf_path: Optional source 3MF to extract thumbnails
        and 3D model geometry from.
    :param resume_mode: When True, skip Bambu's proprietary start-gcode
        (homing, bed probe, AMS load, purge, calibration) and the initial
        M73.  Used for mid-print resume gcode that carries its own
        preamble (heat → Z+5 safety lift → home X/Y only → travel →
        optional prime → descend to resume Z).  Re-running Bambu's full
        start sequence on a bed with a partial print risks nozzle
        collision on Z rehome and wastes ~18 minutes on init.
    :param printer_model: The model the OWNER DECLARED (``bambu_p2s``,
        ``bambu_h2s``, …) — never one inferred from a serial prefix or a
        firmware string.  Selects the per-model end gcode.  ``None``, an
        empty string, or a model with no template of its own all get the A1
        files, which is what every Bambu print used before this parameter
        existed.
    :param quiet_start: The placement verdict's start plan for a print
        beside a part still on the plate (``clear_z_mm``,
        ``lift_floor_mm``, ``travel_to_mm``, ``first_layer_z_mm``,
        ``approach_mm``, ``home_xy_gcode``, ``switched_off``,
        ``planned_for_machine``, ``plate_fingerprint``).  The file then
        opens with Kiln's own prologue (:func:`kiln.printers.safe_motion.
        build_quiet_start_preamble`) under a contract header the pre-print
        gate judges live, never the vendor's start, and the plan's floor is
        this build's *lift_floor_mm*.  Exclusive with *resume_mode*.
    :param lift_floor_mm: Other parts stand on the plate: every Kiln-owned
        lift in the file -- a colour change, the end block -- rises to at
        least this height before the head moves sideways.  Given without a
        *quiet_start*, the build refuses (``ValueError`` with
        :data:`kiln.plate_state.PRINT_AROUND_SENTENCE`): a vendor-start file
        for an occupied plate is not written by Kiln at all.
    :returns: :class:`Bambu3MFResult` with output path and metadata.
    :raises FileNotFoundError: If the start/end gcode data files are missing.
    :raises ValueError: If the gcode body has no layer changes, or if a
        template could not be fully resolved.
    """
    if settings is None:
        settings = BambuPrintSettings()
    if quiet_start is not None:
        if resume_mode:
            msg = "a quiet start and a resume are two different prologues; pass one"
            raise ValueError(msg)
        quiet_start = _check_quiet_plan(quiet_start)
        lift_floor_mm = float(quiet_start["lift_floor_mm"])
    elif lift_floor_mm is not None and not resume_mode:
        # A floor means a part is on the plate.  Without the plan there is no
        # honest file: one with the vendor's start homes Z onto that part the
        # moment someone starts it from the printer's own screen.
        from kiln.plate_state import PRINT_AROUND_SENTENCE

        raise ValueError(PRINT_AROUND_SENTENCE)
    # The caller's word, else the G-code's own, else PLA on the A1 -- read
    # here, at the one place every wrapping door passes through, so a door
    # that slices PETG and wraps with the defaults no longer tells the
    # printer PLA and purges at PLA temperatures.
    settings = resolve_settings_from_gcode(settings, gcode_body)

    # A multicolor gcode wrapped as a single-filament 3MF is a print that
    # tool-changes 186 times into whatever is in tray 1: the toolpath is
    # right and everything the PRINTER reads — the filament list, the AMS
    # load blocks around each T command — says one color.  The gcode
    # itself is the authority (Orca writes ``; filament_colour = a;b;c``
    # and a ``T`` per change), so it is read here, at the one place every
    # wrapping door passes through, rather than asked of each caller.
    # An explicit multi-filament setting always wins.
    if settings.num_filaments <= 1:
        derived = _declared_filaments_in_gcode(gcode_body)
        if derived is not None:
            count, colors, types = derived
            settings = replace(
                settings,
                num_filaments=count,
                filament_colors=colors or settings.filament_colors,
                filament_types=[bambu_filament_type(t) for t in types] if types else settings.filament_types,
            )
            logger.info(
                "Gcode declares %d filaments (%s) — wrapping as multicolor.",
                count, ", ".join(colors) if colors else "no colors declared",
            )

    # Analyze the gcode body.
    total_layers = _count_layers(gcode_body)
    if total_layers == 0:
        msg = "Gcode body has no ;LAYER_CHANGE markers — cannot build 3MF."
        raise ValueError(msg)

    max_z = _find_max_z(gcode_body)

    # Try to extract PrusaSlicer's own time estimate (much more accurate
    # than a flat per-layer heuristic).  Falls back to layers * 6 if the
    # slicer didn't embed an estimate.
    est_time_sec = _extract_slicer_time_estimate(gcode_body)
    if est_time_sec <= 0:
        # Fallback: estimate from gcode size.  Typical FDM printers process
        # ~40-60 bytes of gcode per second at normal speeds; 50 B/s is a
        # reasonable middle ground.  This gives much better estimates than
        # the old ``layers * 6`` heuristic (which produced ~100 s for a
        # 20-minute coaster).
        est_time_sec = max(total_layers * 6, len(gcode_body) // 50)

    # Apply Bambu speed correction: PrusaSlicer overestimates by ~2x for
    # printers with input shaping because it doesn't model their actual
    # acceleration profiles.  This corrects the M73 R (remaining time)
    # values so the printer LCD shows accurate time from the first second.
    #
    # PrusaSlicer's estimate ONLY.  The correction is calibrated against
    # that slicer's motion model (see get_slicer_time_factor), and Orca /
    # BambuStudio are Bambu's own fork: they model the input shaping this
    # factor exists to compensate for, so halving their number reports a
    # print as taking half as long as it does.  Measured 2026-08-27 on one
    # model through one profile — PrusaSlicer 2h19m, OrcaSlicer 1h52m —
    # Orca already lands BELOW the uncorrected Prusa figure, which is the
    # correction the factor was approximating.
    if not _gcode_is_bambu_dialect(gcode_body):
        try:
            from kiln.printer_intelligence import get_slicer_time_factor

            time_factor = get_slicer_time_factor("bambu_a1")
            est_time_sec = max(60, int(est_time_sec * time_factor))
        except ImportError:
            pass

    est_minutes = max(1, est_time_sec // 60)

    logger.info(
        "Building Bambu 3MF: %d layers, max_z=%.1f, est=%dm",
        total_layers,
        max_z,
        est_minutes,
    )

    # Load and resolve templates for the declared model.  Both resolved
    # strings are then checked for surviving placeholders: this text is
    # copied into the 3MF and sent to the printer verbatim, so a template we
    # could not fully resolve must stop the build rather than reach a machine.
    start_template, start_source, start_nozzle = _start_gcode_choice(
        printer_model, settings.nozzle_diameter,
    )
    start_gcode = _resolve_start_gcode(
        start_template,
        hotend_temp=settings.hotend_temp,
        bed_temp=settings.bed_temp,
        filament_type=settings.filament_type,
    )
    _assert_fully_resolved(start_gcode, source=f"{start_source} start gcode")

    end_template, end_source = _select_end_gcode(printer_model)
    end_gcode = _resolve_end_gcode(
        end_template,
        max_z=max_z,
        printer_model=end_source,
        lift_floor_mm=lift_floor_mm,
        z_top_mm=_machine_top_mm(printer_model),
    )
    _assert_fully_resolved(end_gcode, source=f"{end_source} end gcode")

    # Correct the M73 remaining-time values the capture carries, so the LCD is
    # right during the startup sequence too.  Each is the estimate for the
    # print that was sliced when the sequence was captured, so the baseline is
    # read from the sequence in hand rather than assumed: the A1 was captured
    # on a ~186 minute print and every model captured since on a ~21 minute
    # one, and scaling those by the A1's 186 would understate the remaining
    # time by roughly nine times.
    est_minutes_with_startup = max(1, (est_time_sec + _BAMBU_STARTUP_OVERHEAD_SEC) // 60)
    capture_minutes = max(
        (int(r) for r in re.findall(r"M73 P\d+ R(\d+)", start_gcode)), default=0,
    )
    if capture_minutes > 0:
        def _scale_start_m73(match: re.Match) -> str:
            p = int(match.group(1))
            old_r = int(match.group(2))
            new_r = max(1, round(old_r * est_minutes_with_startup / capture_minutes))
            return f"M73 P{p} R{new_r}"
        start_gcode = re.sub(r"M73 P(\d+) R(\d+)", _scale_start_m73, start_gcode)

    # Post-process the PrusaSlicer body.
    processed_body = _postprocess_prusa_body(
        gcode_body,
        total_layers=total_layers,
        est_time_sec=est_time_sec,
    )

    # Multi-filament: wrap T commands in M620/M621 AMS blocks
    if settings.num_filaments > 1:
        processed_body = _wrap_tool_changes(
            processed_body,
            printer_model=printer_model,
            hotend_temp=settings.hotend_temp,
            filament_type=settings.filament_type,
            lift_floor_mm=lift_floor_mm,
        )

    # Build the header.
    header = _build_gcode_header(
        total_layers=total_layers,
        max_z=max_z,
        est_print_time_sec=est_time_sec,
        filament_type=settings.filament_type,
        nozzle_diameter=settings.nozzle_diameter,
        hotend_temp=settings.hotend_temp,
        bed_temp=settings.bed_temp,
        num_filaments=settings.num_filaments,
        filament_types=settings.get_filament_types(),
    )

    # Inject an initial M73 at the very start so the firmware shows the
    # correct time estimate from the first second — before the ~600-line
    # startup sequence (homing, AMS load, calibration) completes.  Without
    # this, the firmware shows a garbage estimate until layer printing
    # begins and the per-layer M73 commands kick in.
    # (est_minutes_with_startup computed above, before start_gcode M73 scaling)
    initial_m73 = f"M73 P0 R{est_minutes_with_startup}\n"

    # Assemble complete gcode.
    if quiet_start is not None:
        # The quiet start: Kiln's contract header and prologue in place of
        # the vendor's start and the initial M73 -- no Z home on the plate,
        # no probe, no purge line across it.  The pre-print gate reads the
        # header back from this very file before the printer is told to
        # start, and asks the machine whether it is homed and idle.
        from kiln.printers.safe_motion import build_quiet_start_preamble

        preamble = build_quiet_start_preamble(
            hotend_temp=int(settings.hotend_temp),
            bed_temp=int(settings.bed_temp),
            clear_z_mm=float(quiet_start["clear_z_mm"]),
            travel_to_mm=(float(quiet_start["travel_to_mm"][0]), float(quiet_start["travel_to_mm"][1])),
            first_layer_z_mm=float(quiet_start["first_layer_z_mm"]),
            approach_mm=float(quiet_start.get("approach_mm", 5.0)),
            home_xy_gcode=str(quiet_start.get("home_xy_gcode") or "G28 X Y"),
            purge_at_mm=_purge_at(quiet_start.get("purge_at_mm")),
        )
        complete_gcode = (
            header + _quiet_start_header(quiet_start) + "\n".join(preamble) + "\n\n"
            + processed_body + "\n" + end_gcode
        )
        _assert_quiet_start_file(complete_gcode, quiet_start)
    elif resume_mode:
        # Resume-mode: suppress Bambu's proprietary start sequence and initial
        # M73.  The resume gcode body carries its own safety preamble (heat →
        # Z+5 lift → G28 X Y only → travel Z → optional filament prime →
        # descend to resume Z — the canonical shape lives in
        # kiln.printers.safe_motion.build_resume_preamble; preamble builders
        # should call it rather than hand-writing the sequence).  Running
        # Bambu's start-gcode on a bed with a partial print would re-home Z
        # (nozzle collision risk), re-probe bed (impossible with print on
        # it), and waste ~18 min on AMS load + calibration before the resume
        # preamble ever executes.
        complete_gcode = header + processed_body + "\n" + end_gcode
    else:
        complete_gcode = initial_m73 + header + start_gcode + "\n" + processed_body + "\n" + end_gcode

    # Build metadata.
    gcode_bytes = complete_gcode.encode("utf-8")
    gcode_md5 = hashlib.md5(gcode_bytes).hexdigest()  # noqa: S324

    f_colors = settings.get_filament_colors()
    f_types = settings.get_filament_types()

    # Include startup overhead in the prediction shown on the printer
    # display.  The M73 R values use the raw est_time_sec (they count
    # down during printing, after startup is already finished).
    est_time_sec_with_startup = est_time_sec + _BAMBU_STARTUP_OVERHEAD_SEC

    # The weight the screen shows, read from the body the slicer wrote —
    # before Bambu's start sequence is added, so the purge line is not
    # counted as the part.  A Kiln slice carries the slicer's own grams;
    # the declared types are the density source when a body has none.
    usage = filament_usage_from_gcode(gcode_body, filament_types=f_types)

    slice_info = _build_slice_info(
        total_layers=total_layers,
        est_print_time_sec=est_time_sec_with_startup,
        filament_type=settings.filament_type,
        filament_color=settings.filament_color,
        nozzle_diameter=settings.nozzle_diameter,
        model_name=settings.model_name,
        num_filaments=settings.num_filaments,
        filament_colors=f_colors,
        filament_types=f_types,
        usage=usage,
    )
    plate_json = _build_plate_json(
        filament_color=settings.filament_color,
        nozzle_diameter=settings.nozzle_diameter,
        bed_type=settings.bed_type,
        num_filaments=settings.num_filaments,
        filament_colors=f_colors,
    )
    model_settings = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<config>\n"
        '  <object id="1">\n'
        f'    <metadata key="name" value="{settings.model_name}"/>\n'
        "  </object>\n"
        "</config>"
    )

    # Extract thumbnails and geometry from source 3MF if available.
    #
    # The root model part is copied VERBATIM, so every part it references has
    # to travel with it.  Under the 3MF production extension a root part holds
    # <component objectid="..." p:path="/3D/Objects/object_N.model"/> and the
    # geometry lives in those sub-parts.  Reading only 3D/3dmodel.model
    # produced an archive whose components pointed at parts that were never
    # written — a dangling reference we manufactured ourselves.  Bambu Studio,
    # OrcaSlicer and PrusaSlicer all write that layout, so it is the common
    # case for a sliced project file, not an exotic one.
    #
    # [Content_Types].xml already declares .model by Default Extension, so
    # carrying extra parts needs no manifest change, and _RELS_XML only has to
    # name the root part — sub-parts are reached through p:path, not rels.
    thumbnails: dict[str, bytes] = {}
    model_parts: dict[str, bytes] = {}
    model_data: str = _MINIMAL_3D_MODEL
    if source_3mf_path and os.path.isfile(source_3mf_path):
        try:
            with zipfile.ZipFile(source_3mf_path) as zf:
                for name in zf.namelist():
                    if name.endswith(".png") and (
                        name.startswith("Metadata/")
                        or name.startswith("Auxiliaries/.thumbnails/")
                    ):
                        thumbnails[name] = zf.read(name)
                    elif name == "3D/3dmodel.model":
                        model_data = zf.read(name).decode("utf-8")
                    elif name.startswith("3D/") and name.endswith(".model"):
                        # Copy every sibling part, referenced or not.  An
                        # unreferenced part is harmless weight; a missing
                        # referenced one breaks the file.
                        model_parts[name] = zf.read(name)
        except (zipfile.BadZipFile, KeyError):
            logger.warning(
                "Could not extract thumbnails from %s", source_3mf_path
            )

    # No thumbnails in the source: render the STLs instead, in the same
    # filament colors this file declares, so the printer shows the part
    # rather than a blank preview.  ``plate_json`` is the declaration —
    # the very string written to the archive below.
    rendered_here = False
    if not thumbnails and stl_paths:
        thumbnails = _stl_thumbnail_set(stl_paths, plate_json)
        rendered_here = bool(thumbnails)

    # Build the 3MF.
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("3D/3dmodel.model", model_data)
        # Sub-parts the root model reaches through p:path.  Written before the
        # metadata so a reader walking the archive in order resolves every
        # component reference in the root part.
        for name, data in model_parts.items():
            zf.writestr(name, data)
        zf.writestr("Metadata/plate_1.gcode", complete_gcode)
        zf.writestr("Metadata/plate_1.gcode.md5", gcode_md5)
        zf.writestr("Metadata/slice_info.config", slice_info)
        zf.writestr("Metadata/plate_1.json", plate_json)
        zf.writestr("Metadata/model_settings.config", model_settings)
        zf.writestr(
            "Metadata/_rels/model_settings.config.rels", _MODEL_SETTINGS_RELS
        )
        zf.writestr(
            "Metadata/cut_information.xml",
            '<?xml version="1.0" encoding="UTF-8"?>\n<cut_information/>',
        )
        filament_seq = list(range(settings.num_filaments))
        zf.writestr(
            "Metadata/filament_sequence.json",
            json.dumps({"filament_sequence": filament_seq}),
        )
        zf.writestr("Metadata/project_settings.config", "{}")
        if quiet_start is not None:
            zf.writestr(KILN_QUIET_START_MARKER, json.dumps(quiet_start, sort_keys=True))
        for name, data in thumbnails.items():
            zf.writestr(name, data)
        if rendered_here and "Metadata/plate_1.png" in thumbnails:
            # Kiln drew these itself, in the plate's declared colour: say
            # so, with a hash of the picture it wrote.
            zf.writestr(
                KILN_PREVIEW_MARKER,
                json.dumps(
                    {
                        "colors": _declared_filament_colors(plate_json) or [],
                        "renderer": "plate_preview",
                        "from": "stl",
                        "sha": hashlib.sha256(thumbnails["Metadata/plate_1.png"]).hexdigest()[:32],
                    }
                ),
            )

    file_size = os.path.getsize(output_path)
    file_md5 = hashlib.md5(  # noqa: S324
        Path(output_path).read_bytes()
    ).hexdigest()

    logger.info(
        "Built Bambu 3MF: %s (%d bytes, %d layers)",
        output_path,
        file_size,
        total_layers,
    )

    return Bambu3MFResult(
        output_path=output_path,
        total_layers=total_layers,
        max_z=max_z,
        file_size=file_size,
        md5=file_md5,
        est_print_time_sec=est_time_sec_with_startup,
        start_gcode_model=start_source,
        start_gcode_nozzle=start_nozzle,
        requested_nozzle=_nozzle_key(settings.nozzle_diameter) or None,
        end_gcode_model=end_source,
        requested_model=printer_model,
        filament_type=str(settings.filament_type),
        hotend_temp=int(settings.hotend_temp),
        bed_temp=int(settings.bed_temp),
        quiet_start=quiet_start is not None,
        lift_floor_mm=lift_floor_mm,
    )


def repackage_gcode_as_bambu_3mf(
    gcode_path: str,
    output_path: str,
    *,
    source_3mf_path: str | None = None,
    stl_paths: list[str] | None = None,
    estimated_time_minutes: int = 0,
) -> str:
    """Wrap already-Bambu gcode in a minimal 3MF container.

    Unlike :func:`build_bambu_3mf` which adds BambuStudio start/end
    sequences to PrusaSlicer output, this function takes gcode that
    **already contains** the Bambu startup sequence (e.g. extracted from
    a .gcode.3mf via :func:`extract_plate_object_gcode`) and simply
    packages it in the 3MF zip structure that Bambu firmware requires
    for the ``project_file`` MQTT command.

    This is necessary because Bambu printers ignore the ``gcode_file``
    MQTT command for raw .gcode uploads — they only respond to
    ``project_file`` which expects a .3mf archive.

    Thumbnails are resolved in this order:
      1. Copied from ``source_3mf_path`` if it's a valid 3MF zip.
      2. Rendered from ``stl_paths`` when no source thumbnails were
         found, in the filament colors the copied plate metadata
         declares.  Without this fallback the printer's LCD shows a
         blank preview for freshly-sliced parts.

    :param gcode_path: Path to the .gcode file (already Bambu-ready).
    :param output_path: Path for the output .gcode.3mf file.
    :param source_3mf_path: Optional source 3MF to copy thumbnails and
        plate metadata from.
    :param stl_paths: Optional list of STL paths.  When no thumbnails
        were extracted from *source_3mf_path*, renders of these STLs
        are embedded so the Bambu touchscreen preview matches the
        sliced geometry.
    :param estimated_time_minutes: Object's estimated print time in
        minutes (from extraction).  Used to update the ``prediction``
        field in slice_info.config so the printer display shows
        accurate time remaining.
    :returns: The *output_path* for convenience.
    :raises FileNotFoundError: If *gcode_path* does not exist.
    """
    abs_path = os.path.abspath(gcode_path)
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(f"Gcode file not found: {abs_path}")

    gcode_bytes = Path(abs_path).read_bytes()
    gcode_md5 = hashlib.md5(gcode_bytes).hexdigest()  # noqa: S324

    # Extract thumbnails and plate metadata from source 3MF if available.
    thumbnails: dict[str, bytes] = {}
    plate_json: str | None = None
    slice_info: str | None = None
    if source_3mf_path and os.path.isfile(source_3mf_path):
        try:
            with zipfile.ZipFile(source_3mf_path) as zf:
                for name in zf.namelist():
                    if name.endswith(".png") and (
                        name.startswith("Metadata/")
                        or name.startswith("Auxiliaries/.thumbnails/")
                    ):
                        thumbnails[name] = zf.read(name)
                # Copy plate metadata for accurate printer display
                if "Metadata/plate_1.json" in zf.namelist():
                    plate_json = zf.read("Metadata/plate_1.json").decode(
                        "utf-8", errors="replace"
                    )
                if "Metadata/slice_info.config" in zf.namelist():
                    slice_info = zf.read("Metadata/slice_info.config").decode(
                        "utf-8", errors="replace"
                    )
        except (zipfile.BadZipFile, KeyError):
            logger.warning(
                "Could not extract metadata from %s", source_3mf_path
            )

    # Fallback: render the STLs so the Bambu LCD shows the actual part
    # rather than a blank square.  Same helper :func:`build_bambu_3mf`
    # uses — one implementation, so the two doors cannot drift apart.
    # The colors come from the plate metadata copied out of the source
    # above; when there is none, the archive declares no color and the
    # render stays neutral rather than inventing one.
    rendered_here = False
    if not thumbnails and stl_paths:
        thumbnails = _stl_thumbnail_set(stl_paths, plate_json)
        rendered_here = bool(thumbnails)

    # Update the time prediction in slice_info.config so the printer
    # display shows correct time remaining instead of the full plate's
    # estimate.  The ``prediction`` value is in seconds.  Include startup
    # overhead so the estimate is accurate from the first second.
    if slice_info and estimated_time_minutes > 0:
        prediction_sec = estimated_time_minutes * 60 + _BAMBU_STARTUP_OVERHEAD_SEC
        slice_info = re.sub(
            r'(<metadata\s+key="prediction"\s+value=")(\d+)(")',
            rf"\g<1>{prediction_sec}\3",
            slice_info,
        )
    # A copied slice_info whose weight the slicer left at 0.00 gets the
    # weight of the gcode this archive actually carries — same reader as
    # the builder and the completion, so no door shows a blank tile.
    if slice_info and not _slice_info_knows_its_weight(slice_info):
        slice_info = _fill_slice_info_usage(
            slice_info,
            filament_usage_from_gcode(gcode_bytes.decode("utf-8", errors="replace")),
        )

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("3D/3dmodel.model", _MINIMAL_3D_MODEL)
        zf.writestr("Metadata/plate_1.gcode", gcode_bytes)
        zf.writestr("Metadata/plate_1.gcode.md5", gcode_md5)
        if plate_json:
            zf.writestr("Metadata/plate_1.json", plate_json)
        if slice_info:
            zf.writestr("Metadata/slice_info.config", slice_info)
        for name, data in thumbnails.items():
            zf.writestr(name, data)
        if rendered_here and "Metadata/plate_1.png" in thumbnails:
            # Kiln drew these itself, in the plate's declared colour: say
            # so, with a hash of the picture it wrote.
            zf.writestr(
                KILN_PREVIEW_MARKER,
                json.dumps(
                    {
                        "colors": _declared_filament_colors(plate_json) or [],
                        "renderer": "plate_preview",
                        "from": "stl",
                        "sha": hashlib.sha256(thumbnails["Metadata/plate_1.png"]).hexdigest()[:32],
                    }
                ),
            )

    logger.info(
        "Repackaged gcode as Bambu 3MF: %s (%d bytes, est %dm)",
        output_path,
        os.path.getsize(output_path),
        estimated_time_minutes,
    )
    return output_path


def _reset_cache() -> None:
    """Reset lazy singletons — for testing only."""
    global _a1_start_gcode, _a1_end_gcode, _printer_intel_raw  # noqa: PLW0603
    _a1_start_gcode = None
    _a1_end_gcode = None
    _printer_intel_raw = None
    _model_gcode_cache.clear()
    _fallback_warned.clear()
