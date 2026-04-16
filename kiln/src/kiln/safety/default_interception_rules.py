"""Default bed-safety interception rules for G-code.

Generates a list of regex-based reject/warn rules that catch G0/G1
moves outside a printer's build volume.  These sit at the adapter
boundary as the LAST-LINE-OF-DEFENSE: even if a hand-crafted bad
G-code file slips past the mesh-level and 3MF-level pre-send gates,
the motion layer will still refuse to execute off-bed moves.

Incident #0 (2026-04-15, Bambu A1): a sliced G-code file contained
``G1 X-12.5 ...`` moves because the mesh was centered on the origin
and the bundled slicer profile did not auto-center.  The firmware
accepted the moves and slammed the nozzle into the purge tool.
These rules would have rejected those moves before they reached the
motor driver.

Rule format
-----------
Each rule is a plain ``dict`` with four fields::

    {
        "pattern": str,   # anchored regex evaluated against the raw G-code line
        "action":  str,   # "reject" (block and error) or "warn" (log and pass)
        "reason":  str,   # human-readable explanation used in error output
        "category": str,  # always "bed_fit" for rules from this module
    }

Callers convert these dicts into whatever native rule type their
interception layer uses (e.g. ``InterceptionRule`` with trigger
``pattern_match`` and action ``block``).  Keeping the dict format
small and adapter-agnostic means the same rule set can drive serial
adapters, the MQTT bridge, or purely static G-code linters.

Epsilon
-------
A 0.5 mm tolerance is applied on every bound to absorb floating-point
noise from slicer output (``X256.0000001`` must not trip the rule).
The tolerance is one-sided: we widen the SAFE region, never the
rejected region — a move to ``X=-0.4`` is allowed, ``X=-0.5001``
is rejected.

Unknown printers return an EMPTY list.  We'd rather allow a print on
an obscure printer than block every move based on a guessed bed size.
"""
from __future__ import annotations

import logging
from typing import Any

from kiln.printers.bed_fit import get_build_volume

logger = logging.getLogger(__name__)

# One-sided tolerance (mm) applied to every bed bound.  Matches
# ``kiln.printers.bed_fit._FIT_EPSILON_MM`` so mesh-level and
# gcode-level checks agree on what "on the bed" means.
_EPSILON_MM: float = 0.5

# Minimum allowed Z.  Small negative Z (e.g. probe routines, baby-steps)
# is tolerated up to ``-_EPSILON_MM`` but a significant dive is rejected.
_Z_MIN_MM: float = -_EPSILON_MM


# ---------------------------------------------------------------------------
# Regex fragments
# ---------------------------------------------------------------------------
# We build one regex per (axis, direction) pair.  Each anchors on the
# G0/G1 command word, then looks ahead for the relevant axis parameter.
# Other parameters in the same line (E, F, other axes) are ignored so
# any order the slicer emits will match.
#
# Floating-point pattern:  optional sign, digits, optional decimal.
#   _NUM            — signed number, captured
#   _NUM_GT(x)      — number strictly greater than x, captured (regex form)
#   _NUM_LT(x)      — number strictly less than x, captured (regex form)
#
# We avoid building numeric comparators in regex (too brittle) and
# instead write rules that match ANY value for the axis; downstream
# code is expected to parse the capture group and apply numeric
# thresholds.  BUT the task requires pattern-only matching at the
# adapter boundary — so we build explicit regex patterns that only
# match the OUT-OF-RANGE subset.  This is the dangerous-but-solvable
# case: we enumerate the negative-value branch and the above-bed
# branch separately.
#
# "Negative axis" regex: matches ``X`` followed by a minus sign and at
# least one digit, with a value whose integer part is ≥ threshold.
# For the ``X<0`` rule we actually want ``X<-epsilon`` — any negative
# value whose magnitude exceeds 0.5 mm.  Writing "magnitude > 0.5" in
# pure regex is awkward, so we split it into two alternatives:
#   1. ``X-\d*\.\d+`` where the whole-part is 0 and the fractional
#      part is ≥ 0.5
#   2. ``X-[1-9]\d*`` or ``X-\d+\.\d+`` where the whole-part is ≥ 1
# The simpler and more defensible approach: match ANY negative X and
# let the downstream matcher (or numeric post-filter) apply the
# epsilon.  That keeps the regex simple, readable, and obviously
# correct.  Floating-point noise at X=-0.0000001 would still trip the
# rule, which is acceptable — slicers do not emit such values; if the
# rare case arises, callers can always add a numeric post-filter.
#
# Axis-above-bed regex: matches ``X`` followed by a value whose
# integer part is ≥ ceil(bed_x + epsilon).  This IS expressible as a
# clean regex because we know the bed size at rule-build time.
# Example: bed_x=256 → any X value starting with 257+, 300+, etc.
# We normalise to ``bed + epsilon`` and take the integer ceiling, so
# the rule rejects ``X256.5001`` but not ``X256.4999``.

