"""Tests for the connection-slot half of the serve-sibling story.

Leftover ``kiln serve`` processes were documented as a memory cost.  On a
Bambu or an Elegoo that was false: those printers ration LAN connection
slots, each server holds one from first use, and enough of them lock the
user out of their own printer.  The pile-up warning said "Nothing is broken
and no print is at risk" to a user whose printer had just stopped answering,
which is what sent them to power-cycle a healthy machine instead of running
``kiln trim`` (2026-08-14 field report).

These tests pin the two pieces that make the real cause visible: the stakes
sentence tracking what is actually plugged in, and the scan that names which
local processes hold a connection right now.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from kiln import serve_siblings

_MY_UID = os.getuid()


def _write_config(text: str) -> None:
    """Write ~/.kiln/config.yaml under the relocated test HOME (conftest)."""
    cfg = Path.home() / ".kiln"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.yaml").write_text(text, encoding="utf-8")


class TestSlotRationedPrinters:
    """Which printers make a pile-up a correctness problem."""

    def test_bambu_in_config_rations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KILN_PRINTER_TYPE", raising=False)
        _write_config(
            "printers:\n"
            "  default:\n"
            "    type: bambu\n"
            "    host: 192.168.1.6\n"
        )
        assert serve_siblings.slot_rationed_printers() is True

    def test_elegoo_in_config_rations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KILN_PRINTER_TYPE", raising=False)
        _write_config("printers:\n  default:\n    type: elegoo\n    host: 10.0.0.4\n")
        assert serve_siblings.slot_rationed_printers() is True

    def test_octoprint_does_not_ration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KILN_PRINTER_TYPE", raising=False)
        _write_config("printers:\n  default:\n    type: octoprint\n    host: h\n")
        assert serve_siblings.slot_rationed_printers() is False

    def test_env_configured_bambu_rations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The env-var door counts too, not just config.yaml."""
        monkeypatch.setenv("KILN_PRINTER_TYPE", "bambu")
        assert serve_siblings.slot_rationed_printers() is True

    def test_missing_config_is_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No config answers False rather than raising into a health check."""
        monkeypatch.delenv("KILN_PRINTER_TYPE", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert serve_siblings.slot_rationed_printers() is False

    def test_list_shaped_printers_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KILN_PRINTER_TYPE", raising=False)
        _write_config("printers:\n  - type: bambu\n    host: 192.168.1.6\n")
        assert serve_siblings.slot_rationed_printers() is True


class TestSlotRationedHosts:
    """The hosts to scan, which is what the report actually needs."""

    def test_returns_the_configured_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KILN_PRINTER_TYPE", raising=False)
        _write_config("printers:\n  default:\n    type: bambu\n    host: 192.168.1.6\n")
        assert serve_siblings.slot_rationed_hosts() == ["192.168.1.6"]

    def test_skips_printers_that_do_not_ration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KILN_PRINTER_TYPE", raising=False)
        _write_config(
            "printers:\n"
            "  a:\n    type: octoprint\n    host: http://octopi.local\n"
            "  b:\n    type: bambu\n    host: 192.168.1.6\n"
        )
        assert serve_siblings.slot_rationed_hosts() == ["192.168.1.6"]

    def test_deduplicates_aliases_of_one_machine(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """config.yaml registers an alias per printer; scan each host once."""
        monkeypatch.delenv("KILN_PRINTER_TYPE", raising=False)
        _write_config(
            "printers:\n"
            "  default:\n    type: bambu\n    host: 192.168.1.6\n"
            "  a1:\n      type: bambu\n      host: 192.168.1.6\n"
        )
        assert serve_siblings.slot_rationed_hosts() == ["192.168.1.6"]

    def test_strips_url_scheme_and_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """lsof matches a bare host, so a URL-shaped config must normalise."""
        monkeypatch.delenv("KILN_PRINTER_TYPE", raising=False)
        _write_config(
            "printers:\n  default:\n    type: elegoo\n    host: http://10.0.0.4:3030/\n"
        )
        assert serve_siblings.slot_rationed_hosts() == ["10.0.0.4"]

    def test_env_host_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KILN_PRINTER_TYPE", "bambu")
        monkeypatch.setenv("KILN_PRINTER_HOST", "192.168.1.9")
        assert "192.168.1.9" in serve_siblings.slot_rationed_hosts()


class TestPrinterSlotReport:
    """The one answer both the terminal and the agent tools read."""

    def _report(self, kiln_count: int, hosts: list[str] | None = None) -> dict:
        held = {
            "supported": True,
            "holders": [{"pid": 100 + i, "is_kiln": True} for i in range(kiln_count)],
            "kiln_count": kiln_count,
        }
        with patch.object(
            serve_siblings, "slot_rationed_hosts", return_value=hosts or ["192.168.1.6"]
        ), patch.object(
            serve_siblings, "printer_connection_holders", return_value=held
        ):
            return serve_siblings.printer_slot_report()

    def test_warns_when_kiln_holds_more_than_one_slot(self) -> None:
        report = self._report(3)
        assert report["checked"] is True
        assert "3 copies" in report["warning"]
        assert "Power-cycling the printer will not help" in report["warning"]
        assert report["hosts"][0]["pids"] == [100, 101, 102]

    def test_silent_at_one_slot(self) -> None:
        """One server holding its own connection is the healthy case."""
        report = self._report(1)
        assert report["checked"] is True
        assert report["warning"] is None

    def test_no_rationing_printer_means_no_scan(self) -> None:
        with patch.object(serve_siblings, "slot_rationed_hosts", return_value=[]), patch.object(
            serve_siblings, "printer_connection_holders"
        ) as holders:
            report = serve_siblings.printer_slot_report()
        holders.assert_not_called()
        assert report["checked"] is False
        assert report["warning"] is None

    def test_unnamed_host_is_not_scanned(self) -> None:
        """A rationing type with no host has nothing to look up."""
        with patch.object(serve_siblings, "slot_rationed_hosts", return_value=[""]):
            report = serve_siblings.printer_slot_report()
        assert report["checked"] is False

    def test_unscannable_system_reports_unchecked(self) -> None:
        with patch.object(
            serve_siblings, "slot_rationed_hosts", return_value=["192.168.1.6"]
        ), patch.object(
            serve_siblings,
            "printer_connection_holders",
            return_value={"supported": False, "holders": [], "kiln_count": 0},
        ):
            report = serve_siblings.printer_slot_report()
        assert report["checked"] is False
        assert report["warning"] is None


class TestWarningStakes:
    """The pile-up warning must not misstate what is at risk."""

    def _warning(self) -> str:
        procs = [{"pid": 100 + i, "age": "05:00"} for i in range(6)]
        with patch.object(serve_siblings, "_list_serve_processes", return_value=procs):
            return serve_siblings.check_serve_siblings()["warning"]

    def test_names_the_lockout_when_a_bambu_is_configured(self) -> None:
        with patch.object(serve_siblings, "slot_rationed_printers", return_value=True):
            warning = self._warning()
        assert "only a few LAN connections" in warning
        assert "times out" in warning
        # The old unconditional reassurance would be a lie here.
        assert "Nothing is broken" not in warning
        # Still true, and still worth saying — a trim is safe mid-print.
        assert "No print already running is at risk" in warning

    def test_keeps_the_reassurance_on_http_printers(self) -> None:
        with patch.object(serve_siblings, "slot_rationed_printers", return_value=False):
            warning = self._warning()
        assert "Nothing is broken and no print is at risk" in warning
        assert "only a few LAN connections" not in warning

    def test_both_branches_still_offer_the_fix(self) -> None:
        for rationed in (True, False):
            with patch.object(
                serve_siblings, "slot_rationed_printers", return_value=rationed
            ):
                warning = self._warning()
            assert "kiln trim" in warning


def _lsof_output(lines: list[str]) -> str:
    header = "COMMAND  PID        USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME"
    return "\n".join([header, *lines]) + "\n"


class TestPrinterConnectionHolders:
    """Ask the kernel who holds a slot, rather than inferring it."""

    def _run(self, output: str, returncode: int = 0) -> dict:
        completed = subprocess.CompletedProcess(
            args=["lsof"], returncode=returncode, stdout=output, stderr=""
        )
        with patch.object(serve_siblings.subprocess, "run", return_value=completed), patch.object(
            serve_siblings,
            "_list_serve_processes",
            return_value=[{"pid": 3673, "age": "05:00"}, {"pid": 21356, "age": "04:00"}],
        ):
            return serve_siblings.printer_connection_holders("192.168.1.6")

    def test_counts_kiln_servers_holding_a_connection(self) -> None:
        result = self._run(
            _lsof_output(
                [
                    "Python  3673 adamarreola   14u  IPv4 0xaf32 0t0 TCP "
                    "192.168.1.93:61864->192.168.1.6:8883 (ESTABLISHED)",
                    "Python 21356 adamarreola    4u  IPv4 0xdd55 0t0 TCP "
                    "192.168.1.93:63786->192.168.1.6:8883 (ESTABLISHED)",
                ]
            )
        )
        assert result["supported"] is True
        assert result["kiln_count"] == 2
        assert [h["pid"] for h in result["holders"]] == [3673, 21356]

    def test_ignores_sockets_that_are_not_established(self) -> None:
        """A closing socket is not a held slot."""
        result = self._run(
            _lsof_output(
                [
                    "Python  3673 adamarreola   14u  IPv4 0xaf32 0t0 TCP "
                    "192.168.1.93:61864->192.168.1.6:8883 (ESTABLISHED)",
                    "Python 99999 adamarreola    5u  IPv4 0xbbbb 0t0 TCP "
                    "192.168.1.93:63999->192.168.1.6:8883 (CLOSE_WAIT)",
                ]
            )
        )
        assert [h["pid"] for h in result["holders"]] == [3673]

    def test_separates_kiln_from_other_software(self) -> None:
        """Bambu Studio holding a slot is a real answer, just a different one."""
        result = self._run(
            _lsof_output(
                [
                    "BambuStu 555 adamarreola   9u  IPv4 0xcccc 0t0 TCP "
                    "192.168.1.93:60000->192.168.1.6:8883 (ESTABLISHED)",
                ]
            )
        )
        assert result["kiln_count"] == 0
        assert result["holders"][0]["is_kiln"] is False
        assert result["holders"][0]["command"] == "BambuStu"

    def test_no_matches_is_zero_not_unknown(self) -> None:
        """lsof exits 1 with no output when nothing matches."""
        result = self._run("", returncode=1)
        assert result["supported"] is True
        assert result["holders"] == []

    def test_missing_lsof_reports_unknown_not_clean(self) -> None:
        """A diagnostic must not pass because its tool was absent."""
        with patch.object(
            serve_siblings.subprocess, "run", side_effect=FileNotFoundError("lsof")
        ):
            result = serve_siblings.printer_connection_holders("192.168.1.6")
        assert result["supported"] is False
        assert result["holders"] == []

    def test_empty_host_is_unknown(self) -> None:
        assert serve_siblings.printer_connection_holders("")["supported"] is False
