"""High-level decoration helpers that route through the emboss engine.

Custom product generators (dedicated MCP tools, third-party plugins,
free-tier-via-REST-API callers) all need to apply text / SVG / image
decoration to a face of a generated STL.  Doing this correctly requires:

- Detecting the target face's normal (cardinal or arbitrary axis-angle)
- Applying the correct rotation to align decoration with face normal
- Auto-sizing text to fit the face
- Wrapping multiple decoration elements in proper SCAD scope
- Handling deboss (difference) vs emboss (union) with correct math
- **Auto-detecting fit problems** — text falling off the side,
  dropping into a cutout, shrinking below the FDM legibility floor

Reinventing any of those is exactly how the nameplate text-jutting-off,
license-plate-frame half-depth-cutout, and multi-line-extrude-broken
bugs shipped on 2026-05-03.  The pieces below wrap the canonical
:mod:`kiln.emboss_generator` + :mod:`kiln.surface_intelligence` engine
so every caller gets the correct path with one function call.

These helpers are the **single supported path** for adding decoration
to a custom-generated STL.  Hand-rolling ``linear_extrude(text(...))``
SCAD inline is an antipattern — see kiln-pro CLAUDE.md
"Custom product decoration — route through the emboss engine".
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any

_logger = logging.getLogger(__name__)


# FDM legibility floor — text smaller than this disappears into layer
# lines on a 0.4mm nozzle / 0.2mm layer-height print.  Empirical, not
# a hard physical limit — at 4mm cap-height "Bold" stays readable but
# "Regular" already softens.  Below this the helper auto-warns.
_FDM_TEXT_LEGIBILITY_FLOOR_MM = 4.0


class TextDoesNotFitError(ValueError):
    """Raised when text cannot fit a target strip even at the legibility floor.

    Carries the verdict dict on ``.verdict`` so the catching tool can
    surface the specific reason and the suggested fixes to the user
    without re-deriving them.

    Per kiln-pro CLAUDE.md "ship-readiness" rule: a tool must not
    produce a preview / SCAD / STL of text that won't be readable,
    because the user trusts the preview.  Hand the user a clear
    actionable error instead — that's the difference between a
    professional product and a "preview-of-broken" toy.
    """

    def __init__(self, verdict: dict[str, Any]) -> None:
        self.verdict = verdict
        msg = "; ".join(verdict.get("warnings", []) or ["text does not fit"])
        super().__init__(msg)


def fit_text_to_strip(
    text: str,
    *,
    strip_width_mm: float,
    strip_height_mm: float,
    safety_margin: float = 0.85,
    min_size_mm: float = _FDM_TEXT_LEGIBILITY_FLOOR_MM,
    char_aspect: float = 0.6,
    raise_on_no_fit: bool = False,
) -> dict[str, Any]:
    """Compute a font size that fits text into a width × height strip.

    Returns a verdict dict any caller can act on:

    .. code-block:: python

        {
            "fits": True / False,
            "font_size_mm": <chosen size, in mm>,
            "text_width_mm": <actual width at that size>,
            "warnings": [<human-readable strings>],
            "suggestions": [<actionable fix suggestions>],
            "constraint": "width" | "height" | "min_floor" | "ok",
        }

    Use it BEFORE handing decoration to the engine — every product
    that lays out text in a band (license plate frame top/bottom band,
    award plaque caption, plinth nameplate, badge with name+title)
    benefits from the same "will it fit / would it overflow / would
    it be too small to read" check.

    The 2026-05-03 license-plate "KILN dropping into the cutout" bug
    is the canonical example: a band 24mm tall housing text auto-sized
    to 15mm with valign=center has its bottom at -7.5mm relative to
    the band centre, which on that frame falls inside the 152mm
    plate cutout window.  Catching that here, before the SCAD ships,
    means users never see a visibly broken render.

    :param text: The string to fit.
    :param strip_width_mm: Available width in mm.
    :param strip_height_mm: Available height in mm.
    :param safety_margin: Fraction of the strip the text may occupy
        (default 0.85 — leaves a 7.5% margin top/bottom and similar
        on each side).  Drop to 0.7 for "very polished" callers,
        raise to 0.95 for "use every pixel."
    :param min_size_mm: Minimum legible font size in mm.  Defaults to
        the FDM legibility floor (4mm at 0.4mm nozzle / 0.2mm layer).
    :param char_aspect: Average glyph width as a fraction of font
        size (default 0.6 — empirical for Liberation Sans Bold).
        Raise to 0.7 for serif fonts, drop to 0.5 for condensed.
    :param raise_on_no_fit: When ``True`` and the text cannot fit
        legibly, raise :class:`TextDoesNotFitError` instead of
        returning a verdict with ``fits=False``.  Use this in
        product-tool entry points so the pipeline halts before
        producing a broken STL.
    :returns: Verdict dict.
    :raises TextDoesNotFitError: When ``raise_on_no_fit=True`` and
        text cannot fit.
    """
    chars = max(1, len(text))
    # Width-fit: text width = chars × char_aspect × font_size
    # → font_size = (strip_width × safety) / (chars × char_aspect)
    width_fit = (strip_width_mm * safety_margin) / (chars * char_aspect)
    # Height-fit: cap-height ≈ 0.7 × font_size; glyph extent (with
    # descenders) ≈ font_size; use safety on the strip height.
    height_fit = strip_height_mm * safety_margin
    chosen = min(width_fit, height_fit)

    warnings: list[str] = []
    suggestions: list[str] = []
    fits = True
    constraint = "ok"

    if chosen < min_size_mm:
        fits = False
        constraint = "min_floor"
        # How much shorter would the text need to be to fit?
        max_chars_at_min = int(
            (strip_width_mm * safety_margin) / (min_size_mm * char_aspect)
        )
        # How much wider would the strip need to be?
        min_strip_w = chars * char_aspect * min_size_mm / safety_margin
        warnings.append(
            f"Text {text!r} would shrink to {chosen:.1f}mm — below "
            f"the {min_size_mm:.1f}mm FDM legibility floor."
        )
        suggestions.append(
            f"Shorten to ≤{max_chars_at_min} characters "
            f"(currently {chars})"
        )
        suggestions.append(
            f"Widen the strip to ≥{min_strip_w:.0f}mm "
            f"(currently {strip_width_mm:.0f}mm)"
        )
        if "\n" not in text and " " in text:
            words = text.split()
            if len(words) >= 2:
                suggestions.append(
                    f"Split onto multiple lines "
                    f"(e.g. {words[0]!r} on line 1, "
                    f"{' '.join(words[1:])!r} on line 2)"
                )
    elif chosen == width_fit and width_fit < height_fit:
        constraint = "width"
    elif chosen == height_fit:
        constraint = "height"

    text_width_mm = chars * char_aspect * chosen
    if text_width_mm > strip_width_mm:
        fits = False
        warnings.append(
            f"Text {text!r} at {chosen:.1f}mm would extend "
            f"{text_width_mm:.0f}mm — exceeds available width "
            f"{strip_width_mm:.0f}mm."
        )
        if not suggestions:
            suggestions.append(
                f"Shorten to ≤{int(strip_width_mm * safety_margin / (chosen * char_aspect))} characters "
                f"or widen the strip."
            )

    verdict = {
        "fits": fits,
        "font_size_mm": chosen,
        "text_width_mm": text_width_mm,
        "warnings": warnings,
        "suggestions": suggestions,
        "constraint": constraint,
    }
    if not fits and raise_on_no_fit:
        raise TextDoesNotFitError(verdict)
    return verdict


def emboss_text_on_face(
    body_stl: str,
    text: str,
    *,
    face_name: str | None = None,
    mode: str = "emboss",
    depth_mm: float = 0.8,
    scale: float = 0.85,
    offset_x_mm: float = 0.0,
    offset_y_mm: float = 0.0,
    font: str = "Liberation Sans:style=Bold",
    font_size_mm: float = 0.0,
    output_dir: str | None = None,
    output_stl: str | None = None,
) -> str:
    """Apply a single line of text as emboss/deboss onto a face of an STL.

    Routes through :func:`kiln.emboss_generator.generate_emboss_scad`
    (face-normal-aware rotation + auto-sizing) and
    :func:`kiln.emboss_generator.compile_embossed_model` (compile to
    STL).  Detects the target face via
    :func:`kiln.surface_intelligence.find_named_face` (when *face_name*
    is given) or :func:`find_largest_flat_face` (auto).

    :param body_stl: Path to the host STL the text gets applied to.
    :param text: The text to emboss/deboss.  Single line; for multiple
        lines, call this helper once per line with different
        *offset_y_mm*.
    :param face_name: ``"top"`` / ``"bottom"`` / ``"front"`` /
        ``"back"`` / ``"left"`` / ``"right"`` to target a cardinal face.
        ``None`` means auto-detect the largest flat face.
    :param mode: ``"emboss"`` (raised) or ``"deboss"`` (recessed).
    :param depth_mm: Depth of the emboss/deboss in mm.
    :param scale: Fraction of the face the text spans (0.0–1.0).
        Engine auto-sizes the font so the text fits at this scale.
    :param offset_x_mm: Horizontal offset from face centre in mm.
    :param offset_y_mm: Vertical offset from face centre in mm.
    :param font: OpenSCAD font specifier.
    :param font_size_mm: Explicit font size in mm.  When 0 (default),
        the engine auto-sizes to fit the face.  Pass a positive value
        to override (e.g. for typography hierarchy across multi-line
        layouts where auto-sizing's width/height coupling produces
        inverted sizing).
    :param output_dir: Directory for intermediate SCAD + final STL.
        Auto-created in /tmp if omitted.
    :param output_stl: Final STL path.  Auto-named if omitted.
    :returns: Absolute path to the new STL with text applied.
    :raises FileNotFoundError: If *body_stl* doesn't exist.
    :raises ValueError: If face detection fails.
    :raises RuntimeError: If OpenSCAD compilation fails.
    """
    from kiln.emboss_generator import compile_embossed_model, generate_emboss_scad
    from kiln.surface_intelligence import find_largest_flat_face, find_named_face

    if not os.path.isfile(body_stl):
        raise FileNotFoundError(f"Host STL not found: {body_stl}")

    # 1. Detect target face
    if face_name:
        face = find_named_face(body_stl, face_name)
    else:
        face = find_largest_flat_face(body_stl)

    # 2. Set up output paths
    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="kiln_emboss_text_")
    else:
        os.makedirs(output_dir, mode=0o700, exist_ok=True)
    if output_stl is None:
        # Stable name based on text + offsets so repeated calls overwrite
        # rather than accumulate.
        slug = "".join(c if c.isalnum() else "_" for c in text)[:24]
        output_stl = os.path.join(output_dir, f"with_{slug}.stl")

    # 3. Build the emboss SCAD via the engine — this handles
    # face-normal-aware rotation, auto-text-sizing to fit the face,
    # and the linear_extrude depth correctly.
    content_info: dict[str, Any] = {
        "type": "openscad_text",
        "text": text,
        "font": font,
    }
    if font_size_mm > 0:
        content_info["font_size"] = font_size_mm
    scad_result = generate_emboss_scad(
        model_path=body_stl,
        content_info=content_info,
        face=face,
        output_dir=output_dir,
        depth_mm=depth_mm,
        mode=mode,
        scale=scale,
        offset_x_mm=offset_x_mm,
        offset_y_mm=offset_y_mm,
    )

    # 4. Compile to STL
    compile_result = compile_embossed_model(scad_result["scad_path"], output_stl)
    if not compile_result.get("success"):
        raise RuntimeError(
            f"Emboss compilation failed for text {text!r}: "
            f"{compile_result.get('error', 'unknown error')}"
        )

    return output_stl


def emboss_text_lines_on_face(
    body_stl: str,
    lines: list[str],
    *,
    face_name: str | None = None,
    mode: str = "emboss",
    depth_mm: float = 0.8,
    line_scale: float = 0.85,
    line_spacing_mm: float = 0.0,
    output_dir: str | None = None,
    hierarchy: list[float] | None = None,
    **kwargs: Any,
) -> str:
    """Apply multiple lines of text to a face by chaining emboss calls.

    Each line is sized to fit a proportional strip of the face — line 1
    is the primary (full strip), line 2 the secondary (~70% as tall),
    line 3 the tertiary (~50%).  Without this, the engine's auto-sizer
    independently maxes each line to fill the face, which produces
    "JOHN" at 22mm and "CEO" at 50mm (CEO has fewer chars → less
    width-clamped → larger).  See decoration_helpers test fixtures for
    the regression that pinned this.

    Each line is positioned at a different *offset_y_mm* relative to
    the face centre — top line at +line_spacing/2 if 2 lines, etc.

    For per-line scale customisation pass ``hierarchy=[1.0, 0.6, 0.4]``
    or call :func:`emboss_text_on_face` directly per line.

    :param hierarchy: Per-line size multipliers relative to *line_scale*.
        Defaults to ``[1.0]`` for 1 line, ``[1.0, 0.7]`` for 2 lines,
        ``[1.0, 0.7, 0.5]`` for 3+ lines (recursive 0.7^i ratio).
    :returns: Final STL path with all lines applied.
    """
    if not lines:
        return body_stl

    n = len(lines)

    # Default hierarchy: primary 1.0, each subsequent 70% of prior.
    # Matches the "name big, title smaller, division smallest" pattern
    # in real nameplates / business cards / awards plaques.
    if hierarchy is None:
        hierarchy = [0.7 ** i for i in range(n)]
    # Pad / trim hierarchy to match line count.
    if len(hierarchy) < n:
        hierarchy = list(hierarchy) + [hierarchy[-1] * 0.7] * (n - len(hierarchy))
    hierarchy = hierarchy[:n]

    # Resolve the face once so we can pre-compute per-line font sizes
    # and offsets in face-local mm.
    from kiln.surface_intelligence import (
        find_largest_flat_face,
        find_named_face,
    )

    face = (
        find_named_face(body_stl, face_name)
        if face_name
        else find_largest_flat_face(body_stl)
    )
    face_w = face["width_mm"]
    face_h = face["height_mm"]

    # Compute the primary line's font size: the larger of "fits within
    # the strip's height" and "fits within the face width."  Width-fit
    # uses the longest line so all lines stay inside the face.  Take
    # the smaller of the two as the upper bound for the primary line.
    #
    # Safety margin: 0.85 (was 0.95) leaves ~15% of the face as a
    # visual frame around the text so glyphs never appear to fly off
    # the edges.  The 2026-05-03 nameplate user feedback flagged
    # "Josh Beckham" at 0.95 as visually crammed even though it was
    # technically inside the bounding box — the 0.85 factor gives
    # the same "professional desk sign" margins as a real engraved
    # nameplate.
    longest_chars = max(len(s) for s in lines if s) or 1
    width_fit_primary = (face_w * line_scale * 0.85) / (longest_chars * 0.6)
    height_fit_primary = (face_h * line_scale / n) * 0.85
    primary_size = min(width_fit_primary, height_fit_primary)
    # Per-line sizes follow the hierarchy ratios from the primary.
    per_line_sizes = [primary_size * hierarchy[i] for i in range(n)]

    # Hard-stop if any line would not fit legibly.  The 2026-05-03
    # user mandate "kiln should never let users put text that won't
    # fit" applies here too — the pipeline must not produce a SCAD/STL/
    # preview the user can't actually use.  Caller catches
    # TextDoesNotFitError and surfaces ``verdict.suggestions`` to the
    # user (shorten to N chars, widen to W mm, split lines).
    fit_warnings: list[str] = []
    for i, (line, size) in enumerate(zip(lines, per_line_sizes, strict=False)):
        if not line:
            continue
        text_w = len(line) * size * 0.6
        if text_w > face_w * 0.95:
            verdict = {
                "fits": False,
                "font_size_mm": size,
                "text_width_mm": text_w,
                "constraint": "width",
                "warnings": [
                    f"Line {i + 1} ({line!r}) at {size:.1f}mm would "
                    f"extend {text_w:.0f}mm — exceeds face width "
                    f"{face_w:.0f}mm."
                ],
                "suggestions": [
                    "Shorten this line to fit the face",
                    f"Use a wider product (current face: {face_w:.0f}mm)",
                    "Split onto multiple lines",
                ],
            }
            raise TextDoesNotFitError(verdict)
        if size < _FDM_TEXT_LEGIBILITY_FLOOR_MM:
            max_chars_at_floor = int(
                (face_w * line_scale * 0.85)
                / (_FDM_TEXT_LEGIBILITY_FLOOR_MM * 0.6)
            )
            verdict = {
                "fits": False,
                "font_size_mm": size,
                "text_width_mm": text_w,
                "constraint": "min_floor",
                "warnings": [
                    f"Line {i + 1} ({line!r}) auto-sized to {size:.1f}mm "
                    f"— below the {_FDM_TEXT_LEGIBILITY_FLOOR_MM}mm "
                    f"FDM legibility floor."
                ],
                "suggestions": [
                    f"Shorten this line to ≤{max_chars_at_floor} chars",
                    f"Use a larger product or fewer lines "
                    f"(current: {n} lines × {face_h:.0f}mm height)",
                ],
            }
            raise TextDoesNotFitError(verdict)
        # Tight-but-fits — log a soft warning that callers can surface.
        if text_w > face_w * 0.85:
            fit_warnings.append(
                f"Line {i + 1} ({line!r}) at {size:.1f}mm uses "
                f"{text_w / face_w * 100:.0f}% of face width — tight "
                f"fit, no margin for material expansion."
            )
    for warning in fit_warnings:
        _logger.warning("emboss_text_lines: %s", warning)

    if line_spacing_mm <= 0:
        # Total typography stack height = sum of per-line heights × 1.4
        # (1.4 = baseline-to-baseline spacing for clean reads).  Then
        # centre that stack on the face.
        line_spacing_mm = sum(per_line_sizes) * 1.4 / max(1, n)

    # Centre line(s) vertically: distribute around face centre
    if n == 1:
        offsets = [0.0]
    else:
        total_height = (n - 1) * line_spacing_mm
        top = total_height / 2.0
        offsets = [top - i * line_spacing_mm for i in range(n)]

    current = body_stl
    for i, (line, offset) in enumerate(zip(lines, offsets, strict=False)):
        if not line:
            continue
        current = emboss_text_on_face(
            current,
            line,
            face_name=face_name,
            mode=mode,
            depth_mm=depth_mm,
            scale=line_scale,
            offset_y_mm=offset,
            font_size_mm=per_line_sizes[i],
            output_dir=output_dir,
            output_stl=(
                os.path.join(output_dir, f"line_{i}.stl") if output_dir else None
            ),
            **kwargs,
        )

    return current
