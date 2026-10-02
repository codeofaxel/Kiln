"""Always allow: a standing yes with no end, for ONE printer.

A standing window is capped at a day, so a person who asks their printer
for something every day had to say yes every day, for ever.  Always allow
is the same record with no end.  What keeps that from being the cap with
a hole in it is pinned here, at the doors a person and an agent actually
reach — the ``kiln consent`` command and the registered tools, called the
way a host calls them:

* only a person at a terminal turns it on, by typing the printer's name;
  no tool, no dialog answer, no typed code, no environment variable and
  no hand-written record does;
* it is for a machine, not a label: a different machine under the name
  turns it off, and says so;
* the preview is still required, and every safety check at print start
  still runs — a file the printer would refuse is still refused;
* every start under it says so on its result and on the audit line;
* turning it off is one step, from anywhere, the agent included.

A/B: the tests that pin a guard were run with that guard removed and
observed failing; each says which in its docstring.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import time

import pytest
from click.testing import CliRunner

from kiln import (
    consent_window_note,
    consent_windows,
    preview_evidence,
    print_consent,
    print_signoff,
    screen_code,
    server,
)
from kiln.preview_gate import PreviewGate
from kiln.print_consent import (
    CHOICE_THIS_PRINT,
    FIELD_ANSWER,
    FIELD_FOR_HOW_LONG,
    SOURCE_ALWAYS,
    SOURCE_ELICITED,
    SOURCE_WINDOW,
    answer_from_content,
)
from kiln.printers.base import (
    JobProgress,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterFile,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
)

# ---------------------------------------------------------------------------
# A printer, and the two doors
# ---------------------------------------------------------------------------


class _Printer(PrinterAdapter):
    """A real adapter subclass, so a start runs the whole template: the
    sign-off backstop and every check :func:`run_adapter_gate` makes."""

    def __init__(self, serial: str) -> None:
        self.serial = serial
        self.started: list[str] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def capabilities(self) -> PrinterCapabilities:
        return PrinterCapabilities()

    def get_state(self) -> PrinterState:
        return PrinterState(state=PrinterStatus.IDLE, connected=True)

    def get_job(self) -> JobProgress:
        return JobProgress()

    def list_files(self) -> list[PrinterFile]:
        return [PrinterFile(name="part.gcode", path="part.gcode", size_bytes=1)]

    def upload_file(self, file_path: str) -> UploadResult:
        return UploadResult(success=True, file_name=os.path.basename(file_path), message="ok")

    def _start_print_impl(self, file_name: str, **kwargs) -> PrintResult:
        self.started.append(file_name)
        return PrintResult(success=True, message="started")

    def cancel_print(self) -> PrintResult:
        return PrintResult(success=True, message="")

    def pause_print(self) -> PrintResult:
        return PrintResult(success=True, message="")

    def _resume_print_impl(self) -> PrintResult:
        return PrintResult(success=True, message="")

    def emergency_stop(self) -> PrintResult:
        return PrintResult(success=True, message="")

    def _load_filament_impl(self, plan):
        raise NotImplementedError

    def _unload_filament_impl(self, plan):
        raise NotImplementedError

    def _purge_filament_impl(self, plan):
        raise NotImplementedError

    def set_tool_temp(self, target: float) -> bool:
        return True

    def set_bed_temp(self, target: float) -> bool:
        return True

    def send_gcode(self, commands: list[str]) -> bool:
        return True

    def delete_file(self, file_path: str) -> bool:
        return True


_result_line_installed = False


def _install_result_line() -> None:
    """The hook that puts the standing-window block on a print result, on
    the real server, once — what startup does (``server._start``)."""
    global _result_line_installed  # noqa: PLW0603
    if not _result_line_installed:
        assert consent_window_note.install(server.mcp)
        _result_line_installed = True


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Fresh home, fresh gate, no bypass, not hosted, nobody at a terminal."""
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    for var in ("KILN_SKIP_PREVIEW_GATE", "KILN_HOSTED_MULTITENANT", "KILN_NO_LOCAL_STAGE",
                "KILN_PRINTER_HOST", "KILN_AUTO_PRINT_MARKETPLACE", "KILN_AUTO_PRINT_GENERATED"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("KILN_EMERGENCY_PERSIST", "0")
    preview_evidence._reset_for_tests()
    print_signoff._reset_for_tests()
    print_consent._reset_for_tests()
    consent_windows._reset_for_tests()
    import kiln.preview_gate as pg

    monkeypatch.setattr(pg, "_gate", PreviewGate())
    monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_read_config_printers", lambda: {})
    # The real limiter, fresh: another test's starts are not this test's.
    monkeypatch.setattr(server, "_tool_limiter", type(server._tool_limiter)())
    monkeypatch.setattr(consent_windows, "person_at_terminal", lambda: False)
    monkeypatch.setattr("kiln.local_stage.host_renders_apps", lambda *a, **k: False)
    monkeypatch.setattr(screen_code, "_show_hook", lambda issued: False)  # never a real banner
    screen_code._reset_for_tests()
    server._ensure_internal_tool_plugins_registered()
    _install_result_line()
    yield
    screen_code._reset_for_tests()
    preview_evidence._reset_for_tests()
    print_signoff._reset_for_tests()
    print_consent._reset_for_tests()
    consent_windows._reset_for_tests()


@pytest.fixture
def garage():
    """One printer, set up as ``garage``."""
    printer = _Printer("SERIAL-A")
    server._get_registry().register("garage", printer)
    return printer


@pytest.fixture
def at_terminal(monkeypatch):
    """A person is at this terminal: stdin and stdout are both a TTY."""
    monkeypatch.setattr(consent_windows, "person_at_terminal", lambda: True)


def _kiln(*args: str, typed: str | None = None):
    """The command a person types, and what they type when it asks."""
    from kiln.cli.main import cli

    return CliRunner().invoke(cli, list(args), input=None if typed is None else typed + "\n")


def _said(result) -> str:
    """What the command printed, as one line: an error is drawn in a box
    that wraps, and a sentence is the same sentence however it wrapped."""
    return " ".join(result.output.replace("│", " ").split())


def _turn_on(name: str = "garage"):
    """A person turns always allow on for *name*, the only way there is."""
    result = _kiln("consent", "window", "--always", "--printer", name, typed=name)
    assert result.exit_code == 0, result.output
    [entry] = [w for w in consent_windows.live_windows() if w.always]
    return entry


def _client():
    """A host connected to the real server, in process.  SDK 2 connects
    ``Client`` to a server object directly; 1.x has the memory-stream
    helper that 2.x removed."""
    try:
        from mcp import Client
    except ImportError:
        from mcp.shared.memory import create_connected_server_and_client_session

        return create_connected_server_and_client_session(server.mcp)
    return Client(server.mcp)


def _call(tool: str, **arguments) -> dict:
    """A registered tool, called the way a host calls it: a ``tools/call``
    request from a connected client, through the server's own dispatch —
    the consent wrapper before the tool and the result hooks after it.
    Returns the result as the host reads it."""

    async def _one_call():
        async with _client() as client:
            return await client.call_tool(tool, arguments)

    result = asyncio.run(_one_call())
    structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    text = "".join(b.text for b in result.content if getattr(b, "type", None) == "text")
    return json.loads(text)


def _gcode(tmp_path: pathlib.Path, name: str = "part.gcode") -> str:
    path = tmp_path / name
    path.write_text("G28\nG1 X10 Y10 F3000\n")
    return str(path)


def _previewed(path: str) -> str:
    """The SAW half, on record the way an agent puts it there."""
    preview_evidence.record("stage", path, via="panel_fetch")
    out = server.issue_preview_token(path, door="stage")
    assert out["success"], out
    return out["token"]


@pytest.fixture
def audits(monkeypatch):
    seen: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        server, "_audit", lambda tool, action, details=None: seen.append((tool, action, details or {})),
    )
    return seen


