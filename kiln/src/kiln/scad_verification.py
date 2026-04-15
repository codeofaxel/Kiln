"""Pre-print SCAD verification — catch text-orientation bugs before they
burn hours of print time.

The original Fig-the-dog jewelry tray session surfaced a whole class
of bugs where text() engraved on an exterior bottom face prints
reversed (rubber-stamp orientation) because the author forgot a
``mirror([1, 0, 0])`` transform.  Kiln now ships the fix in every
product generator, but user-authored SCAD and third-party decoration
paths can still produce the same bug.

This module provides a fast, dependency-free static analyzer that
inspects SCAD source for known failure modes and returns structured
warnings / errors so the caller can abort the print BEFORE sending
gcode to a 3D printer.

Usage::

    from kiln.scad_verification import verify_flip_readability

    report = verify_flip_readability("/path/to/model.scad")
    if not report["ok"]:
        for issue in report["issues"]:
            print(f"[{issue['severity']}] {issue['message']}")
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)


# Regex patterns for the SCAD fragments we care about.  These are
# deliberately loose — a strict parser would be nicer but Python's
# regex is enough to catch the specific bugs the session surfaced.

# translate([..., ..., Z]) where Z is negative or <= 0 (bottom-face marker)
_TRANSLATE_BOTTOM_RE = re.compile(
    r"translate\(\s*\[\s*[-\d.eE+]+\s*,\s*[-\d.eE+]+\s*,\s*(-?[\d.eE+]+)\s*\]\s*\)"
)

# text("...", size=...) — the content we want to be flip-readable
_TEXT_CALL_RE = re.compile(
    r'text\(\s*"((?:[^"\\]|\\.)*)"\s*,[^)]*?size\s*=\s*([\d.]+)',
    re.MULTILINE,
)

# mirror([1, 0, 0]) — the fix for bottom-face text
_MIRROR_X_RE = re.compile(r"mirror\(\s*\[\s*1\s*,\s*0\s*,\s*0\s*\]\s*\)")

# rotate([180, 0, 0]) — alternative fix (flips Y and Z; works for
# top/bottom symmetry but NOT for bottom-face engravings that need
# to read when flipped around Y axis)
_ROTATE_180X_RE = re.compile(r"rotate\(\s*\[\s*180\s*,\s*0\s*,\s*0\s*\]\s*\)")

# linear_extrude(height=H) — gives us the text's physical depth
_LINEAR_EXTRUDE_RE = re.compile(r"linear_extrude\(\s*height\s*=\s*([\d.]+)")

# Proven depth floor for FDM PLA engraving (see Ash coaster lessons).
# Anything below this gets partially filled by first-layer squish and
# reads faint under normal indoor light.
_FDM_MIN_ENGRAVING_DEPTH_MM = 1.0
_FDM_RECOMMENDED_DEPTH_MM = 1.2


@dataclass
class Issue:
    """A single verification finding."""

    severity: str  # "warning" | "error"
    code: str
    message: str
    line_number: int | None = None
    fragment: str = ""
    suggested_fix: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "line_number": self.line_number,
            "fragment": self.fragment,
            "suggested_fix": self.suggested_fix,
        }


@dataclass
class FlipReadabilityReport:
    ok: bool = True
    issues: list[Issue] = field(default_factory=list)
    text_entries_checked: int = 0
    scad_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "issues": [i.to_dict() for i in self.issues],
            "text_entries_checked": self.text_entries_checked,
            "scad_path": self.scad_path,
            "errors": [i.to_dict() for i in self.issues if i.severity == "error"],
            "warnings": [i.to_dict() for i in self.issues if i.severity == "warning"],
        }


def _line_number_of(source: str, index: int) -> int:
    """1-based line number for a byte index into *source*."""
    return source.count("\n", 0, index) + 1


def _nearest_surrounding_block(source: str, text_match_start: int) -> str:
    """Return the ~5 lines preceding the text() call so we can check
    whether it lives inside a mirror/rotate transform scope.
    """
    line_starts = [0]
    for m in re.finditer(r"\n", source):
        line_starts.append(m.end())
    line = _line_number_of(source, text_match_start) - 1
    start = line_starts[max(0, line - 5)]
    end = min(len(source), text_match_start + 1)
    return source[start:end]


def verify_flip_readability(scad_path: str) -> dict[str, Any]:
    """Analyze a SCAD file for flip-readability and FDM-depth issues.

    Fast, dependency-free.  Does NOT execute OpenSCAD or render anything
    — pure string analysis of the source.  Safe to call synchronously
    before ``start_print``.

    :param scad_path: Absolute path to the ``.scad`` file to inspect.
    :returns: Report dict (see :class:`FlipReadabilityReport`) with
        ``ok`` (bool), ``issues`` (list), and per-severity buckets.
    """
    report = FlipReadabilityReport(scad_path=os.path.abspath(scad_path))

    if not os.path.isfile(scad_path):
        report.ok = False
        report.issues.append(
            Issue(
                severity="error",
                code="SCAD_NOT_FOUND",
                message=f"SCAD file not found: {scad_path}",
            )
        )
        return report.to_dict()

    try:
        source = Path(scad_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        report.ok = False
        report.issues.append(
            Issue(
                severity="error",
                code="SCAD_UNREADABLE",
                message=f"Could not read SCAD file: {exc}",
            )
        )
        return report.to_dict()

    # Iterate every text() call and classify by surrounding context.
    for text_match in _TEXT_CALL_RE.finditer(source):
        report.text_entries_checked += 1
        text_content = text_match.group(1)
        text_line = _line_number_of(source, text_match.start())
        preceding = _nearest_surrounding_block(source, text_match.start())

        # Find the nearest translate() immediately preceding this text.
        translate_matches = list(_TRANSLATE_BOTTOM_RE.finditer(preceding))
        if not translate_matches:
            # No translate — likely not a bottom-face carve; skip.
            continue
        nearest_translate = translate_matches[-1]
        z_component = float(nearest_translate.group(1))

        # Bottom-face heuristic: Z very close to 0 or slightly negative.
        # This catches the common pattern ``translate([0, 0, -0.01])``
        # used for exterior-bottom engravings.
        is_bottom_face = z_component <= 0.1

        if not is_bottom_face:
            continue  # Not a bottom-face text — no flip concern

        # Now check whether a mirror([1, 0, 0]) is in the preceding
        # context.  If missing, flag as error.
        has_mirror_x = bool(_MIRROR_X_RE.search(preceding))
        if not has_mirror_x:
            report.issues.append(
                Issue(
                    severity="error",
                    code="BOTTOM_TEXT_NOT_MIRRORED",
                    message=(
                        f"text({text_content!r}) on a bottom face without "
                        f"mirror([1, 0, 0]) — will print reversed (stamp "
                        f"face) when the part is flipped for reading."
                    ),
                    line_number=text_line,
                    fragment=preceding.strip().split("\n")[-1],
                    suggested_fix=(
                        "Add `mirror([1, 0, 0])` above the linear_extrude "
                        "so the engraving reads correctly after a Y-axis "
                        "flip."
                    ),
                )
            )

        # Depth check: pull the linear_extrude height in the same block.
        extrude_match = _LINEAR_EXTRUDE_RE.search(preceding)
        if extrude_match:
            depth = float(extrude_match.group(1))
            # The epsilon pad (0.01-0.02mm) is common; subtract it.
            effective = depth - 0.02
            if effective < _FDM_MIN_ENGRAVING_DEPTH_MM:
                report.issues.append(
                    Issue(
                        severity="warning",
                        code="BOTTOM_TEXT_SHALLOW",
                        message=(
                            f"text({text_content!r}) engraving depth "
                            f"{effective:.2f}mm is below the FDM-readable "
                            f"floor ({_FDM_MIN_ENGRAVING_DEPTH_MM}mm). "
                            f"First-layer squish can fill shallow recesses."
                        ),
                        line_number=text_line,
                        fragment=f"linear_extrude(height={depth})",
                        suggested_fix=(
                            f"Increase depth to at least "
                            f"{_FDM_RECOMMENDED_DEPTH_MM}mm "
                            f"(6 layers at 0.2mm layer height)."
                        ),
                    )
                )

    report.ok = not any(i.severity == "error" for i in report.issues)
    return report.to_dict()
