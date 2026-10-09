"""``slice_and_print`` with hardware: slice here, plan the pause, print THAT file.

"Put 4 magnets in this and print it" has to work from every door.  The door
slices on the person's own computer, asks Kiln's hardware planner (a served
tool, reached through the tool registry) to write the pause into the sliced
file, and prints the file that comes back.

What these tests pin is the one thing a wrong answer here costs a print for:
a part that needs a pause must never be printed from the plain slice, because
the cavity would be covered with nothing in it.  Every way the pause can fail
to arrive (a free account, an unknown printer, a planner that errors, a file
that never reached this computer) is a refusal before the printer is sent
anything.

The slicer, the printer and the planner are faked; the tool is the real one,
driven the way ``test_print_start_verdict`` drives it.
"""

from __future__ import annotations

import io
import os
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiln.printers.base import PrinterState, PrinterStatus, PrintResult, UploadResult
from kiln.slicer import SliceResult

PLANNER = "plan_hardware_insertion"

#: What the slicer wrote, and what the planner hands back.  The second is this
#: test's own stand-in for the planner's edit: the tool never reads either, it
#: only moves bytes.
SLICE = b"; sliced for the test\nG28\nG1 X10 Y10\nG1 X20 Y20\n"
WRITTEN = SLICE + b"; the file the planner wrote\n"

FREE_UPGRADE = {
    "headline": "Plan this hardware with a paid plan.",
    "url": "https://kiln3d.com/pricing",
}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Printer:
    """A printer that remembers what it was sent: the name and the bytes."""

    def __init__(self) -> None:
        self.uploads: list[tuple[str, bytes]] = []
        self.starts: list[tuple[str, dict]] = []

    def get_state(self) -> PrinterState:
        return PrinterState(connected=True, state=PrinterStatus.PRINTING, state_age_seconds=0.0)

    def upload_file(self, path: str) -> UploadResult:
        name = os.path.basename(path)
        self.uploads.append((name, Path(path).read_bytes()))
        return UploadResult(success=True, file_name=name, message="uploaded")

    def start_print(self, file_name: str, **kwargs: Any) -> PrintResult:
        self.starts.append((file_name, kwargs))
        return PrintResult(success=True, message="Started printing.")