ON_LINE = 'Started without asking. Always allow is on for garage. Say "ask me first" to turn it off.'
OFF_LINE = "Always allow is off for garage. Kiln will ask before each print."
SWAPPED_LINE = (
    "Always allow is off for garage. A different printer is now set up under that name, "
    "so Kiln turned it off. Kiln will ask before each print."
)


@pytest.fixture
def no_rate_limit(monkeypatch):
    """Several starts in one test: the limiter is not what is under test
    (it has its own test below, with always allow on)."""
    monkeypatch.setattr(server, "_check_rate_limit", lambda *_a, **_k: None)


def _start(tmp_path: pathlib.Path, *, printer_name: str | None = "garage", previewed: bool = True) -> dict:
    """An agent starts ``part.gcode``, having shown it (or not)."""
    arguments: dict = {"file_name": "part.gcode"}
    if printer_name is not None:
        arguments["printer_name"] = printer_name
    if previewed:
        arguments["preview_token"] = _previewed(_gcode(tmp_path))
    return _call("start_print", **arguments)


def _always_entries() -> list[consent_windows.Window]:
    return [w for w in consent_windows.live_windows() if w.always]


# ---------------------------------------------------------------------------
# Turning it on: a person, at a terminal, typing the printer's name
# ---------------------------------------------------------------------------


