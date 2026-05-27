"""Coverage for the start_print -> kiln-pro nozzle-capacity wire.

start_print consults the public-Kiln nozzle bridge before sending the
print command.  The bridge surfaces a per-printer capacity verdict
based on accumulated nozzle wear vs. the planned print's filament
weight.  Three branches matter:

- bridge.available() is False (free tier, no kiln-pro): silent skip;
  start_print returns the normal success payload with no
  ``nozzle_advisory`` field.
- verdict status in {"approaching", "exceeded_p50"}: advisory only;
  start_print attaches ``nozzle_advisory`` to the success payload
  but does NOT block.
- verdict status == "exceeded_p90": refuse the print with code
  ``NOZZLE_CAPACITY_EXCEEDED`` unless ``KILN_SKIP_NOZZLE_CHECK=1``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kiln.printers.base import (
    PrinterFile,
    PrinterState,
    PrinterStatus,
    PrintResult,
)
from kiln.printers.octoprint import OctoPrintAdapter

# Importing the bridge eagerly registers ``kiln._pro_nozzle_bridge`` in
# sys.modules so the kiln package's lazy ``__getattr__`` resolves the
# attribute access that ``unittest.mock.patch`` needs.
import kiln._pro_nozzle_bridge  # noqa: F401
from kiln.server import start_print as server_start_print


def _fake_registry() -> MagicMock:
    """Stand in for the real PrinterRegistry — one named entry suffices."""
    reg = MagicMock()
    reg.count = 1
    reg.list_names.return_value = ["test_printer"]
    return reg


def _make_adapter(*, filament_used_mm: float | None = 5000.0) -> MagicMock:
    """Mock adapter with idle state + one file carrying gcode metadata."""
    adapter = MagicMock(spec=OctoPrintAdapter)
    adapter.start_print.return_value = PrintResult(
        success=True, message="Started printing benchy.gcode."
    )
    adapter.get_state.return_value = PrinterState(
        state=PrinterStatus.IDLE,
        connected=True,
        tool_temp_actual=22.0,
        tool_temp_target=0.0,
        bed_temp_actual=21.0,
        bed_temp_target=0.0,
    )
    adapter.get_job.return_value = MagicMock(file_name=None)
    adapter.list_files.return_value = [
        PrinterFile(
            name="benchy.gcode",
            path="benchy.gcode",
            size_bytes=1234,
            filament_used_mm=filament_used_mm,
            material="PLA",
        ),
    ]
    return adapter


@pytest.fixture
def env_skip_preview():
    """Bypass the preview-token gate for these unit tests."""
    with patch.dict("os.environ", {"KILN_SKIP_PREVIEW_GATE": "1"}):
        yield


@pytest.fixture(autouse=True)
def _bypass_rate_limit():
    """The 5s start_print rate limit is shared module state and would
    cross-pollinate adjacent unit tests; neutralise it here."""
    with patch("kiln.server._check_rate_limit", return_value=None):
        yield


class TestNozzleAdvisorySilentSkip:
    """Free-tier installs have no kiln-pro: the wire degrades silently."""

    @patch("kiln.server._get_registry")
    @patch("kiln.server._get_adapter")
    def test_bridge_unavailable_no_advisory(
        self, mock_get_adapter, mock_get_registry, env_skip_preview
    ):
        mock_get_adapter.return_value = _make_adapter()
        mock_get_registry.return_value = _fake_registry()

        with patch("kiln._pro_nozzle_bridge.available", return_value=False):
            result = server_start_print("benchy.gcode")

        assert result["success"] is True
        assert "nozzle_advisory" not in result


class TestNozzleAdvisoryAttached:
    """Approaching / exceeded_p50 verdicts attach an advisory."""

    @patch("kiln.server._get_registry")
    @patch("kiln.server._get_adapter")
    def test_approaching_attaches_advisory(
        self, mock_get_adapter, mock_get_registry, env_skip_preview
    ):
        mock_get_adapter.return_value = _make_adapter()
        mock_get_registry.return_value = _fake_registry()
        verdict = {
            "status": "approaching",
            "narrative": "Wear projected to cross planning window this print",
            "percent_used": 0.62,
        }
        with patch(
            "kiln._pro_nozzle_bridge.available", return_value=True
        ), patch(
            "kiln._pro_nozzle_bridge.consult_capacity", return_value=verdict
        ):
            result = server_start_print("benchy.gcode")

        assert result["success"] is True, f"unexpected refusal: {result}"
        advisory = result.get("nozzle_advisory")
        assert advisory is not None
        assert advisory["status"] == "approaching"
        assert advisory["percent_used"] == pytest.approx(0.62)
        assert "planning window" in advisory["narrative"]

    @patch("kiln.server._get_registry")
    @patch("kiln.server._get_adapter")
    def test_exceeded_p50_attaches_advisory(
        self, mock_get_adapter, mock_get_registry, env_skip_preview
    ):
        mock_get_adapter.return_value = _make_adapter()
        mock_get_registry.return_value = _fake_registry()
        verdict = {
            "status": "exceeded_p50",
            "narrative": "Wear past population median for this filament class",
            "percent_used": 1.05,
        }
        with patch(
            "kiln._pro_nozzle_bridge.available", return_value=True
        ), patch(
            "kiln._pro_nozzle_bridge.consult_capacity", return_value=verdict
        ):
            result = server_start_print("benchy.gcode")

        assert result["success"] is True
        advisory = result.get("nozzle_advisory")
        assert advisory is not None
        assert advisory["status"] == "exceeded_p50"

    @patch("kiln.server._get_registry")
    @patch("kiln.server._get_adapter")
    def test_unknown_baseline_silently_skips(
        self, mock_get_adapter, mock_get_registry, env_skip_preview
    ):
        mock_get_adapter.return_value = _make_adapter()
        mock_get_registry.return_value = _fake_registry()
        verdict = {
            "status": "unknown_baseline",
            "narrative": "No threshold for this filament/nozzle pair",
        }
        with patch(
            "kiln._pro_nozzle_bridge.available", return_value=True
        ), patch(
            "kiln._pro_nozzle_bridge.consult_capacity", return_value=verdict
        ):
            result = server_start_print("benchy.gcode")

        assert result["success"] is True
        assert "nozzle_advisory" not in result


class TestNozzleAdvisoryHardWarning:
    """exceeded_p90 verdicts refuse the print unless the override env is set."""

    @patch("kiln.server._get_registry")
    @patch("kiln.server._get_adapter")
    def test_exceeded_p90_refuses_print(
        self, mock_get_adapter, mock_get_registry, env_skip_preview
    ):
        adapter = _make_adapter()
        mock_get_adapter.return_value = adapter
        mock_get_registry.return_value = _fake_registry()
        verdict = {
            "status": "exceeded_p90",
            "narrative": "Wear past population p90 — replace nozzle now",
            "percent_used": 1.45,
        }
        with patch(
            "kiln._pro_nozzle_bridge.available", return_value=True
        ), patch(
            "kiln._pro_nozzle_bridge.consult_capacity", return_value=verdict
        ):
            result = server_start_print("benchy.gcode")

        assert result["success"] is False
        assert result["error"]["code"] == "NOZZLE_CAPACITY_EXCEEDED"
        adapter.start_print.assert_not_called()

    @patch.dict("os.environ", {"KILN_SKIP_NOZZLE_CHECK": "1"})
    @patch("kiln.server._get_registry")
    @patch("kiln.server._get_adapter")
    def test_skip_env_var_overrides_p90_block(
        self, mock_get_adapter, mock_get_registry, env_skip_preview
    ):
        adapter = _make_adapter()
        mock_get_adapter.return_value = adapter
        mock_get_registry.return_value = _fake_registry()
        verdict = {
            "status": "exceeded_p90",
            "narrative": "Wear past p90, override engaged",
            "percent_used": 1.45,
        }
        with patch(
            "kiln._pro_nozzle_bridge.available", return_value=True
        ), patch(
            "kiln._pro_nozzle_bridge.consult_capacity", return_value=verdict
        ):
            result = server_start_print("benchy.gcode")

        assert result["success"] is True
        # The advisory is still surfaced when the override is engaged so
        # the agent can pass the warning along to the user even though
        # the block was bypassed.
        assert result.get("nozzle_advisory", {}).get("status") == "exceeded_p90"
        adapter.start_print.assert_called_once()


class TestNozzleAdvisoryNoMetadata:
    """When the printer file lacks filament_used_mm, the wire silently skips."""

    @patch("kiln.server._get_registry")
    @patch("kiln.server._get_adapter")
    def test_missing_filament_metadata_silently_skips(
        self, mock_get_adapter, mock_get_registry, env_skip_preview
    ):
        # filament_used_mm=None matches adapters that don't enrich
        # PrinterFile with gcode metadata.  consult_capacity must not
        # be called when grams cannot be determined.
        mock_get_adapter.return_value = _make_adapter(filament_used_mm=None)
        mock_get_registry.return_value = _fake_registry()
        capacity_mock = MagicMock()
        with patch(
            "kiln._pro_nozzle_bridge.available", return_value=True
        ), patch(
            "kiln._pro_nozzle_bridge.consult_capacity", capacity_mock
        ):
            result = server_start_print("benchy.gcode")

        assert result["success"] is True
        assert "nozzle_advisory" not in result
        capacity_mock.assert_not_called()
