"""Live integration smoke tests against a real OctoPrint instance.

These tests are SKIPPED by default. Run them with:

    pytest tests/test_live_octoprint.py -m live -v

Required environment variables:
    KILN_LIVE_OCTOPRINT_HOST  — e.g. http://192.168.1.50:5000
    KILN_LIVE_OCTOPRINT_KEY   — OctoPrint API key

Optional:
    KILN_LIVE_TEST_GCODE      — path to a small .gcode file for upload tests
                                (defaults to creating a minimal temp file)

You can run OctoPrint locally via Docker for CI:

    docker run -d -p 5000:5000 octoprint/octoprint:latest

Then grab the API key from the setup wizard at http://localhost:5000.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from kiln.printers.base import PrinterStatus
from kiln.printers.octoprint import OctoPrintAdapter

# ---------------------------------------------------------------------------
# Skip all tests in this module unless running with -m live
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.live

LIVE_HOST = os.environ.get("KILN_LIVE_OCTOPRINT_HOST", "")
LIVE_KEY = os.environ.get("KILN_LIVE_OCTOPRINT_KEY", "")

skip_reason = ""
if not LIVE_HOST:
    skip_reason = "KILN_LIVE_OCTOPRINT_HOST not set"
elif not LIVE_KEY:
    skip_reason = "KILN_LIVE_OCTOPRINT_KEY not set"

if skip_reason:
    pytestmark = [pytestmark, pytest.mark.skip(reason=skip_reason)]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapter() -> OctoPrintAdapter:
    """Create a live OctoPrintAdapter from environment variables."""
    return OctoPrintAdapter(
        host=LIVE_HOST,
        api_key=LIVE_KEY,
        timeout=15,
        retries=2,
    )


@pytest.fixture(scope="module")
def test_gcode_path() -> Path:
    """Return a path to a small test G-code file.

    Uses KILN_LIVE_TEST_GCODE if set, otherwise creates a minimal temp file.
    """
    env_path = os.environ.get("KILN_LIVE_TEST_GCODE")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p

    # Create a minimal valid G-code file
    tmp = tempfile.NamedTemporaryFile(
        suffix=".gcode",
        prefix="kiln_smoke_",
        delete=False,
        mode="w",
    )
    tmp.write("; Kiln smoke test file\n")
    tmp.write("G28 ; Home all axes\n")
    tmp.write("M104 S0 ; Ensure hotend off\n")
    tmp.write("M140 S0 ; Ensure bed off\n")
    tmp.close()
    return Path(tmp.name)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLiveConnection:
    """Verify basic connectivity to the OctoPrint instance."""

    def test_get_state(self, adapter: OctoPrintAdapter):
        """Adapter can connect and return a valid printer state."""
        state = adapter.get_state()
        assert state.connected is True
        assert state.state != PrinterStatus.OFFLINE
        assert state.state != PrinterStatus.UNKNOWN

    def test_capabilities(self, adapter: OctoPrintAdapter):
        """Adapter reports expected OctoPrint capabilities."""
        caps = adapter.capabilities
        assert caps.can_upload is True
        assert caps.can_set_temp is True
        assert caps.can_send_gcode is True
        assert caps.can_pause is True

    def test_get_job(self, adapter: OctoPrintAdapter):
        """get_job returns without error (may be idle or printing)."""
        job = adapter.get_job()
        # completion is None when idle, 0-100 when printing
        assert job.completion is None or 0 <= job.completion <= 100


class TestLiveFiles:
    """Test file operations against the real OctoPrint instance."""

    def test_list_files(self, adapter: OctoPrintAdapter):
        """list_files returns a list (may be empty on fresh instance)."""
        files = adapter.list_files()
        assert isinstance(files, list)
        for f in files:
            assert f.name
            assert f.path

    def test_upload_and_delete(
        self,
        adapter: OctoPrintAdapter,
        test_gcode_path: Path,
    ):
        """Upload a test file, verify it appears in the file list, then delete it."""
        # Upload
        result = adapter.upload_file(str(test_gcode_path))
        assert result.success is True
        assert result.file_name

        # Verify it shows up
        files = adapter.list_files()
        names = [f.name for f in files]
        assert result.file_name in names

        # Clean up — delete the file
        deleted = adapter.delete_file(result.file_name)
        assert deleted is True

        # Verify deletion
        files_after = adapter.list_files()
        names_after = [f.name for f in files_after]
        assert result.file_name not in names_after


class TestLiveTemperature:
    """Test temperature reads (not writes — too dangerous for automated tests)."""

    def test_read_temperatures(self, adapter: OctoPrintAdapter):
        """State includes plausible temperature readings."""
        state = adapter.get_state()
        # Room temp range check — tool should be between -10 and 350
        if state.tool_temp_actual is not None:
            assert -10 < state.tool_temp_actual < 350
        if state.bed_temp_actual is not None:
            assert -10 < state.bed_temp_actual < 200
