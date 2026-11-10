"""Which printers need Kiln's camera add-on, and what their camera says without it.

Covered: every Bambu model Kiln places, the printer's own report over its
model, a model Kiln cannot place, a camera the person registered, every other
backend; the refusal a snapshot raises without the add-on; what live video
says either way; the two lookups other code patches; and a camera that works
the moment the add-on is added, in the process that asked before it existed.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import socket
from pathlib import Path
from unittest import mock

import pytest

import kiln.printers
from kiln import camera_addon
from kiln.printers import bambu as bambu_mod
from kiln.printers import base as base_mod
from kiln.printers import engagement
from kiln.printers.bambu import BambuAdapter
from kiln.printers.base import (
    CameraStreamError,
    JobProgress,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterState,
    PrinterStatus,
)
from kiln.registry import PrinterRegistry
from tests._camera_addon_fakes import FakeInstaller, bare_computer, forget_package, has_pip
from tests._fake_bambu_camera import FakeBambuCamera
from tests.test_camera_addon import BANNED

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 300 + b"\xff\xd9"
NOT_A_PICTURE = b"\x00\x00\x00\x00\x01\x00\x00\x00"
#: A serial prefix Kiln has no model for, so only what a test sets decides.
UNPLACED = "ZZZ00A000000001"
STREAM_CAMERA = "rtsp://viewer:hunter2@cam.local:554/live"
SNAPSHOT_CAMERA = "http://cam.local/snap.jpg"
FAMILIES = sorted(bambu_mod._RTSPS_FAMILIES | bambu_mod._PORT6000_FAMILIES)


def _bambu(model: str | None, *, serial: str = UNPLACED) -> BambuAdapter:
    return BambuAdapter(
        host="127.0.0.1", access_code="12345678", serial=serial, timeout=1, printer_model=model,
    )


def _every_backend() -> list[type[PrinterAdapter]]:
    """Every concrete adapter class Kiln ships, found rather than listed."""
    for info in pkgutil.iter_modules(kiln.printers.__path__):
        importlib.import_module(f"kiln.printers.{info.name}")
    found: list[type[PrinterAdapter]] = []

    def walk(cls: type) -> None:
        for sub in cls.__subclasses__():
            shipped = sub.__module__.startswith("kiln.printers.") and not inspect.isabstract(sub)
            if shipped and sub not in found:
                found.append(sub)
            walk(sub)

    walk(PrinterAdapter)
    return found


BACKENDS = _every_backend()


def _unconnected(cls: type[PrinterAdapter]) -> PrinterAdapter:
    """An adapter with none of its connection state: a camera question must not need it."""
    return cls.__new__(cls)


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _said(text: str) -> list[str]:
    return [word for word in BANNED if word in text.lower()]


@pytest.fixture
def home(monkeypatch, tmp_path):
    yield bare_computer(monkeypatch, tmp_path)
    forget_package()


@pytest.fixture
def no_cut(monkeypatch):
    """A cut from a camera stream that fails the test if it is ever tried."""
    cut = mock.Mock(side_effect=AssertionError("no picture may be cut without the add-on"))
    monkeypatch.setattr(bambu_mod, "capture_rtsp_frame", cut)
    monkeypatch.setattr(base_mod, "capture_rtsp_frame", cut)
    return cut


class _Driven:
    """The machine Kiln is driving: a plain object, so it fingerprints as itself."""

    name = "moonraker"
    serial = "DRIVEN-A1"
    host = ""
    capabilities = PrinterCapabilities()

    def get_state(self) -> PrinterState:
        return PrinterState(connected=True, state=PrinterStatus.PRINTING)

    def get_job(self) -> JobProgress:
        return JobProgress(file_name="bracket.gcode", print_time_seconds=600)


@pytest.fixture
def another_printer_is_driven(monkeypatch):
    """Below the fleet tier, with Kiln driving one machine already."""
    monkeypatch.setattr(engagement, "_multi_machine_tier", lambda: False)
    monkeypatch.setattr(engagement, "_still_engaged", lambda *_a, **_k: True)
    driven = _Driven()
    registry = PrinterRegistry()
    registry.register("a1", driven)
    monkeypatch.setattr("kiln.registry.get_registry", lambda: registry)
    engagement.engage(driven, driven.get_job(), reason="started", label="a1")
    return driven


class TestWhoNeedsIt:
    """The adapter answers from what it already knows, without asking the printer."""

    @pytest.mark.parametrize("family", FAMILIES)
    def test_a_bambu_needs_it_exactly_when_its_model_streams_its_camera(self, family):
        assert _bambu(f"bambu_{family}").camera_needs_addon() is (family in bambu_mod._RTSPS_FAMILIES)

    @pytest.mark.parametrize("family", FAMILIES)
    def test_the_answer_foretells_the_snapshot_refusal(self, family, monkeypatch, no_cut):
        monkeypatch.setattr(bambu_mod, "_find_ffmpeg", lambda: None)
        monkeypatch.setattr(bambu_mod, "_CAMERA_PORT", _closed_port())
        adapter = _bambu(f"bambu_{family}")
        with pytest.raises(CameraStreamError) as caught:
            adapter.get_snapshot()
        assert (caught.value.code == camera_addon.REFUSAL_CODE) is adapter.camera_needs_addon()

    def test_the_printers_own_report_outranks_its_model_both_ways(self):
        a1 = _bambu("bambu_a1")
        a1._last_status["ipcam"] = {"rtsp_url": "rtsps://192.168.1.5/streaming/live/1"}
        p2s = _bambu("bambu_p2s")
        p2s._last_status["ipcam"] = {"liveview": {"local": "local"}}
        assert a1.camera_needs_addon() is True
        assert p2s.camera_needs_addon() is False

    def test_a_printer_kiln_cannot_place_is_not_nudged(self):
        assert _bambu(None).camera_needs_addon() is False

    def test_a_camera_the_person_registered_decides_for_a_bambu(self):
        x1c = _bambu("bambu_x1c")
        x1c.set_external_camera(snapshot_url=SNAPSHOT_CAMERA)
        a1 = _bambu("bambu_a1")
        a1.set_external_camera(stream_url=STREAM_CAMERA)
        assert x1c.camera_needs_addon() is False
        assert a1.camera_needs_addon() is True

    @pytest.mark.parametrize("cls", BACKENDS, ids=lambda cls: cls.__name__)
    def test_a_registered_stream_camera_needs_it_on_every_backend(self, cls):
        adapter = _unconnected(cls)
        adapter.set_external_camera(stream_url=STREAM_CAMERA)
        assert adapter.camera_needs_addon() is True
        adapter.set_external_camera(snapshot_url=SNAPSHOT_CAMERA, stream_url=STREAM_CAMERA)
        assert adapter.camera_needs_addon() is False

    @pytest.mark.parametrize(
        "cls", [cls for cls in BACKENDS if cls is not BambuAdapter], ids=lambda cls: cls.__name__
    )
    def test_no_other_backends_own_camera_needs_it(self, cls):
        assert _unconnected(cls).camera_needs_addon() is False

    def test_a_printer_set_up_while_kiln_drives_another_is_still_told(self, home, another_printer_is_driven):
        second = _bambu("bambu_p2s", serial="22E00A000000002")
        assert camera_addon.needed_by(second) is True
        assert camera_addon.printers_needing({"a1": another_printer_is_driven, "p2s": second}) == ["p2s"]


class TestTheRefusal:
    """Without the add-on a snapshot says so in the person's words, under the code doors read."""

    def test_a_p2s_with_nothing_on_the_computer_says_it_needs_the_add_on(self, home, no_cut):
        with pytest.raises(CameraStreamError) as caught:
            _bambu("bambu_p2s").get_snapshot()
        assert caught.value.code == camera_addon.REFUSAL_CODE
        assert str(caught.value) == camera_addon.WORDS["needed"]
        assert _said(str(caught.value)) == []
        assert camera_addon.refusal_fields(caught.value)[camera_addon.RESULT_KEY]["status"] == "needed"
        no_cut.assert_not_called()

    @pytest.mark.parametrize("cls", BACKENDS, ids=lambda cls: cls.__name__)
    def test_a_registered_stream_camera_says_your_camera_and_never_its_password(
        self, cls, home, no_cut
    ):
        adapter = _unconnected(cls)
        adapter.set_external_camera(stream_url=STREAM_CAMERA)
        with pytest.raises(CameraStreamError) as caught:
            adapter.get_snapshot()
        assert caught.value.code == camera_addon.REFUSAL_CODE
        assert str(caught.value) == camera_addon.WORDS["needed_yours"]
        assert "hunter2" not in str(caught.value)
        assert _said(str(caught.value)) == []
        no_cut.assert_not_called()


