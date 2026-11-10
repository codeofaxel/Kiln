"""``kiln install-camera``: the terminal's door onto the camera add-on."""

from __future__ import annotations

import json

import click
import pytest
from click.testing import CliRunner

from kiln import camera_addon
from kiln.cli.install_camera import install_camera, register_install_camera_cli


def _result(status, *, ok=False, **extra):
    words = {"installed": "added", "already_installed": "already", "offline": "offline"}
    message = camera_addon.WORDS.get(words.get(status, status), "x").format(command="kiln install-camera")
    return {"ok": ok, "status": status, "addon": "camera", "message": message,
            "agent_note": "n", "restart_required": False, **extra}


@pytest.fixture
def run(monkeypatch):
    def _run(result, *args, installed=False):
        calls = []

        def fake_install(**kwargs):
            calls.append(kwargs)
            return result

        monkeypatch.setattr(camera_addon, "install", fake_install)
        monkeypatch.setattr(camera_addon, "health", lambda: {"installed": installed, "source": None})
        out = CliRunner().invoke(install_camera, list(args))
        return out, calls

    return _run


def test_typing_the_command_is_the_yes_and_success_is_said_plainly(run):
    out, calls = run(_result("installed", ok=True))
    assert calls == [{"confirm": True}]
    assert out.exit_code == 0
    assert out.output.splitlines() == [
        "  Adding Kiln's camera add-on...",
        "  Camera add-on added. Kiln can show your printer's camera now.",
    ]


def test_already_here_says_so_without_claiming_to_add(run):
    out, _ = run(_result("already_installed", ok=True), installed=True)
    assert out.exit_code == 0
    assert out.output.splitlines() == ["  The camera add-on is already here. Nothing to add."]


def test_a_failure_prints_the_installers_own_line_and_exits_one(run):
    failed = _result("failed", command="kiln install-camera",
                     installer="python -m pip install --target /x thing", detail="a\nERROR: no wheel")
    out, _ = run(failed)
    assert out.exit_code == 1
    lines = out.output.splitlines()
    assert "  Kiln couldn't add the camera add-on here." in lines
    assert "  $ python -m pip install --target /x thing" in lines
    assert "    ERROR: no wheel" in lines
    assert not any("Run this once" in line for line in lines)


def test_no_connection_exits_one_with_the_sentence(run):
    out, _ = run(_result("offline", retryable=True))
    assert out.exit_code == 1
    assert camera_addon.WORDS["offline"] in out.output


def test_json_carries_the_whole_result(run):
    out, _ = run(_result("installed", ok=True), "--json")
    payload = json.loads(out.output)
    assert out.exit_code == 0
    assert payload["status"] == "success" and payload["data"]["status"] == "installed"
    assert "error" not in payload

    out, _ = run(_result("offline", retryable=True), "--json")
    payload = json.loads(out.output)
    assert out.exit_code == 1
    assert payload["status"] == "error"
    assert payload["error"] == {"code": "CAMERA_ADDON_OFFLINE", "message": camera_addon.WORDS["offline"]}
    assert payload["data"]["retryable"] is True


def test_the_command_registers_under_its_name():
    @click.group()
    def cli():
        pass

    register_install_camera_cli(cli)
    assert cli.commands["install-camera"] is install_camera
    assert camera_addon.command().endswith("install-camera")
