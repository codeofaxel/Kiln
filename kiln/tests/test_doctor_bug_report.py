"""``kiln doctor --bug-report``: the checkup as a report the person sends themselves.

The report goes on the clipboard and the support page opens; nothing leaves
the computer on its own.  What the report carries is cleaned first: this
computer's printer addresses and codes, secrets, network addresses and the
account name in a home folder never reach the clipboard.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from click.testing import CliRunner

from kiln.cli import doctor_report
from kiln.cli.main import cli


@pytest.fixture
def hands(monkeypatch, tmp_path):
    """Stand-ins for the clipboard program and the browser, recording what
    each was handed."""
    monkeypatch.setenv("HOME", str(tmp_path))
    seen: dict[str, list] = {"clipboard": [], "browser": []}

    def run(command, **kwargs):
        seen["clipboard"].append((command, kwargs["input"].decode("utf-8")))
        return subprocess.CompletedProcess(command, 0)

    def open_url(url, **how):
        seen["browser"].append(url)
        return True

    monkeypatch.setattr("kiln.cli.doctor_report.sys.platform", "darwin")
    monkeypatch.setattr(doctor_report.run_program, "run", run)
    monkeypatch.setattr(doctor_report.run_program, "open_url", open_url)
    return seen


def test_the_report_is_copied_and_the_support_page_opened(hands):
    from kiln import __version__

    result = CliRunner().invoke(cli, ["doctor", "--bug-report"])

    [(command, copied)] = hands["clipboard"]
    assert command == ["pbcopy"]
    assert f"Kiln {__version__}" in copied
    assert "[ok] python:" in copied
    assert copied.rstrip().endswith("What I was doing when it went wrong:")
    assert hands["browser"] == [doctor_report.SUPPORT_URL]
    assert "on your clipboard" in result.output
    assert "Nothing has been sent." in result.output


def test_without_a_clipboard_the_report_is_printed_with_the_link(hands, monkeypatch):
    def no_clipboard(command, **kwargs):
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(doctor_report.run_program, "run", no_clipboard)
    monkeypatch.setattr(doctor_report.run_program, "open_url", lambda url, **how: False)

    result = CliRunner().invoke(cli, ["doctor", "--bug-report"])

    assert "Here is a bug report" in result.output
    assert "    Kiln checkup (kiln doctor --bug-report)" in result.output
    assert f"Open {doctor_report.SUPPORT_URL}" in result.output
    assert "Nothing has been sent." in result.output


def test_json_carries_the_report_and_opens_nothing(hands):
    result = CliRunner().invoke(cli, ["doctor", "--json", "--bug-report"])

    payload = json.loads(result.output)
    assert payload["bug_report"]["text"].startswith("Kiln checkup")
    assert payload["bug_report"]["send_at"] == doctor_report.SUPPORT_URL
    assert hands["clipboard"] == [] and hands["browser"] == []


def test_plain_doctor_copies_nothing(hands):
    CliRunner().invoke(cli, ["doctor"])
    assert hands["clipboard"] == [] and hands["browser"] == []


def test_what_locates_or_unlocks_a_machine_never_reaches_the_report(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KILN_PRINTER_SERIAL", "01P00C123456789")
    monkeypatch.setenv("KILN_PRINTER_HOST", "printer.example.org")
    checks = [
        {"name": "printer", "ok": False, "detail": "Garage X1C (serial 01P00C123456789) at printer.example.org did not answer"},
        {"name": "network", "ok": True, "detail": "reached 192.168.1.40:8883 and fe80::1ff:fe23:4567:890a"},
        {"name": "config", "ok": True, "warn": True, "detail": "read /Users/jane/.kiln/config.yaml, api_key=sk-abcdef0123456789abcdef"},
    ]

    report = doctor_report.build_report(checks, kiln_version="9.9.9")

    for leaked in ("01P00C123456789", "printer.example.org", "192.168.1.40", "fe80::1ff", "jane", "sk-abcdef"):
        assert leaked not in report, leaked
    assert "Garage X1C" in report  # a printer's name says which line is which; it stays
    assert "[FAILED] printer:" in report and "[warning] config:" in report


def test_a_long_checkup_keeps_every_problem_and_fits_the_form():
    problems = [{"name": f"broken_{i}", "ok": False, "detail": "did not work"} for i in range(5)]
    passing = [{"name": f"fine_{i}", "ok": True, "detail": "x" * 300} for i in range(60)]

    report = doctor_report.build_report(passing + problems, kiln_version="9.9.9")

    assert len(report) <= doctor_report.REPORT_ROOM + 200
    assert all(f"[FAILED] broken {i}:" in report for i in range(5))
    assert report.index("[FAILED]") < report.index("[ok]")
    assert "passing checks left out so the report fits the form" in report


def test_windows_clipboard_gets_utf16(monkeypatch):
    taken = []
    monkeypatch.setattr("kiln.cli.doctor_report.sys.platform", "win32")
    monkeypatch.setattr(
        doctor_report.run_program,
        "run",
        lambda command, **kwargs: taken.append((command, kwargs["input"])) or subprocess.CompletedProcess(command, 0),
    )

    assert doctor_report.copy_to_clipboard("Drucker Größe") is True
    [(command, data)] = taken
    assert command == ["clip"] and data.decode("utf-16") == "Drucker Größe"