class TestTurningItOn:
    def test_a_person_types_the_name_and_it_is_on(self, garage, at_terminal):
        result = _kiln("consent", "window", "--always", "--printer", "garage", typed="garage")
        assert result.exit_code == 0, result.output
        # What they read before they typed it.
        assert "Always allow prints on garage?" in result.output
        assert "Kiln won't check with you first." in result.output
        assert "Type the printer's name to turn it on" in result.output
        assert "Always allow is on for garage." in result.output
        [entry] = _always_entries()
        assert entry.scope == ("garage",) and entry.until is None
        assert entry.machine == "fake:serial:serial-a"
        assert entry.source == print_consent.SOURCE_TERMINAL
        assert entry.set_by.startswith("os_user:")
        # On disk: no end, and marked, so nothing reads it as a date.
        [row] = json.loads(consent_windows._path().read_text())["windows"]
        assert row["until"] is None and row["always"] is True and row["machine"] == entry.machine

    def test_the_name_is_matched_the_way_kiln_matches_names(self, garage, at_terminal):
        """Case and surrounding spaces are not a different printer."""
        assert _kiln("consent", "window", "--always", "--printer", "garage", typed="  Garage ").exit_code == 0
        assert len(_always_entries()) == 1

    @pytest.mark.parametrize("typed", ["workshop", "", "y", "yes", "garag", "garage2"])
    def test_anything_but_the_name_turns_nothing_on(self, garage, at_terminal, typed):
        """A/B: with the name comparison removed from ``open_always`` this
        fails — every one of these turns it on."""
        result = _kiln("consent", "window", "--always", "--printer", "garage", typed=typed)
        assert result.exit_code != 0, result.output
        assert "not turned on" in _said(result)
        assert consent_windows.live_windows() == []

    def test_off_a_terminal_it_refuses_even_with_the_name_piped_in(self, garage):
        """An agent's subprocess, or ``echo garage |``: no terminal, no
        person.  The screen is not even shown."""
        result = _kiln("consent", "window", "--always", "--printer", "garage", typed="garage")
        assert result.exit_code != 0
        assert "terminal" in _said(result).lower()
        assert "Always allow prints on" not in result.output
        assert consent_windows.live_windows() == []

    def test_the_engine_holds_the_terminal_rule_itself(self, garage):
        """The command checks for a terminal before it asks; the writer
        checks again, so the rule does not rest on one caller.  A/B: with
        ``_require_person()`` removed from ``open_always`` this fails."""
        with pytest.raises(consent_windows.NotAPerson):
            consent_windows.open_always(printer_name="garage", typed_name="garage")
        assert consent_windows.live_windows() == []

    def test_it_is_for_one_printer_whatever_the_tier(self, garage, at_terminal, monkeypatch):
        monkeypatch.setattr(consent_windows, "_fleet_tier_allows", lambda: True)
        for flags in (["--fleet"], ["--printers", "garage,workshop"], ["--printers", "garage"]):
            result = _kiln("consent", "window", "--always", *flags, typed="garage")
            assert result.exit_code != 0, (flags, result.output)
            assert "one printer" in _said(result)
        # A length and no end are two different asks.
        result = _kiln("consent", "window", "--always", "--for", "2h", "--printer", "garage", typed="garage")
        assert result.exit_code != 0
        assert consent_windows.live_windows() == []

    def test_a_printer_kiln_cannot_tell_apart_is_refused(self, at_terminal):
        """No serial and no address: Kiln could not notice a different
        machine under the name, so the permission is not given.  The same
        for a name nothing is set up under."""
        anonymous = _Printer("")
        server._get_registry().register("shed", anonymous)
        for name in ("shed", "nowhere"):
            result = _kiln("consent", "window", "--always", "--printer", name, typed=name)
            assert result.exit_code != 0, result.output
            assert "cannot tell which machine" in _said(result)
        assert consent_windows.live_windows() == []

    def test_turning_it_on_twice_leaves_one(self, garage, at_terminal):
        first = _turn_on()
        result = _kiln("consent", "window", "--always", "--printer", "garage", typed="garage")
        assert result.exit_code == 0, result.output
        [entry] = _always_entries()
        assert entry.id != first.id
        assert consent_windows.get_window(first.id).revoked_reason == consent_windows.REASON_REPLACED

    def test_the_hosted_server_has_none(self, garage, at_terminal, monkeypatch):
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        result = _kiln("consent", "window", "--always", "--printer", "garage", typed="garage")
        assert result.exit_code != 0
        monkeypatch.delenv("KILN_HOSTED_MULTITENANT")
        assert consent_windows.live_windows() == []

    def test_a_window_with_an_end_still_needs_its_length(self, garage, at_terminal):
        """``--for`` stopped being required by the parser so ``--always``
        could stand in for it; a window with neither is still refused."""
        result = _kiln("consent", "window", "--printer", "garage")
        assert result.exit_code != 0 and "--for" in _said(result)
        assert consent_windows.live_windows() == []
        assert _kiln("consent", "window", "--for", "2h", "--printer", "garage").exit_code == 0
        [w] = consent_windows.live_windows()
        assert not w.always and w.until is not None


# ---------------------------------------------------------------------------
# No other door turns it on
# ---------------------------------------------------------------------------


class _Host:
    """The client side of the approval dialog: answers with what a person
    picked and typed."""

    def __init__(self, answer: str, typed: str = "") -> None:
        self.answer, self.typed = answer, typed

    async def elicit(self, message, schema):
        import types

        data = {FIELD_ANSWER: self.answer, FIELD_FOR_HOW_LONG: self.typed}
        return types.SimpleNamespace(action="accept", data=types.SimpleNamespace(**data))