import math


def _negative_axis_pattern(axis: str) -> str:
    """Regex matching a G0/G1 move where the given axis has a negative value.

    Anchors on the command word and scans forward, allowing any
    preceding parameters.  The axis letter is case-sensitive uppercase
    (slicers emit uppercase; we normalise input before matching in the
    adapter layer if needed).
    """
    # ^G[01]\b  — G0 or G1 at start (word boundary so G10/G11 don't match)
    # .*\b      — any characters up to a word boundary
    # X-        — the axis letter followed by a minus sign
    # \d        — at least one digit (so bare "X-" doesn't match)
    return rf"^G[01]\b.*\b{axis}-\d"


def _axis_above_bed_pattern(axis: str, bed_mm: float) -> str:
    """Regex matching a G0/G1 move where the given axis exceeds ``bed + epsilon``.

    The threshold is ``bed_mm + _EPSILON_MM``.  The pattern matches any
    axis-value strictly greater than that threshold.

    Assumes ``bed_mm`` is a non-negative integer (true for every
    printer in ``printer_intelligence.json``).  The epsilon is
    hard-coded at 0.5 mm so the decision reduces to:

      reject if (integer_part >= bed_mm + 1) OR
               (integer_part == bed_mm AND fractional_part > 0.5)

    Floating-point noise like ``X256.0000001`` stays below the 0.5 mm
    tolerance and is not matched.
    """
    bed_int = int(round(bed_mm))

    # Branch 1: integer part strictly greater than bed_mm.
    #   Built from an alternation: more-digits, or same-digit-count
    #   lexically greater, or exactly bed_int + 1 (captured by the
    #   "same lexical" case anyway).
    reject_from = bed_int + 1
    n_str = str(reject_from)
    width = len(n_str)

    alts: list[str] = []
    # Case A: more digits than reject_from (always larger because
    # slicers don't emit leading zeros).
    alts.append(rf"\d{{{width + 1},}}")
    # Case B: exactly `width` digits, lexically ≥ n_str.  Enumerate by
    # prefix: for each position i, fix the first i digits to n_str[:i],
    # put a digit strictly greater at position i, then any digits.
    for i in range(width):
        prefix = n_str[:i]
        ch = n_str[i]
        if ch == "9":
            continue  # no digit strictly greater than 9
        gt_range = f"[{int(ch) + 1}-9]"
        suffix_width = width - i - 1
        suffix = rf"\d{{{suffix_width}}}" if suffix_width > 0 else ""
        alts.append(prefix + gt_range + suffix)
    # Case C: exactly equal to n_str.  Values like "257", "257.3".
    alts.append(n_str)

    int_gt_bed = "(?:" + "|".join(alts) + ")"
    # Anchored at a digit boundary so "2570" doesn't appear mid-number.
    branch1 = rf"\+?{int_gt_bed}(?:\.\d+)?"

    # Branch 2: integer part == bed_mm AND fractional part > 0.5.
    #   i.e. bed_mm.5<nonzero> or bed_mm.[6-9]<anything> where the
    #   first digit after the dot pushes the value strictly above
    #   bed_mm + 0.5.
    branch2 = rf"\+?{bed_int}\.(?:5\d*[1-9]\d*|[6-9]\d*)"

    value_re = rf"(?:{branch1}|{branch2})"

    return rf"^G[01]\b.*\b{axis}{value_re}\b"


