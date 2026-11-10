"""Adding the camera add-on: when nothing runs, what runs, and that a failure leaves nothing behind."""

from __future__ import annotations

import os
import sys

import pytest

from kiln import camera_addon
from tests._camera_addon_fakes import (
    PROTOCOLS_PLAIN,
    FakeInstaller,
    bare_computer,
    forget_package,
    has_pip,
    no_installer,
    write_package,
)
from tests.test_camera_addon import BANNED


@pytest.fixture
def home(monkeypatch, tmp_path):
    yield bare_computer(monkeypatch, tmp_path)
    forget_package()


def _install(runner, *, confirm=True, find_spec=has_pip, which=lambda _name: None):
    result = camera_addon.install(confirm=confirm, runner=runner, find_spec=find_spec, which=which)
    said = result["message"].replace(result.get("command", "\0"), "").lower()
    assert not [word for word in BANNED if word in said], result["message"]
    assert result["restart_required"] is False and result["agent_note"]
    return result


def _staging():
    final = camera_addon.addon_dir()
    return final.with_name(f"camera.staging-{os.getpid()}")


def _nothing_left():
    addons = camera_addon.addon_dir().parent
    return not addons.exists() or list(addons.iterdir()) == []


# --- nothing runs ------------------------------------------------------------


def test_the_hosted_server_never_installs(home, monkeypatch):
    monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
    runner = FakeInstaller()
    result = _install(runner)
    assert (result["ok"], result["status"]) == (False, "not_here")
    assert runner.calls == [] and _nothing_left()


def test_a_managed_install_is_never_added_to(home, monkeypatch):
    monkeypatch.setenv("KILN_UPDATE_COMMAND", "hermes plugins update kiln")
    runner = FakeInstaller()
    result = _install(runner)
    assert (result["ok"], result["status"]) == (False, "managed")
    assert result["command"] == "hermes plugins update kiln"
    assert runner.calls == [] and _nothing_left()


def test_already_here_changes_nothing(home):
    write_package(camera_addon.addon_dir())
    runner = FakeInstaller()
    result = _install(runner)
    assert (result["ok"], result["status"]) == (True, "already_installed")
    assert runner.calls == []


def test_without_a_yes_nothing_runs(home):
    runner = FakeInstaller()
    result = _install(runner, confirm=False)
    assert (result["ok"], result["status"]) == (False, "needs_confirmation")
    assert result["message"] == camera_addon.WORDS["confirm"]
    assert runner.calls == [] and _nothing_left()


def test_no_installer_on_this_computer_fails_with_the_command(home):
    runner = FakeInstaller()
    result = _install(runner, find_spec=no_installer)
    assert (result["ok"], result["status"]) == (False, "failed")
    assert result["command"] == "kiln install-camera" and result["command"] in result["message"]
    assert "no installer" in result["detail"]
    assert runner.calls == [] and not camera_addon.addon_dir().parent.exists()


# --- something runs ----------------------------------------------------------


def test_installed_through_this_interpreters_installer_into_a_staging_folder(home):
    runner = FakeInstaller()
    result = _install(runner)

    assert (result["ok"], result["status"]) == (True, "installed")
    assert result["message"] == camera_addon.WORDS["added"]
    assert runner.installs == [
        [sys.executable, "-m", "pip", "install", "--upgrade", "--no-deps",
         "--target", str(_staging()), camera_addon.PIP_REQUIREMENT]
    ]
    program = camera_addon.ffmpeg_path()
    assert program and camera_addon.source() == "addon"
    assert [check[1:] for check in runner.checks] == [["-hide_banner", "-protocols"]]
    assert sorted(p.name for p in camera_addon.addon_dir().parent.iterdir()) == ["camera"]


def test_without_that_installer_the_other_one_runs(home):
    runner = FakeInstaller()
    result = _install(runner, find_spec=no_installer, which=lambda name: "/somewhere/uv")
    assert result["status"] == "installed"
    assert runner.installs == [
        ["/somewhere/uv", "pip", "install", "--no-deps", "--target", str(_staging()),
         "--python", sys.executable, camera_addon.PIP_REQUIREMENT]
    ]


def test_no_connection_is_retryable_and_leaves_nothing(home):
    runner = FakeInstaller(returncode=1, stderr="Failed to establish a new connection")
    result = _install(runner)
    assert (result["ok"], result["status"], result["retryable"]) == (False, "offline", True)
    assert result["message"] == camera_addon.WORDS["offline"]
    assert camera_addon.ffmpeg_path() is None and _nothing_left()


def test_an_installer_that_fails_hands_back_the_command_and_leaves_nothing(home):
    runner = FakeInstaller(returncode=1, stderr="line one\nERROR: no matching wheel")
    result = _install(runner)
    assert (result["ok"], result["status"]) == (False, "failed")
    assert result["command"] == "kiln install-camera"
    assert "no matching wheel" in result["detail"] and "--target" in result["installer"]
    assert "no matching wheel" not in result["message"]
    assert camera_addon.ffmpeg_path() is None and _nothing_left()


def test_an_installer_that_cannot_start_is_a_failure_not_an_exception(home):
    result = _install(FakeInstaller(raises=OSError("gone")))
    assert (result["ok"], result["status"]) == (False, "failed")
    assert _nothing_left()


def test_an_install_interrupted_part_way_leaves_nothing(home):
    class Interrupted(FakeInstaller):
        def __call__(self, argv, **kwargs):
            write_package(_staging())
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        camera_addon.install(confirm=True, runner=Interrupted(), find_spec=has_pip, which=lambda _n: None)
    assert camera_addon.ffmpeg_path() is None and _nothing_left()


@pytest.mark.parametrize(
    "runner",
    [
        pytest.param(FakeInstaller(writes=False), id="installer-put-no-program-there"),
        pytest.param(FakeInstaller(check_returncode=1), id="program-does-not-run"),
        pytest.param(FakeInstaller(protocols=PROTOCOLS_PLAIN), id="program-opens-no-secure-stream"),
    ],
)
def test_a_clean_install_that_cannot_be_used_is_a_failure_and_is_removed(home, runner):
    runner.calls.clear()
    result = _install(runner)
    assert (result["ok"], result["status"]) == (False, "failed")
    assert len(runner.installs) == 1
    assert camera_addon.ffmpeg_path() is None and _nothing_left()


def test_a_broken_folder_from_before_is_replaced(home):
    broken = camera_addon.addon_dir()
    broken.mkdir(parents=True)
    (broken / "leftover.txt").write_text("x", encoding="utf-8")
    assert camera_addon.ffmpeg_path() is None

    assert _install(FakeInstaller())["status"] == "installed"
    assert not (broken / "leftover.txt").exists() and camera_addon.ffmpeg_path()