def _three_mf(body: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", body)
    return buffer.getvalue()


class _WrappingPrinter(_Printer):
    """A printer whose files Kiln wraps: the upload is a ``.3mf``, not the slice."""

    WRAPPED = _three_mf(SLICE)

    def wrap_gcode_as_3mf(self, gcode_path: str, **_kwargs: Any) -> str:
        wrapped = Path(gcode_path).with_suffix(".3mf")
        wrapped.write_bytes(self.WRAPPED)
        return str(wrapped)


class _Planner:
    """The planner as the registry hands it out: a callable that records the
    call, and the bytes of the file it was handed at that moment."""

    def __init__(self, answer: Any) -> None:
        self._answer = answer
        self.calls: list[dict[str, Any]] = []
        self.file_seen: list[bytes] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        self.file_seen.append(Path(kwargs["gcode_path"]).read_bytes())
        if isinstance(self._answer, BaseException):
            raise self._answer
        return self._answer(kwargs) if callable(self._answer) else self._answer


def _answer(**over: Any) -> dict[str, Any]:
    """A planner answer for a part with magnets sealed inside, the way a paid
    account gets it.  Keyword arguments replace or (with ``None``) drop keys."""
    answer: dict[str, Any] = {
        "status": "success",
        "your_tier": "pro",
        "safety_floor": ["Check each piece before you resume."],
        "placements": [{
            "item": "4x 6x3 magnet", "kind": "magnet", "seat": "S1",
            "where": "the pocket in the top face", "when": "pause", "why": "it is sealed inside",
        }],
        "file": {"readable": True, "matches_model": True, "sequential": False},
        "stops": [{"n": 1, "before_layer": 12, "steps": ["Put the magnets in."]}],
    }
    for key, value in over.items():
        if value is None:
            answer.pop(key, None)
        else:
            answer[key] = value
    return answer


def _written_answer(tmp_path: Path, **over: Any) -> dict[str, Any]:
    """An answer that hands back a written file, which sits in a folder of its own."""
    folder = tmp_path / "written"
    folder.mkdir(exist_ok=True)
    path = folder / "out-with-pauses.gcode"
    path.write_bytes(WRITTEN)
    return _answer(
        written_file=str(path),
        written_note="Print this file, not the original.",
        **over,
    )


#: Every part goes in once the print is done: nothing needs a pause.
AFTER_PRINT_ONLY: dict[str, Any] = {
    "placements": [{
        "item": "4x M3 heat-set insert", "kind": "heat_set_insert", "seat": "S1",
        "where": "the boss on the back", "when": "after_print", "why": "it opens on a face",
    }],
    "stops": None,
    "after_print": [{"item": "4x M3 heat-set insert", "when": "After the print."}],
}


# ---------------------------------------------------------------------------
# The real tool, with the slicer and the printer faked
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_preview_gate(monkeypatch):
    """These cases are about the hardware step, not the preview-consent gate."""
    monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")


def _register_slicer_tools() -> dict:
    from kiln.plugins.slicer_tools import _SlicerToolsPlugin

    tools: dict = {}

    class FakeMCP:
        def tool(self_mcp, name: str | None = None, **_kwargs):
            def decorator(fn):
                tools[name or fn.__name__] = fn
                return fn

            return decorator

    _SlicerToolsPlugin().register(FakeMCP())
    return tools


@pytest.fixture(scope="module")
def slicer_tools():
    return _register_slicer_tools()


def _run(
    slicer_tools,
    tmp_path: Path,
    monkeypatch,
    printer: _Printer,
    planner: Any = "absent",
    *,
    hardware: Any = ("4x 6x3 magnet",),
    sliced_mesh: str | None = None,
    printer_id: str | None = None,
    target_model: str | None = "prusa_mk4s",
) -> dict:
    """Drive the real ``slice_and_print``.

    *planner* is a callable registered under the planner's tool name, or
    ``"absent"`` for a registry that does not hold it.  *sliced_mesh* is the
    file the bed-fit gate hands on to the slicer (the caller's own path by
    default); *target_model* is what Kiln knows of the printer's model.
    """
    import kiln.plugins.slicer_tools as _st
    import kiln.server as _srv

    caller_mesh = tmp_path / "input.stl"
    caller_mesh.write_bytes(b"\x00" * 84)
    gcode = tmp_path / "out.gcode"
    gcode.write_bytes(SLICE)
    ini = tmp_path / "profile.ini"
    ini.write_text("layer_height = 0.2\n")

    def fake_slice_file(path, **_kwargs):
        return SliceResult(success=True, output_path=str(gcode), slicer="prusa-slicer", message="ok")

    registry = _srv.mcp._tool_manager._tools
    if callable(planner):
        monkeypatch.setitem(registry, PLANNER, SimpleNamespace(fn=planner))
    else:
        monkeypatch.delitem(registry, PLANNER, raising=False)

    monkeypatch.setattr(_srv, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr(_srv, "_resolve_slice_profile_context", lambda **_k: (printer_id, str(ini)))
    monkeypatch.setattr(_srv, "_resolve_target_printer_model", lambda *_a, **_k: target_model)
    monkeypatch.setattr(_srv, "_PRINTER_TYPE", "octoprint")
    monkeypatch.setattr(_srv, "_resolve_adapter", lambda *_a, **_k: printer)
    monkeypatch.setattr(_srv, "_resolve_effective_printer_name", lambda *_a, **_k: "p1")
    monkeypatch.setattr(_srv, "_emergency_latch_error", lambda *_a, **_k: None)
    monkeypatch.setattr(_srv, "preflight_check", lambda *_a, **_k: {"ready": True})
    monkeypatch.setattr(_srv, "_get_heater_watchdog", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(_srv, "_audit", lambda *_a, **_k: None)
    monkeypatch.setattr(_st, "_maybe_overlay_calibration", lambda overrides, *_a, **_k: (overrides, None))
    monkeypatch.setattr(
        _st, "_apply_bed_fit_gate", lambda *_a, **_k: (sliced_mesh or str(caller_mesh), None, {}),
    )
    monkeypatch.setattr(_st, "_multicolor_flatten_advisory", lambda *_a, **_k: (None, None))
    monkeypatch.setattr("kiln.slicer.slice_file", fake_slice_file)
    monkeypatch.setattr(
        "kiln.printers.bed_fit.verify_3mf_is_safe_to_print", lambda *_a, **_k: {"ok": True, "failed": []},
    )
    # Giving a raw G-code file its preview and weight rewrites it in place.
    # That is not what is under test, and it would make every byte
    # comparison below a comparison with a moving file.
    monkeypatch.setattr("kiln.printers.upload_prep._complete_raw_gcode", lambda *_a, **_k: None)

    kwargs: dict[str, Any] = {"input_path": str(caller_mesh), "material": "PLA", "skip_validation": True}
    if hardware != "unset":
        kwargs["hardware"] = list(hardware) if isinstance(hardware, tuple) else hardware
    return slicer_tools["slice_and_print"](**kwargs)


def _sent_nothing(printer: _Printer) -> None:
    assert printer.uploads == [], "a refused print must not upload anything"
    assert printer.starts == [], "a refused print must not start"


# ---------------------------------------------------------------------------
# 1. No hardware: today's behaviour, exactly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("nothing", [None, [], ["", "   "]])
def test_no_hardware_prints_the_plain_slice_and_never_asks_the_planner(
    slicer_tools, tmp_path, monkeypatch, nothing,
):
    printer, planner = _Printer(), _Planner(_answer())
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, planner, hardware=nothing)

    assert resp["success"] is True
    assert planner.calls == [], "the planner is asked only when hardware is named"
    assert "hardware" not in resp
    assert printer.uploads == [("out.gcode", SLICE)]


def test_the_call_without_the_argument_is_the_same_call(slicer_tools, tmp_path, monkeypatch):
    printer, planner = _Printer(), _Planner(_answer())
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, planner, hardware="unset")

    assert resp["success"] is True and "hardware" not in resp
    assert planner.calls == []
    assert printer.uploads == [("out.gcode", SLICE)]


# ---------------------------------------------------------------------------
# 2. A written file is what the printer gets, under the name it always had
# ---------------------------------------------------------------------------


def test_the_printer_gets_the_written_bytes_under_the_original_name(slicer_tools, tmp_path, monkeypatch):
    printer = _Printer()
    planner = _Planner(lambda _kw: _written_answer(tmp_path))
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, planner)

    assert resp["success"] is True
    assert printer.uploads == [("out.gcode", WRITTEN)]
    assert printer.starts and printer.starts[0][0] == "out.gcode"
    block = resp["hardware"]
    assert block["placements"][0]["when"] == "pause"
    assert block["safety_floor"] == ["Check each piece before you resume."]
    assert block["stops"][0]["before_layer"] == 12
    assert block["written_note"].startswith("Print this file")
    # The block says what goes in and when; it is not the planner's whole answer.
    assert set(block) <= {"placements", "safety_floor", "stops", "after_print", "written_note"}