class TestNoOtherDoor:
    def test_only_the_command_calls_the_writer(self):
        """Every caller of ``open_always`` in the package, by reading it:
        the ``kiln consent`` command and nothing else — no tool, no server
        path, no plugin."""
        import ast

        src = pathlib.Path(server.__file__).parent
        callers: list[str] = []
        for path in src.rglob("*.py"):
            tree = ast.parse(path.read_text())
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(fn):
                    if isinstance(node, ast.Call):
                        called = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                        if called == "open_always":
                            callers.append(f"{path.relative_to(src)}::{fn.name}")
        assert callers == ["cli/consent_commands.py::_turn_on_always"], callers

    def test_no_tool_takes_an_argument_that_could_ask_for_it(self):
        """The registered tools, as a host lists them: none has a name or
        a parameter that says always-allow, so there is nothing to call."""
        tools = server.mcp._tool_manager._tools
        assert "consent_window_status" in tools and "revoke_consent_window" in tools
        offenders = []
        for name, tool in tools.items():
            params = set((getattr(tool, "parameters", None) or {}).get("properties", {}))
            if "always" in name or "always_allow" in params or "always" in params:
                offenders.append(name)
        assert offenders == [], offenders

    @pytest.mark.parametrize("typed", ["always", "forever", "999d", "inf", "nan", "0", "-1h"])
    def test_the_dialog_cannot_be_answered_with_no_end(self, garage, tmp_path, monkeypatch, typed):
        """The approval dialog's length field, filled with every way of
        saying "no end": the yes to THIS print stands, and whatever window
        opens has an end inside a day."""
        monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: True)

        async def _one_ask():
            token = await server._obtain_print_consent(
                "start_print", {"file_name": "part.gcode", "printer_name": "garage"}, _Host(CHOICE_THIS_PRINT, typed),
            )
            if token is not None:
                print_consent.reset_consent(token)

        asyncio.run(_one_ask())
        assert _always_entries() == []
        now = time.time()
        for w in consent_windows.live_windows():
            assert w.until is not None and w.until - now <= print_consent.MAX_WINDOW_SECONDS
        raw = consent_windows._path().read_text() if consent_windows._path().exists() else "{}"
        assert '"always"' not in raw

    @pytest.mark.parametrize("after", ["always", "forever", "always allow", "999d"])
    def test_a_typed_code_cannot_ask_for_it(self, garage, tmp_path, monkeypatch, no_rate_limit, after):
        """The code on the screen, relayed in chat with a way of saying
        "no end" after it: the code is a yes to that one print, the window
        asked for does not open, and the next print asks again."""
        shown: list = []
        monkeypatch.setenv("KILN_SCREEN_CODE", "1")
        monkeypatch.setattr(screen_code, "screen_available", lambda: True)
        monkeypatch.setattr(screen_code, "MIN_READ_S", 0.0)
        monkeypatch.setattr(screen_code, "_show_hook", lambda issued: shown.append(issued) or True)
        refused = _start(tmp_path)
        assert refused["error"]["code"] == "PREVIEW_NOT_CONFIRMED" and shown, refused
        _call("give_print_code", words=f"{shown[0].code} {after}")
        started = _start(tmp_path)
        assert consent_windows.live_windows() == []
        if started.get("success"):
            # The one print the code approved; the window it asked for is reported as not opened.
            assert started[consent_window_note.RESULT_KEY]["opened"] is False
        assert len(garage.started) <= 1
        again = _start(tmp_path)
        assert again["error"]["code"] == "PREVIEW_NOT_CONFIRMED", again
        assert len(garage.started) <= 1 and consent_windows.live_windows() == []

    @pytest.mark.parametrize(
        "row",
        [
            {"source": print_consent.SOURCE_ELICITED},          # the dialog's door
            {"source": print_consent.SOURCE_CODE},              # the typed code's door
            {"source": consent_windows.SOURCE_WEB},             # no local entry comes from the web page
            {"machine": ""},                                    # no machine on record
            {"scope": ["garage", "workshop"]},                  # more than one printer
            {"scope": "fleet"},                                 # every printer
            {"always": "true"},                                 # marked, but not by the writer
            {"always": False},                                  # no end and not marked: run out
        ],
    )
    def test_a_record_with_no_end_that_the_writer_did_not_write_is_closed(self, garage, tmp_path, row):
        """An entry dropped into the file: no end is honoured only as the
        whole of what the terminal door writes.  A/B: with the door check
        removed from ``Window.from_dict`` the first three rows start the
        print."""
        record = {
            "id": "w_handmade", "set_by": "os_user:someone", "set_at": time.time(), "until": None,
            "always": True, "scope": ["garage"], "machine": "fake:serial:serial-a",
            "source": print_consent.SOURCE_TERMINAL, "revoked_at": None, "extensions": [],
            **row,
        }
        path = consent_windows._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"windows": [record]}))
        assert consent_windows.covering("garage") is None
        out = _start(tmp_path)
        assert out["error"]["code"] == "PREVIEW_NOT_CONFIRMED", out
        assert garage.started == []

    def test_the_whole_record_is_what_the_writer_writes(self, garage, tmp_path, no_rate_limit):
        """The control for the test above: the same hand-written row with
        nothing changed IS honoured — so each row there is refused for the
        one thing it changed, not because hand-written rows never work."""
        record = {
            "id": "w_handmade", "set_by": "os_user:someone", "set_at": time.time(), "until": None,
            "always": True, "scope": ["garage"], "machine": "fake:serial:serial-a",
            "source": print_consent.SOURCE_TERMINAL, "revoked_at": None, "extensions": [],
        }
        path = consent_windows._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"windows": [record]}))
        assert _start(tmp_path)["success"] is True and garage.started == ["part.gcode"]

    def test_no_environment_variable_stands_in(self, garage, tmp_path, monkeypatch):
        import re

        for var in ("KILN_ALWAYS_ALLOW", "KILN_ALWAYS_ALLOW_PRINTER", "KILN_CONSENT_ALWAYS", "KILN_AUTO_PRINT"):
            monkeypatch.setenv(var, "garage")
        assert _start(tmp_path)["error"]["code"] == "PREVIEW_NOT_CONFIRMED"
        assert garage.started == []
        src = pathlib.Path(consent_windows.__file__).read_text()
        assert set(re.findall(r"environ(?:\.get)?\s*[\[(]\s*[\"']([A-Z_]+)", src)) <= {"KILN_HOME"}


