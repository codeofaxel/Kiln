"""Adding the camera add-on: when nothing runs, what runs, and that a failure leaves nothing behind."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from kiln import camera_addon
from tests._camera_addon_fakes import (
    NO_BUILD_ANSWERS,
    PROTOCOLS_PLAIN,
    FakeInstaller,
    a_system,
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


def _as_hermes_runs_kiln(monkeypatch) -> dict[str, str]:
    """The keeper and the command line Kiln's own Hermes package sets for the copy it runs."""
    launcher = Path(__file__).resolve().parents[2] / "plugins" / "hermes" / "mcp.json"
    env = json.loads(launcher.read_text(encoding="utf-8"))["mcpServers"]["kiln"]["env"]
    for name in ("KILN_UPDATE_COMMAND", "KILN_CLI"):
        monkeypatch.setenv(name, env[name])
    return env


# --- nothing runs ------------------------------------------------------------


def test_the_hosted_server_never_installs(home, monkeypatch):
    monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
    runner = FakeInstaller()
    result = _install(runner)
    assert (result["ok"], result["status"]) == (False, "not_here")
    assert runner.calls == [] and _nothing_left()


def test_a_managed_copy_waits_for_a_yes_like_any_other(home, monkeypatch):
    _as_hermes_runs_kiln(monkeypatch)
    runner = FakeInstaller()
    result = _install(runner, confirm=False, find_spec=no_installer, which=lambda _name: "/somewhere/uv")
    assert (result["ok"], result["status"]) == (False, "needs_confirmation")
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


def test_a_managed_copy_with_no_installer_is_handed_its_launchers_own_line(home, monkeypatch):
    """The line runs the same release the launcher runs; the keeper's update would not bring the add-on."""
    hermes = _as_hermes_runs_kiln(monkeypatch)
    runner = FakeInstaller()
    result = _install(runner, find_spec=no_installer)
    assert (result["ok"], result["status"]) == (False, "failed")
    assert result["command"] == f"{hermes['KILN_CLI']} install-camera"
    assert result["command"] in result["message"]
    assert hermes["KILN_UPDATE_COMMAND"] not in result["message"]
    assert runner.calls == [] and _nothing_left()


# --- something runs ----------------------------------------------------------