def test_the_planner_is_asked_to_write_for_the_part_that_was_sliced(slicer_tools, tmp_path, monkeypatch):
    printer = _Printer()
    planner = _Planner(lambda _kw: _written_answer(tmp_path))
    _run(slicer_tools, tmp_path, monkeypatch, printer, planner, hardware=("4x 6x3 magnet", "2x M3 nut"))

    (call,) = planner.calls
    assert call["hardware"] == ["4x 6x3 magnet", "2x M3 nut"]
    assert call["write_pauses"] is True
    assert call["material"] == "PLA"
    assert call["printer"] == "prusa_mk4s"
    assert Path(call["gcode_path"]).name == "out.gcode"
    # It reads the file the printer would have received, not an earlier one.
    assert planner.file_seen == [SLICE]


def test_a_written_copy_that_only_carries_the_plan_is_printed_too(slicer_tools, tmp_path, monkeypatch):
    """Parts that go in after the print need no pause, but a planner that hands
    back a copy carrying its plan has asked for that copy to be the one printed."""
    printer = _Printer()
    planner = _Planner(lambda _kw: _written_answer(tmp_path, **AFTER_PRINT_ONLY))
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, planner)

    assert resp["success"] is True
    assert printer.uploads == [("out.gcode", WRITTEN)]
    assert resp["hardware"]["after_print"][0]["item"] == "4x M3 heat-set insert"


