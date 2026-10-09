"""A printer found on the network is saved at the address it answered on.

Discovery finds OctoPrint on 80 or on 5000 (its own port), Moonraker on 7125,
4408 or 80, and PrusaLink on 80 or 8080, and records the port.  Every door
that saves a found printer has to keep that port, or the saved printer is
looked for on port 80 and never answers.  The bridge's first-printer offer
kept it; ``kiln setup`` and ``kiln quickstart`` saved the bare address.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from kiln.discovery import DiscoveredPrinter


@pytest.mark.parametrize(
    ("found", "saved"),
    [
        (DiscoveredPrinter(host="10.0.0.7", port=5000, printer_type="octoprint"), "10.0.0.7:5000"),
        (DiscoveredPrinter(host="10.0.0.7", port=80, printer_type="octoprint"), "10.0.0.7"),
        (DiscoveredPrinter(host="10.0.0.6", port=7125, printer_type="moonraker"), "10.0.0.6:7125"),
        (DiscoveredPrinter(host="10.0.0.8", port=8080, printer_type="prusalink"), "10.0.0.8:8080"),
        (DiscoveredPrinter(host="10.0.0.5", port=8883, printer_type="bambu"), "10.0.0.5"),
        (DiscoveredPrinter(host="10.0.0.9", port=3030, printer_type="elegoo"), "10.0.0.9"),
    ],
    ids=lambda v: v if isinstance(v, str) else f"{v.printer_type}:{v.port}",
)
def test_the_address_a_found_printer_is_saved_at(found, saved) -> None:
    assert found.address == saved


def test_kiln_setup_saves_a_found_octoprint_at_its_port(tmp_path: Path) -> None:
    from kiln.cli.main import cli

    found = DiscoveredPrinter(host="10.0.0.7", port=5000, printer_type="octoprint", name="OctoPi")
    with (
        patch("kiln.terms.is_current", return_value=True),
        patch("kiln.cli.config.get_config_path", return_value=tmp_path / "config.yaml"),
        patch("kiln.cli.discovery.discover_printers", return_value=[found]),
        patch("kiln.cli.printer_model_prompt.prompt_for_printer_model", return_value=None),
        patch("kiln.cli.main.save_printer", return_value=tmp_path / "config.yaml") as save,
        patch("kiln.cli.main._make_adapter", return_value=MagicMock()),
    ):
        result = CliRunner().invoke(cli, ["setup"], input="1\noctopi\nkey-123\n")

    assert save.called, result.output
    assert save.call_args.args[2] == "10.0.0.7:5000", save.call_args


def test_kiln_quickstart_saves_a_found_octoprint_at_its_port() -> None:
    from kiln.cli.main import cli

    found = DiscoveredPrinter(host="10.0.0.7", port=5000, printer_type="octoprint", name="OctoPi")
    with (
        patch("kiln.cli.main._list_printers", return_value=[]),
        patch("kiln.cli.main.save_printer") as save,
        patch("kiln.cli.main.load_printer_config", side_effect=ValueError("not ready")),
        patch("kiln.cli.discovery.discover_printers", return_value=[found]),
    ):
        result = CliRunner().invoke(cli, ["quickstart"])

    assert save.called, result.output
    assert save.call_args.args[2] == "10.0.0.7:5000", save.call_args


def test_discover_printers_hands_an_agent_the_address_to_register() -> None:
    from kiln.plugins.printer_management_tools import plugin

    tools: dict = {}

    class _FakeMCP:
        def tool(self, *a, **kw):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn
            return deco

    plugin.register(_FakeMCP())
    found = [DiscoveredPrinter(host="10.0.0.7", port=5000, printer_type="octoprint")]
    with patch("kiln.discovery.discover_printers", return_value=found):
        result = tools["discover_printers"]()

    assert result["printers"][0]["address"] == "10.0.0.7:5000"