# ---------------------------------------------------------------------------
# What it changes, and what it does not
# ---------------------------------------------------------------------------


class TestAPrintUnderIt:
    def test_it_starts_without_asking_and_says_so(self, garage, at_terminal, tmp_path, audits):
        entry = _turn_on()
        out = _start(tmp_path)
        assert out["success"] is True, out
        assert garage.started == ["part.gcode"]
        # On the result: the line, as written, and whose permission it was.
        block = out[consent_window_note.RESULT_KEY]
        assert block["note"] == ON_LINE
        assert block["always"] is True and block["id"] == entry.id and block["until"] is None
        assert block["opened_by"] == entry.set_by and block["opened_via"] == "terminal"
        assert "revoke_consent_window" in block["for_the_assistant"]
        # On the audit line: its own word, and who turned it on, when, through which door.
        [details] = [d for _, action, d in audits if action == "preview_gate_satisfied"]
        assert details["consent"] == SOURCE_ALWAYS and details["window_id"] == entry.id
        assert details["always_allow"] == {
            "turned_on_by": entry.set_by, "turned_on_at": entry.set_at, "turned_on_via": "terminal",
            "printer": "garage", "machine": "fake:serial:serial-a",
        }

    def test_without_it_the_same_call_is_refused(self, garage, tmp_path):
        """The control: nothing else in this harness is saying yes."""
        out = _start(tmp_path)
        assert out["error"]["code"] == "PREVIEW_NOT_CONFIRMED", out
        assert garage.started == []

    def test_the_preview_is_still_required(self, garage, at_terminal, tmp_path):
        """Always allow replaces the yes.  The print still has to have
        been shown."""
        _turn_on()
        out = _start(tmp_path, previewed=False)
        assert out["error"]["code"] == "PREVIEW_NOT_CONFIRMED", out
        assert "preview" in out["error"]["message"].lower()
        assert garage.started == []
        # The line still rides the refusal, without claiming a start.
        note = out[consent_window_note.RESULT_KEY]["note"]
        assert note.startswith("Always allow is on for garage.") and "Started" not in note

    def test_a_part_left_on_the_plate_still_refuses_the_start(self, garage, at_terminal, tmp_path):
        """A file the start checks refuse is refused with always allow on,
        exactly as it is for a person's own yes."""
        from kiln import plate_state

        _turn_on()
        plate_state.mark_occupied(garage, plate_state.PlateJob(file="earlier.gcode"))
        out = _start(tmp_path)
        assert out["error"]["code"] == plate_state.START_NOT_YET_CODE, out
        assert garage.started == []

    def test_the_emergency_latch_still_refuses_the_start(self, garage, at_terminal, tmp_path, monkeypatch):
        _turn_on()
        latched = {"success": False, "error": {"code": "EMERGENCY_LATCHED", "message": "latched", "retryable": False}}
        monkeypatch.setattr(server, "_emergency_latch_error", lambda tool, printer: latched)
        out = _start(tmp_path)
        assert out["error"]["code"] == "EMERGENCY_LATCHED", out
        assert garage.started == []

    def test_the_printers_own_refusal_still_stands(self, at_terminal, tmp_path):
        """The adapter's gate (bed fit, temperatures, nozzle, plate) runs
        inside every start; a start it refuses does not reach the machine."""
        from kiln.printers.base import PrinterError

        class _Refusing(_Printer):
            def _start_print_impl(self, file_name: str, **kwargs) -> PrintResult:
                raise PrinterError("refused at the machine")

        printer = _Refusing("SERIAL-A")
        server._get_registry().register("garage", printer)
        _turn_on()
        out = _start(tmp_path)
        assert out["success"] is False, out

    def test_the_rate_limit_still_applies(self, garage, at_terminal, tmp_path):
        """Two starts back to back: the second is held by the limiter that
        holds every caller, standing permission or not."""
        _turn_on()
        assert _start(tmp_path)["success"] is True
        again = _start(tmp_path)
        assert again.get("success") is not True, again
        assert garage.started == ["part.gcode"]

    def test_it_covers_that_machine_under_its_other_name(self, garage, at_terminal, tmp_path, no_rate_limit):
        """Kiln also knows the active printer as ``default``.  The
        permission is for the machine: an unnamed start, which is aimed at
        ``default``, reaches the same printer and is covered."""
        server._get_registry().register("default", garage)
        _turn_on("garage")
        out = _start(tmp_path, printer_name=None)
        assert out["success"] is True, out
        assert garage.started == ["part.gcode"]

    def test_it_covers_no_other_printer(self, garage, at_terminal, tmp_path):
        workshop = _Printer("SERIAL-W")
        server._get_registry().register("workshop", workshop)
        _turn_on("garage")
        out = _start(tmp_path, printer_name="workshop")
        assert out["error"]["code"] == "PREVIEW_NOT_CONFIRMED", out
        assert workshop.started == [] and garage.started == []
        assert consent_window_note.RESULT_KEY not in out
        # And it is still on for garage: asking about another printer closes nothing.
        assert len(_always_entries()) == 1

    def test_on_the_hosted_server_it_is_nobodys(self, garage, at_terminal, tmp_path, monkeypatch):
        _turn_on()
        token = _previewed(_gcode(tmp_path))
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        block = server._preview_gate_error("start_print", "part.gcode", token, printer_name="garage")
        assert block is not None and block["error"]["code"] == "PREVIEW_NOT_CONFIRMED"