def test_a_wrapped_upload_is_what_the_planner_reads_and_what_is_replaced(slicer_tools, tmp_path, monkeypatch):
    """A printer whose files Kiln wraps takes a ``.3mf``.  The pause goes into
    THAT file: planned on the raw slice it would never reach the printer."""
    printer = _WrappingPrinter()
    written = _three_mf(WRITTEN)

    def answer(_kw):
        folder = tmp_path / "written"
        folder.mkdir(exist_ok=True)
        path = folder / "plate-with-pauses.3mf"
        path.write_bytes(written)
        return _answer(written_file=str(path), written_note="Print this file, not the original.")

    planner = _Planner(answer)
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, planner)

    assert resp["success"] is True
    (call,) = planner.calls
    assert call["gcode_path"].endswith(".3mf")
    assert planner.file_seen == [_WrappingPrinter.WRAPPED], "the planner must read the wrapped file"
    assert printer.uploads == [("out.3mf", written)]
    assert printer.starts[0][0] == "out.3mf"
    assert printer.starts[0][1]["local_file_path"].endswith("out.3mf")
    assert Path(printer.starts[0][1]["local_file_path"]).read_bytes() == written


# ---------------------------------------------------------------------------
# 3 and 4. A pause that is needed and not written: nothing reaches the printer
# ---------------------------------------------------------------------------


def test_a_free_caller_is_refused_before_upload_and_gets_the_planners_own_upgrade(
    slicer_tools, tmp_path, monkeypatch,
):
    printer = _Printer()
    planner = _Planner(_answer(your_tier="free", upgrade=FREE_UPGRADE, stops=None))
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, planner)

    assert resp["success"] is False
    _sent_nothing(printer)
    assert resp["error"]["code"] == "HARDWARE_PAUSE_NOT_WRITTEN"
    message = resp["error"]["message"]
    assert "needs a pause" in message and "could not write one" in message
    assert "Nothing was sent to the printer." in message
    # Kiln's own sentence names no plan and no price; the planner's block says it.
    assert resp["upgrade"] == FREE_UPGRADE
    assert "$" not in message and "kiln3d.com" not in message
    # What the free answer does know still reaches the person.
    assert resp["hardware"]["placements"][0]["when"] == "pause"
    assert resp["hardware"]["safety_floor"]
    assert resp["slice"]["output_path"].endswith("out.gcode")


def test_a_printer_whose_stop_is_unknown_is_refused_with_the_planners_reason(
    slicer_tools, tmp_path, monkeypatch,
):
    printer = _Printer()
    reason = "Kiln did not write a pause: it does not know a stop this printer obeys."
    planner = _Planner(_answer(written_note=reason, stops=None))
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, planner)

    assert resp["success"] is False and "upgrade" not in resp
    _sent_nothing(printer)
    assert resp["error"]["code"] == "HARDWARE_PAUSE_NOT_WRITTEN"
    assert reason in resp["error"]["message"]


