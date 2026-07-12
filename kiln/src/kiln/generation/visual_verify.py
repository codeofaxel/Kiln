"""Post-generation visual verification using Gemini Vision.

After a 3D model is generated from a text prompt, this module renders a
preview image of the STL and sends it to Gemini Vision to score how well
the result matches the original prompt.  If the score is below a
configurable threshold the caller can use the feedback to regenerate.

Rendering goes through :mod:`kiln.colored_renderer` — the same
smooth-shaded (Gouraud-lit) software renderer ``visualize_model`` uses —
rather than OpenSCAD's flat-shaded preview mode.  A judge scoring
"does this match the prompt" needs to see the model the way a person
would; flat per-facet shading makes a good result look lumpy and can
bias the score against geometry that is actually fine.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from kiln.generation.base import GenerationError

logger = logging.getLogger(__name__)

_GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_REQUEST_TIMEOUT = 60

# Matches the flat gray ("#AAAAAA") the old OpenSCAD preview used — a
# neutral material color that doesn't bias the vision judge toward or
# away from any particular filament color.
_DEFAULT_COLOR = (170, 170, 170)

_VERIFICATION_PROMPT = """\
You are evaluating a 3D model that was generated from a text prompt.

Original prompt: "{prompt}"

Look at this rendered preview of the generated 3D model. Score it from 1-10 on how well it matches the original prompt:

Scoring criteria:
- 9-10: Excellent match, all key features present and correct proportions
- 7-8: Good match, most features present, minor issues
- 5-6: Partial match, some features missing or wrong
- 3-4: Poor match, major features missing or wrong shape
- 1-2: Complete mismatch

