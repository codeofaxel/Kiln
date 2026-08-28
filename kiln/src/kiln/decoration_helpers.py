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
SCAD inline is an antipattern — every custom product decoration
must route through the emboss engine instead.

SIZING CONTRACT (the text-sizing seam, closed 2026-08-08)
---------------------------------------------------------
The LAYOUT layer decides, the engine executes:

- :func:`emboss_text_lines_on_face` owns typography.  It computes every
  line's font size from the engine's own MEASURED glyph metrics
  (:func:`kiln.emboss_generator.measure_text_block_mm`), applies its
  0.85 professional visual margin, the hierarchy ratios, the FDM
  legibility floor, and — on elliptical faces (coasters, oval trays) —
  the real inscribed width at each line's band.  Because its sizes are
  computed from real metrics, the engine's overflow guard never fires
  for them: the size this helper requests is the size that ships.
  (Before the fix it sized from a 0.6-per-char guess; the engine then
  re-fit the real glyphs to its own unmargined box, which destroyed the
  margin — shipped text ran exactly 1/0.85 = 17.6% wider than intent —
  and could clamp the primary line below the secondary, inverting the
  hierarchy the helper exists to protect.)
- :func:`kiln.emboss_generator.generate_emboss_scad` honours explicit
  sizes that fit VERBATIM and clamps down — always warning — only on
  genuine overflow (face box, or an elliptical face's rim).  Its auto
  mode fills the caller's box exactly; margins are a layout concern.
- Nothing fails silently: engine clamp warnings are logged here and
  surfaced through each helper's ``collect_warnings`` sink; impossible
  fits raise :class:`TextDoesNotFitError` with actionable suggestions.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from typing import Any

from kiln.openscad_runner import run_openscad
from kiln.preview_render import downscale_png, effective_supersample

# Public surface — what callers (kiln-pro plugins, REST API, agents)
# import.  Anything not listed here is internal helper and may move
# between minor versions.
__all__ = [
    # Errors
    "DepthBelowLegibilityFloor",
    "TextDoesNotFitError",
    # Verdict + decision helpers
    "compute_text_line_layout",
    "fit_text_to_strip",
    "select_bottom_face_flip",
    # Primary emboss entry points
    "emboss_text_on_face",
    "emboss_text_lines_on_face",
]

_logger = logging.getLogger(__name__)


def _resolve_openscad_binary() -> str | None:
    """Locate the OpenSCAD binary on the current host.

    Same resolution order used by :mod:`kiln.generation.visual_verify`
    and :mod:`kiln.generation.openscad`: prefer ``shutil.which``, fall
    back to the macOS .app bundle's binary path so this works on
    developer machines that don't have OpenSCAD on PATH.

    :returns: Absolute path to the OpenSCAD binary, or ``None`` when
        no binary can be found (caller should skip rendering rather
        than fail).
    """
    found = shutil.which("openscad")
    if found:
        return found
    mac_app = "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD"
    if os.path.isfile(mac_app):
        return mac_app
    return None


def _render_post_flip_preview(
    decorated_stl: str,
    flip_axis: str,
    output_dir: str,
    *,
    filename: str = "flip_preview.png",
) -> str | None:
    """Render a PNG showing what the user sees AFTER physically flipping
    the decorated object along its natural flip axis.

    This is the mandatory verification preview for bottom-face
    engravings — the human approval gate at the inspection-bundle
    surface depends on seeing the post-flip orientation rather than
    the print-orientation view (which hides the engraving on the
    bottom face).

    Implementation mirrors the canonical Kiln OpenSCAD render pattern
    used by :mod:`kiln.model_visualizer` and :mod:`kiln.multicolor_3mf`:
    write a tiny SCAD that ``import()``s the STL with a wrapping
    transformation, call OpenSCAD with ``--imgsize 800,600
    --colorscheme DeepOcean`` and a 3/4 camera, save the resulting
    PNG to *output_dir* under *filename*.

    :param decorated_stl: Path to the decorated STL (engine output).
    :param flip_axis: ``"x"`` for an X-axis physical flip (natural
        for wide-shallow objects) or ``"y"`` for Y-axis (natural for
        tall-narrow objects).  Any other value renders the
        unflipped 3/4 view as a defensive fallback.
    :param output_dir: Directory to write the PNG into.  Created if
        missing.
    :param filename: Override the default ``flip_preview.png``
        filename (e.g. when emitting per-line previews).
    :returns: Absolute path to the PNG on success, or ``None`` when
        OpenSCAD is unavailable / rendering fails.  Engine code path
        treats ``None`` as "skip the preview but don't fail the
        emboss" — the engraving STL is the load-bearing artifact;
        the preview is the human-readable supplement.
    """
    if not os.path.isfile(decorated_stl):
        _logger.debug(
            "post-flip preview: decorated STL missing at %s; skipping",
            decorated_stl,
        )
        return None

    openscad = _resolve_openscad_binary()
    if openscad is None:
        _logger.debug(
            "post-flip preview: no OpenSCAD binary found; skipping render "
            "(caller proceeds without preview)"
        )
        return None

    os.makedirs(output_dir, exist_ok=True)
    png_path = os.path.join(output_dir, filename)
    scad_path = os.path.join(output_dir, filename.replace(".png", ".scad"))

    # Pre-rotate the imported STL by the user's natural physical
    # flip.  Rendered top-down via the canonical 3/4 camera so the
    # post-flip bottom face is what fills the frame.
    if flip_axis == "x":
        flip_clause = "rotate([180, 0, 0])"
    elif flip_axis == "y":
        flip_clause = "rotate([0, 180, 0])"
    else:
        flip_clause = ""

    escaped_stl = decorated_stl.replace("\\", "\\\\").replace('"', '\\"')
    scad_body = f'color("#AAAAAA") {flip_clause} import("{escaped_stl}");\n'
    with open(scad_path, "w", encoding="utf-8") as fh:
        fh.write(scad_body)

    # ``--autocenter --viewall`` lets OpenSCAD frame the rotated STL
    # without us having to know its bounding box ahead of time — the
    # post-flip transformation moves the object off origin and a
    # fixed camera distance crops the engraving out of frame.
    # Supersample: render oversized then Lanczos-downscale for crisp
    # edges — one shared knob governs every OpenSCAD preview surface.
    ss = effective_supersample()
    cmd = [
        openscad,
        "--preview",
        "-o", png_path,
        f"--imgsize={800 * ss},{600 * ss}",
        "--colorscheme=DeepOcean",
        "--autocenter",
        "--viewall",
        "--camera=0,0,0,55,0,25,200",
        scad_path,
    ]

    try:
        result = run_openscad(cmd, timeout=60, output_path=png_path)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        _logger.debug("post-flip preview: render subprocess failed: %s", exc)
        return None

    if result.returncode != 0 or not os.path.isfile(png_path):
        stderr_tail = (result.stderr or "").strip()[:200]
        _logger.debug(
            "post-flip preview: OpenSCAD exit=%d stderr=%s",
            result.returncode, stderr_tail,
        )
        return None
    if os.path.getsize(png_path) == 0:
        _logger.debug("post-flip preview: empty PNG produced; treating as failed")
        return None

    if ss > 1:
        downscale_png(png_path, 800, 600)

    return png_path


# FDM legibility floor — text smaller than this disappears into layer
# lines on a 0.4mm nozzle / 0.2mm layer-height print.  Empirical, not
# a hard physical limit — at 4mm cap-height "Bold" stays readable but
# "Regular" already softens.  Below this the helper auto-warns.
_FDM_TEXT_LEGIBILITY_FLOOR_MM = 4.0


# Emboss/deboss depth legibility floor — the carving needs at least
# ~3x the nozzle diameter of vertical structure for the engraving to
# read at arm's-length.  Calibrated against the 1.2mm A1 (0.4mm
# nozzle) empirical floor calibrated from print testing; at 3x
# this extrapolates to 1.8mm on a 0.6mm Prusa MK4 nozzle and 0.75mm
# on a 0.25mm precision nozzle.  Used by :func:`emboss_text_on_face`
# and :func:`emboss_text_lines_on_face` to refuse depth values that
# would render as illegible smears on the user's printer.
_DEPTH_LEGIBILITY_FLOOR_MULTIPLIER = 3.0


def _depth_legibility_floor_mm(nozzle_diameter_mm: float) -> float:
    """Minimum ``depth_mm`` for emboss/deboss text to read at arm's length.

    Returns ``nozzle_diameter_mm × 3``.  Anything below this is a
    legibility risk — the carving walls don't have enough vertical
    structure to survive shrinkage, sag, and layer-line interference.

    Calibrated against the Bambu A1 (0.4mm nozzle → 1.2mm floor)
    per empirical print testing.

    :param nozzle_diameter_mm: The active printer's nozzle diameter.
    :returns: Floor depth in millimetres.
    """
    return nozzle_diameter_mm * _DEPTH_LEGIBILITY_FLOOR_MULTIPLIER


class DepthBelowLegibilityFloor(ValueError):
    """Raised when the requested emboss/deboss depth is below the
    printer-specific legibility floor.

    Carries ``.floor_mm``, ``.requested_mm``, and ``.nozzle_diameter_mm``
    so the catching tool can hand the user an actionable suggestion
    (e.g. "use depth_mm >= 1.8 on a 0.6mm nozzle") without re-deriving
    the floor.

    Ship-readiness rule: refuse to produce a preview / SCAD / STL of
    text that will print as an illegible smear.  A clear actionable
    error beats a "preview-of-broken" output.

    Example:

        try:
            emboss_text_on_face(
                stl, "KILN", depth_mm=0.6, nozzle_diameter_mm=0.4,
            )
        except DepthBelowLegibilityFloor as err:
            user_msg = (
                f"Engraving depth {err.requested_mm:.1f}mm is below "
                f"your printer's legibility floor "
                f"({err.floor_mm:.1f}mm for a "
                f"{err.nozzle_diameter_mm:.2f}mm nozzle).  "
                f"Try depth_mm={err.floor_mm:.1f} or deeper."
            )
    """

    def __init__(
        self,
        *,
        requested_mm: float,
        floor_mm: float,
        nozzle_diameter_mm: float,
    ) -> None:
        self.requested_mm = requested_mm
        self.floor_mm = floor_mm
        self.nozzle_diameter_mm = nozzle_diameter_mm
        super().__init__(
            f"emboss depth {requested_mm:.2f}mm is below the "
            f"legibility floor {floor_mm:.2f}mm for a "
            f"{nozzle_diameter_mm:.2f}mm nozzle "
            f"({_DEPTH_LEGIBILITY_FLOOR_MULTIPLIER}x rule)"
        )


# --- Aspect-ratio-aware flip-axis selection -----------------------------
#
# Engravings on the bottom face of a printed object are pre-flipped so
# they read correctly AFTER the user physically flips the object to look
# at the bottom.  Which way the user flips depends on the object's
# shape:
#
# - Wide-shallow objects (width > height, e.g. coasters, soap dishes,
#   jewelry trays): the natural flip is around the long X axis — pick
#   up the front edge and tilt it UP toward yourself.  The pre-flip
#   that matches this is ``rotate([180, 0, 0])``.
#
# - Tall-narrow objects (height > width, e.g. bookmarks, pet tags held
#   vertically): the natural flip is around the long Y axis — grab
#   one side and roll the object onto its other side.  The pre-flip
#   that matches this is ``mirror([1, 0, 0])``.
#
# - Square / round objects (aspect ratio < 1.2): either flip works.
#   Default to ``rotate([180, 0, 0])`` because rotation preserves
#   handedness (no left-right inversion of asymmetric glyphs like
#   script-font flourishes or vendor logos).
#
# Selection runs a MANDATORY geometric self-inspection: after picking a
# transformation, the engine mathematically simulates the chain
# (pre-transform → user's natural physical flip → viewer's eye) and
# verifies sample characters end up in left-to-right reading order.
# If the self-inspection fails, the engine retries with the alternative
# transformation; if both fail, a structured error surfaces the issue.
# No OCR, no LLM, no rendering — pure geometry.  The human still gets
# the rendered preview through the existing inspection-bundle channel.

_SQUARE_THRESHOLD = 1.2  # aspect < this counts as "approximately square"


def _select_flip_transformation(
    face_width_mm: float, face_height_mm: float,
) -> tuple[str, str, str]:
    """Pick a pre-flip SCAD transformation for the BOTTOM-face engraving.

    Returns a tuple of ``(transformation_scad, flip_axis, rationale)``:

    - ``transformation_scad`` is the SCAD clause to apply before the
      ``linear_extrude(text(...))`` block (``"rotate([180, 0, 0])"`` or
      ``"mirror([1, 0, 0])"``).
    - ``flip_axis`` is ``"x"`` or ``"y"`` — the user's natural physical
      flip axis that the transformation matches.
    - ``rationale`` is a 1-line human-readable explanation suitable for
      surfacing to an agent log or the inspection-bundle preview.

    :param face_width_mm: Face X-extent in face-local mm.
    :param face_height_mm: Face Y-extent in face-local mm.
    """
    aspect = max(face_width_mm, face_height_mm) / max(
        min(face_width_mm, face_height_mm), 1e-6
    )
    if aspect < _SQUARE_THRESHOLD:
        return (
            "rotate([180, 0, 0])",
            "x",
            f"face is approximately square ({face_width_mm:.0f}×"
            f"{face_height_mm:.0f}mm, aspect {aspect:.2f}); rotation "
            f"default preserves handedness",
        )
    if face_width_mm > face_height_mm:
        return (
            "rotate([180, 0, 0])",
            "x",
            f"wide-shallow face ({face_width_mm:.0f}×{face_height_mm:.0f}mm); "
            f"natural physical flip around long X axis",
        )
    return (
        "mirror([1, 0, 0])",
        "y",
        f"tall-narrow face ({face_width_mm:.0f}×{face_height_mm:.0f}mm); "
        f"natural physical flip around long Y axis",
    )


def _self_inspect_flip_orientation(
    transformation: str, flip_axis: str,
) -> dict[str, Any]:
    """Geometric self-inspection — does the chosen pre-transform leave
    text readable after the user's natural physical flip?

    Simulates the transformation chain on sample character-centroid
    points (laid out left-to-right at the face's centerline) and
    verifies they end up in left-to-right reading order when the
    viewer looks down at the post-flip object.

    :param transformation: The pre-flip SCAD clause —
        ``"rotate([180, 0, 0])"`` or ``"mirror([1, 0, 0])"``.
    :param flip_axis: The user's natural physical flip axis —
        ``"x"`` (rotate object 180° around X — tilt front up) or
        ``"y"`` (rotate object 180° around Y — roll onto side).
    :returns: A dict with ``passed``, ``method``, ``sample_x_order``
        and ``detail``.  Catching code can include this in a verdict.
    """
    # Sample points: 4 character centroids laid out left-to-right
    # at face-local Y=0, with X increasing.  After the chain ends,
    # we expect X to still be left-to-right when the viewer's eye
    # is above the (post-flip) object looking down +Z → -Z.
    points = [(float(i), 0.0, 0.0) for i in range(4)]

    # Step 1: pre-transform (face-local frame → bottom-face world frame).
    if transformation.startswith("rotate"):
        # rotate([180, 0, 0]): (x, y, z) → (x, -y, -z)
        points = [(x, -y, -z) for x, y, z in points]
    elif transformation.startswith("mirror"):
        # mirror([1, 0, 0]): (x, y, z) → (-x, y, z)
        points = [(-x, y, z) for x, y, z in points]
    else:
        return {
            "passed": False,
            "method": "geometric",
            "detail": f"unknown pre-transform {transformation!r}",
        }

    # Step 2: user's natural physical flip.
    if flip_axis == "x":
        # rotate 180° around X: (x, y, z) → (x, -y, -z)
        points = [(x, -y, -z) for x, y, z in points]
    elif flip_axis == "y":
        # rotate 180° around Y: (x, y, z) → (-x, y, -z)
        points = [(-x, y, -z) for x, y, z in points]
    else:
        return {
            "passed": False,
            "method": "geometric",
            "detail": f"unknown flip_axis {flip_axis!r}",
        }

    # Step 3: viewer looks down at the post-flip object from +Z.
    # Reading order = X increasing left-to-right.
    x_values = [p[0] for p in points]
    arranged_correctly = all(
        x_values[i] < x_values[i + 1] for i in range(len(x_values) - 1)
    )
    return {
        "passed": arranged_correctly,
        "method": "geometric",
        "sample_x_order": x_values,
        "detail": (
            "characters in left-to-right reading order after pre-transform "
            "+ user's natural physical flip"
            if arranged_correctly
            else "characters end up reversed; transformation does not "
            "match the assumed flip axis"
        ),
    }


def select_bottom_face_flip(
    face_width_mm: float, face_height_mm: float,
) -> dict[str, Any]:
    """Pick the right pre-flip transformation for a bottom-face engraving,
    self-inspect the choice, and return a verdict ready to consume.

    Combines :func:`_select_flip_transformation` (aspect-ratio decision)
    with :func:`_self_inspect_flip_orientation` (mandatory geometric
    verification).  If the primary choice fails self-inspection, retries
    with the alternative; if both fail, raises ``RuntimeError`` with the
    diagnostic so the caller can surface it to the user instead of
    silently shipping a misoriented engraving.

    :returns: Dict with ``transformation`` (SCAD clause), ``flip_axis``
        (``"x"`` / ``"y"``), ``rationale`` (1-line explanation),
        ``self_inspection`` (verdict dict), and ``confidence``
        (``"high"`` / ``"medium"``).

    Example:

        # 100mm-wide coaster: wide-shallow → X-axis natural flip
        verdict = select_bottom_face_flip(
            face_width_mm=100.0, face_height_mm=60.0,
        )
        assert verdict["transformation"] == "rotate([180, 0, 0])"
        assert verdict["flip_axis"] == "x"
        assert verdict["confidence"] == "high"

        # 60×120mm bookmark: tall-narrow → Y-axis natural flip
        verdict = select_bottom_face_flip(
            face_width_mm=60.0, face_height_mm=120.0,
        )
        assert verdict["transformation"] == "mirror([1, 0, 0])"
        assert verdict["flip_axis"] == "y"
    """
    transformation, flip_axis, rationale = _select_flip_transformation(
        face_width_mm, face_height_mm,
    )
    inspection = _self_inspect_flip_orientation(transformation, flip_axis)

    aspect = max(face_width_mm, face_height_mm) / max(
        min(face_width_mm, face_height_mm), 1e-6
    )
    confidence = "high" if aspect >= _SQUARE_THRESHOLD else "medium"

    if not inspection["passed"]:
        # Fall back to the alternative transformation.
        alt_transformation = (
            "mirror([1, 0, 0])"
            if transformation.startswith("rotate")
            else "rotate([180, 0, 0])"
        )
        alt_flip_axis = "y" if flip_axis == "x" else "x"
        alt_inspection = _self_inspect_flip_orientation(
            alt_transformation, alt_flip_axis,
        )
        if alt_inspection["passed"]:
            return {
                "transformation": alt_transformation,
                "flip_axis": alt_flip_axis,
                "rationale": (
                    f"{rationale} (primary failed self-inspection, "
                    f"fell back to alternative)"
                ),
                "self_inspection": alt_inspection,
                "confidence": "medium",
            }
        # Both failed — the engine doesn't know how to make text
        # readable for this face.  Surface the diagnostic.
        raise RuntimeError(
            f"flip-orientation self-inspection failed for both "
            f"transformations on face {face_width_mm:.0f}×"
            f"{face_height_mm:.0f}mm — engine bug, please report.  "
            f"Primary detail: {inspection['detail']}.  "
            f"Alternative detail: {alt_inspection['detail']}."
        )

    return {
        "transformation": transformation,
        "flip_axis": flip_axis,
        "rationale": rationale,
        "self_inspection": inspection,
        "confidence": confidence,
    }


class TextDoesNotFitError(ValueError):
    """Raised when text cannot fit a target strip even at the legibility floor.

    Carries the verdict dict on ``.verdict`` so the catching tool can
    surface the specific reason and the suggested fixes to the user
    without re-deriving them.

    Ship-readiness rule: a tool must not produce a preview / SCAD /
    STL of text that won't be readable, because the user trusts the
    preview.  Hand the user a clear actionable error instead — that's
    the difference between a professional product and a
    "preview-of-broken" toy.
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


def compute_text_line_layout(
    lines: list[str],
    *,
    face: dict[str, Any],
    line_scale: float = 0.7,
    min_edge_margin_mm: float = 4.0,
    hierarchy: list[float] | None = None,
    line_spacing_mm: float = 0.0,
    font: str = "Liberation Sans:style=Bold",
    min_size_mm: float | None = None,
) -> dict[str, Any]:
    """Compute honest per-line font sizes and offsets for a face.

    This is the sizing half of :func:`emboss_text_lines_on_face`, pure
    math over the face dict so it can be tested and reused without an
    emboss compile.  Sizes come from the engine's MEASURED glyph metrics
    (:func:`kiln.emboss_generator.measure_text_block_mm`) — one cached
    sub-second probe per (text, font) — so the width used here is the
    width that ships.  When no OpenSCAD binary is available the
    0.6-per-char legacy estimate keeps the layout functional (coarse
    beats broken; the engine's guard also degrades to the same
    estimate, so the two layers still agree).

    Constraints applied, in order:

    1. Width: every line's measured run at its hierarchy ratio stays
       within 0.85 of the usable box (``face_w x line_scale``, further
       clamped by *min_edge_margin_mm* — the same clamp the engine
       applies, mirrored here so the engine never has to re-fit).
    2. Height: the primary line's measured glyph height fits its strip
       (``face_h x line_scale / n``) at the same 0.85 margin.
    3. Rim: on an elliptical face every line's corners must sit inside
       the inscribed ellipse at that line's band — the bounding box
       lies about available width near a coaster's rim.  All lines
       shrink by ONE factor so the hierarchy ratios survive exactly.
    4. Floor: any line below the FDM legibility floor raises
       :class:`TextDoesNotFitError` with actionable suggestions.

    :returns: dict with ``font_sizes_mm``, ``offsets_mm`` (face-local y
        per line, top first), ``line_spacing_mm``, ``line_widths_mm`` /
        ``line_heights_mm`` (measured extents at the final sizes),
        ``measured`` (False on the no-OpenSCAD estimate path), and
        ``notes`` (human-readable fit decisions worth surfacing).
    :raises TextDoesNotFitError: when a line cannot fit legibly.
    """
    from kiln.emboss_generator import (
        TextMeasureError,
        ellipse_fit_scale,
        face_inscribed_profile,
        measure_text_block_mm,
    )

    if not lines:
        return {
            "font_sizes_mm": [],
            "offsets_mm": [],
            "line_spacing_mm": 0.0,
            "line_widths_mm": [],
            "line_heights_mm": [],
            "measured": False,
            "notes": [],
        }

    n = len(lines)
    if min_size_mm is None:
        min_size_mm = _FDM_TEXT_LEGIBILITY_FLOOR_MM

    # Default hierarchy: primary 1.0, each subsequent 70% of prior —
    # the "name big, title smaller, division smallest" pattern of real
    # nameplates / business cards / awards plaques.
    if hierarchy is None:
        hierarchy = [0.7 ** i for i in range(n)]
    if len(hierarchy) < n:
        hierarchy = list(hierarchy) + [hierarchy[-1] * 0.7] * (n - len(hierarchy))
    hierarchy = hierarchy[:n]

    face_w = face["width_mm"]
    face_h = face["height_mm"]

    # Per-line glyph extents per mm of font size — measured when the
    # probe is available, the legacy char-aspect estimate otherwise.
    measured = True
    per_mm: list[tuple[float, float]] = []  # (width/mm, height/mm)
    for line in lines:
        if not line:
            per_mm.append((0.0, 0.0))
            continue
        try:
            w48, h48, _, _ = measure_text_block_mm(line, font, 48.0)
            per_mm.append((w48 / 48.0, h48 / 48.0))
        except TextMeasureError:
            measured = False
            per_mm.append((len(line) * 0.6, 1.0))

    notes: list[str] = []
    if not measured:
        notes.append(
            "OpenSCAD probe unavailable — text sized from the "
            "0.6-per-char estimate instead of measured glyphs."
        )

    # Usable box mirrors the engine's own target computation: the scale
    # fraction of the face, never closer than min_edge_margin_mm to an
    # edge.  The 0.85 visual margin then applies INSIDE that box — the
    # professional-desk-sign breathing room around the text.  (0.95 was
    # flagged as visually crammed by the 2026-05-03 nameplate feedback.)
    usable_w = min(face_w * line_scale, max(face_w - 2.0 * min_edge_margin_mm, 1.0))
    usable_h = min(face_h * line_scale, max(face_h - 2.0 * min_edge_margin_mm, 1.0))
    budget_w = usable_w * 0.85
    budget_h_primary = (usable_h / n) * 0.85

    # Primary size: the largest that satisfies every line's width budget
    # at its ratio, and the primary strip's height budget.
    primary = float("inf")
    for (w1, _h1), ratio in zip(per_mm, hierarchy, strict=False):
        if w1 > 0 and ratio > 0:
            primary = min(primary, budget_w / (w1 * ratio))
    h1_primary = per_mm[0][1] if per_mm[0][1] > 0 else 1.0
    primary = min(primary, budget_h_primary / h1_primary)
    if primary == float("inf"):
        primary = budget_h_primary  # all-empty lines; nothing to fit

    sizes = [primary * hierarchy[i] for i in range(n)]

    def _spacing(current_sizes: list[float]) -> float:
        if line_spacing_mm > 0:
            return line_spacing_mm
        # Baseline-to-baseline 1.4x for clean reads, from font sizes.
        return sum(current_sizes) * 1.4 / max(1, n)

    def _offsets(spacing: float) -> list[float]:
        if n == 1:
            return [0.0]
        total = (n - 1) * spacing
        top = total / 2.0
        return [top - i * spacing for i in range(n)]

    spacing = _spacing(sizes)
    offsets = _offsets(spacing)

    # Rim fit on elliptical faces: the corners of each line's run must
    # sit inside the inscribed ellipse at that line's band.  One shared
    # shrink factor keeps the hierarchy exact; shrinking also pulls the
    # (auto) offsets inward, so a single conservative pass suffices.
    profile = face_inscribed_profile(face)
    if profile is not None:
        rim_k = 1.0
        for (w1, h1), size, off in zip(per_mm, sizes, offsets, strict=False):
            if w1 <= 0 or size <= 0:
                continue
            rim_k = min(
                rim_k,
                ellipse_fit_scale(
                    profile[0], profile[1],
                    w1 * size / 2.0, h1 * size / 2.0,
                    0.0, off,
                ),
            )
        if rim_k <= 0.0:
            raise TextDoesNotFitError({
                "fits": False,
                "font_size_mm": 0.0,
                "text_width_mm": 0.0,
                "constraint": "width",
                "warnings": [
                    f"Text stack does not fit the round "
                    f"{face_w:.0f}x{face_h:.0f}mm face at any size."
                ],
                "suggestions": [
                    "Use fewer lines",
                    "Use a larger product",
                ],
            })
        if rim_k < 0.999:
            # 0.5% cushion inside the rim so the engine's own rim guard
            # (same math, re-run on rounded font sizes) never re-fires.
            rim_k *= 0.995
            sizes = [s * rim_k for s in sizes]
            spacing = _spacing(sizes)
            offsets = _offsets(spacing)
            notes.append(
                f"Round face: text sized to the inscribed width at its "
                f"band ({rim_k:.2f}x of the box fit) so no glyph corner "
                f"crosses the rim."
            )

    # Hard refusals on the FINAL numbers — the floor check must see what
    # will actually ship, not an optimistic estimate.
    for i, (line, size, (w1, _h1)) in enumerate(
        zip(lines, sizes, per_mm, strict=False)
    ):
        if not line:
            continue
        text_w = w1 * size
        if text_w > face_w * 0.95:
            raise TextDoesNotFitError({
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
            })
        if size < min_size_mm:
            per_char = (w1 / len(line)) if len(line) else 0.6
            max_chars_at_floor = (
                int(budget_w / (min_size_mm * per_char)) if per_char > 0 else 0
            )
            raise TextDoesNotFitError({
                "fits": False,
                "font_size_mm": size,
                "text_width_mm": text_w,
                "constraint": "min_floor",
                "warnings": [
                    f"Line {i + 1} ({line!r}) sized to {size:.1f}mm "
                    f"— below the {min_size_mm}mm FDM legibility floor."
                ],
                "suggestions": [
                    f"Shorten this line to <={max_chars_at_floor} chars",
                    f"Use a larger product or fewer lines "
                    f"(current: {n} lines x {face_h:.0f}mm height)",
                ],
            })
        # Tight-but-fits — a soft note callers can surface.
        if text_w > face_w * 0.85:
            notes.append(
                f"Line {i + 1} ({line!r}) at {size:.1f}mm uses "
                f"{text_w / face_w * 100:.0f}% of face width — tight "
                f"fit, no margin for material expansion."
            )

    return {
        "font_sizes_mm": sizes,
        "offsets_mm": offsets,
        "line_spacing_mm": spacing,
        "line_widths_mm": [w1 * s for (w1, _h1), s in zip(per_mm, sizes, strict=False)],
        "line_heights_mm": [h1 * s for (_w1, h1), s in zip(per_mm, sizes, strict=False)],
        "measured": measured,
        "notes": notes,
    }


def emboss_text_on_face(
    body_stl: str,
    text: str,
    *,
    face_name: str | None = None,
    mode: str = "emboss",
    depth_mm: float | None = None,
    nozzle_diameter_mm: float = 0.4,
    scale: float = 0.7,
    min_edge_margin_mm: float = 4.0,
    offset_x_mm: float = 0.0,
    offset_y_mm: float = 0.0,
    font: str = "Liberation Sans:style=Bold",
    font_size_mm: float = 0.0,
    output_dir: str | None = None,
    output_stl: str | None = None,
    emit_post_flip_preview: bool = True,
    collect_warnings: list[str] | None = None,
) -> str:
    """Apply a single line of text as emboss/deboss onto a face of an STL.

    Routes through :func:`kiln.emboss_generator.generate_emboss_scad`
    (face-normal-aware rotation + auto-sizing) and
    :func:`kiln.emboss_generator.compile_embossed_model` (compile to
    STL).  Detects the target face via
    :func:`kiln.surface_intelligence.resolve_decoratable_face` — the named
    face when *face_name* is given, otherwise the largest flat face (the
    canvas the calling product built).

    :param body_stl: Path to the host STL the text gets applied to.
    :param text: The text to emboss/deboss.  Single line only.  For
        multiple lines use :func:`emboss_text_lines_on_face` — chaining
        this helper per line with the default auto-sizing maxes EACH
        line independently to fill the face, so a short narrow-glyph
        line renders larger than the primary above it (measured
        2026-08-08: "IIIII" at 28.1mm under "WWWW" at 9.35mm).  Only the
        multi-line helper knows the hierarchy and can size lines
        together.
    :param face_name: ``"top"`` / ``"bottom"`` / ``"front"`` /
        ``"back"`` / ``"left"`` / ``"right"`` to target a cardinal face.
        ``None`` means auto-detect the largest flat face.
    :param mode: ``"emboss"`` (raised) or ``"deboss"`` (recessed).
    :param depth_mm: Depth of the emboss/deboss in mm.  ``None``
        (default) means the helper uses the printer-specific
        legibility floor (``nozzle_diameter_mm × 3``) — caller intent
        is "make it just legible."  Pass an explicit value greater
        than the floor for a deeper engraving (e.g. premium
        nameplates).  Values BELOW the floor raise
        :class:`DepthBelowLegibilityFloor`.
    :param nozzle_diameter_mm: The active printer's nozzle diameter
        in mm.  Default 0.4 matches the Bambu A1 / most consumer
        printers.  A 0.6mm Prusa MK4 nozzle bumps the floor to
        1.8mm; a 0.25mm precision nozzle drops it to 0.75mm.
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
    :param collect_warnings: Optional caller-provided list; any engine
        fit/clamp warnings (explicit size clamped, offset clamped,
        rim-fit on a round face) are appended to it AND logged — the
        engine's verdicts must never vanish between here and the user.
    :returns: Absolute path to the new STL with text applied.
    :raises FileNotFoundError: If *body_stl* doesn't exist.
    :raises ValueError: If face detection fails.
    :raises DepthBelowLegibilityFloor: If *depth_mm* is below
        ``nozzle_diameter_mm × 3``.
    :raises RuntimeError: If OpenSCAD compilation fails.
    """
    from kiln.emboss_generator import compile_embossed_model, generate_emboss_scad
    from kiln.surface_intelligence import resolve_decoratable_face

    if not os.path.isfile(body_stl):
        raise FileNotFoundError(f"Host STL not found: {body_stl}")

    # 0. Resolve depth against the printer-specific legibility floor.
    # ``depth_mm=None`` means "use the floor as the depth" — the most
    # common caller intent ("just make it legible on my printer").
    # Explicit depths below the floor raise; depths at-or-above the
    # floor pass through unchanged.
    floor_mm = _depth_legibility_floor_mm(nozzle_diameter_mm)
    if depth_mm is None:
        depth_mm = floor_mm
    elif depth_mm < floor_mm - 1e-6:  # 1µm tolerance for float quirks
        raise DepthBelowLegibilityFloor(
            requested_mm=depth_mm,
            floor_mm=floor_mm,
            nozzle_diameter_mm=nozzle_diameter_mm,
        )

    # 1. Detect target face.  Auto here means LARGEST, not top-first: the
    # products calling this build a canvas and hand it over unnamed — a
    # wedge nameplate's angled face is the whole product and is largest by
    # area, while its literal top is a millimetre-tall edge.
    face = resolve_decoratable_face(body_stl, face_name, auto="largest")

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

    # 2b. Smart-flip selection for bottom-face engravings.  The engine
    # always emits a face-aligning rotation (``rotate([180, 0, 0])``
    # for the bottom face); for tall-narrow faces where the user's
    # natural physical flip is around the long Y axis, we additionally
    # inject ``mirror([1, 0, 0])`` between the rotation and the text
    # extrude so the engraving reads correctly after that flip.  See
    # :func:`select_bottom_face_flip` for the aspect-ratio decision +
    # mandatory geometric self-inspection.
    is_bottom_face = face.get("normal", [0, 0, 1])[2] < -0.9
    additional_pre_text_transform = ""
    if is_bottom_face:
        flip_decision = select_bottom_face_flip(
            face_width_mm=face.get("width_mm", 0.0),
            face_height_mm=face.get("height_mm", 0.0),
        )
        if flip_decision["transformation"] == "mirror([1, 0, 0])":
            # Engine's outer rotation handles the face alignment; the
            # mirror is the SUPPLEMENT that flips text for the long-Y
            # physical flip.  ``select_bottom_face_flip`` only returns
            # this for genuinely tall-narrow faces — wide-shallow + near-
            # square stay on the engine's default (no mirror).
            additional_pre_text_transform = "mirror([1, 0, 0])"

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
        min_edge_margin_mm=min_edge_margin_mm,
        additional_pre_text_transform=additional_pre_text_transform,
    )

    # 3b. Surface the engine's fit verdicts.  A clamp (explicit size too
    # big, offset off the face, rim-fit on a round face) is information
    # the user acted on — swallowing it here is how a silently-resized
    # engraving reaches a printer.  Log always; hand to the caller's
    # sink when one was provided.
    for engine_warning in scad_result.get("warnings") or []:
        _logger.warning("emboss_text_on_face(%r): %s", text, engine_warning)
        if collect_warnings is not None:
            collect_warnings.append(engine_warning)

    # 4. Compile to STL
    compile_result = compile_embossed_model(scad_result["scad_path"], output_stl)
    if not compile_result.get("success"):
        raise RuntimeError(
            f"Emboss compilation failed for text {text!r}: "
            f"{compile_result.get('error', 'unknown error')}"
        )

    # 5. MANDATORY post-flip preview for bottom-face engravings.
    # The engraving is invisible in the print-orientation view (it
    # lives on the underside), so the inspection-bundle preview
    # surface would mislead the human approver.  Render a separate
    # PNG showing what the user sees AFTER the natural physical
    # flip, save it alongside the STL.  Best-effort: render
    # failures do not block the emboss — the STL is the load-
    # bearing artifact.  Pattern mirrors :func:`kiln.model_visualizer`'s
    # OpenSCAD render path.
    #
    # Suppressed by multi-line callers so the chained per-line emboss
    # path doesn't render the preview once per line — the
    # :func:`emboss_text_lines_on_face` wrapper does a single render
    # against the final cumulative STL.
    is_bottom_face = face.get("normal", [0, 0, 1])[2] < -0.9
    if emit_post_flip_preview and is_bottom_face and output_dir:
        flip_decision = select_bottom_face_flip(
            face_width_mm=face.get("width_mm", 0.0),
            face_height_mm=face.get("height_mm", 0.0),
        )
        _render_post_flip_preview(
            decorated_stl=output_stl,
            flip_axis=flip_decision["flip_axis"],
            output_dir=output_dir,
        )

    return output_stl


def emboss_text_lines_on_face(
    body_stl: str,
    lines: list[str],
    *,
    face_name: str | None = None,
    mode: str = "emboss",
    depth_mm: float | None = None,
    nozzle_diameter_mm: float = 0.4,
    line_scale: float = 0.7,
    min_edge_margin_mm: float = 4.0,
    line_spacing_mm: float = 0.0,
    output_dir: str | None = None,
    hierarchy: list[float] | None = None,
    collect_warnings: list[str] | None = None,
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

    Sizing is MEASURED (see :func:`compute_text_line_layout` and the
    module-level SIZING CONTRACT): every line's font size comes from the
    engine's real glyph metrics with the 0.85 visual margin applied, so
    the size this helper computes is the size that ships — the engine
    never has to re-fit, the margin survives to the mesh, and the
    hierarchy can never invert.  Elliptical faces (coasters, oval
    trays) are fitted against their real inscribed width at each line's
    band, never the square bounding box.

    Each line is positioned at a different *offset_y_mm* relative to
    the face centre — top line at +line_spacing/2 if 2 lines, etc.

    For per-line scale customisation pass ``hierarchy=[1.0, 0.6, 0.4]``
    or call :func:`emboss_text_on_face` directly per line.

    :param depth_mm: Depth of the emboss/deboss in mm.  ``None``
        (default) uses the printer-specific legibility floor
        (``nozzle_diameter_mm × 3``) — caller intent is "make it just
        legible."  Explicit depths BELOW the floor raise
        :class:`DepthBelowLegibilityFloor`.  Pass an explicit value
        above the floor for a deeper engraving.
    :param nozzle_diameter_mm: Active printer's nozzle diameter in mm.
        Default 0.4 matches the Bambu A1 / most consumer printers.
    :param hierarchy: Per-line size multipliers relative to *line_scale*.
        Defaults to ``[1.0]`` for 1 line, ``[1.0, 0.7]`` for 2 lines,
        ``[1.0, 0.7, 0.5]`` for 3+ lines (recursive 0.7^i ratio).
    :param collect_warnings: Optional caller-provided list; layout notes
        (round-face fitting, tight fits, degraded estimate mode) and any
        engine clamp warnings are appended to it, so nothing the
        pipeline decides is silent.  Everything is also logged.
    :returns: Final STL path with all lines applied.
    :raises DepthBelowLegibilityFloor: If *depth_mm* is below
        ``nozzle_diameter_mm × 3``.
    :raises TextDoesNotFitError: If a line cannot fit the face legibly
        (see :func:`compute_text_line_layout`).
    """
    if not lines:
        return body_stl

    # Resolve depth against the printer-specific legibility floor —
    # same contract as :func:`emboss_text_on_face`.  Done once at the
    # top so all per-line calls share the same depth and the same
    # error path.
    floor_mm = _depth_legibility_floor_mm(nozzle_diameter_mm)
    if depth_mm is None:
        depth_mm = floor_mm
    elif depth_mm < floor_mm - 1e-6:  # 1µm tolerance for float quirks
        raise DepthBelowLegibilityFloor(
            requested_mm=depth_mm,
            floor_mm=floor_mm,
            nozzle_diameter_mm=nozzle_diameter_mm,
        )

    # Resolve the face once so the layout math sees the same face every
    # per-line emboss call will target.
    from kiln.surface_intelligence import resolve_decoratable_face

    face = resolve_decoratable_face(body_stl, face_name, auto="largest")

    # All sizing decisions — measured glyph metrics, the 0.85 visual
    # margin, hierarchy ratios, elliptical-face inscribed fitting, and
    # the legibility-floor refusal — live in compute_text_line_layout.
    # The sizes it returns fit the engine's box by construction, so the
    # engine honours them verbatim: requested size == shipped size.
    layout = compute_text_line_layout(
        lines,
        face=face,
        line_scale=line_scale,
        min_edge_margin_mm=min_edge_margin_mm,
        hierarchy=hierarchy,
        line_spacing_mm=line_spacing_mm,
        font=kwargs.get("font", "Liberation Sans:style=Bold"),
    )
    per_line_sizes = layout["font_sizes_mm"]
    offsets = layout["offsets_mm"]
    for note in layout["notes"]:
        _logger.warning("emboss_text_lines: %s", note)
    if collect_warnings is not None:
        collect_warnings.extend(layout["notes"])

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
            nozzle_diameter_mm=nozzle_diameter_mm,
            scale=line_scale,
            min_edge_margin_mm=min_edge_margin_mm,
            offset_y_mm=offset,
            font_size_mm=per_line_sizes[i],
            output_dir=output_dir,
            output_stl=(
                os.path.join(output_dir, f"line_{i}.stl") if output_dir else None
            ),
            # Suppress per-line post-flip rendering — we render once
            # below against the final cumulative STL.  Skipping
            # intermediate renders saves ~1s per line of OpenSCAD time.
            emit_post_flip_preview=False,
            collect_warnings=collect_warnings,
            **kwargs,
        )

    # MANDATORY post-flip preview for bottom-face engravings — same
    # contract as the single-line helper.  Done once against the
    # final cumulative STL so the rendered PNG shows the COMPLETE
    # multi-line layout, not just the first line.
    if face_name == "bottom" or (
        face_name is None
        and current != body_stl
        and output_dir
    ):
        # Resolve the actual face dict if we auto-detected, so the
        # aspect-ratio decision uses the real face's geometry.
        from kiln.surface_intelligence import resolve_decoratable_face

        resolved_face = resolve_decoratable_face(body_stl, face_name, auto="largest")
        is_bottom_face = resolved_face.get("normal", [0, 0, 1])[2] < -0.9
        if is_bottom_face and output_dir:
            flip_decision = select_bottom_face_flip(
                face_width_mm=resolved_face.get("width_mm", 0.0),
                face_height_mm=resolved_face.get("height_mm", 0.0),
            )
            _render_post_flip_preview(
                decorated_stl=current,
                flip_axis=flip_decision["flip_axis"],
                output_dir=output_dir,
            )

    return current
