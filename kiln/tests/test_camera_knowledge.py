"""What Kiln knows about a printer's camera, and where it learns it.

Always allow looks at the bed before a print nobody was asked about.  What a
failed look MEANS depends on whether the printer has a camera, and printer
software that can serve one says so whether or not one is plugged in.  So
Kiln has to know, and it has four ways of knowing, in this order: a camera
the person registered beside the printer; what it has seen or been told for
that machine; what its catalogue says about the model (one word, asked of
Kiln's servers once and kept here); and, only when none of those says, the
person.

Pinned here: the one-word lookup and what happens on each kind of miss, the
turn-on screen for each word, the plain Bambu X1, and what Kiln reports
about cameras it meets -- keeping a camera the owner stood beside a printer
apart from the printer's own.

A/B: each test that pins a guard was run with that guard removed and seen to
fail; the mutation is named in its docstring.
"""

# ruff: noqa: F811 -- the fixtures imported below are used as test parameters
from __future__ import annotations

import json
import sys
import time
import types

import pytest

from kiln import _pro_camera_bridge as bridge
from kiln import plate_state, server, streaming
from kiln.camera_words import ADD_ON, FITTED, NONE, UNKNOWN, WORDS
from kiln.served_answer import Miss
from tests.test_always_allow_on_one_printer import (  # noqa: F401 -- fixtures
    _FRAME,
    _always_entries,
    _CameraPrinter,
    _isolated,
    _kiln,
    _Printer,
    _said,
    _start,
    at_terminal,
    no_rate_limit,
)

MODEL = "ender3_v3_ke"


def _local_module_names() -> list[str]:
    """The private package's module the bridge reads when it is installed
    here, and each package above it -- named by the bridge, not here."""
    parts = bridge.LOCAL_MODULE.split(".")
    return [".".join(parts[: i + 1]) for i in range(len(parts))]


@pytest.fixture(autouse=True)
def _fresh_bridge(monkeypatch):
    """No kiln-pro on this computer, nothing asked yet, nothing recorded
    about cameras today."""
    for name in _local_module_names():
        monkeypatch.setitem(sys.modules, name, None)
    bridge._reset_for_tests()
    streaming._CAMERA_FACTS_RECORDED.clear()
    yield
    bridge._reset_for_tests()
    streaming._CAMERA_FACTS_RECORDED.clear()


@pytest.fixture
def served(monkeypatch):
    """Kiln's servers, as the bridge reaches them.  ``served.answer`` is
    what comes back (a dict, or an exception to raise); ``served.asked`` is
    every model asked about."""
    box = types.SimpleNamespace(answer={"success": True, "status": "ok", "word": NONE}, asked=[])

    def _pro_api_call(tool, _timeout=None, **kwargs):
        assert tool == bridge.WIRE_TOOL
        box.asked.append(kwargs.get("printer_id"))
        if isinstance(box.answer, Exception):
            raise box.answer
        return box.answer

    monkeypatch.setattr(server, "_pro_api_call", _pro_api_call)
    return box


def _says(served, word):
    served.answer = {"success": True, "status": "ok", "printer_id": MODEL, "word": word}


def _printer(frame=_FRAME, *, model: str = MODEL, serial: str = "SERIAL-A") -> _CameraPrinter:
    """A printer whose software can serve a camera, declared as *model*."""
    printer = _CameraPrinter(serial, frame=frame)
    printer._safety_profile_id = model
    server._get_registry().register("garage", printer)
    return printer


NO_PICTURE = RuntimeError("Webcam snapshot failed (HTTP 404)")


# ---------------------------------------------------------------------------
# The one-word lookup
# ---------------------------------------------------------------------------