Respond in EXACTLY this format:
SCORE: <number>
FEEDBACK: <one line describing what matches and what doesn't>
SUGGESTION: <one line on how to improve the prompt or model>"""


@dataclass
class VerificationResult:
    """Outcome of visual verification against the original prompt."""

    score: float  # 1-10
    passed: bool  # score >= threshold
    feedback: str  # What's wrong / what's good
    suggestion: str  # How to fix if score is low


def _load_stl_as_colored_triangles(stl_path: str, color=_DEFAULT_COLOR) -> list:
    """Parse an STL into the uniform-color triangle list the smooth
    renderer expects.

    Reuses :func:`kiln.generation.validation._parse_stl` — the same
    dependency-free binary/ASCII STL reader ``preview.py`` already uses
    for its own STL path — so this needs no mesh library beyond stdlib.
    """
    from kiln.generation.validation import _parse_stl
    from kiln.threemf_parser import ColoredTriangle

    errors: list[str] = []
    raw_triangles, _vertices = _parse_stl(Path(stl_path), errors)
    if errors:
        raise GenerationError(
            f"Could not parse STL for rendering: {'; '.join(errors)}",
            code="STL_PARSE_ERROR",
        )
    if not raw_triangles:
        raise GenerationError(
            f"STL file has no triangles: {stl_path}",
            code="STL_EMPTY",
        )
    return [
        ColoredTriangle(v0=tri[0], v1=tri[1], v2=tri[2], color=color)
        for tri in raw_triangles
    ]


class VisualVerifier:
    """Post-generation visual verification using Gemini Vision."""

    SCORE_THRESHOLD = 7.0  # Minimum score to pass

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gemini-2.5-flash",
        session: requests.Session | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._session = session or requests.Session()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def verify(self, stl_path: str, original_prompt: str) -> VerificationResult:
        """Render an STL preview and score it against the original prompt.

        :param stl_path: Path to the generated STL file.
        :param original_prompt: The text prompt that was used to generate
            the model.
        :returns: A :class:`VerificationResult` with score, pass/fail,
            feedback, and improvement suggestion.
        :raises GenerationError: If rendering or the API call fails in an
            unrecoverable way.  Callers should catch this and degrade
            gracefully.
        """
        png_path = self.render_stl_to_png(stl_path)
        try:
            return self._score_image(png_path, original_prompt)
        finally:
            # Clean up the temporary preview image
            with contextlib.suppress(OSError):
                os.unlink(png_path)

    def render_stl_to_png(self, stl_path: str) -> str:
        """Render an STL file to a PNG preview, smooth-shaded, isometric.

        :param stl_path: Path to the STL file.
        :returns: Path to the rendered PNG (caller is responsible for
            cleanup).
        :raises GenerationError: If the STL can't be parsed or rendered.
        """
        if not os.path.isfile(stl_path):
            raise GenerationError(
                f"STL file not found: {stl_path}",
                code="STL_NOT_FOUND",
            )

        from kiln.colored_renderer import render_colored_mesh_multi_angle

        triangles = _load_stl_as_colored_triangles(stl_path)
        try:
            results = render_colored_mesh_multi_angle(triangles, angles=["isometric"])
        except Exception as exc:  # noqa: BLE001 — surface as GenerationError
            raise GenerationError(
                f"Rendering STL preview failed: {exc}",
                code="RENDER_FAILED",
            ) from exc

        png_path = results[0]["path"]
        if not os.path.isfile(png_path) or os.path.getsize(png_path) == 0:
            raise GenerationError(
                "Renderer produced no PNG output.",
                code="RENDER_EMPTY",
            )

        logger.info(
            "Visual verify: rendered preview -> %s (%.1f KB)",
            png_path,
            os.path.getsize(png_path) / 1024,
        )
        return png_path

    # Angle order matches the historical 5-view contract callers rely on
    # (kiln.plugins.generation_tools indexes the returned list
    # positionally: isometric, front, right/side, top, bottom).
    _MULTI_ANGLES: list[str] = ["isometric", "front", "right", "top", "bottom"]

    def render_multi_angle(self, stl_path: str) -> list[str]:
        """Render an STL file from 5 standard camera angles, smooth-shaded.

        Produces isometric (3/4 view), front, right-side, top-down, and
        bottom-up PNG previews. The bottom view is critical for verifying
        bed adhesion surface and first-layer printability.

        :param stl_path: Path to the STL file.
        :returns: List of 5 PNG paths, in isometric/front/right/top/bottom
            order (caller is responsible for cleanup).
        :raises GenerationError: If the STL can't be parsed or rendered.
        """
        if not os.path.isfile(stl_path):
            raise GenerationError(
                f"STL file not found: {stl_path}",
                code="STL_NOT_FOUND",
            )

        from kiln.colored_renderer import render_colored_mesh_multi_angle

        triangles = _load_stl_as_colored_triangles(stl_path)
        try:
            results = render_colored_mesh_multi_angle(
                triangles, angles=self._MULTI_ANGLES
            )
        except Exception as exc:  # noqa: BLE001 — surface as GenerationError
            raise GenerationError(
                f"Rendering STL multi-angle preview failed: {exc}",
                code="RENDER_FAILED",
            ) from exc

        png_paths: list[str] = []
        for r in results:
            path = r.get("path")
            if not path or not os.path.isfile(path) or os.path.getsize(path) == 0:
                raise GenerationError(
                    f"Renderer produced no PNG output for {r.get('angle')} view.",
                    code="RENDER_EMPTY",
                )
            logger.info(
                "Visual verify [%s]: rendered preview -> %s",
                r.get("angle"),
                path,
            )
            png_paths.append(path)

        return png_paths

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _score_image(self, png_path: str, original_prompt: str) -> VerificationResult:
        """Send the preview image to Gemini Vision and parse the score."""
        with open(png_path, "rb") as fh:
            image_b64 = base64.standard_b64encode(fh.read()).decode("ascii")

        prompt_text = _VERIFICATION_PROMPT.format(prompt=original_prompt)

        url = f"{_GEMINI_API_URL}/{self._model}:generateContent"
        params = {"key": self._api_key}

        body: dict[str, Any] = {
            "contents": [
                {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": "image/png",
                                "data": image_b64,
                            }
                        },
                        {"text": prompt_text},
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 1024,
            },
        }

        logger.info("Visual verify: sending preview to Gemini Vision (%s)", self._model)

        try:
            resp = self._session.post(
                url,
                json=body,
                params=params,
                timeout=_REQUEST_TIMEOUT,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise GenerationError(
                f"Gemini Vision API request failed: {exc}",
                code="VISION_API_ERROR",
            ) from exc

        if not resp.ok:
            raise GenerationError(
                f"Gemini Vision API error (HTTP {resp.status_code}): "
                f"{resp.text[:200]}",
                code="VISION_API_ERROR",
            )

        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            raise GenerationError(
                "Gemini Vision returned no candidates.",
                code="VISION_NO_RESULT",
            )

        parts = candidates[0].get("content", {}).get("parts", [])
        text_segments = []
        for part in parts:
            if part.get("thought"):
                continue
            t = part.get("text", "")
            if t:
                text_segments.append(t)

        response_text = "\n".join(text_segments).strip()
        if not response_text:
            raise GenerationError(
                "Gemini Vision returned empty response.",
                code="VISION_EMPTY",
            )

        return self._parse_response(response_text)

    def _parse_response(self, text: str) -> VerificationResult:
        """Parse the structured SCORE/FEEDBACK/SUGGESTION response.

        Handles malformed responses gracefully by extracting whatever
        information is available and falling back to defaults.
        """
        score = 5.0  # Default mid-range if parsing fails
        feedback = ""
        suggestion = ""

        # Try to extract score
        score_match = re.search(r"SCORE:\s*([\d]+(?:\.[\d]+)?)", text, re.IGNORECASE)
        if score_match:
            try:
                score = float(score_match.group(1))
                score = max(1.0, min(10.0, score))  # Clamp to valid range
            except ValueError:
                pass

        # Try to extract feedback
        feedback_match = re.search(
            r"FEEDBACK:\s*(.+?)(?:\n|SUGGESTION:|$)", text, re.IGNORECASE | re.DOTALL
        )
        if feedback_match:
            feedback = feedback_match.group(1).strip()

        # Try to extract suggestion
        suggestion_match = re.search(
            r"SUGGESTION:\s*(.+?)$", text, re.IGNORECASE | re.DOTALL
        )
        if suggestion_match:
            suggestion = suggestion_match.group(1).strip()

        # Fallback: if we couldn't parse structured fields, use raw text
        if not feedback and not suggestion:
            feedback = text[:200].strip()

        passed = score >= self.SCORE_THRESHOLD

        logger.info(
            "Visual verify: score=%.1f passed=%s feedback=%s",
            score,
            passed,
            feedback[:80],
        )

        return VerificationResult(
            score=score,
            passed=passed,
            feedback=feedback,
            suggestion=suggestion,
        )