# ---------------------------------------------------------------------------
# A different machine under the name
# ---------------------------------------------------------------------------


class TestADifferentMachine:
    def _swap(self) -> _Printer:
        """A different printer is set up under the name ``garage``."""
        replacement = _Printer("SERIAL-B")
        server._get_registry().register("garage", replacement)
        return replacement

    def test_it_turns_itself_off_and_says_so(self, garage, at_terminal, tmp_path, no_rate_limit):
        """A/B: with the machine comparison removed from ``_always_stands``
        this fails — the print starts on the replacement."""
        entry = _turn_on()
        replacement = self._swap()
        out = _start(tmp_path)
        assert out["error"]["code"] == "PREVIEW_NOT_CONFIRMED", out
        assert replacement.started == [] and garage.started == []
        # Off, with the reason on record.
        assert consent_windows.live_windows() == []
        closed = consent_windows.get_window(entry.id)
        assert closed.revoked_at is not None and closed.revoked_reason == consent_windows.REASON_MACHINE_CHANGED
        # Said: in the refusal the agent relays, and on the result.
        assert SWAPPED_LINE in out["error"]["message"]
        assert out[consent_window_note.RESULT_KEY]["note"] == SWAPPED_LINE
        # And everywhere a person looks.
        status = _call("consent_window_status")
        assert status["windows"] == [] and [t["note"] for t in status["turned_off"]] == [SWAPPED_LINE]
        assert SWAPPED_LINE in _said(_kiln("consent", "status"))
        assert _call("printer_status", printer_name="garage")["always_allow"]["note"] == SWAPPED_LINE

    def test_status_alone_notices_the_swap(self, garage, at_terminal):
        """Nobody has tried to print yet: the status surfaces check the
        machine too, so none of them shows as on what a print would find
        off."""
        entry = _turn_on()
        self._swap()
        assert _call("consent_window_status")["windows"] == []
        assert consent_windows.get_window(entry.id).revoked_reason == consent_windows.REASON_MACHINE_CHANGED

    def test_turning_it_on_for_the_new_machine_ends_the_notice(self, garage, at_terminal, tmp_path, no_rate_limit):
        _turn_on()
        replacement = self._swap()
        assert _start(tmp_path)["error"]["code"] == "PREVIEW_NOT_CONFIRMED"
        _turn_on()
        out = _start(tmp_path)
        assert out["success"] is True and replacement.started == ["part.gcode"]
        assert out[consent_window_note.RESULT_KEY]["note"] == ON_LINE
        assert _call("consent_window_status")["turned_off"] == []

    def test_the_notice_is_said_for_a_day(self, garage, at_terminal, monkeypatch):
        _turn_on()
        self._swap()
        assert consent_windows.standing_now() == []
        assert consent_windows.turned_itself_off("garage") is not None
        later = time.time() + consent_windows.TURNED_OFF_SAID_FOR_SECONDS + 60
        assert consent_windows.turned_itself_off("garage", now=later) is None

    def test_a_job_queued_under_it_does_not_start_on_the_new_machine(self, garage, at_terminal, tmp_path):
        """The scheduler's reader: a job queued while always allow was on
        is asked the gate's question again when it is dispatched."""
        from kiln.events import EventBus
        from kiln.queue import PrintQueue
        from kiln.scheduler import JobScheduler

        entry = _turn_on()
        queue = PrintQueue(db_path=str(tmp_path / "q.db"))
        scheduler = JobScheduler(queue, server._get_registry(), EventBus(), poll_interval=0.01)
        record = {"source": SOURCE_ALWAYS, "door": "stage", "printer_name": "garage", "window_id": entry.id}

        waiting = queue.submit("part.gcode", "garage", "test", metadata={"preview_signoff": dict(record)})
        replacement = self._swap()
        summary = scheduler.tick()
        assert summary["dispatched"] == [] and replacement.started == [] and garage.started == []
        error = queue.get_job(waiting).error or ""
        assert "always allow" in error and "different printer" in error

    def test_a_job_queued_under_it_starts_while_it_stands(self, garage, at_terminal, tmp_path):
        """The control for the test above."""
        from kiln.events import EventBus
        from kiln.queue import PrintQueue
        from kiln.scheduler import JobScheduler

        entry = _turn_on()
        queue = PrintQueue(db_path=str(tmp_path / "q.db"))
        scheduler = JobScheduler(queue, server._get_registry(), EventBus(), poll_interval=0.01)
        record = {"source": SOURCE_ALWAYS, "door": "stage", "printer_name": "garage", "window_id": entry.id}
        job = queue.submit("part.gcode", "garage", "test", metadata={"preview_signoff": record})
        summary = scheduler.tick()
        assert [d["job_id"] for d in summary["dispatched"]] == [job], summary
        assert garage.started == ["part.gcode"]

    def test_a_job_queued_under_it_does_not_start_once_it_is_turned_off(self, garage, at_terminal, tmp_path):
        from kiln.events import EventBus
        from kiln.queue import PrintQueue
        from kiln.scheduler import JobScheduler

        entry = _turn_on()
        queue = PrintQueue(db_path=str(tmp_path / "q.db"))
        scheduler = JobScheduler(queue, server._get_registry(), EventBus(), poll_interval=0.01)
        record = {"source": SOURCE_ALWAYS, "door": "stage", "printer_name": "garage", "window_id": entry.id}
        job = queue.submit("part.gcode", "garage", "test", metadata={"preview_signoff": record})
        _call("revoke_consent_window", window_id=entry.id)
        assert scheduler.tick()["dispatched"] == [] and garage.started == []
        assert "always allow" in (queue.get_job(job).error or "")


