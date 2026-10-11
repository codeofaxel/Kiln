"""Setup and diagnostic probes say "not checked" when the one-printer rule says no.

``kiln setup``, ``quickstart``, ``verify`` and ``health`` each read a printer
to prove it is reachable; ``register_printer`` reads a Bambu printer's spools
to prove the connection; a design check reads the printer's own nozzle
setting.  When Kiln is driving another machine below the
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


class TestRegisterPrinter:
    """Registering a printer is free at every tier.  Registering a Bambu
    printer while Kiln is driving another one: the printer is saved, its
    spools are not checked, and nobody is sent after the access code."""

    def _register(self, monkeypatch, tmp_path, ams):
        from kiln import server

        monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
        monkeypatch.setattr("kiln.cli.config.get_config_path", lambda: tmp_path / "config.yaml")
        monkeypatch.setattr(server.BambuAdapter, "get_ams_status", ams, raising=False)
        return server.register_printer(
            name="x1c-two", printer_type="bambu", host="10.0.0.9",
            api_key="12345678", serial="01P00A000000002", verify_connection=True,
        )

    def test_spools_are_not_checked_and_the_printer_is_still_registered(self, monkeypatch, tmp_path):
        def refused(self):
            raise PrinterEngagementError(VERDICT)

        result = self._register(monkeypatch, tmp_path, refused)
        assert result["success"] is True, result
        assert "hand_back_printer" in result["ams_summary"]["not_checked"]
        assert result["ams_summary"]["refusal"]["code"] == "TIER_SINGLE_PRINTER_LIMIT"
        assert result.get("bambu_ready") is not False
        assert any(w.startswith("Spools not checked: ") for w in result["warnings"])
        assert not any("access code" in w for w in result["warnings"])

    def test_a_printer_that_is_down_still_blames_the_connection(self, monkeypatch, tmp_path):
        def down(self):
            raise PrinterError("Connection refused")

        result = self._register(monkeypatch, tmp_path, down)
        assert result["success"] is True
        assert result["bambu_ready"] is False
        assert "error" in result["ams_summary"]
        assert any("access code" in w for w in result["warnings"])


class TestNozzleSetting:
    """A design check for the second printer runs with the next size down the
    ladder and SAYS the printer's own setting was not read, and why."""

    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        import kiln._pro_nozzle_bridge as bridge
        import kiln.assumed_nozzle as assumed

        monkeypatch.setattr(assumed, "_setting_memo", {})
        monkeypatch.setattr(bridge, "_record_memo", {})
        monkeypatch.setattr(bridge, "_service_down_until", 0.0)
        monkeypatch.setattr(bridge, "consult_recorded_nozzle", lambda pid: {"diameter_mm": None, "answered": True})

    def test_the_check_says_the_setting_was_not_read(self, monkeypatch):
        from kiln.assumed_nozzle import assumed_nozzle

        monkeypatch.setattr("kiln.printer_nozzle_reading.observe_printer_nozzle", lambda pid: None)
        monkeypatch.setattr("kiln.printer_identity.resolve_machine", lambda pid: (object(), pid, "name"))
        monkeypatch.setattr("kiln.printer_nozzle_reading.read_refused", lambda adapter: VERDICT | {"engaged_with": "voron"})
        answer = assumed_nozzle("ender")
        assert answer.source in ("stock", "default")
        assert answer.setting_not_read == "voron"
        assert "ender's own setting was not read: Kiln is working with voron" in answer.sentence()
        assert "working with voron" in answer.to_dict()["note"]

    def test_a_printer_kiln_may_read_says_nothing_of_the_rule(self, monkeypatch):
        from kiln.assumed_nozzle import assumed_nozzle

        monkeypatch.setattr("kiln.printer_nozzle_reading.observe_printer_nozzle", lambda pid: None)
        monkeypatch.setattr("kiln.printer_identity.resolve_machine", lambda pid: (object(), pid, "name"))
        monkeypatch.setattr("kiln.printer_nozzle_reading.read_refused", lambda adapter: None)
        answer = assumed_nozzle("ender")
        assert answer.setting_not_read is None
        assert "was not read" not in answer.sentence()

    def test_the_read_asks_the_rule_about_the_read_itself(self, monkeypatch):
        """The rule is asked about ``read_nozzle_setting``, the command that
        runs, so the refusal is worded for a read of a setting."""
        from kiln import printer_nozzle_reading as reading

        asked = []
        monkeypatch.setattr(
            "kiln.printers.engagement.check_command",
            lambda adapter, action: asked.append(action) or {"code": "TIER_SINGLE_PRINTER_LIMIT"},
        )
        assert reading.read_refused(object()) is not None
        assert asked == ["read_nozzle_setting"]