def test_installed_through_this_interpreters_installer_into_a_staging_folder(home):
    runner = FakeInstaller()
    result = _install(runner)

    assert (result["ok"], result["status"]) == (True, "installed")
    assert result["message"] == camera_addon.WORDS["added"]
    assert runner.installs == [
        [sys.executable, "-m", "pip", "install", "--upgrade", "--no-deps", "--only-binary", ":all:",
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
        ["/somewhere/uv", "pip", "install", "--no-deps", "--only-binary", ":all:",
         "--target", str(_staging()), "--python", sys.executable, camera_addon.PIP_REQUIREMENT]
    ]


def test_a_managed_copy_is_added_to_kilns_own_folder_on_a_yes(home, monkeypatch):
    """Hermes runs Kiln through uvx, where there is no pip: uv puts the add-on in Kiln's folder."""
    _as_hermes_runs_kiln(monkeypatch)
    runner = FakeInstaller()
    result = _install(runner, find_spec=no_installer, which=lambda name: "/somewhere/uv")
    assert (result["ok"], result["status"]) == (True, "installed")
    assert result["message"] == camera_addon.WORDS["added"]
    assert runner.installs == [
        ["/somewhere/uv", "pip", "install", "--no-deps", "--only-binary", ":all:",
         "--target", str(_staging()), "--python", sys.executable, camera_addon.PIP_REQUIREMENT]
    ]
    assert camera_addon.source() == "addon"
    assert sorted(p.name for p in camera_addon.addon_dir().parent.iterdir()) == ["camera"]


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


# --- a computer no build of the add-on fits ----------------------------------

_RASPBERRY_PI = 'PRETTY_NAME="Raspbian GNU/Linux 12 (bookworm)"\nID=raspbian\nID_LIKE=debian\n'


class _Needs:
    def camera_needs_addon(self):
        return True


@pytest.mark.parametrize("installer", sorted(NO_BUILD_ANSWERS))
def test_no_build_for_this_computer_is_said_with_the_systems_own_line(home, monkeypatch, tmp_path, installer):
    a_system(monkeypatch, tmp_path, _RASPBERRY_PI)
    result = _install(FakeInstaller(returncode=1, stderr=NO_BUILD_ANSWERS[installer]))
    assert (result["ok"], result["status"]) == (False, "no_build")
    assert result["command"] == "sudo apt install ffmpeg"
    assert result["message"] == camera_addon.WORDS["no_build"].format(command="sudo apt install ffmpeg")
    assert "retryable" not in result
    assert camera_addon.ffmpeg_path() is None and _nothing_left()


def test_no_build_on_a_system_kiln_cannot_name_asks_for_its_package_manager(home):
    result = _install(FakeInstaller(returncode=1, stderr=NO_BUILD_ANSWERS["pip"]))
    assert (result["status"], result["message"]) == ("no_build", camera_addon.WORDS["no_build_any"])
    assert "command" not in result


@pytest.mark.parametrize(
    "os_release, line",
    [
        ("ID=debian\n", "sudo apt install ffmpeg"),
        ("ID=ubuntu\nID_LIKE=debian\n", "sudo apt install ffmpeg"),
        (_RASPBERRY_PI, "sudo apt install ffmpeg"),
        ('ID=linuxmint\nID_LIKE="ubuntu debian"\n', "sudo apt install ffmpeg"),
        ("ID=alpine\n", "apk add ffmpeg"),
        ('ID="fedora"\n', "sudo dnf install ffmpeg"),
        ('ID="rocky"\nID_LIKE="rhel centos fedora"\n', "sudo dnf install ffmpeg"),
        ("ID=arch\n", "sudo pacman -S ffmpeg"),
        ("ID=manjaro\nID_LIKE=arch\n", "sudo pacman -S ffmpeg"),
        ('ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n', None),
        ("NAME=Something\n# ID=debian\n", None),
    ],
)
def test_the_systems_own_line_follows_os_release(home, monkeypatch, tmp_path, os_release, line):
    a_system(monkeypatch, tmp_path, os_release)
    assert camera_addon._system_command() == line


def test_os_release_is_read_from_the_systems_second_place_too(home, monkeypatch, tmp_path):
    second = tmp_path / "usr-lib-os-release"
    second.write_text("ID=alpine\n", encoding="utf-8")
    monkeypatch.setattr(camera_addon, "_OS_RELEASE_FILES", (tmp_path / "missing", second))
    assert camera_addon._system_command() == "apk add ffmpeg"
    monkeypatch.setattr(camera_addon, "_OS_RELEASE_FILES", (tmp_path / "missing",))
    assert camera_addon._system_command() is None


def test_after_no_build_this_process_stops_offering_the_addon(home, monkeypatch, tmp_path):
    a_system(monkeypatch, tmp_path, "ID=alpine\n")
    said = _install(FakeInstaller(returncode=1, stderr=NO_BUILD_ANSWERS["uv-platform-tag"]))["message"]

    block = camera_addon.block(moment="snapshot")
    assert (block["status"], block["message"], block["command"]) == ("no_build", said, "apk add ffmpeg")
    assert "install" not in block and "ask" not in block
    assert str(camera_addon.refusal()) == said
    assert camera_addon.refusal_fields(camera_addon.refusal()) == {camera_addon.RESULT_KEY: block}
    setup = camera_addon.block(printer_name="Garage P2S", moment="setup")
    assert setup["message"] == f"Garage P2S is ready. {said}"
    for door in ("cli", "tool"):
        assert camera_addon.setup_line(_Needs(), door=door) == f"Camera: {said}"

    monkeypatch.setattr(camera_addon, "_LEARNED", {})  # the next process
    assert camera_addon.block(moment="snapshot")["status"] == "needed"


def test_an_install_that_works_later_lets_the_offer_return(home):
    _install(FakeInstaller(returncode=1, stderr=NO_BUILD_ANSWERS["pip"]))
    assert camera_addon.block(moment="health")["status"] == "no_build"

    assert _install(FakeInstaller())["status"] == "installed"
    shutil.rmtree(camera_addon.addon_dir())
    forget_package()
    assert camera_addon.ffmpeg_path() is None
    assert camera_addon.block(moment="health")["status"] == "needed"