@pytest.mark.parametrize(
    "answer, says",
    [
        ({"status": "error", "code": "ENGINE_ERROR", "error": "Kiln could not plan this part."},
         "Kiln could not plan this part."),
        ({"success": False, "error": {"code": "FILE_KIND_NOT_SENT", "message": "That kind of file was not sent."}},
         "That kind of file was not sent."),
        ({"status": "error", "success": False, "code": "SERVER_UNREACHABLE", "why": "offline",
          "error": "Kiln's servers could not be reached."},
         "this computer is offline"),
        ({"status": "error", "success": False, "code": "KILN_AUTH_REJECTED", "why": "signed_out",
          "error": "Kiln is signed out."},
         "Kiln is signed out"),
        ("not a dict", "didn't answer"),
        (None, "didn't answer"),
    ],
    ids=["planner-error", "refused-envelope", "offline", "signed-out", "not-a-dict", "no-answer"],
)
def test_an_error_or_a_missed_answer_refuses_and_sends_nothing(
    slicer_tools, tmp_path, monkeypatch, answer, says,
):
    printer = _Printer()
    planner = _Planner(answer)
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, planner)

    assert resp["success"] is False
    _sent_nothing(printer)
    # It is said as "no plan", not as a plan that did not cover the part.
    assert resp["error"]["code"] == "HARDWARE_PLAN_UNAVAILABLE"
    message = resp["error"]["message"]
    assert says in message
    assert "Nothing was sent to the printer." in message
    assert resp["slice"]["success"] is True, "the slice is attached so the work is not lost"


def test_a_miss_says_which_of_the_four_things_happened(slicer_tools, tmp_path, monkeypatch):
    offline = {
        "status": "error", "success": False, "code": "SERVER_UNREACHABLE", "why": "offline",
        "error": "ignored: Kiln words this itself",
    }
    resp = _run(slicer_tools, tmp_path, monkeypatch, _Printer(), _Planner(offline))

    assert resp["why"] == "offline"
    assert "this computer is offline" in resp["error"]["message"]
    assert resp["error"]["retryable"] is True


def test_a_planner_that_raises_refuses_and_sends_nothing(slicer_tools, tmp_path, monkeypatch):
    printer = _Printer()
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, _Planner(RuntimeError("boom")))

    assert resp["success"] is False
    _sent_nothing(printer)
    assert "Nothing was sent to the printer." in resp["error"]["message"]
    assert "boom" not in resp["error"]["message"]


def test_a_registry_without_the_planner_refuses_and_sends_nothing(slicer_tools, tmp_path, monkeypatch):
    printer = _Printer()
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, "absent")

    assert resp["success"] is False
    _sent_nothing(printer)
    assert "Nothing was sent to the printer." in resp["error"]["message"]


# ---------------------------------------------------------------------------
# A plan that does not cover the part is not a plan
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "over, says",
    [
        ({"file": {"readable": False, "why": "not a sliced file"}}, "could not read the sliced file"),
        ({"file": {"readable": True, "matches_model": False, "why": "different size"}}, "not the part"),
        (
            {"file": {"readable": True, "matches_model": True, "warning": "This file prints one object at a time."}},
            "one object at a time",
        ),
        ({"problems": ["S2: Kiln could not find where this cavity closes."]}, "every piece of hardware"),
        ({"unplaced": ["No free seat fits 2x M3 nut."]}, "No free seat fits 2x M3 nut."),
        ({"placements": None, "stops": None, "next": "Tell Kiln what goes in."}, "nothing in this part"),
    ],
    ids=["unreadable", "mismatch", "one-at-a-time", "unlocated-seat", "unplaced-hardware", "nothing-planned"],
)
def test_a_plan_that_does_not_cover_the_part_is_refused_even_with_a_written_file(
    slicer_tools, tmp_path, monkeypatch, over, says,
):
    printer = _Printer()
    planner = _Planner(lambda _kw: _written_answer(tmp_path, **over))
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, planner)

    assert resp["success"] is False
    _sent_nothing(printer)
    assert resp["error"]["code"] == "HARDWARE_NOT_PLANNED"
    message = resp["error"]["message"]
    assert says.lower() in message.lower()
    assert "Nothing was sent to the printer." in message


def test_a_written_path_that_is_not_a_file_here_is_refused(slicer_tools, tmp_path, monkeypatch):
    printer = _Printer()
    planner = _Planner(_answer(written_file="/tmp/kiln-a-server-folder/never-arrived.gcode"))
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, planner)

    assert resp["success"] is False
    _sent_nothing(printer)
    assert resp["error"]["code"] == "HARDWARE_PAUSE_NOT_WRITTEN"
    assert resp["error"]["retryable"] is True
    assert "did not reach this computer" in resp["error"]["message"]


