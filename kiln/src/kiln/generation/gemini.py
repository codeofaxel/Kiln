"""Google Gemini Deep Think 3D generation provider.

Uses Google's Gemini API with extended thinking to generate printable
3D models from text descriptions or images (photos, sketches, napkin
drawings).  Gemini reasons deeply about the geometry ("deep think") and
produces OpenSCAD code, which is then compiled locally to STL.

This two-stage pipeline (AI reasoning -> local compilation) produces
precise, parametric, watertight meshes ideal for 3D printing.

Supported input modes
---------------------
- **Text-to-3D**: Natural language description -> OpenSCAD -> STL
- **Image-to-3D**: Photo/sketch/napkin drawing + optional text -> OpenSCAD -> STL

Authentication
--------------
Set ``KILN_GEMINI_API_KEY`` or pass ``api_key`` to the constructor.

Model selection
---------------
Defaults to ``gemini-2.5-flash`` (works on free tier with Deep Think).
Set ``KILN_GEMINI_MODEL=gemini-2.5-pro`` for deeper reasoning (requires
paid plan).  If the configured model hits rate limits, automatically
falls back to ``gemini-2.5-flash``.  Supports the full Gemini model
family including 3.x series.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any

import requests

from kiln.generation.base import (
    GenerationAuthError,
    GenerationError,
    GenerationJob,
    GenerationProvider,
    GenerationResult,
    GenerationStatus,
)
from kiln.generation.visual_verify import VerificationResult, VisualVerifier

logger = logging.getLogger(__name__)

_GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_DEFAULT_MODEL = os.environ.get("KILN_GEMINI_MODEL", "").strip() or "gemini-2.5-flash"
_FALLBACK_MODEL = "gemini-2.5-flash"  # Always available on free tier
_REQUEST_TIMEOUT = 180  # Thinking models need more time than standard models
_MACOS_APP_PATH = "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD" if sys.platform == "darwin" else ""

_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2.0  # seconds: 2, 4, 8

# System prompt — instructs Gemini to leverage deep thinking for geometry
_SYSTEM_PROMPT = """\
You are an expert 3D modeling engineer specializing in creating precise, \
printable 3D models using OpenSCAD.

When given a text description, image, sketch, or napkin drawing of a 3D object, \
generate valid OpenSCAD code that faithfully recreates the described or depicted object.

Think step-by-step about the geometry:
1. Decompose the object into primitive shapes and boolean operations
2. Reason about exact dimensions, proportions, and spatial relationships
3. Consider symmetry, repetition, and structural patterns
4. Plan the construction order (what gets unioned, differenced, intersected)
5. Verify the result will be watertight and printable

Requirements:
- Output ONLY valid OpenSCAD code — no explanations, no markdown fences, no prose
- The model MUST be watertight (manifold) for 3D printing
- Use millimeters as the unit
- Center the model at the origin when practical
- Provide a flat bottom surface for stable printing
- Target practical 3D printing size (typically 20-200mm)
- Use $fn between 32-64 for curves (balance quality vs render time)
- Use difference(), union(), intersection() for complex shapes
- Use hull() and minkowski() for organic or rounded shapes
- Use linear_extrude() and rotate_extrude() for 2D-to-3D operations
- Do NOT use import(), surface(), include, or use statements
- Minimize extreme overhangs (>60 degrees) where possible
- For mechanical parts: ensure tolerances (0.2-0.4mm clearance for fits)
- For decorative parts: add fillets/chamfers where aesthetically appropriate

If given an image or sketch:
- Identify the object(s) depicted and their spatial relationships
- Estimate proportions from the image even if dimensions aren't labeled
- Reproduce the essential geometry — prioritize structural accuracy over ornament
- Note if the sketch is ambiguous and choose the most printable interpretation

You have access to the following pre-defined OpenSCAD modules. Use them instead of \
implementing complex geometry from scratch:

PRIMITIVES (fundamental shapes beyond OpenSCAD built-ins):
  cone(r=10, h=20, center=false)
    - Solid cone
  pyramid(base=20, h=15, sides=4, center=false)
    - Regular n-sided pyramid (4=square, 3=triangular, 6=hex)
  torus(R=15, r=5)
    - Donut / ring shape (R=major radius, r=tube radius)
  tube(od=20, id=14, h=30)
    - Hollow cylinder / pipe
  hemisphere(r=15, solid=true)
    - Half sphere, flat side down (solid=false for dome shell)
  capsule(r=5, h=20)
    - Pill / capsule — cylinder with hemispherical ends
  wedge(width=20, depth=30, height=15)
    - Right-angle wedge / ramp
  chamfered_box(width=30, depth=20, height=15, chamfer=2)
    - Box with 45-degree chamfered edges
  countersunk_hole(d=5, depth=10, head_d=10, head_angle=90)
    - Countersunk screw hole (use with difference)
  standoff(od=8, id=3, h=10, base_d=12, base_h=2)
    - Mounting standoff / spacer with optional base flange
  washer(od=12, id=6, h=2)
    - Flat washer / ring
  hex_nut(af=10, h=5, bore=5)
    - Hexagonal nut shape (across-flats dimension)
  arrow_2d(length=30, head_width=15, head_length=10, shaft_width=6)
    - 2D arrow shape (linear_extrude to 3D)
  u_channel(width=20, height=15, length=50, wall=2)
    - U-shaped channel / track
  l_bracket(width=20, height=30, depth=30, thickness=3)
    - Simple L-shaped bracket