# ---------------------------------------------------------------------------
# Seeing that it is on
# ---------------------------------------------------------------------------


class TestSeeingIt:
    def test_every_status_surface_shows_it(self, garage, at_terminal):
        entry = _turn_on()
        # The command a person types.
        status = _kiln("consent", "status")
        assert status.exit_code == 0 and "Always allow is on for garage" in status.output and entry.id in status.output
        [row] = json.loads(_kiln("consent", "status", "--json").output)["windows"]
        assert row["always"] is True and row["until"] is None and row["remaining_minutes"] is None
        # The tools an agent reads.
        [row] = _call("consent_window_status")["windows"]
        assert row["always"] is True and row["id"] == entry.id and row["until"] is None
        [row] = _call("consent_window_status", printer_name="garage")["windows"]
        assert row["id"] == entry.id
        safety = _call("safety_status")
        assert "always allow on garage" in safety["summary"]
        assert [w["id"] for w in safety["standing_windows"]] == [entry.id]
        # The printer's own status.
        block = _call("printer_status", printer_name="garage")["always_allow"]
        assert block["always"] is True and block["id"] == entry.id
        assert block["note"] == 'Always allow is on for garage. Say "ask me first" to turn it off.'

    def test_a_printer_without_it_shows_nothing(self, garage, at_terminal):
        workshop = _Printer("SERIAL-W")
        server._get_registry().register("workshop", workshop)
        _turn_on("garage")
        assert "always_allow" not in _call("printer_status", printer_name="workshop")
        assert _call("consent_window_status", printer_name="workshop")["windows"] == []

    def test_kiln_doctor_shows_it(self, garage, at_terminal):
        entry = _turn_on()
        result = _kiln("doctor", "--json")
        assert result.exit_code in (0, 1), result.output
        checks = {c["name"]: c for c in json.loads(result.output)["checks"]}
        line = checks["standing_consent_windows"]["detail"]
        assert entry.id in line and "always allow on garage" in line and "kiln consent revoke" in line

    def test_a_refusal_names_it_where_it_names_a_terminal(self, garage, tmp_path):
        """With it off, the refusal an agent relays says how a person
        turns it on — in the sentence that already names a terminal."""
        message = _start(tmp_path)["error"]["message"]
        assert "kiln consent window --for 2h --printer garage" in message
        assert "kiln consent window --always --printer garage" in message

    def test_the_hosted_refusal_does_not_name_it(self, garage, tmp_path, monkeypatch):
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        message = server._no_yes_message("start_print", "part.gcode", "garage")
        assert "--always" not in message and "kiln consent" not in message


