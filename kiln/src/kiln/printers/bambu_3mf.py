"""Bambu Lab 3MF packaging for PrusaSlicer gcode."""

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

from kiln.gcode import (
    FIRST_PSEUDO_TOOL,
    GCODE_NUMBER,
    LAYER_CHANGE_LINE,
    LAYER_Z_LINE,
    PACKAGED_BY_KILN,
    axis_value,
    extruded_mm_per_tool,
    has_axis_word,
    slicer_filament_totals,
    slicer_filament_types,
    slicer_nozzle_diameters,
    slicer_print_time,
)
from kiln.untrusted_zip import UntrustedZipFile

logger = logging.getLogger(__name__)

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
# ---------------------------------------------------------------------------

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
    ("bambu_h3c", "0.4"): "bambu_h3c_start_gcode.gcode",
}

# End G-code, one file per model we ship a template for
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
_MODEL_FLUSH_STATION: dict[str, tuple[float | None, float | None]] = {
    # Run on the owner's A1 Combo; ``None`` in Y keeps the head's own row.
    "bambu_a1": (-48.2, None),
}

KILN_COLOUR_CHANGE_NOTE = "; AMS switch to filament"


_MODEL_END_GCODE_FILES: dict[str, str] = {
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
    "bambu_h2c": "bambu_h2c_end_gcode.gcode",
    # The H3C's template reads its retraction from _MODEL_END_VALUES.
    "bambu_h3c": "bambu_h3c_end_gcode.gcode",
}

# : Values an end template reads besides the part's height, per model
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
    # The H3C's end opens with one retraction and asks how long
    "bambu_h3c": {
        "retraction_length": [0.4],
        "current_extruder": 0,
    },
}

# Lazy cache for the per-model files, keyed by filename.
_model_gcode_cache: dict[str, str] = {}

_fallback_warned: set[tuple[str, str]] = set()


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class BambuPrintSettings:
    """Print-specific settings for Bambu 3MF building."""

    hotend_temp: int | None = None
    bed_temp: int | None = None
    filament_type: str | None = None
    filament_color: str = "#FFFFFF"
    nozzle_diameter: float | None = None
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
    start_gcode_model: str = "bambu_a1"
    start_gcode_nozzle: str = "0.4"
    requested_nozzle: str | None = None
    # : The model whose END sequence this file carries
    end_gcode_model: str = "bambu_a1"
    requested_model: str | None = None
    quiet_start: bool = False
    lift_floor_mm: float | None = None
    filament_type: str = "PLA"
    hotend_temp: int = 220
    bed_temp: int = 65
    levelling_region: tuple[float, float, float, float] | None = None
    levelling_source: str = "capture"
    bed_type: str = "textured_plate"

    @property
    def levelling_warning(self) -> str | None:
        """Say so when the bed is not levelled where this print goes."""
        if self.levelling_source == "first_layer" or self.quiet_start:
            return None
        if self.levelling_source == "whole_bed":
            return (
                "Kiln could not read where this print's first layer goes, so the start "
                "sequence levels the whole plate. That takes longer and loses nothing."
            )
        return (
            "Kiln could not read where this print's first layer goes and has no bed size "
            "for this machine, so the start sequence levels the patch it was recorded "
            "with, not this print's. Watch the first layer."
        )

    @property
    def start_gcode_warning(self) -> str | None:
        """Say so when the start sequence is not this machine's own."""
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
        """Say so when the END sequence is not this machine's own."""
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
        if self.levelling_region is not None:
            d["levelling_region"] = [round(v, 3) for v in self.levelling_region]
            d["levelling_source"] = self.levelling_source
        if self.levelling_warning:
            d["levelling_warning"] = self.levelling_warning
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
    """The A1 end sequence, with its print height fixed rather than templated."""
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


#: Number words on a motion command, comment stripped first.
_MOTION_WORD_RE = re.compile(r"\b([XYS])(\d+(?:\.\d+)?)\b")


def start_sequence_motion(
    printer_model: str | None,
    nozzle_diameter: float | str | None = 0.4,
) -> dict[str, float] | None:
    """The motion limits this model's own start sequence leaves it with."""
    model = _normalize_model(printer_model)
    if not any(m == model for m, _n in _MODEL_START_GCODE_FILES):
        return None
    text, source_model, _source_nozzle = _start_gcode_choice(model, nozzle_diameter)
    if source_model != model:
        return None

    motion: dict[str, float] = {}
    accelerations: list[float] = []
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        command = line.split()[0]
        words = dict(_MOTION_WORD_RE.findall(line))
        if command == "M204" and "S" in words:
            accelerations.append(float(words["S"]))
            continue
        prefix = {"M201": "max_accel", "M203": "max_feedrate", "M205": "jerk"}.get(command)
        if prefix is None:
            continue
        for axis in ("X", "Y"):
            if axis in words:
                motion[f"{prefix}_{axis.lower()}"] = float(words[axis])
    if accelerations:
        motion["accel"] = min(accelerations)
    return motion or None