HONEYCOMB & PATTERNS:
  honeycomb_wall(width, height, thickness, cell_size, wall_thickness=1.2)
    - Flat honeycomb panel with hex cutouts
  honeycomb_cylinder(od, height, cell_size, wall_thickness=1.2, base_height=3)
    - Cylindrical honeycomb (e.g., pencil holder with hex pattern)

LATTICE:
  lattice_cylinder(od, id, height, strut_width=1.5, strut_count=12, ring_count=6)
  lattice_box(width, depth, height, strut_width=1.5, cell_size=10)
  grid_pattern(width, height, rows, cols, bar_width=1.2)

MECHANICAL:
  snap_fit_clip(width, thickness, cantilever_length, gap=0.3, deflection=0.8)
  threaded_hole(diameter, pitch, depth, starts=1)
  knurl(diameter, height, pitch=1.5, depth=0.5)
  dovetail(width, height, depth, angle=15)
  living_hinge(length, width, n_cuts=10, kerf=0.8, bridge=2)

DECORATIVE:
  rounded_box(width, depth, height, radius=2)
  shell(width, depth, height, wall=1.6, radius=2)
  fillet_base(width, depth, height, fillet_r=3)
  text_emboss(text_str, size=10, depth=1, font="Liberation Sans")
  star(points=5, outer_r=20, inner_r=10, height=5)

VORONOI:
  voronoi_panel(width, height, thickness, n_seeds=20, seed=42)

GEARS:
  spur_gear(teeth=20, mod=2, pressure_angle=20, thickness=5, bore=0)
    - Parametric involute spur gear
  herringbone_gear(teeth=20, mod=2, pressure_angle=20, thickness=10, bore=0, helix_angle=30)
    - Double-helical gear, stronger and self-aligning
  gear_profile_2d(teeth=20, mod=2, pressure_angle=20)
    - 2D gear profile helper
  rack_gear(length=50, mod=2, height=10, thickness=5)
    - Linear rack gear for rack-and-pinion assemblies

THREADS:
  external_thread(diameter=10, length=20, pitch=1.5, starts=1)
    - Printable external (male) thread
  internal_thread(diameter=10, length=20, pitch=1.5, wall=3)
    - Printable internal (female) thread
  bottle_thread(outer_diameter=30, height=10, pitch=3, wall=2)
    - Wide-pitch bottle-cap style thread

PRACTICAL (everyday useful shapes):
  cable_clip(diameter=6, wall=2, base_width=12, base_height=3, screw_hole=3)
    - Cable management clip with screw mount
  wall_hook(width=20, depth=30, height=40, thickness=4, hook_depth=15, hook_gap=8, screw_hole=4)
    - Wall-mountable hook / hanger with reinforcement
  phone_stand(width=75, depth=60, height=80, thickness=4, angle=65, slot_width=12)
    - Angled phone / tablet stand with device slot
  shelf_bracket(width=20, depth=80, height=80, thickness=4, gussets=2)
    - L-shaped shelf bracket with reinforcing gussets
  pipe_clamp(od=25, wall=3, gap=3, ear_width=12, bolt_hole=4)
    - Two-ear pipe / tube clamp with bolt holes
  pegboard_hook(hole_spacing=25.4, peg_d=5, hook_length=40, hook_drop=25, width=8, thickness=4)
    - Hook for standard pegboard (25.4mm hole spacing)
  funnel(top_d=60, bottom_d=12, height=50, wall=1.5, spout_h=15)
    - Conical funnel with tubular spout and rim

CONTAINERS:
  box_with_lid(width=60, depth=40, height=30, wall=2, lid_height=8, tolerance=0.3)
    - Parametric box with snap-on lid, side-by-side for printing
  rounded_box_simple(w, d, h, wall, r=2)
    - Helper: hollow box with Minkowski-rounded edges
  screw_container(outer_d=40, height=50, wall=2, thread_pitch=3)
    - Cylindrical container with external thread collar
  divider_grid(width=100, depth=80, height=30, rows=2, cols=3, wall=1.5)
    - Rectangular organizer grid / divider tray
  stackable_bin(width=80, depth=60, height=40, wall=2, stack_lip=3)
    - Stackable storage bin with interlocking lip

These modules are automatically available — do NOT use `use` or `include` to load them. \
Simply call them directly in your code.