def test_a_download_that_failed_is_refused_when_a_pause_is_needed(slicer_tools, tmp_path, monkeypatch):
    """The stub marks a file it could not fetch, and removes the server's path."""
    printer = _Printer()
    planner = _Planner(_answer(files=[{"format": "gcode", "on_this_computer": False}], stops=None))
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, planner)

    assert resp["success"] is False
    _sent_nothing(printer)
    assert resp["error"]["code"] == "HARDWARE_PAUSE_NOT_WRITTEN"
    assert resp["error"]["retryable"] is True
    assert "did not reach this computer" in resp["error"]["message"]


def test_a_written_file_of_another_kind_is_not_swapped_in(slicer_tools, tmp_path, monkeypatch):
    printer = _WrappingPrinter()
    planner = _Planner(lambda _kw: _written_answer(tmp_path))
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, planner)

    assert resp["success"] is False
    _sent_nothing(printer)


# ---------------------------------------------------------------------------
# 5. Every part goes in after the print: the original prints
# ---------------------------------------------------------------------------


def test_when_nothing_needs_a_pause_the_original_prints_and_the_steps_ride_along(
    slicer_tools, tmp_path, monkeypatch,
):
    printer = _Printer()
    planner = _Planner(_answer(**AFTER_PRINT_ONLY))
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, planner)

    assert resp["success"] is True
    assert printer.uploads == [("out.gcode", SLICE)]
    assert resp["hardware"]["after_print"] == [{"item": "4x M3 heat-set insert", "when": "After the print."}]
    assert resp["hardware"]["placements"][0]["when"] == "after_print"
    assert "stops" not in resp["hardware"]


# ---------------------------------------------------------------------------
# 6. What the planner is handed
# ---------------------------------------------------------------------------


def test_the_planner_gets_the_mesh_that_was_sliced_not_the_callers_path(slicer_tools, tmp_path, monkeypatch):
    """A part the bed-fit gate turned or centred is sliced from a copy, and the
    planner refuses a part that is not the one in the file."""
    turned = tmp_path / "input_turned.stl"
    turned.write_bytes(b"\x00" * 84)
    planner = _Planner(lambda _kw: _written_answer(tmp_path))
    _run(slicer_tools, tmp_path, monkeypatch, _Printer(), planner, sliced_mesh=str(turned))

    (call,) = planner.calls
    assert call["model_path"] == str(turned)
    assert call["model_path"] != str(tmp_path / "input.stl")


def test_the_printer_is_the_profile_id_when_there_is_one_else_the_catalogue_model_else_blank(
    slicer_tools, tmp_path, monkeypatch,
):
    seen = []
    for printer_id, model in (("bambu_a1", "prusa_mk4s"), (None, "voron_2"), (None, None)):
        planner = _Planner(lambda _kw: _written_answer(tmp_path))
        _run(
            slicer_tools, tmp_path, monkeypatch, _Printer(), planner,
            printer_id=printer_id, target_model=model,
        )
        seen.append(planner.calls[0]["printer"])
    assert seen == ["bambu_a1", "voron_2", ""]


# ---------------------------------------------------------------------------
# The real discovery stub, with only the network stood in for
# ---------------------------------------------------------------------------


class _Reply:
    def __init__(self, status: int = 200, content: bytes = b"", body: dict | None = None) -> None:
        self.status_code = status
        self.content = content
        self._body = body

    def json(self) -> dict:
        if self._body is None:
            raise ValueError("no json")
        return self._body


