"""Tests for the Gemini Deep Think generation provider (kiln.generation.gemini)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import requests as requests_lib
import responses

from kiln.generation.base import (
    GenerationAuthError,
    GenerationError,
    GenerationJob,
    GenerationResult,
    GenerationStatus,
)
from kiln.generation.gemini import (
    _GEMINI_API_URL,
    GeminiDeepThinkProvider,
    _extract_openscad_code,
    _find_openscad,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GEMINI_API_KEY = "test-gemini-key-abc123"
TEST_MODEL = "gemini-2.0-flash"
GENERATE_URL = f"{_GEMINI_API_URL}/{TEST_MODEL}:generateContent"

SAMPLE_SCAD_CODE = """\
// Simple cube
cube([20, 20, 20], center=true);
"""

GEMINI_SUCCESS_RESPONSE = {
    "candidates": [
        {
            "content": {
                "parts": [
                    {"text": SAMPLE_SCAD_CODE}
                ]
            }
        }
    ]
}

GEMINI_FENCED_RESPONSE = {
    "candidates": [
        {
            "content": {
                "parts": [
                    {"text": "Here's the model:\n```openscad\ncube([30, 30, 30]);\n```\n"}
                ]
            }
        }
    ]
}

GEMINI_EMPTY_RESPONSE = {
    "candidates": []
}

GEMINI_NO_PARTS_RESPONSE = {
    "candidates": [
        {
            "content": {
                "parts": []
            }
        }
    ]
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_openscad(tmp_path):
    """Create a fake openscad binary that writes a dummy STL file."""
    fake_bin = tmp_path / "openscad"
    # Script: copy the output path from args and write dummy data
    fake_bin.write_text(
        '#!/bin/bash\n'
        '# Fake OpenSCAD: write dummy STL bytes to the -o output file\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in\n'
        '    -o) shift; echo "FAKE_STL_DATA" > "$1" ;;\n'
        '  esac\n'
        '  shift\n'
        'done\n'
    )
    fake_bin.chmod(0o755)
    return str(fake_bin)


@pytest.fixture
def failing_openscad(tmp_path):
    """Create a fake openscad binary that always fails."""
    fake_bin = tmp_path / "openscad"
    fake_bin.write_text(
        '#!/bin/bash\n'
        'echo "ERROR: syntax error" >&2\n'
        'exit 1\n'
    )
    fake_bin.chmod(0o755)
    return str(fake_bin)


@pytest.fixture
def empty_openscad(tmp_path):
    """Create a fake openscad binary that produces no output."""
    fake_bin = tmp_path / "openscad"
    fake_bin.write_text(
        '#!/bin/bash\n'
        '# Do nothing — produce no output file\n'
    )
    fake_bin.chmod(0o755)
    return str(fake_bin)


@pytest.fixture
def gemini_provider(mock_openscad):
    """GeminiDeepThinkProvider configured with test settings."""
    return GeminiDeepThinkProvider(
        api_key=GEMINI_API_KEY,
        openscad_path=mock_openscad,
    )


# ===================================================================
# _find_openscad tests
# ===================================================================


class TestFindOpenSCAD:
    """Locate openscad binary: explicit path, PATH lookup, missing."""
    def test_explicit_path_valid(self, mock_openscad):
        assert _find_openscad(mock_openscad) == mock_openscad

    def test_explicit_path_not_found(self, tmp_path):
        bad_path = str(tmp_path / "nonexistent")
        with pytest.raises(GenerationError, match="not found"):
            _find_openscad(bad_path)

    def test_no_openscad_anywhere(self):
        with (
            patch("shutil.which", return_value=None),
            patch("os.path.isfile", return_value=False),
            pytest.raises(GenerationError, match="not found"),
        ):
            _find_openscad()


# ===================================================================
# _extract_openscad_code tests
# ===================================================================


class TestExtractOpenSCADCode:
    """Extract OpenSCAD code from Gemini responses: plain, fenced, mixed, empty."""
    def test_plain_code(self):
        code = "cube([10, 10, 10]);"
        assert _extract_openscad_code(code) == code

    def test_fenced_code(self):
        text = "Here's the model:\n```openscad\ncube([10, 10, 10]);\n```\n"
        assert _extract_openscad_code(text) == "cube([10, 10, 10]);"

    def test_fenced_scad_variant(self):
        text = "```scad\nsphere(r=5);\n```"
        assert _extract_openscad_code(text) == "sphere(r=5);"

    def test_fenced_no_lang(self):
        text = "```\ncylinder(h=10, r=5);\n```"
        assert _extract_openscad_code(text) == "cylinder(h=10, r=5);"

    def test_mixed_text_and_code(self):
        text = "This is a great model.\ncube([10, 10, 10]);\nsphere(r=5);"
        result = _extract_openscad_code(text)
        assert "cube([10, 10, 10]);" in result
        assert "sphere(r=5);" in result

    def test_empty_string(self):
        assert _extract_openscad_code("") == ""

    def test_code_with_comments(self):
        text = "// A nice model\ncube([10, 10, 10]);"
        result = _extract_openscad_code(text)
        assert "// A nice model" in result
        assert "cube([10, 10, 10]);" in result

    def test_module_definition(self):
        text = "module my_part() {\n  cube([10, 10, 10]);\n}\nmy_part();"
        result = _extract_openscad_code(text)
        assert "module my_part()" in result


# ===================================================================
# Init tests
# ===================================================================


class TestGeminiDeepThinkProviderInit:
    """Constructor: API key required, env fallback, explicit override, OpenSCAD required."""
    def test_requires_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("KILN_GEMINI_API_KEY", None)
            with pytest.raises(GenerationAuthError, match="API key required"):
                GeminiDeepThinkProvider(api_key="", openscad_path="/usr/bin/true")

    def test_api_key_from_env(self, mock_openscad):
        with patch.dict(os.environ, {"KILN_GEMINI_API_KEY": "env-key"}):
            provider = GeminiDeepThinkProvider(openscad_path=mock_openscad)
            assert provider._api_key == "env-key"

    def test_explicit_key_overrides_env(self, mock_openscad):
        with patch.dict(os.environ, {"KILN_GEMINI_API_KEY": "env-key"}):
            provider = GeminiDeepThinkProvider(
                api_key="explicit",
                openscad_path=mock_openscad,
            )
            assert provider._api_key == "explicit"

    def test_requires_openscad(self):
        with (
            patch("kiln.generation.gemini._find_openscad", side_effect=GenerationError("not found")),
            pytest.raises(GenerationError, match="not found"),
        ):
            GeminiDeepThinkProvider(api_key="key")


# ===================================================================
# Properties tests
# ===================================================================


class TestGeminiDeepThinkProviderProperties:
    """Provider name, display_name, and list_styles."""
    def test_name(self, gemini_provider):
        assert gemini_provider.name == "gemini"

    def test_display_name(self, gemini_provider):
        assert gemini_provider.display_name == "Gemini Deep Think"

    def test_list_styles(self, gemini_provider):
        styles = gemini_provider.list_styles()
        assert "organic" in styles
        assert "mechanical" in styles
        assert "decorative" in styles


# ===================================================================
# Generate tests
# ===================================================================


class TestGeminiDeepThinkProviderGenerate:
    """Generate flow: success, fenced responses, styles, empty, dangerous code, compilation failures."""
    @responses.activate
    def test_generate_success(self, gemini_provider, tmp_path):
        responses.add(
            responses.POST,
            GENERATE_URL,
            json=GEMINI_SUCCESS_RESPONSE,
        )
        job = gemini_provider.generate(
            "a small cube",
            output_dir=str(tmp_path),
        )
        assert isinstance(job, GenerationJob)
        assert job.provider == "gemini"
        assert job.status == GenerationStatus.SUCCEEDED
        assert job.progress == 100
        assert job.id.startswith("gemini-")
        assert job.format == "stl"

    @responses.activate
    def test_generate_with_fenced_response(self, gemini_provider, tmp_path):
        responses.add(
            responses.POST,
            GENERATE_URL,
            json=GEMINI_FENCED_RESPONSE,
        )
        job = gemini_provider.generate(
            "a box",
            output_dir=str(tmp_path),
        )
        assert job.status == GenerationStatus.SUCCEEDED

    @responses.activate
    def test_generate_with_style(self, gemini_provider, tmp_path):
        responses.add(
            responses.POST,
            GENERATE_URL,
            json=GEMINI_SUCCESS_RESPONSE,
        )
        job = gemini_provider.generate(
            "a vase",
            style="organic",
            output_dir=str(tmp_path),
        )
        assert job.status == GenerationStatus.SUCCEEDED

    @responses.activate
    def test_generate_empty_response_fails(self, gemini_provider, tmp_path):
        responses.add(
            responses.POST,
            GENERATE_URL,
            json=GEMINI_EMPTY_RESPONSE,
        )
        with pytest.raises(GenerationError, match="no results"):
            gemini_provider.generate(
                "a cube",
                output_dir=str(tmp_path),
            )

    @responses.activate
    def test_generate_no_parts_fails(self, gemini_provider, tmp_path):
        responses.add(
            responses.POST,
            GENERATE_URL,
            json=GEMINI_NO_PARTS_RESPONSE,
        )
        with pytest.raises(GenerationError, match="no content parts"):
            gemini_provider.generate(
                "a cube",
                output_dir=str(tmp_path),
            )

    def test_generate_unsupported_format(self, gemini_provider, tmp_path):
        with pytest.raises(GenerationError, match="only supports STL"):
            gemini_provider.generate(
                "a cube",
                format="obj",
                output_dir=str(tmp_path),
            )

    @responses.activate
    def test_generate_dangerous_code_blocked(self, mock_openscad, tmp_path):
        dangerous_response = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": 'import("evil.stl");\ncube([10,10,10]);'}
                        ]
                    }
                }
            ]
        }
        responses.add(
            responses.POST,
            GENERATE_URL,
            json=dangerous_response,
        )
        provider = GeminiDeepThinkProvider(
            api_key=GEMINI_API_KEY,
            openscad_path=mock_openscad,
        )
        job = provider.generate(
            "import something",
            output_dir=str(tmp_path),
        )
        assert job.status == GenerationStatus.FAILED
        assert "blocked" in job.error.lower()

    @responses.activate
    @pytest.mark.parametrize(
        "dangerous_code",
        [
            'import("evil.stl");',
            'surface("heightmap.dat");',
            'include <secrets.scad>',
            'use <library.scad>',
        ],
        ids=["import", "surface", "include", "use"],
    )
    def test_all_dangerous_patterns_blocked(self, mock_openscad, tmp_path, dangerous_code):
        resp = {
            "candidates": [
                {"content": {"parts": [{"text": dangerous_code}]}}
            ]
        }
        responses.add(responses.POST, GENERATE_URL, json=resp)
        provider = GeminiDeepThinkProvider(
            api_key=GEMINI_API_KEY,
            openscad_path=mock_openscad,
        )
        job = provider.generate("test", output_dir=str(tmp_path))
        assert job.status == GenerationStatus.FAILED
        assert "blocked" in job.error.lower()

    @responses.activate
    def test_generate_compilation_failure(self, failing_openscad, tmp_path):
        responses.add(
            responses.POST,
            GENERATE_URL,
            json=GEMINI_SUCCESS_RESPONSE,
        )
        provider = GeminiDeepThinkProvider(
            api_key=GEMINI_API_KEY,
            openscad_path=failing_openscad,
        )
        job = provider.generate(
            "a cube",
            output_dir=str(tmp_path),
        )
        assert job.status == GenerationStatus.FAILED
        assert "compilation failed" in job.error.lower()

    @responses.activate
    def test_generate_empty_output(self, empty_openscad, tmp_path):
        responses.add(
            responses.POST,
            GENERATE_URL,
            json=GEMINI_SUCCESS_RESPONSE,
        )
        provider = GeminiDeepThinkProvider(
            api_key=GEMINI_API_KEY,
            openscad_path=empty_openscad,
        )
        job = provider.generate(
            "a cube",
            output_dir=str(tmp_path),
        )
        assert job.status == GenerationStatus.FAILED
        assert "no output" in job.error.lower()


# ===================================================================
# GetJobStatus tests
# ===================================================================


class TestGeminiDeepThinkProviderGetJobStatus:
    """Job status retrieval: existing jobs, not-found errors."""
    @responses.activate
    def test_get_job_status_existing(self, gemini_provider, tmp_path):
        responses.add(
            responses.POST,
            GENERATE_URL,
            json=GEMINI_SUCCESS_RESPONSE,
        )
        job = gemini_provider.generate(
            "a cube",
            output_dir=str(tmp_path),
        )
        status = gemini_provider.get_job_status(job.id)
        assert status.status == GenerationStatus.SUCCEEDED
        assert status.id == job.id

    def test_get_job_status_not_found(self, gemini_provider):
        with pytest.raises(GenerationError, match="not found"):
            gemini_provider.get_job_status("nonexistent-id")


# ===================================================================
# DownloadResult tests
# ===================================================================


class TestGeminiDeepThinkProviderDownloadResult:
    """Download result: success path, file not found."""
    @responses.activate
    def test_download_result_success(self, gemini_provider, tmp_path):
        responses.add(
            responses.POST,
            GENERATE_URL,
            json=GEMINI_SUCCESS_RESPONSE,
        )
        job = gemini_provider.generate(
            "a cube",
            output_dir=str(tmp_path),
        )
        result = gemini_provider.download_result(job.id)
        assert isinstance(result, GenerationResult)
        assert result.provider == "gemini"
        assert result.format == "stl"
        assert result.file_size_bytes > 0
        assert Path(result.local_path).exists()

    def test_download_result_not_found(self, gemini_provider):
        with pytest.raises(GenerationError, match="No generated file"):
            gemini_provider.download_result("nonexistent-id")


# ===================================================================
# GetScadCode tests
# ===================================================================


class TestGeminiDeepThinkProviderGetScadCode:
    """SCAD code retrieval: stored code, not-found returns None."""
    @responses.activate
    def test_get_scad_code(self, gemini_provider, tmp_path):
        responses.add(
            responses.POST,
            GENERATE_URL,
            json=GEMINI_SUCCESS_RESPONSE,
        )
        job = gemini_provider.generate(
            "a cube",
            output_dir=str(tmp_path),
        )
        code = gemini_provider.get_scad_code(job.id)
        assert code is not None
        assert "cube" in code

    def test_get_scad_code_not_found(self, gemini_provider):
        assert gemini_provider.get_scad_code("nonexistent") is None


# ===================================================================
# Error handling tests
# ===================================================================


class TestGeminiDeepThinkProviderErrors:
    """Error handling: auth (401/403), rate limits (429), 500, connection, timeout."""
    @responses.activate
    def test_401_raises_auth_error(self, gemini_provider, tmp_path):
        responses.add(
            responses.POST,
            GENERATE_URL,
            status=401,
            json={"error": {"message": "Unauthorized"}},
        )
        with pytest.raises(GenerationAuthError, match="invalid or expired"):
            gemini_provider.generate(
                "a cube",
                output_dir=str(tmp_path),
            )

    @responses.activate
    def test_403_raises_auth_error(self, gemini_provider, tmp_path):
        responses.add(
            responses.POST,
            GENERATE_URL,
            status=403,
            json={"error": {"message": "Forbidden"}},
        )
        with pytest.raises(GenerationAuthError, match="invalid or expired"):
            gemini_provider.generate(
                "a cube",
                output_dir=str(tmp_path),
            )

    @responses.activate
    def test_429_raises_rate_limit_error(self, gemini_provider, tmp_path):
        for _ in range(4):
            responses.add(
                responses.POST,
                GENERATE_URL,
                status=429,
                json={"error": {"message": "Rate limited"}},
            )
        with pytest.raises(GenerationError, match="rate limit"):
            gemini_provider.generate(
                "a cube",
                output_dir=str(tmp_path),
            )

    @responses.activate
    def test_500_raises_api_error(self, gemini_provider, tmp_path):
        responses.add(
            responses.POST,
            GENERATE_URL,
            status=500,
            body="Internal Server Error",
        )
        with pytest.raises(GenerationError, match="500"):
            gemini_provider.generate(
                "a cube",
                output_dir=str(tmp_path),
            )

    @responses.activate
    def test_connection_error(self, gemini_provider, tmp_path):
        responses.add(
            responses.POST,
            GENERATE_URL,
            body=requests_lib.ConnectionError("Connection failed"),
        )
        with pytest.raises(GenerationError, match="Could not connect"):
            gemini_provider.generate(
                "a cube",
                output_dir=str(tmp_path),
            )

    @responses.activate
    def test_timeout_error(self, gemini_provider, tmp_path):
        responses.add(
            responses.POST,
            GENERATE_URL,
            body=requests_lib.Timeout("Timed out"),
        )
        with pytest.raises(GenerationError, match="timed out"):
            gemini_provider.generate(
                "a cube",
                output_dir=str(tmp_path),
            )

    @responses.activate
    def test_gemini_error_message_in_response(self, gemini_provider, tmp_path):
        responses.add(
            responses.POST,
            GENERATE_URL,
            json={"error": {"message": "Model not found"}},
        )
        with pytest.raises(GenerationError, match="no results"):
            gemini_provider.generate(
                "a cube",
                output_dir=str(tmp_path),
            )


# ===================================================================
# Retry logic tests
# ===================================================================


class TestGeminiDeepThinkProviderRetry:
    """Retry logic: transient 502/503 retried, exhaustion raises error."""
    @responses.activate
    def test_retries_on_502(self, gemini_provider, tmp_path):
        responses.add(
            responses.POST,
            GENERATE_URL,
            status=502,
        )
        responses.add(
            responses.POST,
            GENERATE_URL,
            json=GEMINI_SUCCESS_RESPONSE,
        )
        with patch("time.sleep"):
            job = gemini_provider.generate(
                "a cube",
                output_dir=str(tmp_path),
            )
        assert job.status == GenerationStatus.SUCCEEDED

    @responses.activate
    def test_retries_on_503(self, gemini_provider, tmp_path):
        responses.add(
            responses.POST,
            GENERATE_URL,
            status=503,
        )
        responses.add(
            responses.POST,
            GENERATE_URL,
            json=GEMINI_SUCCESS_RESPONSE,
        )
        with patch("time.sleep"):
            job = gemini_provider.generate(
                "a cube",
                output_dir=str(tmp_path),
            )
        assert job.status == GenerationStatus.SUCCEEDED

    @responses.activate
    def test_retry_exhaustion_raises(self, gemini_provider, tmp_path):
        for _ in range(5):
            responses.add(
                responses.POST,
                GENERATE_URL,
                status=502,
            )
        with patch("time.sleep"), pytest.raises(GenerationError, match="HTTP 502"):
            gemini_provider.generate(
                "a cube",
                output_dir=str(tmp_path),
            )
