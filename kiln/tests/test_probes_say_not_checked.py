"""Setup and diagnostic probes say "not checked" when the one-printer rule says no.

``kiln setup``, ``quickstart``, ``verify`` and ``health`` each read a printer
to prove it is reachable.  When Kiln is driving another machine below the
fleet tier, that read is refused by the one-printer rule -- and each probe
used to report it as a dead connection ("cannot reach", "check your
credentials", "offline").  Nothing is wrong with that printer: Kiln was not
allowed to look.  Pinned here through the real commands: the probe answers in
the rule's own words, says how to move Kiln over, and calls nothing a fault;
any other failure keeps today's words.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from kiln.cli.main import _engaged_elsewhere, cli
from kiln.printers.base import PrinterEngagementError, PrinterError

VERDICT = {
    "code": "TIER_SINGLE_PRINTER_LIMIT",
    "reason": "Kiln is working with voron right now, and with one printer at a time on the free plan.",
    "suggestions": ["Ender still shows its own status on the printer. To point Kiln at it instead, "
                    "hand voron back with hand_back_printer."],
}

WRONG = ("cannot reach", "credentials", "offline", "Status check failed", "Connection test failed")


class _Refused:
    """A printer whose every read the rule refuses."""

    host = "http://ender.local"

    def get_state(self):
        raise PrinterEngagementError(VERDICT)


class _Down:
    """A printer that really is not answering."""

    host = "http://ender.local"

    def get_state(self):
        raise PrinterError("Connection refused")


def test_the_rules_words_are_read_from_its_refusal():
    try:
        raise PrinterError("status failed", cause=PrinterEngagementError(VERDICT))
    except PrinterError as exc:
        said = _engaged_elsewhere(exc)
    assert said is not None and "voron" in said and "hand_back_printer" in said
    assert _engaged_elsewhere(PrinterError("Connection refused")) is None


class TestHealth:
    def _run(self, monkeypatch, adapter, *argv):
        monkeypatch.setattr("kiln.cli.main._get_adapter_from_ctx", lambda ctx: adapter)
        return CliRunner().invoke(cli, ["health", *argv])

    def test_a_printer_kiln_may_not_look_at_is_not_checked_and_not_unhealthy(self, monkeypatch):
        out = self._run(monkeypatch, _Refused(), "--json")
        data = json.loads(out.output)["data"]
        assert data["printer_online"] is None
        assert "hand_back_printer" in data["printer_not_checked"]
        assert data["healthy"] is True
        assert "printer_error" not in data

    def test_the_text_says_not_checked(self, monkeypatch):
        out = self._run(monkeypatch, _Refused())
        assert "Printer: not checked" in out.output and "voron" in out.output
        assert "Printer: offline" not in out.output

    def test_a_printer_that_is_down_is_still_offline(self, monkeypatch):
        data = json.loads(self._run(monkeypatch, _Down(), "--json").output)["data"]
        assert data["printer_online"] is False and data["healthy"] is False
        assert "Connection refused" in data["printer_error"]


class TestQuickstart:
    @pytest.fixture(autouse=True)
    def _one_printer(self):
        with patch("kiln.cli.discovery.discover_printers", return_value=[]), patch(
            "kiln.cli.main._list_printers",
            return_value=[{"name": "ender", "type": "moonraker", "host": "http://ender.local", "active": True}],
        ), patch("kiln.cli.main.load_printer_config", return_value={"type": "moonraker", "host": "http://ender.local"}):
            yield

    def test_the_status_block_says_not_checked(self):
        with patch("kiln.cli.main._make_adapter", return_value=_Refused()):
            data = json.loads(CliRunner().invoke(cli, ["quickstart", "--json"]).output)["data"]
        assert data["status"]["connected"] is None
        assert "hand_back_printer" in data["status"]["not_checked"]
        assert "error" not in data["status"]

    def test_a_printer_that_is_down_still_fails(self):
        with patch("kiln.cli.main._make_adapter", return_value=_Down()):
            data = json.loads(CliRunner().invoke(cli, ["quickstart", "--json"]).output)["data"]
        assert data["status"] == {"error": "Connection refused", "connected": False}


class TestVerify:
    def _check(self, adapter) -> dict:
        with patch("kiln.cli.main.load_printer_config", return_value={"type": "moonraker", "host": "http://ender.local"}), \
             patch("kiln.cli.main._make_adapter", return_value=adapter):
            out = CliRunner().invoke(cli, ["verify", "--json"])
        return next(c for c in json.loads(out.output)["checks"] if c["name"] == "printer_reachable")

    def test_reachability_is_not_checked_and_not_a_failure(self):
        check = self._check(_Refused())
        assert check["ok"] is True and check["detail"].startswith("not checked: ")
        assert "hand_back_printer" in check["detail"]
        assert not any(word in check["detail"] for word in WRONG)

    def test_a_printer_that_is_down_is_still_unreachable(self):
        check = self._check(_Down())
        assert check["ok"] is False and check["detail"].startswith("cannot reach")


class TestSetup:
    """``kiln setup`` is interactive; its connection test is driven through
    the same command with every prompt answered and discovery skipped."""

    def _run(self, adapter) -> str:
        with patch("kiln.cli.main._make_adapter", return_value=adapter), \
             patch("kiln.cli.main.load_printer_config", return_value={"type": "moonraker", "host": "http://ender.local"}), \
             patch("kiln.cli.main.save_printer"), \
             patch("kiln.cli.connection_prompt.ask_connection_needs", return_value={}), \
             patch("kiln.cli.discovery.probe_host", return_value=[]):
            return CliRunner().invoke(
                cli, ["setup", "--skip-discovery"],
                input="ender.local\nmoonraker\nender\nvoron_2\n" + "\n" * 12,
            ).output

    def test_the_connection_is_not_tested_and_no_credentials_are_blamed(self):
        out = self._run(_Refused())
        assert "Connection not tested" in out, out
        assert "hand_back_printer" in out
        assert "may need correct credentials" not in out

    def test_a_printer_that_is_down_still_asks_for_credentials(self):
        out = self._run(_Down())
        assert "Connection test failed" in out and "may need correct credentials" in out