def _start_gcode_choice(
    printer_model: str | None,
    nozzle_diameter: float | str | None = 0.4,
) -> tuple[str, str, str]:
    """Pick the start gcode for a DECLARED printer model and nozzle."""
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
    """Where this model's AMS block takes the head to flush."""
    model = _normalize_model(printer_model)
    if model in _MODEL_FLUSH_STATION:
        return _MODEL_FLUSH_STATION[model]
    if any(known == model for known, _nozzle in _MODEL_START_GCODE_FILES):
        return None
    return _MODEL_FLUSH_STATION["bambu_a1"]


def _select_end_gcode(printer_model: str | None) -> tuple[str, str]:
    """Pick the end gcode template for a DECLARED printer model."""
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

_BRACKET_PLACEHOLDER_RE = re.compile(r"\[[a-z_][a-z0-9_\[\]]*\]", re.IGNORECASE)
_BRACE_PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")


def _assert_fully_resolved(gcode: str, *, source: str) -> None:
    """Refuse to emit a template that still carries a placeholder."""
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


_CAPTURE_HOTEND_TEMP = 220

TEXTURED_PLATE = "textured_plate"
BED_TYPES: dict[str, str] = {
    TEXTURED_PLATE: "Textured PEI Plate",
    "hot_plate": "High Temp Plate",
    "cool_plate": "Cool Plate",
    "eng_plate": "Engineering Plate",
    "supertack_plate": "Supertack Plate",
}

_FALLBACK_HOTEND_TEMP = 220
_FALLBACK_BED_TEMP = 65
_FALLBACK_FILAMENT_TYPE = "PLA"
_FALLBACK_NOZZLE_DIAMETER = 0.4

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
    """*word* as the filament type a Bambu understands."""
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


# : The temperatures a body prints its first layer at
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
    """The settings the build runs with: the caller's word, else the G-code's own, else the fallback."""
    hotend, bed = _print_temperatures(gcode_body)
    nozzle_diameter = settings.nozzle_diameter
    if nozzle_diameter is None:
        stated = slicer_nozzle_diameters(gcode_body)
        nozzle_diameter = stated[-1][0] if stated else _FALLBACK_NOZZLE_DIAMETER
    filament_type = settings.filament_type
    if not filament_type:
        read = slicer_filament_types(gcode_body)
        if read:
            filament_type = read[0].split(",")[0].strip()
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
        nozzle_diameter=nozzle_diameter,
    )


def _capture_bed_temp(template: str) -> int | None:
    """The bed temperature a captured start sequence was taken at."""
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
    source_model: str | None = None,
    levelling_region: tuple[float, float, float, float] | None = None,
    max_z: float | None = None,
    bed_type: str = TEXTURED_PLATE,
) -> str:
    """Put this print's own values into a captured start sequence."""
    if source_model is not None:
        template = _fit_start_to_print(
            template,
            source_model=source_model,
            levelling_region=levelling_region,
            max_z=max_z,
            bed_type=bed_type,
            bed_temp=bed_temp,
        )
    capture_bed = _capture_bed_temp(template)
    lines = template.split("\n")
    resolved: list[str] = []

    for line in lines:
        stripped = line.strip()

        if hotend_temp != _CAPTURE_HOTEND_TEMP and (
            stripped.startswith(f"M104 S{_CAPTURE_HOTEND_TEMP}")
            or stripped.startswith(f"M109 S{_CAPTURE_HOTEND_TEMP}")
        ):
            line = line.replace(f"S{_CAPTURE_HOTEND_TEMP}", f"S{hotend_temp}")

        elif (
            capture_bed is not None
            and bed_temp != capture_bed
            and (
                stripped.startswith(f"M140 S{capture_bed}")
                or stripped.startswith(f"M190 S{capture_bed}")
            )
        ):
            line = line.replace(f"S{capture_bed}", f"S{bed_temp}")

        elif filament_type != "PLA" and "set_filament_type:PLA" in line:
            line = line.replace("set_filament_type:PLA", f"set_filament_type:{filament_type}")

        resolved.append(line)

    return "\n".join(resolved)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

_START_VARIANTS_PATH = _DATA_DIR / "bambu_start_variants.json"
_start_variants: dict[str, Any] | None = None

_LEVELLING_PATCH_RE = re.compile(
    r"^(?P<head>\s*G29(?:\.30| A\d?)(?: U\d+)?(?: O)?) X\S+ Y\S+ I\S+ J\S+(?P<tail>.*)$"
)


def _load_start_variants() -> dict[str, Any]:
    global _start_variants  # noqa: PLW0603
    if _start_variants is None:
        _start_variants = json.loads(_START_VARIANTS_PATH.read_text(encoding="utf-8"))
    return _start_variants