class TestTheLookup:
    def test_the_word_is_asked_once_and_kept(self, served):
        _says(served, FITTED)
        assert bridge.catalogue_word(MODEL) == FITTED
        assert bridge.catalogue_word(MODEL.upper() + "  ") == FITTED
        assert served.asked == [MODEL], "the second ask is answered from this computer"
        assert bridge.kept_word(MODEL) == FITTED and bridge.why_unanswered(MODEL) == ""

    @pytest.mark.parametrize(
        "declared, asked",
        [("creality_k1", "k1"), ("K1", "k1"), ("bambu_a1", "bambu_a1"), ("some_unlisted_printer", "some_unlisted_printer")],
    )
    def test_the_model_is_asked_about_by_its_catalogue_id(self, served, declared, asked):
        """A/B: with the declared spelling sent as written this fails on
        ``creality_k1`` — the catalogue is asked about an id it does not
        list, and answers unknown for a model it knows."""
        bridge.catalogue_word(declared)
        assert served.asked == [asked]

    def test_no_model_asks_nothing(self, served):
        assert bridge.catalogue_word("") is None and bridge.catalogue_word(None) is None
        assert served.asked == []

    def test_signed_out_is_no_answer_and_says_so(self, served):
        served.answer = {"success": False, "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "sign in"}
        assert bridge.catalogue_word(MODEL) is None
        assert bridge.why_unanswered(MODEL) == "signed_out"
        assert bridge.kept_word(MODEL) is None, "a miss is never kept as a word"

    def test_an_offline_computer_uses_what_it_was_told(self, served, monkeypatch):
        """A/B: with the kept word not returned on a miss this fails."""
        plate_state.keep_catalogue_word(MODEL, ADD_ON)
        monkeypatch.setattr(bridge, "FRESH_FOR_S", 0.0)  # old enough to ask again
        served.answer = OSError("no route to host")
        assert bridge.catalogue_word(MODEL) == ADD_ON
        assert bridge.why_unanswered(MODEL) in ("offline", "unanswered")
        # And the service is left alone for a while: no second call.
        assert bridge.catalogue_word(MODEL) == ADD_ON and len(served.asked) == 1

    def test_unknown_is_asked_about_again_sooner(self, served, monkeypatch):
        _says(served, UNKNOWN)
        assert bridge.catalogue_word(MODEL) == UNKNOWN
        monkeypatch.setattr(bridge, "UNKNOWN_FRESH_FOR_S", 0.0)
        _says(served, NONE)
        assert bridge.catalogue_word(MODEL) == NONE and served.asked == [MODEL, MODEL]

    def test_a_word_kiln_does_not_have_is_not_an_answer(self, served):
        """A/B: with any string accepted this fails — "built_in" is kept."""
        served.answer = {"success": True, "status": "ok", "word": "built_in"}
        assert bridge.catalogue_word(MODEL) is None and bridge.kept_word(MODEL) is None

    def test_with_the_private_package_here_nothing_is_asked(self, served, monkeypatch):
        *parents, leaf = _local_module_names()
        for name in parents:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
        local = types.ModuleType(leaf)
        local.camera_word = lambda model: FITTED
        monkeypatch.setitem(sys.modules, leaf, local)
        assert bridge.catalogue_word(MODEL) == FITTED and served.asked == []

    def test_the_words_are_the_four(self):
        assert WORDS == (FITTED, ADD_ON, NONE, UNKNOWN)

    def test_the_miss_type_is_the_served_one(self):
        assert Miss("signed_out").cause == "signed_out"


# ---------------------------------------------------------------------------
# Turning always allow on, for each word
# ---------------------------------------------------------------------------


class TestTheTurnOnScreen:
    def test_a_model_the_catalogue_says_has_none_is_not_asked_about(self, served, at_terminal, tmp_path):
        """A/B: with the catalogue left out of ``bed_camera`` this fails —
        the person is asked a question Kiln's catalogue already answers."""
        printer = _printer(NO_PICTURE)
        _says(served, NONE)
        on = _kiln("consent", "window", "--always", "--printer", "garage", typed="garage")
        assert on.exit_code == 0, on.output
        assert "Does garage have a camera" not in _said(on)
        assert "garage has no camera Kiln can use" in _said(on)
        assert _always_entries()[0].bed_check == "none"
        assert _start(tmp_path).get("success") is True and printer.started == ["part.gcode"]

    def test_a_model_that_ships_with_one_is_not_asked_about_and_is_never_called_camera_less(
        self, served, at_terminal, tmp_path,
    ):
        printer = _printer(NO_PICTURE)
        _says(served, FITTED)
        on = _kiln("consent", "window", "--always", "--printer", "garage", typed="garage")
        assert on.exit_code == 0, on.output
        assert "Does garage have a camera" not in _said(on) and "has no camera" not in _said(on)
        assert _always_entries()[0].bed_check == "camera"
        out = _start(tmp_path)
        assert out["error"]["code"] == "PREVIEW_NOT_CONFIRMED" and printer.started == [], out
        # Known from then on without asking anything: the word is kept.
        assert plate_state.knows_a_camera(printer) == "printer"

    @pytest.mark.parametrize("word", [ADD_ON, UNKNOWN])
    def test_a_model_that_may_or_may_not_have_one_is_the_persons_to_say(self, served, at_terminal, word):
        _printer(NO_PICTURE)
        _says(served, word)
        on = _kiln("consent", "window", "--always", "--printer", "garage", typed="n\ngarage")
        assert on.exit_code == 0, on.output
        assert "Does garage have a camera" in _said(on)
        assert "Sign in" not in _said(on), "Kiln asked its catalogue; signing in would change nothing"

    def test_a_camera_that_answers_beats_a_catalogue_that_says_none(self, served, at_terminal):
        """The owner plugged a webcam into the print server of a model that
        ships with none.  Kiln uses what it can see."""
        _printer(_FRAME)
        _says(served, NONE)
        on = _kiln("consent", "window", "--always", "--printer", "garage", typed="garage")
        assert on.exit_code == 0, on.output
        assert "Kiln looks at the bed through the camera before every print" in _said(on)
        assert _always_entries()[0].bed_check == "camera"

    def test_signed_out_the_person_is_told_signing_in_would_answer(self, served, at_terminal):
        """A/B: with the nudge removed this fails."""
        _printer(NO_PICTURE)
        served.answer = {"success": False, "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "sign in"}
        on = _kiln("consent", "window", "--always", "--printer", "garage", typed="n\ngarage")
        assert on.exit_code == 0, on.output
        said = _said(on)
        assert "Sign in (free) with `kiln signin` and Kiln can look yours up instead of asking." in said
        assert said.index("Sign in (free)") < said.index("Does garage have a camera")

    def test_offline_the_person_is_asked_with_no_talk_of_signing_in(self, served, at_terminal):
        _printer(NO_PICTURE)
        served.answer = OSError("no route to host")
        on = _kiln("consent", "window", "--always", "--printer", "garage", typed="n\ngarage")
        assert on.exit_code == 0, on.output
        assert "Does garage have a camera" in _said(on) and "Sign in" not in _said(on)

    def test_a_printer_with_no_declared_model_asks_the_catalogue_nothing(self, served, at_terminal):
        printer = _CameraPrinter("SERIAL-A", frame=NO_PICTURE)
        server._get_registry().register("garage", printer)
        on = _kiln("consent", "window", "--always", "--printer", "garage", typed="n\ngarage")
        assert on.exit_code == 0, on.output
        assert served.asked == [] and "Does garage have a camera" in _said(on)


# ---------------------------------------------------------------------------
# The plain X1
# ---------------------------------------------------------------------------


class TestWhichBambu:
    @staticmethod
    def _bambu(serial: str, declared: str = ""):
        from kiln.printers.bambu import BambuAdapter

        adapter = BambuAdapter.__new__(BambuAdapter)
        adapter._serial = serial
        adapter._printer_model = declared
        return adapter

    @pytest.mark.parametrize(
        "serial, declared, fitted",
        [
            ("00M00A000000001", "bambu_x1c", True),    # the X1 Carbon's own prefix
            ("03900A000000001", "bambu_a1", True),     # an A1
            ("ZZZ00A000000001", "bambu_a1", True),     # a prefix Kiln does not know, declared a model that ships with one
            ("ZZZ00A000000001", "bambu_x1c", None),    # could be the X1: its camera is an optional upgrade
            ("ZZZ00A000000001", "", None),             # nothing says which machine this is
            ("03W00A000000001", "bambu_x1e", True),    # the X1E is its own model
        ],
    )
    def test_only_a_machine_known_to_ship_with_a_camera_says_so(self, serial, declared, fitted):
        """A/B: with the flag True for every machine this fails on the
        two rows that could be the X1."""
        assert self._bambu(serial, declared).camera_fitted_at_factory is fitted

    def test_a_machine_that_could_be_the_x1_is_not_answered_from_the_x1_carbons_row(self):
        """The catalogue's row for this family is the X1 Carbon's, whose
        camera is fitted.  A/B: with the declared model always asked about,
        this fails — a possible X1 is told it ships with a camera."""
        assert self._bambu("ZZZ00A000000001", "bambu_x1c").camera_catalogue_model() == ""
        assert self._bambu("00M00A000000001", "bambu_x1c").camera_catalogue_model() == "bambu_x1c"
        assert self._bambu("03900A000000001", "bambu_a1").camera_catalogue_model() == "bambu_a1"

    def test_other_backends_do_not_say(self):
        from kiln.printers.base import PrinterAdapter

        assert PrinterAdapter.camera_fitted_at_factory is None


# ---------------------------------------------------------------------------
# What Kiln reports about the cameras it meets
# ---------------------------------------------------------------------------


@pytest.fixture
def reported(monkeypatch):
    seen: list[tuple[str, str, str, str]] = []
    monkeypatch.setattr(streaming, "_record_outcome", lambda *key: seen.append(key))
    return seen


class TestWhatIsReported:
    def test_a_still_through_the_printers_own_connection_is_reported_once_a_day(self, reported):
        printer = _printer(_FRAME)
        plate_state.look(printer)
        plate_state.look(printer)
        assert reported == [(MODEL, "still", "printer", "still_ok")]
        assert "still" in streaming.VIDEO_CHANNELS and "still_ok" in streaming.VIDEO_EVENTS

    def test_no_picture_reports_nothing(self, reported):
        plate_state.look(_printer(NO_PICTURE))
        assert reported == []

    @pytest.mark.parametrize(
        "camera_url, source",
        [("http://192.168.1.20:8080/snap.jpg", "user_same_host"), ("http://192.168.1.99/snap.jpg", "user_other")],
    )
    def test_a_camera_the_owner_registered_is_reported_as_theirs(self, reported, monkeypatch, camera_url, source):
        """A camera on a tripod must never be reported as the printer's own:
        it would teach the catalogue that the model ships with one.  A/B:
        with the source taken as ``printer`` for every still this fails."""
        from kiln.printers import base

        printer = _printer(NO_PICTURE)
        printer._host = "192.168.1.20"
        monkeypatch.setattr(base, "fetch_external_snapshot", lambda camera: _FRAME)
        printer.set_external_camera(snapshot_url=camera_url)
        plate_state.look(printer)
        assert reported == [(MODEL, "still", source, "still_ok")]

    def test_an_owner_saying_it_has_one_is_reported_as_said(self, reported, served, at_terminal, monkeypatch):
        _printer(NO_PICTURE)
        _says(served, UNKNOWN)
        monkeypatch.setattr(streaming, "video_model_for", lambda name: MODEL)
        on = _kiln("consent", "window", "--always", "--printer", "garage", typed="y\ngarage")
        assert on.exit_code == 0, on.output
        assert reported == [(MODEL, "none", "printer", "owner_said")]

    def test_the_new_tokens_fit_the_heartbeats_key(self):
        from kiln.daily_stats import _VIDEO_KEY_RE

        for key in (f"{MODEL}|still|user_same_host|still_ok", f"{MODEL}|none|printer|owner_said"):
            assert _VIDEO_KEY_RE.match(key), key


# ---------------------------------------------------------------------------
# The record on this computer
# ---------------------------------------------------------------------------


def test_the_word_is_kept_by_model_beside_the_cameras_seen(tmp_path):
    plate_state.keep_catalogue_word("  Ender3_V3_KE ", FITTED)
    word, age = plate_state.catalogue_word_on_record(MODEL)
    assert word == FITTED and 0 <= age < 5
    stored = json.loads((tmp_path / "home" / "plate_state.json").read_text())
    assert stored["camera_words"][MODEL]["word"] == FITTED
    assert plate_state.catalogue_word_on_record("some_other_model") is None
    assert time.time() > 0