IMPORTANT: You MUST use these library modules whenever the user's request matches their \
purpose. Before writing any code, scan the prompt for keywords that match library modules:
- "cone", "conical" → use cone()
- "pyramid" → use pyramid()
- "torus", "donut", "ring shape" → use torus()
- "tube", "pipe", "hollow cylinder" → use tube()
- "hemisphere", "half sphere", "dome" → use hemisphere()
- "capsule", "pill", "rounded cylinder" → use capsule()
- "wedge", "ramp", "incline" → use wedge()
- "chamfered box", "chamfer" → use chamfered_box()
- "countersunk", "counterbore", "screw recess" → use countersunk_hole()
- "standoff", "spacer", "mounting post" → use standoff()
- "washer", "flat ring" → use washer()
- "hex nut", "hexagonal nut", "nut shape" → use hex_nut()
- "arrow", "pointer", "direction indicator" → use arrow_2d() with linear_extrude
- "channel", "U channel", "track", "rail" → use u_channel()
- "honeycomb", "hex pattern", "hexagonal" → use honeycomb_wall() or honeycomb_cylinder()
- "lattice", "strut", "cage" → use lattice_cylinder() or lattice_box()
- "snap fit", "clip", "cantilever" → use snap_fit_clip()
- "thread", "screw", "bolt" → use threaded_hole()
- "knurl", "grip", "textured surface" → use knurl()
- "dovetail", "joint" → use dovetail()
- "living hinge", "hinge", "flexible", "fold" → use living_hinge()
- "rounded box", "rounded edges", "fillet" → use rounded_box() or fillet_base()
- "shell", "hollow", "container" → use shell()
- "voronoi", "organic pattern" → use voronoi_panel()
- "star", "star shape" → use star()
- "text", "emboss", "engrave", "label" → use text_emboss()
- "gear", "spur gear", "cog", "pinion" → use spur_gear() or herringbone_gear()
- "thread", "screw", "bolt thread", "bottle cap" → use external_thread() or bottle_thread()
- "cable clip", "cable holder", "cord organizer" → use cable_clip()
- "hook", "wall hook", "hanger", "coat hook" → use wall_hook()
- "phone stand", "tablet stand", "device holder" → use phone_stand()
- "shelf bracket", "bracket", "L bracket" → use shelf_bracket()
- "pipe clamp", "tube clamp", "hose clamp" → use pipe_clamp()
- "pegboard", "pegboard hook", "tool holder" → use pegboard_hook()
- "funnel", "pour spout" → use funnel()
- "box with lid", "container", "storage box" → use box_with_lid() or screw_container()
- "divider", "organizer", "grid compartment" → use divider_grid()
- "stackable", "stacking bin" → use stackable_bin()

