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

Where the templates come from, and why start and end differ
-----------------------------------------------------------
BambuStudio ships its start/end G-code per model as template files in
``profiles/BBL/machine/"Bambu Lab <MODEL> <NOZZLE> nozzle template
machine_{start,end}_gcode.json"``.  Those templates are UNRESOLVED: they
carry BambuStudio's own expression language --- ``[bed_temperature_initial_layer_single]``,
``{nozzle_temperature_initial_layer[initial_extruder]}``, and
``{if ...}{else}{endif}`` blocks.

``bambu_a1_start_gcode.gcode`` and ``bambu_a1_end_gcode.gcode`` are NOT those
templates.  They are post-expansion captures taken from a real BambuStudio
slice of the same bundle version (both carry the bundle's own
``;===== date:`` stamps), which is why they hold literal values and no
placeholders, and why they are 20-odd lines longer than the templates: they
also contain the slicer's injected preamble (``M201``/``M203`` machine limits,
``M73`` progress) and postamble (``; MACHINE_END_GCODE_END``, spaghetti
detector).  They are the A1-proven artifacts and are left exactly as they are.

That provenance is what splits start from end here:

* **End G-code is per model.**  Across every model's end template the only
  variable is ``max_layer_z`` --- a value this module already computes from
  the G-code body --- plus, on A1 and A1 mini, the bed centre and two
  slicing flags that are constants for Kiln.  Nothing has to be guessed, so
  each supported model ships its own end template and
  :func:`_resolve_end_gcode` expands it at build time.
* **Start G-code is still A1-flavoured for every model.**  The start
  templates need ~100 further values that BambuStudio *computes during
  slicing* and stores nowhere --- ``outer_wall_volumetric_speed``,
  ``flush_temperatures``, ``min_vitrification_temperature``,
  ``hold_chamber_temp_for_flat_print``, ``first_layer_print_min`` --- and
  several of them gate real motion and heating (a bed-obstacle probe height,
  which bed-levelling branch runs, purge feedrates).  Resolving those by hand
  would mean guessing numbers that are sent to a machine that moves, so this
  module does not.  A declared non-A1 model therefore still gets the A1 start
  sequence and :func:`_select_start_gcode` says so in the log.

Nozzle size does not enter into this, and that is the bundle's own doing: it
publishes end G-code at the 0.4 nozzle only — one file per model, no per-nozzle
variant — so the end template a 0.6 owner gets is the only one that exists
rather than a 0.4 file standing in for theirs.  Start G-code is the opposite:
P1P, P1S, X1 Carbon and X1E each publish four start templates (0.2 / 0.4 / 0.6
/ 0.8) whose contents genuinely differ, and ``[nozzle_diameter]`` appears in
their conditionals.  So whenever per-model start templates do become
resolvable, ``_MODEL_START_GCODE_FILES`` has to be keyed on nozzle diameter as
well as model, or refuse a nozzle it has no template for — never silently hand
a 0.6 nozzle the 0.4 sequence.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import re
import zipfile
from dataclasses import dataclass
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

# Start G-code.  One entry, and that is the honest state of it: see the module
# docstring for why the other models' start templates cannot be resolved.
_MODEL_START_GCODE_FILES: dict[str, str] = {
    "bambu_a1": "bambu_a1_start_gcode.gcode",
}