class TestWhatLiveVideoSays:
    """A model whose camera streams says why there is no live video, in the person's words."""

    @pytest.mark.parametrize(
        ("program", "words"), [(None, "needed"), ("/usr/bin/ffmpeg", "pictures_only")],
        ids=["add-on-missing", "add-on-here"],
    )
    @pytest.mark.parametrize("family", sorted(bambu_mod._RTSPS_FAMILIES))
    def test_a_streaming_model_says_why_in_plain_words(self, monkeypatch, family, program, words):
        monkeypatch.setattr(bambu_mod, "_find_ffmpeg", lambda: program)
        cap = _bambu(f"bambu_{family}").stream_capability()
        assert (cap.available, cap.channel) == (False, "rtsp")
        assert cap.reason == camera_addon.WORDS[words]
        assert _said(cap.reason) == []
        assert cap.requires == (camera_addon.RESULT_KEY,)


class TestTheLookupSeams:
    """The two lookups other code patches answer what the add-on's resolver answers, and are what the cameras ask."""

    @pytest.mark.parametrize("answer", ["/somewhere/program", None])
    def test_both_lookups_answer_the_resolvers_answer(self, monkeypatch, answer):
        monkeypatch.setattr(camera_addon, "ffmpeg_path", lambda: answer)
        assert bambu_mod._find_ffmpeg() == answer
        assert base_mod.find_ffmpeg() == answer

    def test_a_streaming_model_asks_through_its_lookup(self, monkeypatch):
        monkeypatch.setattr(camera_addon, "ffmpeg_path", lambda: "/the/resolvers/program")
        monkeypatch.setattr(bambu_mod, "_find_ffmpeg", lambda: None)
        with pytest.raises(CameraStreamError) as caught:
            _bambu("bambu_p2s").get_snapshot()
        assert caught.value.code == camera_addon.REFUSAL_CODE
        assert _bambu("bambu_p2s").stream_capability().reason == camera_addon.WORDS["needed"]

        cut = mock.Mock(return_value=JPEG)
        monkeypatch.setattr(bambu_mod, "capture_rtsp_frame", cut)
        monkeypatch.setattr(camera_addon, "ffmpeg_path", lambda: None)
        monkeypatch.setattr(bambu_mod, "_find_ffmpeg", lambda: "/the/lookups/program")
        assert _bambu("bambu_p2s").get_snapshot() == JPEG
        assert cut.call_args.kwargs["ffmpeg"] == "/the/lookups/program"

    def test_a_registered_camera_asks_through_its_lookup(self, monkeypatch):
        adapter = _unconnected(BambuAdapter)
        adapter.set_external_camera(stream_url=STREAM_CAMERA)
        monkeypatch.setattr(camera_addon, "ffmpeg_path", lambda: "/the/resolvers/program")
        monkeypatch.setattr(base_mod, "find_ffmpeg", lambda: None)
        with pytest.raises(CameraStreamError) as caught:
            adapter.get_snapshot()
        assert caught.value.code == camera_addon.REFUSAL_CODE

        cut = mock.Mock(return_value=JPEG)
        monkeypatch.setattr(base_mod, "capture_rtsp_frame", cut)
        monkeypatch.setattr(camera_addon, "ffmpeg_path", lambda: None)
        monkeypatch.setattr(base_mod, "find_ffmpeg", lambda: "/the/lookups/program")
        assert adapter.get_snapshot() == JPEG
        assert cut.call_args.kwargs["ffmpeg"] == "/the/lookups/program"