def bed_type_name(bed_type: str | None) -> str:
    """The maker's own name for a plate, from the key its files use for it."""
    key = str(bed_type or "").strip().lower()
    if key not in BED_TYPES:
        msg = (
            f"Unknown plate {bed_type!r}. Kiln knows: {', '.join(sorted(BED_TYPES))}."
        )
        raise ValueError(msg)
    return BED_TYPES[key]


def first_layer_region(gcode_body: str) -> tuple[float, float, float, float] | None:
    """The patch of bed a body's first layer covers: ``(x, y, width, depth)``."""
    x = y = None
    half_width = 0.0
    relative_e = True
    last_e = 0.0
    low_x = low_y = float("inf")
    high_x = high_y = float("-inf")
    layers_seen = 0
    for raw in gcode_body.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith(";"):
            if LAYER_CHANGE_LINE.match(line):
                layers_seen += 1
                if layers_seen == 2:
                    break
            elif line.startswith(";WIDTH:"):
                with contextlib.suppress(ValueError):
                    half_width = float(line[7:]) / 2.0
            continue
        code = line.split(";", 1)[0]
        words = code.split()
        if not words:
            continue
        command = words[0]
        if command == "M83":
            relative_e = True
            continue
        if command == "M82":
            relative_e = False
            continue
        values: dict[str, float] = {}
        for word in words[1:]:
            if word[0] in "XYE" and len(word) > 1:
                with contextlib.suppress(ValueError):
                    values[word[0]] = float(word[1:])
        if command == "G92":
            if "E" in values:
                last_e = values["E"]
            continue
        if command not in ("G0", "G1", "G2", "G3"):
            continue
        new_x, new_y = values.get("X", x), values.get("Y", y)
        extruding = False
        if "E" in values:
            extruding = values["E"] > 0 if relative_e else values["E"] > last_e
            if not relative_e:
                last_e = values["E"]
        moved = ("X" in values or "Y" in values) and new_x is not None and new_y is not None
        if layers_seen == 1 and extruding and moved:
            for px, py in ((x, y), (new_x, new_y)):
                if px is None or py is None:
                    continue
                low_x, high_x = min(low_x, px - half_width), max(high_x, px + half_width)
                low_y, high_y = min(low_y, py - half_width), max(high_y, py + half_width)
        x, y = new_x, new_y
    if high_x - low_x <= 0 or high_y - low_y <= 0:
        return None
    return low_x, low_y, high_x - low_x, high_y - low_y


def _whole_bed_region(printer_model: str | None) -> tuple[float, float, float, float] | None:
    """The whole plate as a levelling patch, for a body whose first layer could not be read: more than this print needs, never less."""
    center = _bed_center(printer_model) if printer_model else None
    if center is None:
        return None
    return 0.0, 0.0, center[0] * 2.0, center[1] * 2.0


def _replace_run(lines: list[str], find: list[str], replace: list[str], *, what: str) -> list[str]:
    """*lines* with the one run equal to *find* swapped for *replace*."""
    bare = [line.rstrip() for line in lines]
    span = len(find)
    hits = [i for i in range(len(bare) - span + 1) if bare[i:i + span] == find]
    if len(hits) != 1:
        msg = (
            f"Cannot fit the start sequence to this print ({what}): expected one place to "
            f"change and found {len(hits)}. Refusing to build a 3MF: this text is sent to "
            f"the printer verbatim."
        )
        raise ValueError(msg)
    at = hits[0]
    return lines[:at] + list(replace) + lines[at + span:]


