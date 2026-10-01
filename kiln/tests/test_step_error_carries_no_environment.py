"""A failed STEP conversion's error never carries the process environment.

The FreeCAD launcher prints the whole environment ahead of the script's own
output.  When a conversion produced no result, the error quoted the first 300
characters of that output -- which, on a machine converting through the
launcher, is the start of the environment, alphabetically: the names that
sort first include the ones API keys live under.  The error is shown to the
person, handed to an agent, and logged.  Seen 2026-09-30 in a test's own
failure output.

Pinned at the function that words the error: what the child said itself
survives, and nothing the environment holds does.
"""

from __future__ import annotations

import subprocess

import pytest

from kiln.step_import import StepImportError, _parse_kiln_result


def _finished(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["FreeCAD", "-c", "script.py"], returncode=0, stdout=stdout, stderr="")


def test_the_environment_the_launcher_printed_is_not_in_the_error(monkeypatch) -> None:
    monkeypatch.setenv("AAA_SERVICE_KEY", "sk-live-0123456789abcdef")
    stdout = "AAA_SERVICE_KEY=sk-live-0123456789abcdef\nHOME=/Users/someone\nSTEP read failed: no shapes in file\n"
    with pytest.raises(StepImportError) as caught:
        _parse_kiln_result(_finished(stdout), "FreeCAD")
    message = str(caught.value)
    assert "sk-live-0123456789abcdef" not in message and "/Users/someone" not in message
    assert "STEP read failed: no shapes in file" in message


def test_a_value_that_spans_lines_goes_whole(monkeypatch) -> None:
    """A key pasted with its line breaks is one variable and three lines."""
    key = "-----BEGIN KEY-----\nMIIEvQIBADANBg\n-----END KEY-----"
    monkeypatch.setenv("AAA_SIGNING_KEY", key)
    with pytest.raises(StepImportError) as caught:
        _parse_kiln_result(_finished(f"AAA_SIGNING_KEY={key}\nconversion wrote nothing\n"), "FreeCAD")
    message = str(caught.value)
    assert "MIIEvQIBADANBg" not in message and "BEGIN KEY" not in message
    assert "conversion wrote nothing" in message


def test_a_variable_the_launcher_added_itself_is_dropped_too() -> None:
    """Not in this process's environment, still a NAME=value line."""
    with pytest.raises(StepImportError) as caught:
        _parse_kiln_result(_finished("LAUNCHER_ONLY_TOKEN=abc123def456\n"), "FreeCAD")
    assert "abc123def456" not in str(caught.value)


def test_one_variable_is_never_cut_out_of_the_middle_of_another(monkeypatch) -> None:
    """``PWD=/work`` sits inside ``OLDPWD=/work/kiln``; removing it there
    would leave the tail of the other value behind."""
    monkeypatch.setenv("PWD", "/work")
    with pytest.raises(StepImportError) as caught:
        _parse_kiln_result(_finished("OLDPWD=/work/kiln-secret-dir\nPWD=/work\nno shapes\n"), "FreeCAD")
    message = str(caught.value)
    assert "kiln-secret-dir" not in message and message.endswith("stdout: no shapes")
