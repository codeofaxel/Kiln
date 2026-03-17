"""Post-generation visual verification using Gemini Vision.

After a 3D model is generated from a text prompt, this module renders a
preview image of the STL and sends it to Gemini Vision to score how well
the result matches the original prompt.  If the score is below a
configurable threshold the caller can use the feedback to regenerate.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import subprocess
import shutil
import sys
import tempfile
from dataclasses import dataclass
from typing import Any

import requests

from kiln.generation.base import GenerationError

logger = logging.getLogger(__name__)

_GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_REQUEST_TIMEOUT = 60
_MACOS_APP_PATH = (
    "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD" if sys.platform == "darwin" else ""
)

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


class VisualVerifier:
    """Post-generation visual verification using Gemini Vision."""

    SCORE_THRESHOLD = 7.0  # Minimum score to pass

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gemini-2.5-flash",
        session: requests.Session | None = None,
        openscad_path: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._session = session or requests.Session()
        self._openscad = openscad_path or self._find_openscad()

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
            try:
                os.unlink(png_path)
            except OSError:
                pass

    def render_stl_to_png(self, stl_path: str) -> str:
        """Render an STL file to a PNG preview using OpenSCAD.

        Uses OpenSCAD's command-line rendering with a sensible default
        camera angle that shows the model from a 3/4 perspective.

        :param stl_path: Path to the STL file.
        :returns: Path to the rendered PNG (caller is responsible for
            cleanup).
        :raises GenerationError: If OpenSCAD cannot render the image.
        """
        if not os.path.isfile(stl_path):
            raise GenerationError(
                f"STL file not found: {stl_path}",
                code="STL_NOT_FOUND",
            )

        fd, png_path = tempfile.mkstemp(suffix=".png", prefix="kiln_verify_")
        os.close(fd)

        # Build the OpenSCAD command for PNG rendering.
        # --render forces full CGAL render (not just preview).
        # --camera sets a 3/4 view: translation x,y,z then rotation x,y,z
        # then distance.
        # --imgsize sets output resolution.
        # We import the STL via a tiny wrapper script so OpenSCAD can
        # render it.
        scad_fd, scad_path = tempfile.mkstemp(suffix=".scad", prefix="kiln_verify_")
        try:
            with os.fdopen(scad_fd, "w") as fh:
                # Use an import statement to load the STL inside OpenSCAD
                escaped = stl_path.replace("\\", "\\\\").replace('"', '\\"')
                fh.write(f'import("{escaped}");\n')

            cmd = [
                self._openscad,
                "--render",
                "-o", png_path,
                "--imgsize=800,600",
                "--camera=0,0,0,55,0,25,200",
                scad_path,
            ]

            logger.debug("Visual verify: rendering STL preview: %s", " ".join(cmd))

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except FileNotFoundError:
                raise GenerationError(
                    "OpenSCAD binary not found for PNG rendering.",
                    code="OPENSCAD_NOT_FOUND",
                )
            except subprocess.TimeoutExpired:
                raise GenerationError(
                    "OpenSCAD PNG rendering timed out.",
                    code="RENDER_TIMEOUT",
                )

            if result.returncode != 0:
                stderr = (result.stderr or "").strip()[:300]
                raise GenerationError(
                    f"OpenSCAD PNG render failed (exit {result.returncode}): {stderr}",
                    code="RENDER_FAILED",
                )

            if not os.path.isfile(png_path) or os.path.getsize(png_path) == 0:
                raise GenerationError(
                    "OpenSCAD produced no PNG output.",
                    code="RENDER_EMPTY",
                )

            logger.info(
                "Visual verify: rendered preview -> %s (%.1f KB)",
                png_path,
                os.path.getsize(png_path) / 1024,
            )
            return png_path

        finally:
            try:
                os.unlink(scad_path)
            except OSError:
                pass

    # Camera angles for multi-angle rendering: (label, --camera value)
    _ANGLES: list[tuple[str, str]] = [
        ("isometric", "--camera=0,0,0,55,0,25,200"),
        ("front", "--camera=0,0,0,0,0,0,200"),
        ("right_side", "--camera=0,0,0,0,0,90,200"),
        ("top", "--camera=0,0,0,90,0,0,200"),
        ("bottom", "--camera=0,0,0,-90,0,0,200"),
    ]

    def render_multi_angle(self, stl_path: str) -> list[str]:
        """Render an STL file from 5 different camera angles.

        Produces isometric (3/4 view), front, right-side, top-down, and
        bottom-up PNG previews using OpenSCAD.  The bottom view is
        critical for verifying bed adhesion surface and first-layer
        printability.

        :param stl_path: Path to the STL file.
        :returns: List of 4 PNG paths (caller is responsible for cleanup).
        :raises GenerationError: If OpenSCAD cannot render any image.
        """
        if not os.path.isfile(stl_path):
            raise GenerationError(
                f"STL file not found: {stl_path}",
                code="STL_NOT_FOUND",
            )

        # Build a temporary .scad wrapper once and reuse for all angles.
        scad_fd, scad_path = tempfile.mkstemp(suffix=".scad", prefix="kiln_verify_")
        try:
            with os.fdopen(scad_fd, "w") as fh:
                escaped = stl_path.replace("\\", "\\\\").replace('"', '\\"')
                fh.write(f'import("{escaped}");\n')

            png_paths: list[str] = []
            for label, camera_arg in self._ANGLES:
                fd, png_path = tempfile.mkstemp(
                    suffix=f"_{label}.png", prefix="kiln_verify_"
                )
                os.close(fd)

                cmd = [
                    self._openscad,
                    "--render",
                    "-o", png_path,
                    "--imgsize=800,600",
                    camera_arg,
                    scad_path,
                ]

                logger.debug(
                    "Visual verify [%s]: rendering STL preview: %s",
                    label,
                    " ".join(cmd),
                )

                try:
                    result = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                except FileNotFoundError:
                    raise GenerationError(
                        "OpenSCAD binary not found for PNG rendering.",
                        code="OPENSCAD_NOT_FOUND",
                    )
                except subprocess.TimeoutExpired:
                    raise GenerationError(
                        f"OpenSCAD PNG rendering timed out ({label} view).",
                        code="RENDER_TIMEOUT",
                    )

                if result.returncode != 0:
                    stderr = (result.stderr or "").strip()[:300]
                    raise GenerationError(
                        f"OpenSCAD PNG render failed for {label} view "
                        f"(exit {result.returncode}): {stderr}",
                        code="RENDER_FAILED",
                    )

                if not os.path.isfile(png_path) or os.path.getsize(png_path) == 0:
                    raise GenerationError(
                        f"OpenSCAD produced no PNG output for {label} view.",
                        code="RENDER_EMPTY",
                    )

                logger.info(
                    "Visual verify [%s]: rendered preview -> %s (%.1f KB)",
                    label,
                    png_path,
                    os.path.getsize(png_path) / 1024,
                )
                png_paths.append(png_path)

            return png_paths

        finally:
            try:
                os.unlink(scad_path)
            except OSError:
                pass

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

    @staticmethod
    def _find_openscad() -> str:
        """Locate the OpenSCAD binary for PNG rendering."""
        which = shutil.which("openscad")
        if which:
            return which

        if (
            _MACOS_APP_PATH
            and os.path.isfile(_MACOS_APP_PATH)
            and os.access(_MACOS_APP_PATH, os.X_OK)
        ):
            return _MACOS_APP_PATH

        # Return "openscad" and let the caller handle FileNotFoundError
        return "openscad"