class _Servers:
    """Kiln's servers as an install without kiln-pro meets them: files go up
    and get tokens, one tool call is answered, and files come back by token."""

    WRITTEN_TOKEN = "written-token-0003xxxxxxxx"

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.uploads: list[tuple[str, str]] = []
        self.asked: list[dict[str, Any]] = []
        self.downloads: dict[str, _Reply] = {
            f"/api/artifact/{self.WRITTEN_TOKEN}": _Reply(content=WRITTEN),
        }

    def post(self, url: str, **kwargs: Any) -> _Reply:
        route = url.split("api.kiln3d.com", 1)[-1]
        name = kwargs["files"]["file"][0]
        self.uploads.append((route, name))
        if route == "/api/view/mesh":
            return _Reply(body={"artifact_token": "mesh-token-0001xxxxxxxxxx"})
        return _Reply(body={"file_token": "file-token-0002xxxxxxxxxx"})

    def get(self, url: str, **_kwargs: Any) -> _Reply:
        return self.downloads.get(url.split("api.kiln3d.com", 1)[-1], _Reply(status=404, body={"error": "gone"}))

    def call(self, tool_name: str, _timeout: float | None = None, _asked_by_user: bool = True, **kwargs: Any) -> Any:
        self.asked.append({"tool": tool_name, **kwargs})
        return self.answer() if callable(self.answer) else self.answer


def _served_answer() -> dict[str, Any]:
    """The answer for a paid caller, the way Kiln's servers hand it back: the
    file they wrote is named by a path on THEIR disk and by a token."""
    return _answer(
        written_file="/tmp/kiln-hw-stops-abc123/out-hardware-stops.gcode",
        written_note="Print this file, not the original.",
        files=[{
            "at": ["written_file"], "format": "gcode", "artifact_token": _Servers.WRITTEN_TOKEN,
            "filename": "out-hardware-stops.gcode",
        }],
    )


@pytest.fixture
def served(monkeypatch, tmp_path):
    """The stub the manifest registers for the planner, in place of the tool."""
    import httpx

    import kiln.server as _srv
    from kiln import served_makes

    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    monkeypatch.setattr(served_makes, "_bearer", lambda: "bearer-token")
    # Registering the stubs re-fills these; they are put back as they were.
    for name in ("_PRO_TOOL_TIERS", "_PRO_TOOL_QUOTA", "_PRO_TOOL_NUDGES", "_PRO_TOOL_OFFLINE_KIND"):
        monkeypatch.setattr(_srv, name, dict(getattr(_srv, name)))
    stubs: dict[str, Any] = {}

    class _Collect:
        def tool(self, *_a: Any, **_k: Any):
            def decorator(fn):
                stubs[fn.__name__] = fn
                return fn

            return decorator

    _srv._register_pro_tool_stubs(_Collect())
    assert PLANNER in stubs, "the bundled manifest no longer lists the planner"

    def make(answer: Any) -> tuple[Any, _Servers]:
        servers = _Servers(answer)
        monkeypatch.setattr(httpx, "post", servers.post)
        monkeypatch.setattr(httpx, "get", servers.get)
        monkeypatch.setattr(_srv, "_pro_api_call", servers.call)
        return stubs[PLANNER], servers

    return make


def test_the_stub_brings_the_written_file_here_and_it_is_what_prints(slicer_tools, tmp_path, monkeypatch, served):
    stub, servers = served(_served_answer)
    printer = _Printer()
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, stub)

    assert resp["success"] is True
    assert printer.uploads == [("out.gcode", WRITTEN)], "the bytes the servers wrote, under the name the slice had"
    (asked,) = servers.asked
    assert asked["tool"] == PLANNER and asked["write_pauses"] is True
    assert asked["hardware"] == ["4x 6x3 magnet"]
    # The part and the sliced file went up; neither path (they are this computer's) did.
    assert sorted(route for route, _ in servers.uploads) == ["/api/tool-inputs", "/api/view/mesh"]
    assert asked["source_artifact_token"] and asked["file_tokens"]["gcode_path"]["name"] == "out.gcode"
    assert "model_path" not in asked and "gcode_path" not in asked
    assert resp["hardware"]["stops"][0]["before_layer"] == 12


def test_a_download_that_fails_in_the_stub_is_refused_not_printed_plain(slicer_tools, tmp_path, monkeypatch, served):
    stub, servers = served(_served_answer)
    servers.downloads.clear()
    printer = _Printer()
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, stub)

    assert resp["success"] is False
    _sent_nothing(printer)
    assert resp["error"]["code"] == "HARDWARE_PAUSE_NOT_WRITTEN"
    assert "did not reach this computer" in resp["error"]["message"]


