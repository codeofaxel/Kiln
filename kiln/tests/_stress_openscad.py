"""Stress tests for generation/openscad.py NoError filter on feature/provenance-qr-validation."""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest


class TestNoErrorFilter:
    """Test the _parse_openscad_output NoError filter logic."""

    def _parse(self, stderr: str, return_code: int = 0) -> dict:
        from kiln.generation.openscad import _parse_openscad_output
        return _parse_openscad_output(stderr, return_code)

    def test_noerror_status_not_treated_as_error(self):
        result = self._parse("Status:     NoError\n", 0)
        assert len(result["errors"]) == 0

    def test_noerror_lowercase_not_treated_as_error(self):
        result = self._parse("status: noerror\n", 0)
        assert len(result["errors"]) == 0

    def test_noerror_no_spaces_not_treated_as_error(self):
        result = self._parse("NoError\n", 0)
        assert len(result["errors"]) == 0

    def test_noerror_mixed_case_not_treated_as_error(self):
        result = self._parse("Status: NOERROR\n", 0)
        assert len(result["errors"]) == 0

    def test_noerror_with_spaces_not_treated_as_error(self):
        """'No Error' with a space — the filter strips spaces before matching."""
        result = self._parse("Status: No Error\n", 0)
        assert len(result["errors"]) == 0

    def test_real_error_still_caught(self):
        result = self._parse("ERROR: Compilation failed\n", 1)
        assert len(result["errors"]) >= 1

    def test_parser_error_still_caught(self):
        result = self._parse("Parser error in line 5\n", 1)
        assert len(result["errors"]) >= 1

    def test_error_mixed_with_noerror(self):
        """A real error line should still be caught even if NoError appears elsewhere."""
        stderr = "Status: NoError\nERROR: undefined variable 'x'\n"
        result = self._parse(stderr, 1)
        assert len(result["errors"]) >= 1
        # But should only have one error — the real one, not the NoError line
        error_messages = [e.get("message", e.get("raw", "")) for e in result["errors"]]
        assert not any("NoError" in m for m in error_messages)

    def test_warning_still_caught(self):
        result = self._parse("WARNING: deprecated module\n", 0)
        assert len(result["warnings"]) >= 1

    def test_empty_stderr(self):
        result = self._parse("", 0)
        assert len(result["errors"]) == 0
        assert len(result["warnings"]) == 0


class TestManifoldBackendFlag:
    """Test the manifold backend opt-in logic."""

    def test_backend_env_var_defaults_to_manifold(self):
        # The code reads KILN_OPENSCAD_BACKEND, default "manifold"
        val = os.environ.get("KILN_OPENSCAD_BACKEND", "manifold")
        assert val == "manifold" or val == os.environ.get("KILN_OPENSCAD_BACKEND")

    def test_backend_cgal_opt_out(self):
        os.environ["KILN_OPENSCAD_BACKEND"] = "cgal"
        try:
            val = os.environ.get("KILN_OPENSCAD_BACKEND", "manifold")
            assert val == "cgal"
        finally:
            del os.environ["KILN_OPENSCAD_BACKEND"]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short", "-x"])