# ---------------------------------------------------------------------------
# Turning it off: one step, from anywhere
# ---------------------------------------------------------------------------


class TestTurningItOff:
    def test_the_agent_turns_it_off_in_one_call(self, garage, at_terminal, tmp_path, no_rate_limit):
        _turn_on()
        out = _call("revoke_consent_window", printer_name="garage")
        assert out["success"] is True and out["note"] == OFF_LINE
        assert consent_windows.live_windows() == []
        # And the next print asks.
        assert _start(tmp_path)["error"]["code"] == "PREVIEW_NOT_CONFIRMED"
        assert garage.started == []

    def test_by_the_machines_other_name(self, garage, at_terminal):
        """The person says "ask me first" about the printer; the agent may
        know it as ``default``."""
        server._get_registry().register("default", garage)
        _turn_on("garage")
        out = _call("revoke_consent_window", printer_name="default")
        assert out["note"] == OFF_LINE and consent_windows.live_windows() == []

    def test_by_id_and_all_at_once(self, garage, at_terminal):
        entry = _turn_on()
        assert _call("revoke_consent_window", window_id=entry.id)["note"] == OFF_LINE
        _turn_on()
        assert _call("revoke_consent_window", all_windows=True)["note"] == OFF_LINE
        assert consent_windows.live_windows() == []

    def test_from_a_shell_with_nobody_at_it(self, garage, at_terminal, monkeypatch):
        """Turning it off needs no terminal and no typed name."""
        entry = _turn_on()
        monkeypatch.setattr(consent_windows, "person_at_terminal", lambda: False)
        result = _kiln("consent", "revoke", entry.id)
        assert result.exit_code == 0 and OFF_LINE in _said(result)
        assert consent_windows.live_windows() == []

    def test_it_has_no_end_to_extend(self, garage, at_terminal):
        entry = _turn_on()
        result = _kiln("consent", "extend", entry.id, "--for", "1h")
        assert result.exit_code != 0 and "no end" in _said(result)
        [still] = _always_entries()
        assert still.id == entry.id and still.until is None


# ---------------------------------------------------------------------------
# Beside a window with an end
# ---------------------------------------------------------------------------


class TestBesideATimedWindow:
    def test_a_timed_window_is_read_as_before(self, garage, at_terminal, tmp_path, audits):
        w = consent_windows.open_window(seconds=3600, scope=("garage",))
        out = _start(tmp_path)
        assert out["success"] is True, out
        block = out[consent_window_note.RESULT_KEY]
        assert block["id"] == w.id and "always" not in block and "standing window is open" in block["note"]
        [details] = [d for _, action, d in audits if action == "preview_gate_satisfied"]
        assert details["consent"] == SOURCE_WINDOW and "always_allow" not in details

    def test_with_both_the_result_names_always_allow(self, garage, at_terminal, tmp_path):
        consent_windows.open_window(seconds=3600, scope=("garage",))
        entry = _turn_on()
        out = _start(tmp_path)
        assert out[consent_window_note.RESULT_KEY]["id"] == entry.id
        assert out[consent_window_note.RESULT_KEY]["note"] == ON_LINE

    def test_the_cap_on_a_length_is_where_it_was(self, garage, at_terminal):
        result = _kiln("consent", "window", "--for", "25h", "--printer", "garage")
        assert result.exit_code != 0 and "24 hours" in _said(result)
        assert consent_windows.live_windows() == []

    def test_a_persons_own_yes_is_recorded_as_theirs(self, garage, tmp_path, audits):
        """With always allow off, a dialog yes is audited as a dialog yes:
        the new word appears only on starts that rest on it."""
        token = _previewed(_gcode(tmp_path))
        reset = print_consent.set_consent(print_consent.PrintConsent(
            tool="start_print", file_name="part.gcode", printer_name="garage", source=SOURCE_ELICITED,
        ))
        try:
            assert server._preview_gate_error("start_print", "part.gcode", token, printer_name="garage") is None
        finally:
            print_consent.reset_consent(reset)
        [details] = [d for _, action, d in audits if action == "preview_gate_satisfied"]
        assert details["consent"] == SOURCE_ELICITED and "always_allow" not in details


def test_the_one_parser_has_no_word_for_it():
    """The form's answers are three yeses and a no; "always" is not on it,
    and an answer that was not on the form is not a yes."""
    answer = answer_from_content("accept", {FIELD_ANSWER: "always"})
    assert not answer.accepted and answer.action == "unavailable"
