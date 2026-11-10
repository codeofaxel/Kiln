"""The camera add-on: found fresh on every look, said in plain words, usable with no restart."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiln import camera_addon
from kiln.printers.base import CameraStreamError, ExternalCamera
from tests._camera_addon_fakes import (
    NO_BUILD_ANSWERS,
    PROTOCOLS_PLAIN,
    FakeInstaller,
    a_system,
    bare_computer,
    forget_package,
    has_pip,
    program_on_this_computer,
    write_package,
)

BANNED = ("ffmpeg", "rtsp", "port", "pip", "imageio", "322", "6000")


class _Needs:
    def __init__(self, answer=True):
        self._answer = answer

    def camera_needs_addon(self):
        return self._answer


@pytest.fixture
def home(monkeypatch, tmp_path):
    yield bare_computer(monkeypatch, tmp_path)
    forget_package()


def _install(**kw):
    return camera_addon.install(confirm=True, find_spec=has_pip, which=lambda _n: None, **kw)


# --- no restart --------------------------------------------------------------


def test_addon_works_in_the_process_that_installed_it(home):
    module_before = sys.modules["kiln.camera_addon"]

    result = _install(runner=FakeInstaller())

    assert result["status"] == "installed" and result["restart_required"] is False
    program = camera_addon.ffmpeg_path()
    assert program and Path(program).is_file()
    assert Path(program).is_relative_to(camera_addon.addon_dir())
    assert camera_addon.source() == "addon"
    assert sys.modules["kiln.camera_addon"] is module_before


def test_asked_before_the_folder_existed(home):
    assert not camera_addon.addon_dir().exists()
    assert camera_addon.ffmpeg_path() is None
    assert camera_addon.needed_by(_Needs()) is True

    assert _install(runner=FakeInstaller())["status"] == "installed"

    assert camera_addon.ffmpeg_path() is not None
    assert camera_addon.needed_by(_Needs()) is False


def test_a_miss_is_not_remembered(home):
    assert camera_addon.ffmpeg_path() is None
    program = write_package(camera_addon.addon_dir())
    assert camera_addon.ffmpeg_path() == program


def test_an_addon_folder_that_went_away_is_a_miss_and_a_new_one_is_found(home):
    assert _install(runner=FakeInstaller())["status"] == "installed"
    assert camera_addon.ffmpeg_path() is not None

    shutil.rmtree(camera_addon.addon_dir())
    assert camera_addon.ffmpeg_path() is None

    assert _install(runner=FakeInstaller())["status"] == "installed"
    assert Path(camera_addon.ffmpeg_path() or "").is_file()


def test_a_copy_that_was_loaded_and_then_removed_does_not_hide_the_addon(home, tmp_path):
    extra = tmp_path / "environment"
    write_package(extra)
    sys.path.append(str(extra))
    assert camera_addon.source() == "extra"

    shutil.rmtree(extra)
    in_folder = write_package(camera_addon.addon_dir())

    assert camera_addon.ffmpeg_path() == in_folder
    assert camera_addon.source() == "addon"


# --- where it is found -------------------------------------------------------


def test_a_program_on_the_computer_wins_over_everything(home, monkeypatch, tmp_path):
    write_package(camera_addon.addon_dir())
    extra = tmp_path / "environment"
    write_package(extra)
    sys.path.append(str(extra))
    program_on_this_computer(monkeypatch, "/somewhere/program")

    assert camera_addon.ffmpeg_path() == "/somewhere/program"
    assert camera_addon.health() == {"installed": True, "source": "system"}


def test_the_environments_copy_wins_over_kilns_folder(home, tmp_path):
    write_package(camera_addon.addon_dir())
    extra = tmp_path / "environment"
    in_environment = write_package(extra)
    sys.path.append(str(extra))

    assert camera_addon.ffmpeg_path() == in_environment
    assert camera_addon.source() == "extra"


def test_nothing_anywhere_is_none(home):
    assert camera_addon.ffmpeg_path() is None
    assert camera_addon.health() == {"installed": False, "source": None}


# --- a program already on this computer --------------------------------------


@pytest.fixture
def on_the_computer(home, monkeypatch, tmp_path):
    """A program file on this computer, asked through a runner that answers and counts each check."""
    program = tmp_path / "bin" / "ffmpeg"
    program.parent.mkdir()
    program.write_text("stand-in\n", encoding="utf-8")
    monkeypatch.setattr(camera_addon, "_system_program", lambda: str(program))
    checker = FakeInstaller()
    monkeypatch.setattr(camera_addon.subprocess, "run", checker)
    return program, checker


def test_a_program_that_opens_a_secure_stream_is_used(on_the_computer):
    program, checker = on_the_computer
    assert camera_addon.ffmpeg_path() == str(program)
    assert camera_addon.source() == "system"
    assert checker.checks == [[str(program), "-hide_banner", "-protocols"]]


@pytest.mark.parametrize(
    "answer",
    [{"protocols": PROTOCOLS_PLAIN}, {"check_returncode": 1}],
    ids=["no-secure-stream", "does-not-run"],
)
def test_a_program_that_cannot_open_a_secure_stream_is_passed_over(on_the_computer, answer):
    _program, checker = on_the_computer
    for name, value in answer.items():
        setattr(checker, name, value)

    assert camera_addon.ffmpeg_path() is None
    assert camera_addon.needed_by(_Needs()) is True
    write_package(camera_addon.addon_dir())
    assert camera_addon.source() == "addon"


def test_it_is_asked_once_per_process_and_again_once_the_program_changes(on_the_computer):
    program, checker = on_the_computer
    for _ in range(3):
        camera_addon.ffmpeg_path()
    assert len(checker.checks) == 1

    program.write_text("a newer stand-in\n", encoding="utf-8")
    camera_addon.ffmpeg_path()
    assert len(checker.checks) == 2


def test_after_no_build_a_program_that_cannot_read_the_camera_is_said_so(on_the_computer, monkeypatch, tmp_path):
    _program, checker = on_the_computer
    checker.protocols = PROTOCOLS_PLAIN
    a_system(monkeypatch, tmp_path, "ID=debian\n")
    heard = _install(runner=FakeInstaller(returncode=1, stderr=NO_BUILD_ANSWERS["pip"]))
    assert heard["status"] == "no_build"

    said = camera_addon.WORDS["system_cannot"].format(command="sudo apt install ffmpeg")
    assert str(camera_addon.refusal()) == said
    block = camera_addon.block(moment="snapshot")
    assert (block["status"], block["message"], block["command"]) == ("no_build", said, "sudo apt install ffmpeg")
    assert "install" not in block
    # A camera the person registered may send a plain stream, so it hears the add-on's own sentence.
    assert str(camera_addon.refusal(yours=True)) == heard["message"]


# --- who needs it ------------------------------------------------------------


def test_only_an_adapter_that_really_says_true_needs_it(home):
    assert camera_addon.needed_by(_Needs(True)) is True
    assert camera_addon.needed_by(_Needs(False)) is False
    assert camera_addon.needed_by(MagicMock()) is False
    assert camera_addon.needed_by(object()) is False


def test_printers_needing_names_them_in_order_and_none_once_it_is_here(home):
    adapters = {"Garage": _Needs(), "Old one": _Needs(False), "Desk": _Needs()}
    assert camera_addon.printers_needing(adapters) == ["Garage", "Desk"]
    write_package(camera_addon.addon_dir())
    assert camera_addon.printers_needing(adapters) == []


# --- what is said ------------------------------------------------------------


def test_words_never_name_the_tool():
    for key, sentence in camera_addon.WORDS.items():
        said = sentence.replace("{command}", "").replace('upgrade_kiln addon="camera"', "").lower()
        for word in BANNED:
            assert word not in said, (key, word)


def test_the_refusal_is_the_sentence_and_the_code(home):
    plain = camera_addon.refusal()
    assert str(plain) == "Kiln needs one free add-on to show this printer's camera."
    assert plain.code == camera_addon.REFUSAL_CODE == "FFMPEG_REQUIRED"
    assert str(camera_addon.refusal(yours=True)) == "Kiln needs one free add-on to show your camera."


def test_the_block_tells_an_agent_what_to_say_and_what_to_call(home):
    block = camera_addon.block(moment="snapshot")
    assert block["status"] == "needed"
    assert block["message"] == camera_addon.WORDS["needed"]
    assert block["ask"] == "Want me to add it now?"
    assert block["install"] == {"tool": "upgrade_kiln", "arguments": {"addon": "camera", "confirm": True}}
    assert block["command"] == "kiln install-camera"
    assert block["free"] is True and block["restart_required"] is False
    assert "upgrade_kiln" in block["agent_note"]


def test_the_setup_block_names_the_printer(home):
    block = camera_addon.block(printer_name="Garage P2S", moment="setup")
    assert f'{block["message"]} {block["ask"]}' == (
        "Garage P2S is ready. Kiln needs one free add-on to show its camera. Want me to add it now?"
    )
    with pytest.raises(ValueError):
        camera_addon.block(moment="later")


def test_a_copy_another_program_keeps_up_to_date_hears_what_any_other_copy_hears(home, monkeypatch):
    """The add-on lives in Kiln's own folder, so the copy's keeper changes nothing about it."""

    def said():
        blocks = [camera_addon.block(printer_name="Garage P2S", moment=m) for m in camera_addon.MOMENTS]
        return blocks, [camera_addon.setup_line(_Needs(), door=door) for door in ("cli", "tool")]

    plain = said()
    monkeypatch.setenv("KILN_UPDATE_COMMAND", "hermes plugins update kiln")

    assert said() == plain
    for block in plain[0]:
        assert block["status"] == "needed"
        assert block["install"] == {"tool": "upgrade_kiln", "arguments": {"addon": "camera", "confirm": True}}
        assert "hermes" not in repr(block)


def test_refusal_fields_ride_only_beside_this_refusal(home):
    assert camera_addon.refusal_fields(None) == {}
    assert camera_addon.refusal_fields(CameraStreamError("no", code="CAMERA_NOT_A_PICTURE")) == {}
    assert camera_addon.refusal_fields(RuntimeError("no")) == {}
    fields = camera_addon.refusal_fields(camera_addon.refusal())
    assert fields == {camera_addon.RESULT_KEY: camera_addon.block(moment="snapshot")}


def test_the_block_says_the_same_sentence_as_the_refusal_it_rides_beside(home):
    """A camera the person registered is "your camera" in the error and in the block alike."""
    for yours, key in ((False, "needed"), (True, "needed_yours")):
        exc = camera_addon.refusal(yours=yours)
        block = camera_addon.refusal_fields(exc)[camera_addon.RESULT_KEY]
        assert str(exc) == block["message"] == camera_addon.WORDS[key]


def test_setup_lines_for_a_person_and_for_an_agent(home):
    assert camera_addon.setup_line(_Needs(), door="cli") == (
        "Camera: needs Kiln's free camera add-on. Add it with: kiln install-camera"
    )
    assert camera_addon.setup_line(_Needs(), door="tool") == (
        'Camera: needs Kiln\'s free camera add-on (ask first, then upgrade_kiln addon="camera").'
    )
    assert camera_addon.setup_line(_Needs(False), door="cli") == ""
    with pytest.raises(ValueError):
        camera_addon.setup_line(_Needs(), door="web")


def test_setup_line_for_a_registered_camera_follows_what_it_sends(home):
    stream = ExternalCamera(stream_url="rtsp://user:secret@10.0.0.9/live")
    pictures = ExternalCamera(snapshot_url="http://10.0.0.9/snap.jpg", stream_url="rtsp://10.0.0.9/live")
    line = camera_addon.setup_line_for_camera(stream, door="cli")
    assert line == camera_addon.setup_line(_Needs(), door="cli") and "secret" not in line
    assert camera_addon.setup_line_for_camera(pictures, door="cli") == ""
    write_package(camera_addon.addon_dir())
    assert camera_addon.setup_line_for_camera(stream, door="cli") == ""


def test_the_extra_and_the_installed_requirement_are_one():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert f'camera = ["{camera_addon.PIP_REQUIREMENT}"]' in pyproject.read_text(encoding="utf-8")