class TestAPrinterKilnCannotPlace:
    """Both feeds are tried; without the add-on the camera port's own answer stands and the add-on is not offered."""

    def test_the_ports_own_refusal_is_said_alone(self, monkeypatch, no_cut):
        monkeypatch.setattr(bambu_mod, "_find_ffmpeg", lambda: None)
        with FakeBambuCamera(frames=[NOT_A_PICTURE]) as server:
            monkeypatch.setattr(bambu_mod, "_CAMERA_PORT", server.port)
            adapter = _bambu(None)
            with pytest.raises(CameraStreamError) as port_alone:
                adapter._capture_jpeg_frame(raise_errors=True)
            with pytest.raises(CameraStreamError) as caught:
                adapter.get_snapshot()
        assert caught.value.code == port_alone.value.code == "CAMERA_NOT_A_PICTURE"
        assert str(caught.value) == str(port_alone.value)
        assert camera_addon.refusal_fields(caught.value) == {}
        assert "ffmpeg" not in str(caught.value).lower()

    def test_a_port_that_sends_nothing_is_said_as_that(self, monkeypatch, no_cut):
        monkeypatch.setattr(bambu_mod, "_find_ffmpeg", lambda: None)
        monkeypatch.setattr(BambuAdapter, "_capture_jpeg_frame", lambda self, **kw: None)
        with pytest.raises(CameraStreamError) as caught:
            _bambu(None).get_snapshot()
        assert caught.value.code == "CAMERA_UNREACHABLE"
        assert str(caught.value) == "The camera port sent no picture."
        assert camera_addon.refusal_fields(caught.value) == {}


class TestNoRestart:
    """An adapter that asked before the add-on existed uses it the moment it is added."""

    def test_a_p2s_shows_its_camera_the_moment_the_add_on_is_added(self, home, monkeypatch):
        cut = mock.Mock(return_value=JPEG)
        monkeypatch.setattr(bambu_mod, "capture_rtsp_frame", cut)
        adapter = _bambu("bambu_p2s")
        with pytest.raises(CameraStreamError) as before:
            adapter.get_snapshot()
        assert before.value.code == camera_addon.REFUSAL_CODE
        assert adapter.stream_capability().reason == camera_addon.WORDS["needed"]
        assert camera_addon.needed_by(adapter) is True
        cut.assert_not_called()

        added = camera_addon.install(
            confirm=True, runner=FakeInstaller(), find_spec=has_pip, which=lambda _n: None,
        )

        assert added["status"] == "installed"
        assert adapter.get_snapshot() == JPEG
        assert cut.call_args.args[0] == adapter._raw_stream_url()
        assert Path(cut.call_args.kwargs["ffmpeg"]).is_relative_to(camera_addon.addon_dir())
        assert adapter.stream_capability().reason == camera_addon.WORDS["pictures_only"]
        assert adapter.camera_needs_addon() is True
        assert camera_addon.needed_by(adapter) is False