def _fit_start_to_print(
    template: str,
    *,
    source_model: str,
    levelling_region: tuple[float, float, float, float] | None,
    max_z: float | None,
    bed_type: str,
    bed_temp: int,
) -> str:
    """A captured start sequence with this print's patch, height and plate."""
    plate = bed_type_name(bed_type)
    table = _load_start_variants()
    variants = table["models"].get(_normalize_model(source_model), {})
    lines = template.split("\n")

    conditions: list[str] = []
    if max_z is not None and math.ceil(max_z) >= table["tall_from_mm"]:
        conditions.append("tall")
    textured = str(bed_type).strip().lower() == TEXTURED_PLATE
    hot_above = table["hot_bed_above"].get(_normalize_model(source_model))
    hot = hot_above is not None and bed_temp > hot_above
    if not textured:
        conditions.append("other_plate_hot_bed" if hot else "other_plate")
    elif hot:
        conditions.append("hot_bed")

    values = {
        "{max_print_z}": str(math.ceil(max_z)) if max_z is not None else "",
        "{curr_bed_type}": plate,
    }
    for condition in conditions:
        for change in variants.get(condition, ()):
            replace = list(change["replace"])
            for token, value in values.items():
                replace = [line.replace(token, value) for line in replace]
            lines = _replace_run(lines, list(change["find"]), replace, what=f"{source_model}, {condition}")

    if levelling_region is not None:
        x, y, width, depth = (_format_template_number(v) for v in levelling_region)
        patch = f" X{x} Y{y} I{width} J{depth}"
        lines = [
            _LEVELLING_PATCH_RE.sub(lambda m: m.group("head") + patch + m.group("tail"), line)
            for line in lines
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

_TPL_IF_RE = re.compile(r"^\s*\{if\s+(?P<cond>.+)\}\s*$")
_TPL_ELSIF_RE = re.compile(r"^\s*\{elsif\s+(?P<cond>.+)\}\s*$")
_TPL_ELSE_RE = re.compile(r"^\s*\{else\}\s*$")
_TPL_ENDIF_RE = re.compile(r"^\s*\{endif\}\s*$")
_TPL_EXPR_RE = re.compile(r"\{(?P<expr>[^{}]*)\}")
_TPL_VAR_RE = re.compile(r"\[(?P<name>[a-z_][a-z0-9_]*)\]")

# Slicing flags the templates branch on
_KILN_SPIRAL_MODE = False
_KILN_PRINT_SEQUENCE = "by layer"

def _bed_center(printer_model: str) -> tuple[float, float] | None:
    """Bed centre ``(x, y)`` in mm for a DECLARED model, or ``None`` if unknown."""
    from kiln.printers.bed_fit import build_volume_exact

    volume = build_volume_exact(_normalize_model(printer_model))
    if volume is None:
        return None
    return volume[0] / 2.0, volume[1] / 2.0


def _no_bed_on_record(printer_model: str | None) -> str:
    """Why this model has no bed centre, as a sentence that names the fix."""
    model = _normalize_model(printer_model) if printer_model else ""
    from kiln.printers.bed_fit import catalogue_printer_id, no_bed_reason

    listed = catalogue_printer_id(model) if model else None
    if listed is None:
        return (
            f"Kiln's catalogue has no row for {model or 'this printer'!r}, so it has no bed "
            "centre to build the file's end sequence around and will not guess one."
        )
    # The same sentence every bed door says, with what this door needs it for.
    return no_bed_reason(listed, instead=None, needed_for="take the centre of, for the file's end sequence")


def _format_template_number(value: float) -> str:
    """Format a resolved number the way BambuStudio writes it."""
    if isinstance(value, bool):  # bool is an int subclass — not a coordinate
        msg = f"Boolean {value!r} used where a number was expected"
        raise ValueError(msg)
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def _eval_template_expr(expr: str, variables: dict[str, Any]) -> Any:
    """Evaluate one BambuStudio template expression."""
    py_expr = expr.replace("&&", " and ").replace("||", " or ")
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
    """Expand a BambuStudio end-gcode template's conditionals and expressions."""
    out: list[str] = []
    stack: list[tuple[bool, bool, bool]] = []

    def emitting() -> bool:
        return all(frame[0] for frame in stack)

    for lineno, line in enumerate(template.split("\n"), 1):
        if_match = _TPL_IF_RE.match(line)
        if if_match:
            parent = emitting()
            active = bool(_eval_template_expr(if_match.group("cond"), variables)) if parent else False
            stack.append((active, parent, active))
            if parent:
                out.append(_indent_of(line))
            continue
        elsif_match = _TPL_ELSIF_RE.match(line)
        if elsif_match:
            if not stack:
                msg = f"Gcode template has {{elsif}} with no {{if}} at line {lineno}"
                raise ValueError(msg)
            _, parent, taken = stack[-1]
            active = (
                bool(_eval_template_expr(elsif_match.group("cond"), variables))
                if parent and not taken
                else False
            )
            stack[-1] = (active, parent, taken or active)
            continue
        if _TPL_ELSE_RE.match(line):
            if not stack:
                msg = f"Gcode template has {{else}} with no {{if}} at line {lineno}"
                raise ValueError(msg)
            _, parent, taken = stack[-1]
            stack[-1] = (parent and not taken, parent, True)
            continue
        if _TPL_ENDIF_RE.match(line):
            if not stack:
                msg = f"Gcode template has {{endif}} with no {{if}} at line {lineno}"
                raise ValueError(msg)
            _, parent, _taken = stack.pop()
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
    """How high this machine's Z goes, from the public catalogue: the firmware's own travel limit where it is on record, else the build volume's height."""
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
        return float(volume[2]) if volume and volume[2] > 0 else None
    return None


def _end_template_variables(max_z: float, printer_model: str | None) -> dict[str, Any]:
    """Everything an end template may read."""
    variables: dict[str, Any] = {
        "max_layer_z": float(max_z),
        "spiral_mode": _KILN_SPIRAL_MODE,
        "print_sequence": _KILN_PRINT_SEQUENCE,
        **_MODEL_END_VALUES.get(_normalize_model(printer_model), {}),
    }
    center = _bed_center(printer_model) if printer_model else None
    if center is not None:
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
    """Resolve an end gcode template with print-specific values."""
    variables = _end_template_variables(max_z, printer_model)
    try:
        expanded = _expand_end_template(template, variables)
    except ValueError as exc:
        if "first_layer_center_no_wipe_tower" in str(exc) and "first_layer_center_no_wipe_tower" not in variables:
            raise ValueError(_no_bed_on_record(printer_model)) from exc
        raise

    safe_z = max_z + 5.0
    if z_top_mm is not None:
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


_Z_ONLY_MOVE_RE = re.compile(rf"^(\s*G[01]\s+)Z({GCODE_NUMBER})(\b.*)$")


def _raise_absolute_z_to_floor(gcode: str, floor_mm: float) -> str:
    """Every absolute ``G0``/``G1`` line that moves only Z, raised to at least *floor_mm*; nothing lowered, nothing else touched."""
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
            if m and not has_axis_word(m.group(3), "XYE"):
                z = float(m.group(2))
                if z < floor_mm:
                    line = f"{m.group(1)}Z{floor_mm:.2f}{m.group(3)}"
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Gcode post-processing
# ---------------------------------------------------------------------------


def _count_layers(gcode_body: str) -> int:
    """The layers the body lays, in either family's spelling."""
    return len(LAYER_CHANGE_LINE.findall(gcode_body))


_GCODE_TOOL_SELECT_RE = re.compile(r"^T(\d+)\b", re.MULTILINE)

_GCODE_FILAMENT_COLOUR_RE = re.compile(
    r"^;\s*filament_colour\s*=\s*(.+)$", re.MULTILINE,
)

_GCODE_FILAMENT_DENSITY_RE = re.compile(
    r"^;\s*filament_density\s*[:=]\s*(.+)$", re.MULTILINE | re.IGNORECASE,
)
_GCODE_FILAMENT_DIAMETER_RE = re.compile(
    r"^;\s*filament_diameter\s*[:=]\s*(.+)$", re.MULTILINE | re.IGNORECASE,
)
_GCODE_ANY_TOOL_SELECT_RE = re.compile(r"^\s*T(\d+)\b", re.MULTILINE)
_FILAMENT_DIAMETER_MM = 1.75


@dataclass(frozen=True)
class FilamentUsage:
    """Filament a G-code body consumes, per extruder index (0-based)."""

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
    """The family's nominal density from Kiln's material table, or PLA's."""
    from kiln.slicer_filament import DEFAULT_MATERIAL, material_density

    row = material_density(filament_type) or material_density(DEFAULT_MATERIAL)
    return row[1] if row else 0.0


def _real_tools_used(gcode_body: str) -> list[int]:
    return sorted(
        {
            int(t)
            for t in _GCODE_ANY_TOOL_SELECT_RE.findall(gcode_body)
            if int(t) < FIRST_PSEUDO_TOOL
        }
    )


def _place_on_used_tools(values: list[float], tools: list[int]) -> list[float]:
    """Spread a slicer's per-extruder comma list onto tray indices."""
    if not values or len(values) != len(tools) or tools == list(range(len(values))):
        return values
    out = [0.0] * (max(tools) + 1)
    for tool, value in zip(tools, values, strict=True):
        out[tool] = value
    return out


def filament_usage_from_gcode(
    gcode_body: str,
    *,
    filament_types: list[str] | None = None,
) -> FilamentUsage:
    """How much filament *gcode_body* consumes, per extruder and in total."""
    tools = _real_tools_used(gcode_body)
    totals = slicer_filament_totals(gcode_body)
    mm = _place_on_used_tools(list(totals.mm), tools) if totals.mm else []
    grams = _place_on_used_tools(list(totals.grams), tools) if totals.grams else []

    if mm and grams and any(g > 0 for g in grams) and len(grams) == len(mm):
        source = "slicer_grams"
    else:
        source = "slicer_length" if mm else "e_moves"
        if not mm:
            mm = extruded_mm_per_tool(gcode_body)
        densities = _number_list(
            (_GCODE_FILAMENT_DENSITY_RE.search(gcode_body) or [None, ""])[1]
        )
        diameters = _number_list(
            (_GCODE_FILAMENT_DIAMETER_RE.search(gcode_body) or [None, ""])[1]
        )
        body_types = slicer_filament_types(gcode_body)

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
    """Write *usage* where the printer's screen reads it, touching nothing else."""
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


def _declared_filaments_in_gcode(
    gcode_body: str,
) -> tuple[int, list[str], list[str]] | None:
    """What the gcode itself says about its filaments, or ``None``."""
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
        """Exactly *count* entries: declared ones kept, the rest filled."""
        if not values:
            return []
        return (values + [filler] * count)[:count]

    colors = _fit(_declared(_GCODE_FILAMENT_COLOUR_RE), "#FFFFFF")
    types = _fit([t or "PLA" for t in slicer_filament_types(gcode_body)], "PLA")
    return count, colors, types


def _find_max_z(gcode_body: str) -> float:
    """The highest layer height the body names (``;Z:`` or ``; Z_HEIGHT:``)."""
    z_heights = LAYER_Z_LINE.findall(gcode_body)
    return max(float(z) for z in z_heights) if z_heights else 10.0


def _postprocess_prusa_body(
    gcode_body: str,
    *,
    total_layers: int,
    est_time_sec: int,
) -> str:
    """Post-process PrusaSlicer gcode body for Bambu firmware compatibility."""
    body_lines = gcode_body.split("\n")

    _skip_prefixes = (
        "M83", "M82", "G21", "G90", "G92", "M107",
        "M104", "M140", "M190", "M109", "G28",
    )
    cleaned: list[str] = []
    in_header = True
    for line in body_lines:
        stripped = line.strip()
        if in_header:
            if stripped.startswith(";BEFORE_LAYER_CHANGE") or LAYER_CHANGE_LINE.match(stripped):
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

    layer_num = 0
    processed: list[str] = []
    for line in cleaned:
        stripped = line.strip()
        if LAYER_CHANGE_LINE.match(stripped):
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
    """Wrap PrusaSlicer ``T`` commands in Bambu M620/M621 AMS load blocks."""
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
                mz = axis_value(code, "Z")
                if mz is not None:
                    last_z = mz
                mx = axis_value(code, "X")
                my = axis_value(code, "Y")
                if mx is not None or my is not None:
                    last_xy = (
                        mx if mx is not None else last_xy[0],
                        my if my is not None else last_xy[1],
                    )
        if stripped.startswith("M620 "):
            saw_m620 = True
            result.append(line)
            continue
        if stripped.startswith("M621 "):
            saw_m620 = False
            result.append(line)
            continue

        m = re.match(r"^T(\d+)$", stripped)
        if m and not saw_m620:
            n = int(m.group(1))
            if 0 <= n < 16:
                flush_temp = min(hotend_temp + 30, 260)
                here_z = last_z if last_z is not None else layer_z
                if lift_floor_mm is not None and here_z is not None:
                    lifted = max(float(lift_floor_mm), here_z + 3.0)
                    result.append(f"G1 Z{lifted:.2f} F600  ; Kiln: lift clear of everything on the plate before the cutter")
                result.append(f"M620 S{n}A   {KILN_COLOUR_CHANGE_NOTE} {n}")
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
    density_str = ",".join(f"{_material_density(t):g}" for t in types)

    return (
        f"; HEADER_BLOCK_START\n"
        f"; BambuStudio 02.05.00.66\n"
        f"; model printing time: {est_h}h {est_m}m {est_s}s; "
        f"total estimated time: {est_h}h {est_m + 5}m 0s\n"
        f"; total layer number: {total_layers}\n"
        f"; filament_density: {density_str}\n"
        f"; filament_diameter: 1.75\n"
        f"; max_z_height: {max_z:.2f}\n"
        f"; filament: {num_filaments}\n"
        f"; HEADER_BLOCK_END\n"
        f"{PACKAGED_BY_KILN}\n"
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
    """Build the ``slice_info.config`` XML for the 3MF."""
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
    """Build the ``plate_1.json`` metadata."""
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

# Minimal 3D model placeholder — a 1 mm cube at origin
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

_PLACEHOLDER_EXTENT_MM = 1.0

# : A model part past this is real geometry and is not parsed to find out
_PLACEHOLDER_MAX_PARSE_BYTES = 64 * 1024

_VERTEX_TAG_RE = re.compile(r"<vertex\b[^>]*>")
_VERTEX_AXIS_RE = re.compile(r'\b([xyz])="([^"]*)"')


def carries_placeholder_model(path: str | os.PathLike[str]) -> bool:
    """Whether the print archive at *path* has no model of its own."""
    try:
        with UntrustedZipFile(path) as zf:
            names = zf.namelist()
            if not any(n.startswith("Metadata/plate_") and n.endswith(".gcode") for n in names):
                return False
            parts = [n for n in names if n.startswith("3D/") and n.endswith(".model")]
            if not parts:
                return True
            if parts != ["3D/3dmodel.model"]:
                return False
            if zf.getinfo("3D/3dmodel.model").file_size > _PLACEHOLDER_MAX_PARSE_BYTES:
                return False
            xml = zf.read("3D/3dmodel.model").decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, KeyError, OSError):
        return False
    lows = [math.inf, math.inf, math.inf]
    highs = [-math.inf, -math.inf, -math.inf]
    for tag in _VERTEX_TAG_RE.findall(xml):
        for axis, raw in _VERTEX_AXIS_RE.findall(tag):
            try:
                value = float(raw)
            except ValueError:
                return False
            i = "xyz".index(axis)
            lows[i] = min(lows[i], value)
            highs[i] = max(highs[i], value)
    if lows[0] == math.inf:
        return True
    return max(h - lo for h, lo in zip(highs, lows, strict=True)) <= _PLACEHOLDER_EXTENT_MM + 1e-6


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
    """The filament colors THIS archive declares, read back from its own JSON."""
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


KILN_PREVIEW_MARKER = "Metadata/kiln_preview.json"

KILN_QUIET_START_MARKER = "Metadata/kiln_quiet_start.json"
_QUIET_PLAN_KEYS = ("clear_z_mm", "lift_floor_mm", "travel_to_mm", "first_layer_z_mm", "planned_for_machine", "plate_fingerprint")


def _check_quiet_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """The plan with every number it must carry, read as numbers; a plan missing one is refused here, before any line is written."""
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
    """The plan's chute as ``(x, y)`` with ``None`` for a kept axis, or ``None`` when the plan names no chute."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    x, y = value
    try:
        return (None if x is None else float(x), None if y is None else float(y))
    except (TypeError, ValueError):
        return None


def _quiet_start_header(plan: dict[str, Any]) -> str:
    """The contract the pre-print gate reads back from the file."""
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
    """Refuse this build's own output if a vendor motion slipped in."""
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
        if seen_end and first_motion is None and upper.startswith(("G0", "G1")) and has_axis_word(upper, "XYZ"):
            first_motion = code
    expected = f"G1 Z{float(plan['clear_z_mm']):.2f}"
    if first_motion is None or not first_motion.upper().startswith(expected.upper()):
        msg = f"quiet-start build's first motion is {first_motion!r}, not the absolute lift {expected!r}; refusing to write it"
        raise ValueError(msg)

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
    """Why *path* must not go to a Bambu printer — empty when it may."""
    p = Path(path)
    if Path(name or p.name).suffix.lower() != ".3mf":
        return []
    try:
        zf = UntrustedZipFile(str(p))
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
        # The tile also shows the print's weight
        if "Metadata/slice_info.config" in names:
            info = zf.read("Metadata/slice_info.config").decode("utf-8", errors="replace")
            if not _slice_info_knows_its_weight(info):
                body = zf.read("Metadata/plate_1.gcode").decode("utf-8", errors="replace")
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
    """Give a sliced Bambu archive the preview family its screen needs."""
    src = Path(path)
    dst = Path(output_path) if output_path else src
    slice_info: str | None = None
    with UntrustedZipFile(str(src)) as zf:
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
    with UntrustedZipFile(str(src)) as src_zf, zipfile.ZipFile(str(tmp), "w", zipfile.ZIP_DEFLATED) as dst_zf:
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
    """Route a model file to the two thumbnail inputs of the wrap functions."""
    if not model_path or not os.path.isfile(model_path):
        return (None, None)
    ext = os.path.splitext(model_path)[1].lower()
    if ext in (".stl", ".obj", ".glb"):
        return ([model_path], None)
    if ext == ".3mf":
        return (None, model_path)
    if ext in (".step", ".stp"):
        try:
            from kiln.step_import import ensure_mesh_path

            # DELIBERATELY drops the conversion record; pinned by
            # test_the_thumbnail_path_deliberately_keeps_no_record.
            return ([ensure_mesh_path(model_path)[0]], None)
        except Exception as exc:  # noqa: BLE001 — no preview beats no print
            logger.info(
                "No LCD thumbnail for %s (%s)",
                os.path.basename(model_path), exc,
            )
    return (None, None)


def _thumbnail_aspect_groups() -> dict[float, list[str]]:
    """The thumbnail paths, grouped by the shape of the image they want."""
    groups: dict[float, list[str]] = {}
    for name, (width, height) in _BAMBU_THUMBNAIL_SPECS.items():
        groups.setdefault(round(width / height, 1), []).append(name)
    return groups


def _fit_to_specs(source: bytes, names: list[str]) -> dict[str, bytes]:
    """*source* scaled to each of *names*' declared sizes."""
    try:
        from PIL import Image
    except ImportError:
        # Pillow absent: ship the render at its native size under every path
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
    """A picture of the part, at the size a thumbnail slot wants."""
    from kiln.multicolor_3mf import render_plate_preview

    return render_plate_preview(
        stl_paths, colors=colors, width=width, height=height,
    )


def _stl_thumbnail_set(
    stl_paths: list[str],
    plate_json: str | None,
) -> dict[str, bytes]:
    """Every BambuStudio thumbnail entry, rendered from *stl_paths*."""
    if not stl_paths:
        return {}
    thumbnails: dict[str, bytes] = {}
    try:
        colors = _declared_filament_colors(plate_json)
        for names in _thumbnail_aspect_groups().values():
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
    """Build a Bambu-compatible 3MF from PrusaSlicer gcode body."""
    if settings is None:
        settings = BambuPrintSettings()
    if quiet_start is not None:
        if resume_mode:
            msg = "a quiet start and a resume are two different prologues; pass one"
            raise ValueError(msg)
        quiet_start = _check_quiet_plan(quiet_start)
        lift_floor_mm = float(quiet_start["lift_floor_mm"])
    elif lift_floor_mm is not None and not resume_mode:
        # A floor means a part is on the plate
        from kiln.plate_state import PRINT_AROUND_SENTENCE

        raise ValueError(PRINT_AROUND_SENTENCE)
    settings = resolve_settings_from_gcode(settings, gcode_body)

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
        msg = "Gcode body has no layer markers (;LAYER_CHANGE or ; CHANGE_LAYER) — cannot build 3MF."
        raise ValueError(msg)

    max_z = _find_max_z(gcode_body)

    printed = slicer_print_time(gcode_body)
    est_time_sec = printed.seconds if printed is not None else 0
    if est_time_sec <= 0:
        # Fallback: estimate from gcode size
        est_time_sec = max(total_layers * 6, len(gcode_body) // 50)

    est_minutes = max(1, est_time_sec // 60)

    logger.info(
        "Building Bambu 3MF: %d layers, max_z=%.1f, est=%dm",
        total_layers,
        max_z,
        est_minutes,
    )

    # Load and resolve templates for the declared model
    start_template, start_source, start_nozzle = _start_gcode_choice(
        printer_model, settings.nozzle_diameter,
    )
    levelling_region = first_layer_region(gcode_body)
    levelling_source = "first_layer"
    if levelling_region is None:
        levelling_region = _whole_bed_region(printer_model or start_source)
        levelling_source = "whole_bed" if levelling_region is not None else "capture"
        if quiet_start is None and not resume_mode:
            logger.warning(
                "Could not read where this print's first layer goes; the start sequence levels %s.",
                "the whole plate" if levelling_region is not None else "the patch it was recorded with",
            )
    start_gcode = _resolve_start_gcode(
        start_template,
        hotend_temp=settings.hotend_temp,
        bed_temp=settings.bed_temp,
        filament_type=settings.filament_type,
        source_model=start_source,
        levelling_region=levelling_region,
        max_z=max_z,
        bed_type=settings.bed_type,
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

    processed_body = _postprocess_prusa_body(
        gcode_body,
        total_layers=total_layers,
        est_time_sec=est_time_sec,
    )

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

    initial_m73 = f"M73 P0 R{est_minutes_with_startup}\n"

    # Assemble complete gcode.
    if quiet_start is not None:
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
        complete_gcode = header + processed_body + "\n" + end_gcode
    else:
        complete_gcode = initial_m73 + header + start_gcode + "\n" + processed_body + "\n" + end_gcode

    # Build metadata.
    gcode_bytes = complete_gcode.encode("utf-8")
    gcode_md5 = hashlib.md5(gcode_bytes).hexdigest()  # noqa: S324

    f_colors = settings.get_filament_colors()
    f_types = settings.get_filament_types()

    # Include startup overhead in the prediction shown on the printer display
    est_time_sec_with_startup = est_time_sec + _BAMBU_STARTUP_OVERHEAD_SEC

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

    thumbnails: dict[str, bytes] = {}
    model_parts: dict[str, bytes] = {}
    model_data: str = _MINIMAL_3D_MODEL
    if source_3mf_path and os.path.isfile(source_3mf_path):
        try:
            with UntrustedZipFile(source_3mf_path) as zf:
                for name in zf.namelist():
                    if name.endswith(".png") and (
                        name.startswith("Metadata/")
                        or name.startswith("Auxiliaries/.thumbnails/")
                    ):
                        thumbnails[name] = zf.read(name)
                    elif name == "3D/3dmodel.model":
                        model_data = zf.read(name).decode("utf-8")
                    elif name.startswith("3D/") and name.endswith(".model"):
                        # Copy every sibling part, referenced or not
                        model_parts[name] = zf.read(name)
        except (zipfile.BadZipFile, KeyError):
            logger.warning(
                "Could not extract thumbnails from %s", source_3mf_path
            )

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
        # Sub-parts the root model reaches through p:path
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
        levelling_region=levelling_region,
        levelling_source=levelling_source,
        bed_type=str(settings.bed_type).strip().lower(),
    )


def repackage_gcode_as_bambu_3mf(
    gcode_path: str,
    output_path: str,
    *,
    source_3mf_path: str | None = None,
    stl_paths: list[str] | None = None,
    estimated_time_minutes: int = 0,
) -> str:
    """Wrap already-Bambu gcode in a minimal 3MF container."""
    abs_path = os.path.abspath(gcode_path)
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(f"Gcode file not found: {abs_path}")

    gcode_bytes = Path(abs_path).read_bytes()
    gcode_md5 = hashlib.md5(gcode_bytes).hexdigest()  # noqa: S324

    thumbnails: dict[str, bytes] = {}
    plate_json: str | None = None
    slice_info: str | None = None
    if source_3mf_path and os.path.isfile(source_3mf_path):
        try:
            with UntrustedZipFile(source_3mf_path) as zf:
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

    rendered_here = False
    if not thumbnails and stl_paths:
        thumbnails = _stl_thumbnail_set(stl_paths, plate_json)
        rendered_here = bool(thumbnails)

    if slice_info and estimated_time_minutes > 0:
        prediction_sec = estimated_time_minutes * 60 + _BAMBU_STARTUP_OVERHEAD_SEC
        slice_info = re.sub(
            r'(<metadata\s+key="prediction"\s+value=")(\d+)(")',
            rf"\g<1>{prediction_sec}\3",
            slice_info,
        )
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
