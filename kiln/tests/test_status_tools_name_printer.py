"""The status TOOLS name the machine they are describing.

Engine-level tests cover the resolver; these call the tools an agent, the
REST API and the web monitor actually invoke, because that wrapper is where
the field either reaches a caller or does not.
"""
import textwrap
from unittest.mock import MagicMock, PropertyMock, patch

from kiln import printer_model_resolver as pmr
from kiln.printers.octoprint import OctoPrintAdapter
from kiln.server import print_status_lite, printer_status


def _config(tmp_path, monkeypatch, body: str) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(textwrap.dedent(body))
    monkeypatch.setattr(pmr, "_CONFIG_PATH", cfg)
    pmr.invalidate_cache()


def _adapter(state, job, caps):
    adapter = MagicMock(spec=OctoPrintAdapter)
    adapter.get_state.return_value = state
    adapter.get_job.return_value = job
    type(adapter).capabilities = PropertyMock(return_value=caps)
    return adapter


class TestStatusToolsNameTheirPrinter:
    def test_printer_status_reports_the_configured_name(
        self, tmp_path, monkeypatch, mock_printer_state_idle, mock_job_progress, mock_capabilities
    ):
        _config(tmp_path, monkeypatch, """
            active_printer: workshop-a1
            printers:
              workshop-a1: {host: 192.168.1.50, type: bambu}
        """)
        with patch("kiln.server._get_adapter") as get_adapter:
            get_adapter.return_value = _adapter(
                mock_printer_state_idle, mock_job_progress, mock_capabilities
            )
            result = printer_status()

        assert result["success"] is True
        assert result["printer_name"] == "workshop-a1"

    def test_printer_status_omits_the_key_when_the_config_names_nothing(
        self, tmp_path, monkeypatch, mock_printer_state_idle, mock_job_progress, mock_capabilities
    ):
        # Absent beats a placeholder: a caller can tell "unnamed" from "named
        # something we did not pass on" only if the key is missing.
        _config(tmp_path, monkeypatch, """
            active_printer: default
            printers:
              default: {host: 192.168.1.50}
        """)
        with patch("kiln.server._get_adapter") as get_adapter:
            get_adapter.return_value = _adapter(
                mock_printer_state_idle, mock_job_progress, mock_capabilities
            )
            result = printer_status()

        assert result["success"] is True
        assert "printer_name" not in result

    def test_a_status_read_survives_an_unreadable_config(
        self, tmp_path, monkeypatch, mock_printer_state_idle, mock_job_progress, mock_capabilities
    ):
        # The label is a convenience. Nobody watching a print should lose the
        # reading because the name could not be worked out.
        monkeypatch.setattr(
            pmr, "resolve_active_printer_name", MagicMock(side_effect=OSError("nope"))
        )
        with patch("kiln.server._get_adapter") as get_adapter:
            get_adapter.return_value = _adapter(
                mock_printer_state_idle, mock_job_progress, mock_capabilities
            )
            result = printer_status()

        assert result["success"] is True
        assert "printer_name" not in result

    def test_status_lite_answers_for_the_printer_it_was_asked_about(
        self, tmp_path, monkeypatch, mock_printer_state_idle, mock_job_progress
    ):
        # An explicit argument is its own answer and must outrank the config's
        # active printer, or the lite tool would label another machine's row.
        _config(tmp_path, monkeypatch, """
            active_printer: workshop-a1
            printers:
              workshop-a1: {host: 192.168.1.50}
              basement-mk4: {host: 192.168.1.51}
        """)
        adapter = _adapter(mock_printer_state_idle, mock_job_progress, None)
        with patch("kiln.server._get_registry") as registry:
            registry.return_value.get.return_value = adapter
            result = print_status_lite(printer_name="basement-mk4")

        assert result["printer_name"] == "basement-mk4"

    def test_status_lite_falls_back_to_the_active_printer(
        self, tmp_path, monkeypatch, mock_printer_state_idle, mock_job_progress
    ):
        _config(tmp_path, monkeypatch, """
            active_printer: workshop-a1
            printers:
              workshop-a1: {host: 192.168.1.50}
        """)
        with patch("kiln.server._get_adapter") as get_adapter:
            get_adapter.return_value = _adapter(
                mock_printer_state_idle, mock_job_progress, None
            )
            result = print_status_lite()

        assert result["printer_name"] == "workshop-a1"