Do NOT simplify or skip complex features. If the user asks for a honeycomb pattern, \
you MUST produce visible honeycomb cells. If they ask for a living hinge, you MUST \
include the slit pattern. A plain box is NEVER acceptable when a patterned feature \
was requested. These library modules are tested and guaranteed to produce manifold output."""

# Supported image MIME types for multimodal input
_SUPPORTED_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def _find_openscad(explicit_path: str | None = None) -> str:
    """Locate the OpenSCAD binary.

    :param explicit_path: If provided, verify it exists and is executable.
    :returns: Absolute path to the OpenSCAD binary.
    :raises GenerationError: If no binary is found.
    """
    if explicit_path:
        if os.path.isfile(explicit_path) and os.access(explicit_path, os.X_OK):
            return explicit_path
        raise GenerationError(
            f"OpenSCAD binary not found at {explicit_path}",
            code="OPENSCAD_NOT_FOUND",
        )

    which = shutil.which("openscad")
    if which:
        return which

    if _MACOS_APP_PATH and os.path.isfile(_MACOS_APP_PATH) and os.access(_MACOS_APP_PATH, os.X_OK):
        return _MACOS_APP_PATH

    raise GenerationError(
        "OpenSCAD not found. Gemini Deep Think requires OpenSCAD to compile generated code.\n"
        "  Linux/WSL: apt install openscad\n"
        "  macOS: brew install openscad\n"
        "  Or download from https://openscad.org",
        code="OPENSCAD_NOT_FOUND",
    )


def _extract_openscad_code(text: str) -> str:
    """Extract OpenSCAD code from Gemini response.

    Handles responses that may include markdown code fences or
    plain OpenSCAD code.
    """
    # Try to extract from markdown code fence first
    match = re.search(r"```(?:openscad|scad)?\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()

    # If no code fence, try to find OpenSCAD-like content
    # Look for lines containing OpenSCAD keywords
    lines = text.strip().split("\n")
    scad_lines: list[str] = []
    in_code = False
    for line in lines:
        stripped = line.strip()
        # Detect start of OpenSCAD code
        if not in_code and re.match(
            r"^(//|/\*|\$|cube|sphere|cylinder|translate|rotate|scale|union|difference|intersection|module|linear_extrude|rotate_extrude|polygon|circle|square|hull|minkowski|color|mirror|resize|offset|text|polyhedron|for|if|let|function|echo|import|surface|include|use)",
            stripped,
        ):
            in_code = True
        if in_code:
            scad_lines.append(line)

    if scad_lines:
        return "\n".join(scad_lines).strip()

    # Last resort: return the entire text and let OpenSCAD error if invalid
    return text.strip()


# Block dangerous OpenSCAD functions that could access the filesystem
_DANGEROUS_PATTERNS = [
    r"\bimport\s*\(",
    r"\bsurface\s*\(",
    r"\binclude\s*<",
    r"\buse\s*<",
]


def _check_scad_safety(code: str) -> str | None:
    """Check OpenSCAD code for dangerous filesystem operations.

    :param code: OpenSCAD source code to validate.
    :returns: Error message if dangerous patterns found, else ``None``.
    """
    for pattern in _DANGEROUS_PATTERNS:
        if re.search(pattern, code, re.IGNORECASE):
            return "Generated code contains blocked file I/O operations (import/surface/include/use)."
    return None


def _get_thinking_config(model: str) -> dict[str, Any]:
    """Return the appropriate thinking configuration for the model family.

    Gemini 3.x uses ``thinkingLevel`` ("minimal", "low", "medium", "high").
    Gemini 2.5 uses ``thinkingBudget`` (integer token count, max 24576).

    :param model: The Gemini model ID string.
    :returns: Dict to merge into ``generationConfig.thinkingConfig``.
    """
    if model.startswith("gemini-3"):
        # Gemini 3.x family — use thinkingLevel for best results
        return {"thinkingLevel": "high"}

    # Gemini 2.5 family — use thinkingBudget (max = 24576 tokens)
    # High budget enables deepest geometric reasoning
    return {"thinkingBudget": 24576}


def _encode_image_file(path: str) -> dict[str, Any]:
    """Read and base64-encode a local image file for the Gemini API.

    :param path: Absolute or relative path to the image file.
    :returns: A Gemini ``inlineData`` part dict.
    :raises GenerationError: If the file doesn't exist or has an
        unsupported format.
    """
    if not os.path.isfile(path):
        raise GenerationError(
            f"Image file not found: {path}",
            code="IMAGE_NOT_FOUND",
        )

    ext = os.path.splitext(path)[1].lower()
    mime_type = _SUPPORTED_IMAGE_TYPES.get(ext)
    if not mime_type:
        # Fall back to mimetypes stdlib
        mime_type = mimetypes.guess_type(path)[0]
    if not mime_type or not mime_type.startswith("image/"):
        supported = ", ".join(sorted(_SUPPORTED_IMAGE_TYPES.keys()))
        raise GenerationError(
            f"Unsupported image format {ext!r}. Supported: {supported}",
            code="UNSUPPORTED_IMAGE_FORMAT",
        )

    with open(path, "rb") as fh:
        data = base64.standard_b64encode(fh.read()).decode("ascii")

    return {"inlineData": {"mimeType": mime_type, "data": data}}


class GeminiDeepThinkProvider(GenerationProvider):
    """Google Gemini Deep Think text-to-3D and image-to-3D generation.

    Uses Gemini's extended thinking capabilities to generate OpenSCAD code
    from natural language descriptions or images (photos, sketches, napkin
    drawings), then compiles it locally to STL.

    :param api_key: Google Gemini API key.  Falls back to
        ``KILN_GEMINI_API_KEY`` env var.
    :param model: Gemini model to use (default: ``gemini-2.5-flash``).
        Override with ``KILN_GEMINI_MODEL`` env var.
    :param openscad_path: Explicit path to the ``openscad`` binary.
    :param compile_timeout: Max OpenSCAD compilation time in seconds.
    """

    def __init__(
        self,
        api_key: str = "",
        *,
        model: str = _DEFAULT_MODEL,
        openscad_path: str | None = None,
        compile_timeout: int = 120,
    ) -> None:
        self._api_key = api_key or os.environ.get("KILN_GEMINI_API_KEY", "")
        if not self._api_key:
            raise GenerationAuthError(
                "Gemini API key required.  Set KILN_GEMINI_API_KEY or pass api_key.",
                code="AUTH_REQUIRED",
            )
        self._model = model
        self._openscad = _find_openscad(openscad_path)
        self._compile_timeout = compile_timeout
        self._session = requests.Session()
        self._jobs: dict[str, GenerationJob] = {}
        self._paths: dict[str, str] = {}
        self._prompts: dict[str, str] = {}
        self._scad_code: dict[str, str] = {}
        self._verification_scores: dict[str, VerificationResult] = {}
        self._request_count = 0  # Track Gemini API calls this session
        self._first_request_time: float | None = None

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def display_name(self) -> str:
        return "Gemini Deep Think"

    def get_verification_result(self, job_id: str) -> VerificationResult | None:
        """Return the visual verification result for a job, if available."""
        return self._verification_scores.get(job_id)

    def generate(
        self,
        prompt: str,
        *,
        format: str = "stl",
        style: str | None = None,
        verify: bool = True,
        **kwargs: Any,
    ) -> GenerationJob:
        """Generate a 3D model from text or image via Gemini + OpenSCAD.

        Stage 1: Gemini reasons deeply about the geometry and produces
        OpenSCAD code.  When an image is provided, Gemini uses multimodal
        understanding to interpret the visual and generate matching geometry.
        Stage 2: OpenSCAD compiles the code to STL locally.
        Stage 3 (optional): Visual verification via Gemini Vision scores how
        well the result matches the original prompt.  If the score is below
        the threshold, regenerates with feedback (up to 2 retries).

        :param prompt: Natural language description of the desired 3D model.
        :param format: Output format (only ``"stl"`` supported).
        :param style: Optional style hint (``"organic"``, ``"mechanical"``,
            ``"decorative"``).
        :param verify: Whether to run visual verification after generation
            (default ``True``).  Set to ``False`` to skip (``--no-verify``).
        :param image_path: (kwarg) Path to a local image file (photo, sketch,
            napkin drawing) for image-to-3D generation.
        :param output_dir: (kwarg) Directory for output files.
        :returns: :class:`GenerationJob` with ``SUCCEEDED`` or ``FAILED``
            status.
        """
        if format != "stl":
            raise GenerationError(
                f"Gemini Deep Think only supports STL output, got {format!r}.",
                code="UNSUPPORTED_FORMAT",
            )

        job_id = f"gemini-{uuid.uuid4().hex[:12]}"
        output_dir = kwargs.get(
            "output_dir",
            os.path.join(tempfile.gettempdir(), "kiln_generated"),
        )
        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(output_dir, f"{job_id}.stl")

        # Build the user prompt with optional style hint
        style_hint = ""
        if style:
            style_hint = f"\nStyle preference: {style}."

        user_prompt = (
            f"Create a 3D printable model of: {prompt}{style_hint}\n\n"
            f"Think carefully about the geometry, proportions, and printability. "
            f"Output valid OpenSCAD code only."
        )

        # Prepare optional image input for multimodal generation
        image_parts: list[dict[str, Any]] = []
        image_path = kwargs.get("image_path")
        if image_path:
            try:
                image_part = _encode_image_file(image_path)
                image_parts.append(image_part)
                logger.info(
                    "Gemini Deep Think: including image input from %s",
                    image_path,
                )
            except GenerationError:
                raise
            except Exception as exc:
                logger.warning("Failed to encode image %s: %s", image_path, exc)
                # Continue without image — text-only fallback

        # ---- Stage 1: Call Gemini API to generate OpenSCAD code ----
        try:
            raw_response = self._call_gemini(user_prompt, image_parts=image_parts)
        except GenerationError:
            raise
        except Exception as exc:
            job = GenerationJob(
                id=job_id,
                provider=self.name,
                prompt=prompt,
                status=GenerationStatus.FAILED,
                progress=0,
                created_at=time.time(),
                format=format,
                error=f"Gemini API call failed: {exc}",
            )
            self._jobs[job_id] = job
            return job

        # Extract OpenSCAD code from the response
        # NOTE: Safety check runs ONLY on extracted code, not the raw response.
        # The raw response may contain explanatory text with words like "import"
        # that would cause false-positive safety rejections.
        scad_code = _extract_openscad_code(raw_response)

        if not scad_code.strip():
            job = GenerationJob(
                id=job_id,
                provider=self.name,
                prompt=prompt,
                status=GenerationStatus.FAILED,
                progress=0,
                created_at=time.time(),
                format=format,
                error="Gemini returned no usable OpenSCAD code.",
            )
            self._jobs[job_id] = job
            return job

        # Safety check on the extracted code only
        safety_error = _check_scad_safety(scad_code)
        if safety_error:
            job = GenerationJob(
                id=job_id,
                provider=self.name,
                prompt=prompt,
                status=GenerationStatus.FAILED,
                progress=0,
                created_at=time.time(),
                format=format,
                error=safety_error,
            )
            self._jobs[job_id] = job
            return job

        self._scad_code[job_id] = scad_code

        # ---- Stage 2: Compile OpenSCAD code to STL (with self-healing) ----
        max_compile_retries = 2  # up to 2 retries = 3 total attempts
        compile_result = self._compile_scad(scad_code, out_path, job_id, prompt, format)

        for attempt in range(1, max_compile_retries + 1):
            if compile_result.status != GenerationStatus.FAILED:
                break
            if not compile_result.error or "compilation failed" not in compile_result.error.lower():
                break  # Only retry on compilation errors, not timeouts

            logger.info(
                "Gemini Deep Think: OpenSCAD compile failed (attempt %d/%d), "
                "retrying with error feedback...",
                attempt,
                max_compile_retries + 1,
            )

            # Feed the error back to Gemini for a corrected version
            retry_prompt = (
                f"The following OpenSCAD code failed to compile:\n\n"
                f"```openscad\n{scad_code}\n```\n\n"
                f"Compiler error:\n{compile_result.error}\n\n"
                f"Please fix the OpenSCAD code so it compiles successfully. "
                f"Output only the corrected OpenSCAD code.\n\n"
                f"Remember: you have access to pre-defined library modules "
                f"(honeycomb_wall, honeycomb_cylinder, lattice_cylinder, "
                f"lattice_box, grid_pattern, snap_fit_clip, threaded_hole, "
                f"knurl, dovetail, living_hinge, rounded_box, shell, "
                f"fillet_base, text_emboss, star, voronoi_panel, "
                f"cone, pyramid, torus, tube, hemisphere, capsule, wedge, "
                f"chamfered_box, countersunk_hole, standoff, washer, hex_nut, "
                f"u_channel, arrow_2d, l_bracket, "
                f"spur_gear, herringbone_gear, rack_gear, "
                f"external_thread, internal_thread, bottle_thread, "
                f"cable_clip, wall_hook, phone_stand, shelf_bracket, "
                f"pipe_clamp, pegboard_hook, funnel, "
                f"box_with_lid, screw_container, divider_grid, stackable_bin). "
                f"Use them instead of implementing complex geometry from scratch "
                f"if they match what you're trying to build — they are tested "
                f"and guaranteed to produce manifold output."
            )

            try:
                retry_response = self._call_gemini(retry_prompt)
            except Exception as exc:
                logger.warning("Gemini retry call failed: %s", exc)
                break

            # Extract and validate the retried code
            scad_code = _extract_openscad_code(retry_response)
            if not scad_code.strip():
                logger.warning("Retried code was empty, aborting retry.")
                break

            safety_error = _check_scad_safety(scad_code)
            if safety_error:
                logger.warning("Retried code failed safety check, aborting retry.")
                break

            self._scad_code[job_id] = scad_code

            # Clean up previous failed output
            if os.path.isfile(out_path):
                with contextlib.suppress(OSError):
                    os.unlink(out_path)

            compile_result = self._compile_scad(scad_code, out_path, job_id, prompt, format)

        # ---- Stage 3: Visual verification (optional) ----
        if verify and compile_result.status == GenerationStatus.SUCCEEDED:
            compile_result = self._run_visual_verification(
                compile_result,
                out_path=out_path,
                job_id=job_id,
                prompt=prompt,
                format=format,
                style=style,
                image_parts=image_parts,
            )

        return compile_result

    def get_job_status(self, job_id: str) -> GenerationJob:
        """Return the stored job state.

        Gemini Deep Think jobs are synchronous, so this simply returns
        the result from :meth:`generate`.
        """
        job = self._jobs.get(job_id)
        if not job:
            raise GenerationError(
                f"Job {job_id!r} not found.",
                code="JOB_NOT_FOUND",
            )
        return job

    def download_result(
        self,
        job_id: str,
        output_dir: str = os.path.join(tempfile.gettempdir(), "kiln_generated"),
    ) -> GenerationResult:
        """Return the path to the already-generated STL.

        For Gemini Deep Think, the file is generated synchronously during
        :meth:`generate`, so this just verifies the file exists.
        """
        path = self._paths.get(job_id)
        if not path or not os.path.isfile(path):
            raise GenerationError(
                f"No generated file for job {job_id!r}.",
                code="NO_RESULT",
            )

        prompt = self._prompts.get(job_id, "")

        return GenerationResult(
            job_id=job_id,
            provider=self.name,
            local_path=path,
            format="stl",
            file_size_bytes=os.path.getsize(path),
            prompt=prompt,
        )

    def get_scad_code(self, job_id: str) -> str | None:
        """Return the generated OpenSCAD source code for a job.

        Useful for debugging or iterating on the generated geometry.
        """
        return self._scad_code.get(job_id)

    def list_styles(self) -> list[str]:
        return ["organic", "mechanical", "decorative"]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    _MAX_VERIFY_RETRIES = 2  # Up to 2 regeneration attempts based on visual feedback

    def _run_visual_verification(
        self,
        compile_result: GenerationJob,
        *,
        out_path: str,
        job_id: str,
        prompt: str,
        format: str,
        style: str | None,
        image_parts: list[dict[str, Any]] | None,
    ) -> GenerationJob:
        """Run visual verification and optionally regenerate on low scores.

        Renders the STL to a PNG preview, sends it to Gemini Vision, and
        checks the score.  If the score is below the threshold, regenerates
        with the feedback as additional context (up to
        ``_MAX_VERIFY_RETRIES`` times).

        If verification itself fails (e.g. the STL can't be parsed,
        API error), the error is logged and the original result is returned
        unchanged -- verification failures never crash the pipeline.
        """
        try:
            verifier = VisualVerifier(
                api_key=self._api_key,
                model=self._model,
                session=self._session,
            )
        except Exception as exc:
            logger.warning("Visual verify: failed to initialise verifier: %s", exc)
            return compile_result

        for verify_attempt in range(self._MAX_VERIFY_RETRIES + 1):
            try:
                vr = verifier.verify(out_path, prompt)
            except Exception as exc:
                logger.warning(
                    "Visual verify: verification failed (attempt %d), skipping: %s",
                    verify_attempt + 1,
                    exc,
                )
                # Can't verify -- return what we have
                return compile_result

            self._verification_scores[job_id] = vr
            logger.info(
                "Visual verify: score=%.1f passed=%s (attempt %d/%d)",
                vr.score,
                vr.passed,
                verify_attempt + 1,
                self._MAX_VERIFY_RETRIES + 1,
            )

            if vr.passed:
                # Score is good enough -- return the result
                return compile_result

            # Score too low -- try to regenerate if we have retries left
            if verify_attempt >= self._MAX_VERIFY_RETRIES:
                logger.info(
                    "Visual verify: score %.1f below threshold after %d attempts, "
                    "returning best result.",
                    vr.score,
                    self._MAX_VERIFY_RETRIES + 1,
                )
                return compile_result

            logger.info(
                "Visual verify: score %.1f below threshold (%.1f), regenerating "
                "with feedback (retry %d/%d)...",
                vr.score,
                VisualVerifier.SCORE_THRESHOLD,
                verify_attempt + 1,
                self._MAX_VERIFY_RETRIES,
            )

            # Build a retry prompt incorporating the visual feedback
            style_hint = ""
            if style:
                style_hint = f"\nStyle preference: {style}."

            current_scad = self._scad_code.get(job_id, "")
            retry_prompt = (
                f"Create a 3D printable model of: {prompt}{style_hint}\n\n"
                f"A previous attempt was made but scored {vr.score:.1f}/10 in "
                f"visual verification.\n"
                f"Feedback: {vr.feedback}\n"
                f"Suggestion: {vr.suggestion}\n\n"
                f"Previous OpenSCAD code:\n```openscad\n{current_scad}\n```\n\n"
                f"Please generate improved OpenSCAD code that better matches the "
                f"original prompt. Address the feedback above. "
                f"Output valid OpenSCAD code only."
            )

            try:
                retry_response = self._call_gemini(
                    retry_prompt, image_parts=image_parts
                )
            except Exception as exc:
                logger.warning(
                    "Visual verify: Gemini retry call failed: %s", exc
                )
                return compile_result

            scad_code = _extract_openscad_code(retry_response)
            if not scad_code.strip():
                logger.warning("Visual verify: retried code was empty, aborting.")
                return compile_result

            safety_error = _check_scad_safety(scad_code)
            if safety_error:
                logger.warning(
                    "Visual verify: retried code failed safety check, aborting."
                )
                return compile_result

            self._scad_code[job_id] = scad_code

            # Save original STL in case regen fails — we'll fall back to it
            backup_path = out_path + ".backup"
            if os.path.isfile(out_path):
                with contextlib.suppress(OSError):
                    os.rename(out_path, backup_path)

            compile_result = self._compile_scad(
                scad_code, out_path, job_id, prompt, format
            )

            if compile_result.status != GenerationStatus.SUCCEEDED:
                logger.warning(
                    "Visual verify: recompilation failed, falling back to original model."
                )
                # Restore original STL and return success
                if os.path.isfile(backup_path):
                    with contextlib.suppress(OSError):
                        os.rename(backup_path, out_path)
                    self._paths[job_id] = out_path
                    return GenerationJob(
                        id=job_id,
                        provider=self.name,
                        prompt=prompt,
                        status=GenerationStatus.SUCCEEDED,
                        progress=100,
                        created_at=time.time(),
                        format=format,
                    )
                return compile_result

            # Regen succeeded — clean up backup
            with contextlib.suppress(OSError):
                if os.path.isfile(backup_path):
                    os.unlink(backup_path)

            # Loop back to verify the new result

        return compile_result

    def _call_gemini(
        self,
        prompt: str,
        *,
        image_parts: list[dict[str, Any]] | None = None,
    ) -> str:
        """Call the Gemini API with thinking enabled.

        If the configured model returns a quota/rate error and a fallback
        model is available, automatically retries with the fallback.

        :param prompt: The text prompt to send.
        :param image_parts: Optional list of image ``inlineData`` dicts
            for multimodal (image-to-3D) generation.
        :returns: The text content from Gemini's response (thinking parts
            filtered out).
        :raises GenerationError: On API errors or empty responses.
        """
        # Build content parts: images first (if any), then text prompt
        content_parts: list[dict[str, Any]] = []
        if image_parts:
            content_parts.extend(image_parts)
        content_parts.append({"text": prompt})

        # Try configured model, then fallback if quota exceeded
        models_to_try = [self._model]
        if self._model != _FALLBACK_MODEL:
            models_to_try.append(_FALLBACK_MODEL)

        last_error: Exception | None = None
        for model in models_to_try:
            thinking_config = _get_thinking_config(model)
            url = f"{_GEMINI_API_URL}/{model}:generateContent"
            params = {"key": self._api_key}

            body: dict[str, Any] = {
                "contents": [
                    {
                        "parts": content_parts,
                    }
                ],
                "systemInstruction": {
                    "parts": [{"text": _SYSTEM_PROMPT}],
                },
                "generationConfig": {
                    "temperature": 0.7,
                    "maxOutputTokens": 16384,
                    "thinkingConfig": thinking_config,
                },
            }

            logger.info(
                "Gemini Deep Think: calling %s with thinking config %s",
                model,
                thinking_config,
            )

            try:
                resp = self._request("POST", url, json_body=body, params=params)
            except GenerationError as exc:
                if "rate limit" in str(exc).lower() and model != models_to_try[-1]:
                    logger.warning(
                        "Gemini model %s hit rate limit, falling back to %s",
                        model,
                        _FALLBACK_MODEL,
                    )
                    last_error = exc
                    continue
                raise

            data = resp.json()

            # Check for quota errors in the response body (some come as 200 with error)
            if "error" in data and not data.get("candidates"):
                error_msg = data["error"].get("message", "")
                if ("quota" in error_msg.lower() or "rate" in error_msg.lower()) and model != models_to_try[-1]:
                    logger.warning(
                        "Gemini model %s quota exceeded, falling back to %s",
                        model,
                        _FALLBACK_MODEL,
                    )
                    last_error = GenerationError(error_msg, code="RATE_LIMITED")
                    continue

            # Success — break out of model loop
            break
        else:
            # All models exhausted
            if last_error:
                raise last_error
            raise GenerationError(
                "All Gemini models failed.",
                code="ALL_MODELS_FAILED",
            )

        # Extract text from Gemini response
        candidates = data.get("candidates", [])
        if not candidates:
            error_msg = data.get("error", {}).get("message", "No candidates returned.")
            raise GenerationError(
                f"Gemini returned no results: {error_msg}",
                code="NO_RESULT",
            )

        content = candidates[0].get("content", {})
        parts = content.get("parts", [])
        if not parts:
            raise GenerationError(
                "Gemini response contained no content parts.",
                code="EMPTY_RESPONSE",
            )

        # Filter out thinking/thought parts — only collect text output.
        # Thinking models return parts with "thought": true for their
        # internal reasoning; we want only the final code output.
        text_segments: list[str] = []
        thought_tokens = 0
        for part in parts:
            if part.get("thought"):
                # This is an internal thinking part — skip but log
                thought_text = part.get("text", "")
                thought_tokens += len(thought_text)
                continue
            text = part.get("text", "")
            if text:
                text_segments.append(text)

        if thought_tokens > 0:
            logger.info(
                "Gemini Deep Think: model used ~%d chars of internal reasoning",
                thought_tokens,
            )

        combined_text = "\n".join(text_segments).strip()
        if not combined_text:
            raise GenerationError(
                "Gemini returned empty text (thinking completed but no code output).",
                code="EMPTY_RESPONSE",
            )

        # Log usage metadata if available
        usage = data.get("usageMetadata", {})
        if usage:
            logger.info(
                "Gemini Deep Think usage: prompt=%s, candidates=%s, thoughts=%s, total=%s",
                usage.get("promptTokenCount", "?"),
                usage.get("candidatesTokenCount", "?"),
                usage.get("thoughtsTokenCount", "?"),
                usage.get("totalTokenCount", "?"),
            )

        return combined_text

    def _compile_scad(
        self,
        scad_code: str,
        out_path: str,
        job_id: str,
        prompt: str,
        format: str,
    ) -> GenerationJob:
        """Compile OpenSCAD code to STL."""
        # Prepend library modules so generated code can call them directly
        full_code = scad_code
        try:
            from kiln.generation.scad_library import get_library_source

            library_code = get_library_source()
            if library_code:
                full_code = library_code + "\n\n// === USER-GENERATED CODE ===\n\n" + scad_code
        except ImportError:
            logger.debug("scad_library not available, compiling without library modules")

        scad_fd, scad_path = tempfile.mkstemp(suffix=".scad", prefix="kiln_gemini_")
        try:
            # OpenSCAD reads source files as UTF-8; write explicitly so
            # non-ASCII characters in the library or generated code
            # (e.g. the comparison glyph in threshold comments) survive
            # on platforms whose default text encoding is not UTF-8.
            with os.fdopen(scad_fd, "w", encoding="utf-8") as fh:
                fh.write(full_code)

            cmd = [self._openscad, "-o", out_path, scad_path]
            logger.info("Gemini Deep Think: compiling OpenSCAD: %s", " ".join(cmd))

            work_dir = tempfile.mkdtemp(prefix="kiln_gemini_scad_")
            try:
                try:
                    result = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=self._compile_timeout,
                        cwd=work_dir,
                    )
                except subprocess.TimeoutExpired:
                    job = GenerationJob(
                        id=job_id,
                        provider=self.name,
                        prompt=prompt,
                        status=GenerationStatus.FAILED,
                        progress=0,
                        created_at=time.time(),
                        format=format,
                        error=f"OpenSCAD compilation timed out after {self._compile_timeout}s.",
                    )
                    self._jobs[job_id] = job
                    return job
                except OSError as exc:
                    raise GenerationError(
                        f"Failed to run OpenSCAD: {exc}",
                        code="OPENSCAD_EXEC_ERROR",
                    ) from exc
            finally:
                shutil.rmtree(work_dir, ignore_errors=True)

            if result.returncode != 0:
                stderr = (result.stderr or "").strip()[:500]
                job = GenerationJob(
                    id=job_id,
                    provider=self.name,
                    prompt=prompt,
                    status=GenerationStatus.FAILED,
                    progress=0,
                    created_at=time.time(),
                    format=format,
                    error=f"OpenSCAD compilation failed (exit {result.returncode}): {stderr}",
                )
                self._jobs[job_id] = job
                return job

            if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
                job = GenerationJob(
                    id=job_id,
                    provider=self.name,
                    prompt=prompt,
                    status=GenerationStatus.FAILED,
                    progress=0,
                    created_at=time.time(),
                    format=format,
                    error="OpenSCAD produced no output file.",
                )
                self._jobs[job_id] = job
                return job

            self._paths[job_id] = out_path
            self._prompts[job_id] = prompt

            file_size = os.path.getsize(out_path)
            logger.info(
                "Gemini Deep Think: compiled successfully -> %s (%.1f KB)",
                out_path,
                file_size / 1024,
            )

            job = GenerationJob(
                id=job_id,
                provider=self.name,
                prompt=prompt,
                status=GenerationStatus.SUCCEEDED,
                progress=100,
                created_at=time.time(),
                format=format,
            )
            self._jobs[job_id] = job
            return job

        finally:
            with contextlib.suppress(OSError):
                os.unlink(scad_path)

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        timeout: int | None = None,
        stream: bool = False,
    ) -> requests.Response:
        """Make an HTTP request with rate-limit retry and backoff.

        Retries up to ``_MAX_RETRIES`` times on HTTP 429 (rate limit)
        and 502/503/504 (transient server errors).  Uses exponential
        backoff: 2s, 4s, 8s.
        """
        req_timeout = timeout or _REQUEST_TIMEOUT

        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = self._session.request(
                    method,
                    url,
                    json=json_body,
                    params=params,
                    timeout=req_timeout,
                    stream=stream,
                )
            except requests.ConnectionError:
                raise GenerationError(
                    "Could not connect to Gemini API.",
                    code="CONNECTION_ERROR",
                ) from None
            except requests.Timeout:
                raise GenerationError(
                    "Gemini API request timed out.",
                    code="TIMEOUT",
                ) from None

            if resp.status_code in (429, 502, 503, 504) and attempt < _MAX_RETRIES:
                wait = _RETRY_BACKOFF_BASE * (2**attempt)
                logger.warning(
                    "Gemini API returned %d, retrying in %.0fs (attempt %d/%d)",
                    resp.status_code,
                    wait,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(wait)
                continue

            self._handle_http_error(resp)
            self._request_count += 1
            if self._first_request_time is None:
                self._first_request_time = time.time()
            return resp

        raise GenerationError(
            "Gemini API request failed after retries.",
            code="RETRY_EXHAUSTED",
        )

    def _handle_http_error(self, resp: requests.Response) -> None:
        """Raise a typed exception for non-2xx responses."""
        if resp.ok:
            return

        if resp.status_code in (401, 403):
            raise GenerationAuthError(
                "Gemini API key is invalid or expired.",
                code="AUTH_INVALID",
            )
        if resp.status_code == 429:
            elapsed = ""
            if self._first_request_time:
                mins = (time.time() - self._first_request_time) / 60
                elapsed = f" ({self._request_count} requests in {mins:.0f} min)"
            raise GenerationError(
                f"Gemini API rate limit exceeded{elapsed}.\n"
                "Free tier limits: 15 requests/min, 1500 requests/day.\n"
                "  • If you just ran multiple generations, wait 60 seconds.\n"
                "  • If you've been generating all session, the daily quota may be exhausted.\n"
                "    Daily quota resets at midnight Pacific time.\n"
                "  • Check usage at: https://aistudio.google.com/apikey\n"
                "  • To increase limits, add billing at https://ai.google.dev/pricing",
                code="RATE_LIMITED",
            )

        body = ""
        try:
            body = resp.json().get("error", {}).get("message", resp.text[:200])
        except Exception:
            body = resp.text[:200]

        raise GenerationError(
            f"Gemini API error (HTTP {resp.status_code}): {body}",
            code="API_ERROR",
        )