def test_a_signed_out_install_is_told_to_sign_in_and_sends_nothing(slicer_tools, tmp_path, monkeypatch, served):
    stub, _servers = served({
        "status": "error", "success": False, "code": "KILN_ACCOUNT_NOT_PAIRED", "why": "signed_out",
        "error": "This machine isn't signed in to Kiln.",
    })
    printer = _Printer()
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, stub)

    assert resp["success"] is False
    _sent_nothing(printer)
    assert resp["why"] == "signed_out"
    message = resp["error"]["message"]
    assert "Kiln is signed out" in message and "sign in" in message.lower()


# ---------------------------------------------------------------------------
# The swap: the original name, in one step, and never half done
# ---------------------------------------------------------------------------


def test_the_swap_keeps_the_name_the_mode_and_leaves_no_temporary_file(tmp_path):
    from kiln.plugins.slicer_tools import _swap_in_written_file

    target = tmp_path / "print" / "job.gcode"
    target.parent.mkdir()
    target.write_bytes(SLICE)
    target.chmod(0o640)
    source = tmp_path / "elsewhere.gcode"
    source.write_bytes(WRITTEN)

    _swap_in_written_file(str(source), str(target))

    assert target.read_bytes() == WRITTEN
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert sorted(p.name for p in target.parent.iterdir()) == ["job.gcode"]
    # The written copy was only on its way here: it does not stay behind as a
    # second file the size of the print.
    assert not source.exists()


def test_a_swap_that_fails_leaves_the_original_whole(tmp_path, monkeypatch):
    import shutil

    from kiln.plugins.slicer_tools import _swap_in_written_file

    target = tmp_path / "job.gcode"
    target.write_bytes(SLICE)
    source = tmp_path / "elsewhere.gcode"
    source.write_bytes(WRITTEN)

    def broken(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copyfile", broken)
    with pytest.raises(OSError):
        _swap_in_written_file(str(source), str(target))

    assert target.read_bytes() == SLICE
    assert sorted(p.name for p in tmp_path.iterdir()) == ["elsewhere.gcode", "job.gcode"]


def test_a_swap_that_fails_refuses_the_print(slicer_tools, tmp_path, monkeypatch):
    import kiln.plugins.slicer_tools as _st

    def broken(*_a, **_k):
        raise OSError("read-only folder")

    monkeypatch.setattr(_st, "_swap_in_written_file", broken)
    printer = _Printer()
    planner = _Planner(lambda _kw: _written_answer(tmp_path))
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, planner)

    assert resp["success"] is False
    _sent_nothing(printer)
    assert "Nothing was sent to the printer." in resp["error"]["message"]


def test_a_file_that_already_pauses_before_every_layer_prints_as_it_is(slicer_tools, tmp_path, monkeypatch):
    """The planner checked the pauses already in the file against this printer,
    found each one right, and wrote nothing: the file prints unchanged."""
    printer = _Printer()
    already = [{"n": 1, "before_layer": 12, "steps": ["Put the magnets in."],
                "already_in_file": "Your file already pauses before this layer."}]
    planner = _Planner(_answer(
        stops=already, written_note="Your file already pauses before every layer it should; print it as it is.",
    ))
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, planner)

    assert resp["success"] is True, resp
    assert printer.uploads == [("out.gcode", SLICE)]
    assert resp["hardware"]["stops"][0]["already_in_file"]


def test_a_pause_missing_from_one_layer_is_still_refused(slicer_tools, tmp_path, monkeypatch):
    printer = _Printer()
    stops = [{"n": 1, "before_layer": 12, "already_in_file": "Your file already pauses before this layer."},
             {"n": 2, "before_layer": 30}]
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, _Planner(_answer(stops=stops)))
    assert resp["success"] is False and resp["error"]["code"] == "HARDWARE_PAUSE_NOT_WRITTEN"
    assert printer.uploads == []