# End G-code, one file per model we ship a template for.
_MODEL_END_GCODE_FILES: dict[str, str] = {
    "bambu_a1": "bambu_a1_end_gcode.gcode",
    "bambu_a1_mini": "bambu_a1_mini_end_gcode.gcode",
    "bambu_p1p": "bambu_p1p_end_gcode.gcode",
    "bambu_p1s": "bambu_p1s_end_gcode.gcode",
    "bambu_p2s": "bambu_p2s_end_gcode.gcode",
    "bambu_x1c": "bambu_x1c_end_gcode.gcode",
    "bambu_x1e": "bambu_x1e_end_gcode.gcode",
    "bambu_h2s": "bambu_h2s_end_gcode.gcode",
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

    All temperatures are in degrees Celsius.  Defaults are for PLA on
    the Bambu A1 with a 0.4 mm nozzle.

    For multi-color prints, set ``num_filaments`` > 1 and provide
    ``filament_colors`` / ``filament_types`` lists with that many entries.
    """

    hotend_temp: int = 220
    bed_temp: int = 65
    filament_type: str = "PLA"
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
        return [self.filament_type] * self.num_filaments

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_path": self.output_path,
            "total_layers": self.total_layers,
            "max_z": self.max_z,
            "file_size": self.file_size,
            "md5": self.md5,
            "est_print_time_sec": self.est_print_time_sec,
        }


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
    """Load the A1 end gcode template."""
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


def _select_start_gcode(printer_model: str | None) -> tuple[str, str]:
    """Pick the start gcode template for a DECLARED printer model.

    Returns ``(template_text, source_model)``.  ``source_model`` is the model
    the template actually came from, which is what the caller should believe
    the file is flavoured for --- it is not always the model asked for.

    Every model except the A1 falls back to the A1 sequence today.  That is
    logged once per model per process rather than passed over in silence: a
    P2S owner wrapping a print in A1 initialization is a real defect, and the
    session that first hit it lost time because nothing said so.
    """
    model = _normalize_model(printer_model)
    filename = _MODEL_START_GCODE_FILES.get(model)
    if filename is not None:
        return _load_model_template(filename), model

    if model and ("start", model) not in _fallback_warned:
        _fallback_warned.add(("start", model))
        logger.warning(
            "No start gcode template for %s — using the Bambu A1 initialization "
            "sequence, which is flavoured for the A1 (bed coordinates, purge and "
            "calibration moves).  The print will be wrapped and can start, but the "
            "startup sequence is not this model's own.",
            model,
        )
    return _load_a1_start_gcode(), "bambu_a1"


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
    return _load_a1_end_gcode(), "bambu_a1"


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


def _resolve_start_gcode(
    template: str,
    *,
    hotend_temp: int = 220,
    bed_temp: int = 65,
    filament_type: str = "PLA",
) -> str:
    """Resolve the A1 start gcode template with print-specific values.

    Replaces PLA-default temperatures (220°C hotend, 65°C bed) and
    filament type with the actual print values.  Fixed init temperatures
    (140°C preheat, 250°C flush, 170°C wipe) are preserved.
    """
    lines = template.split("\n")
    resolved: list[str] = []

    for line in lines:
        stripped = line.strip()

        # Replace hotend temp: M104/M109 S220 → S{hotend_temp}
        if hotend_temp != 220 and (
            stripped.startswith("M104 S220") or stripped.startswith("M109 S220")
        ):
            line = line.replace("S220", f"S{hotend_temp}")

        # Replace bed temp: M140/M190 S65 → S{bed_temp}
        elif bed_temp != 65 and (
            stripped.startswith("M140 S65") or stripped.startswith("M190 S65")
        ):
            line = line.replace("S65", f"S{bed_temp}")

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
    ``&&`` / ``||`` / ``!``, string equality, and indexing a known list.  A
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


def _expand_end_template(template: str, variables: dict[str, Any]) -> str:
    """Expand a BambuStudio end-gcode template's conditionals and expressions.

    Whitespace follows BambuStudio's own output, which the A1 ground-truth
    diff pins exactly: an ``{if}`` or ``{endif}`` guard becomes a blank line,
    an ``{else}`` and every line of the branch not taken disappear, and the
    branch that is taken keeps its original indentation.

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
                out.append("")
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
                out.append("")
            continue
        if not emitting():
            continue
        out.append(
            _TPL_EXPR_RE.sub(
                lambda m: _format_template_number(
                    _eval_template_expr(m.group("expr"), variables)
                ),
                line,
            )
        )

    if stack:
        msg = f"Gcode template has {len(stack)} unclosed {{if}} block(s)"
        raise ValueError(msg)
    return "\n".join(out)


def _resolve_end_gcode(
    template: str,
    *,
    max_z: float = 65.0,
    printer_model: str | None = None,
) -> str:
    """Resolve an end gcode template with print-specific values.

    Two steps, in this order:

    1. Expand BambuStudio's template syntax against the real print height.
       A pre-expanded template (the proven A1 capture) passes through
       untouched.
    2. Adjust the safe Z-move height.  The first ``G1 Z... F900`` command is
       the safe-move after the last layer — it needs to clear the print.
       Kiln lifts ``max_z + 5.0`` where Bambu's own template asks for
       ``max_layer_z + 0.5``; the larger clearance is the A1-proven behaviour
       and is applied to every model so there is one rule, not eight.

    :param printer_model: Declared model, used only to look up the bed centre
        for templates that park on it.
    """
    variables: dict[str, Any] = {
        "max_layer_z": float(max_z),
        "spiral_mode": _KILN_SPIRAL_MODE,
        "print_sequence": _KILN_PRINT_SEQUENCE,
    }
    center = _bed_center(printer_model) if printer_model else None
    if center is not None:
        # BambuStudio indexes this as a point; the templates only read [1].
        variables["first_layer_center_no_wipe_tower"] = [center[0], center[1]]

    expanded = _expand_end_template(template, variables)

    safe_z = max_z + 5.0
    return re.sub(
        r"(G1 Z)\d+\.?\d*( F900)",
        rf"\g<1>{safe_z:.1f}\2",
        expanded,
        count=1,
    )


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
    hotend_temp: int = 220,
    filament_type: str = "PLA",
) -> str:
    """Wrap PrusaSlicer ``T`` commands in Bambu M620/M621 AMS load blocks.

    PrusaSlicer multi-material gcode uses bare ``T0``, ``T1``, etc. to
    switch tools.  Bambu firmware requires these to be wrapped in
    ``M620 S{n}A`` / ``M621 S{n}A`` blocks for the AMS to load the
    correct filament.

    Only wraps T0–T15 (real extruder indices).  Leaves T255 (retract)
    and T1000 (virtual tool) untouched.
    """
    lines = gcode.split("\n")
    result: list[str] = []
    # Track the initial T0 from start gcode — don't double-wrap it
    saw_m620 = False

    for line in lines:
        stripped = line.strip()
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
                result.append(f"M620 S{n}A   ; AMS switch to filament {n}")
                result.append("    M1002 gcode_claim_action : 4")
                result.append("    M400")
                result.append("    M1002 set_filament_type:UNKNOWN")
                result.append(f"    M109 S{hotend_temp}")
                result.append(f"    M104 S{flush_temp}")
                result.append("    M400")
                result.append(f"    T{n}")
                result.append("    G1 X-48.2 F3000")
                result.append("    M400")
                result.append(f"    M620.1 E F299.339 T{flush_temp}")
                result.append(f"    M109 S{flush_temp}")
                result.append("    M106 P1 S0")
                result.append("    G92 E0")
                result.append("    G1 E50 F200")
                result.append("    M400")
                result.append(f"    M1002 set_filament_type:{filament_type}")
                result.append(f"M621 S{n}A")
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
) -> str:
    """Build the ``slice_info.config`` XML for the 3MF.

    Supports multi-filament: set ``num_filaments`` > 1 and provide
    ``filament_colors`` / ``filament_types`` lists.
    """
    colors = filament_colors or [filament_color] * num_filaments
    types = filament_types or [filament_type] * num_filaments

    # Build filament entries
    filament_entries: list[str] = []
    for i in range(num_filaments):
        ftype = types[i] if i < len(types) else filament_type
        fcolor = colors[i] if i < len(colors) else filament_color
        filament_entries.append(
            f'    <filament id="{i + 1}" tray_info_idx="GFL99" type="{ftype}" '
            f'color="{fcolor}" used_m="0.00" used_g="0.00" '
            f'used_for_object="true" used_for_support="false" group_id="0" '
            f'nozzle_diameter="{nozzle_diameter:.2f}" volume_type="Standard"/>'
        )

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
        '    <metadata key="weight" value="0.00"/>\n'
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
    :returns: :class:`Bambu3MFResult` with output path and metadata.
    :raises FileNotFoundError: If the start/end gcode data files are missing.
    :raises ValueError: If the gcode body has no layer changes, or if a
        template could not be fully resolved.
    """
    if settings is None:
        settings = BambuPrintSettings()

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
    start_template, start_source = _select_start_gcode(printer_model)
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
    )
    _assert_fully_resolved(end_gcode, source=f"{end_source} end gcode")

    # Correct M73 R values in the start gcode template.  The template has
    # hardcoded R186/R184/R183/R179 from a BambuStudio default (~186 min).
    # Scale them proportionally to our corrected estimate so the LCD is
    # accurate throughout the startup sequence too.
    _DEFAULT_TEMPLATE_MINUTES = 186
    def _scale_start_m73(match: re.Match) -> str:
        p = int(match.group(1))
        old_r = int(match.group(2))
        new_r = max(1, round(old_r * est_minutes_with_startup / _DEFAULT_TEMPLATE_MINUTES))
        return f"M73 P{p} R{new_r}"
    est_minutes_with_startup = max(1, (est_time_sec + _BAMBU_STARTUP_OVERHEAD_SEC) // 60)
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
            hotend_temp=settings.hotend_temp,
            filament_type=settings.filament_type,
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
    if resume_mode:
        # Resume-mode: suppress Bambu's proprietary start sequence and initial
        # M73.  The resume gcode body carries its own safety preamble (heat →
        # Z+5 lift → G28 X Y only → travel Z → optional filament prime →
        # descend to resume Z).  Running Bambu's start-gcode on a bed with a
        # partial print would re-home Z (nozzle collision risk), re-probe bed
        # (impossible with print on it), and waste ~18 min on AMS load +
        # calibration before the resume preamble ever executes.
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

    # If no thumbnails were extracted and STL paths are available,
    # generate a thumbnail via OpenSCAD (best-effort), resized to
    # BambuStudio's expected dimensions per path.
    if not thumbnails and stl_paths:
        try:
            from kiln.multicolor_3mf import _generate_thumbnail
            thumb_data = _generate_thumbnail(stl_paths)
            if thumb_data:
                _thumb_specs: dict[str, tuple[int, int]] = {
                    "Metadata/plate_1.png": (512, 512),
                    "Metadata/plate_1_small.png": (128, 128),
                    "Metadata/top_1.png": (512, 512),
                    "Metadata/pick_1.png": (512, 512),
                    "Auxiliaries/.thumbnails/thumbnail_3mf.png": (240, 180),
                    "Auxiliaries/.thumbnails/thumbnail_middle.png": (680, 510),
                    "Auxiliaries/.thumbnails/thumbnail_small.png": (251, 188),
                }
                try:
                    from io import BytesIO

                    from PIL import Image

                    src_img = Image.open(BytesIO(thumb_data))
                    for name, (tw, th) in _thumb_specs.items():
                        resized = src_img.resize((tw, th), Image.LANCZOS)
                        buf = BytesIO()
                        resized.save(buf, format="PNG")
                        thumbnails[name] = buf.getvalue()
                except ImportError:
                    # Pillow not available — use raw data for all paths
                    for name in _thumb_specs:
                        thumbnails[name] = thumb_data
        except Exception:
            pass

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
        for name, data in thumbnails.items():
            zf.writestr(name, data)

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
      2. Generated from ``stl_paths`` via OpenSCAD when no source
         thumbnails were found.  Without this fallback the printer's
         LCD shows a blank preview for freshly-sliced parts.

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

    # Fallback: generate thumbnails from STL via OpenSCAD so the Bambu
    # LCD shows the actual part rather than a blank square.  Mirrors
    # the logic in :func:`build_bambu_3mf`.
    if not thumbnails and stl_paths:
        try:
            from kiln.multicolor_3mf import _generate_thumbnail
            thumb_data = _generate_thumbnail(stl_paths)
            if thumb_data:
                _thumb_specs: dict[str, tuple[int, int]] = {
                    "Metadata/plate_1.png": (512, 512),
                    "Metadata/plate_1_small.png": (128, 128),
                    "Metadata/top_1.png": (512, 512),
                    "Metadata/pick_1.png": (512, 512),
                    "Auxiliaries/.thumbnails/thumbnail_3mf.png": (240, 180),
                    "Auxiliaries/.thumbnails/thumbnail_middle.png": (680, 510),
                    "Auxiliaries/.thumbnails/thumbnail_small.png": (251, 188),
                }
                try:
                    from io import BytesIO

                    from PIL import Image

                    src_img = Image.open(BytesIO(thumb_data))
                    for name, (tw, th) in _thumb_specs.items():
                        resized = src_img.resize((tw, th), Image.LANCZOS)
                        buf = BytesIO()
                        resized.save(buf, format="PNG")
                        thumbnails[name] = buf.getvalue()
                except ImportError:
                    for name in _thumb_specs:
                        thumbnails[name] = thumb_data
        except Exception:
            logger.warning("STL thumbnail generation failed", exc_info=True)

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