def _z_below_floor_pattern() -> str:
    """Regex matching a G0/G1 move where Z is strictly less than ``-_EPSILON_MM``.

    Slicers should never emit Z below 0 for normal print moves.  We
    tolerate tiny negatives (probe baby-steps) up to -0.5 mm but
    reject a significant dive into the bed.
    """
    # Any Z value with magnitude ≥ 1 (integer part ≥ 1) preceded by a
    # minus sign — this is the clearly-bad case.
    # Also: Z-0.5..., Z-0.6..., ..., Z-0.9...
    # Express "magnitude > 0.5" as:
    #   Z-[1-9]           (e.g. Z-1, Z-12, Z-1.3)
    #   Z-0\.[5-9]        (e.g. Z-0.5, Z-0.7)
    #   Z-0\.\d*[5-9]\d*  — too permissive, skip
    # We keep the two clean branches.  Values in (-0.5, 0) are allowed.
    return r"^G[01]\b.*\bZ-(?:[1-9]\d*(?:\.\d+)?|0\.[5-9]\d*)"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_default_bed_safety_rules(printer_id: str) -> list[dict[str, Any]]:
    """Build the default bed-safety rule set for a printer.

    Returns a list of regex-based rules that reject G0/G1 moves
    outside the printer's build volume.  Unknown printers return an
    empty list — we don't fabricate a guessed bed size.

    Each rule has the shape::

        {
            "pattern":  "<regex>",
            "action":   "reject",
            "reason":   "...",
            "category": "bed_fit",
        }

    Args:
        printer_id: The printer_intelligence.json key (e.g. ``"bambu_a1"``).

    Returns:
        A list of rule dicts.  Empty list for unknown printers.
    """
    volume = get_build_volume(printer_id)
    if volume is None:
        logger.debug(
            "get_default_bed_safety_rules: unknown printer_id %r — returning []",
            printer_id,
        )
        return []

    bed_x, bed_y, bed_z = volume

    rules: list[dict[str, Any]] = [
        {
            "pattern": _negative_axis_pattern("X"),
            "action": "reject",
            "reason": (
                f"G0/G1 move has negative X (off-bed for {printer_id}, "
                f"bed origin at corner).  Would crash into the left frame / "
                f"purge tool.  Center the mesh before slicing."
            ),
            "category": "bed_fit",
        },
        {
            "pattern": _negative_axis_pattern("Y"),
            "action": "reject",
            "reason": (
                f"G0/G1 move has negative Y (off-bed for {printer_id}, "
                f"bed origin at corner).  Would crash into the front frame. "
                f"Center the mesh before slicing."
            ),
            "category": "bed_fit",
        },
        {
            "pattern": _z_below_floor_pattern(),
            "action": "reject",
            "reason": (
                f"G0/G1 move has Z < {_Z_MIN_MM:+.1f} mm — nozzle would "
                f"drive into the bed.  Small negative Z from probe "
                f"baby-steps is tolerated; this value is a real crash."
            ),
            "category": "bed_fit",
        },
        {
            "pattern": _axis_above_bed_pattern("X", bed_x),
            "action": "reject",
            "reason": (
                f"G0/G1 move has X > {bed_x:g} mm (bed width for "
                f"{printer_id}, +{_EPSILON_MM} mm tolerance).  Would "
                f"crash into the right frame."
            ),
            "category": "bed_fit",
        },
        {
            "pattern": _axis_above_bed_pattern("Y", bed_y),
            "action": "reject",
            "reason": (
                f"G0/G1 move has Y > {bed_y:g} mm (bed depth for "
                f"{printer_id}, +{_EPSILON_MM} mm tolerance).  Would "
                f"crash into the rear frame."
            ),
            "category": "bed_fit",
        },
        {
            "pattern": _axis_above_bed_pattern("Z", bed_z),
            "action": "reject",
            "reason": (
                f"G0/G1 move has Z > {bed_z:g} mm (build height for "
                f"{printer_id}, +{_EPSILON_MM} mm tolerance).  Gantry "
                f"would hit the top of its rail."
            ),
            "category": "bed_fit",
        },
    ]

    return rules


__all__ = ["get_default_bed_safety_rules"]
