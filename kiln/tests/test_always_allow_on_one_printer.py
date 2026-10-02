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

    def __init__(self, serial: str, host: str = "") -> None:
        self.serial = serial
        self.host = host
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


def _png(width: int = 640, height: int = 480, grey: int = 128) -> bytes:
    """A real PNG the snapshot screen accepts: big enough, mid-bright, and
    varied enough not to read as a blank frame."""
    import struct
    import zlib

    rows = b""
    for y in range(height):
        row = b"\x00"
        for x in range(width):
            v = (grey + ((x * 7 + y * 13) % 90)) % 256
            row += bytes((v, v, v))
        rows += row

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


_FRAME = _png()


class _CameraPrinter(_Printer):
    """A printer with a camera of its own.  ``frame`` is what it returns:
    a usable picture, nothing, or an error."""

    def __init__(self, serial: str, frame: bytes | Exception = _FRAME) -> None:
        super().__init__(serial)
        self.frame = frame
        self.frames_fetched = 0

    @property
    def snapshot_source(self) -> str:
        return "printer"

    def get_snapshot(self) -> bytes:
        self.frames_fetched += 1
        if isinstance(self.frame, Exception):
            raise self.frame
        return self.frame


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


def _client(person=None):
    """A host connected to the real server, in process.  SDK 2 connects
    ``Client`` to a server object directly; 1.x has the memory-stream
    helper that 2.x removed.  *person* makes it an app that can draw a
    dialog: the callback is the person answering it, over a session with
    the handshake that gives the server a way to ask (SDK 2's default
    connection has none)."""
    extra = {"elicitation_callback": person} if person is not None else {}
    try:
        from mcp import Client
    except ImportError:
        from mcp.shared.memory import create_connected_server_and_client_session

        return create_connected_server_and_client_session(server.mcp, **extra)
    if person is not None:
        return Client(server.mcp, mode="legacy", **extra)
    return Client(server.mcp)


class _Person:
    """Someone at an app that can draw a dialog: answers every question
    Kiln puts to them with *answer*, and keeps what they were asked."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def __call__(self, context, params):
        from mcp import types

        self.asked.append(str(getattr(params, "message", "")))
        return types.ElicitResult(action="accept", content={"answer": self.answer})


def _call(tool: str, _person=None, **arguments) -> dict:
    """A registered tool, called the way a host calls it: a ``tools/call``
    request from a connected client, through the server's own dispatch —
    the consent wrapper before the tool and the result hooks after it.
    Returns the result as the host reads it."""

    async def _one_call():
        async with _client(_person) as client:
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

    def test_every_printer_at_once_is_not_offered(self, garage, at_terminal, monkeypatch):
        """Printers are named.  ``--fleet`` names none, on any tier."""
        monkeypatch.setattr(consent_windows, "_fleet_tier_allows", lambda: True)
        result = _kiln("consent", "window", "--always", "--fleet", typed="garage")
        assert result.exit_code != 0 and "printers you name" in _said(result)
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

    def test_the_line_never_says_started_for_a_start_the_adapter_turned_away(self, garage, at_terminal):
        """Seen live: the adapter's safety gate refused a file, the tool's
        result still read ``accepted`` (the printer had not been heard from
        since), and the line said "Started without asking".  The line now
        says started only when the adapter did."""
        import types

        _turn_on()
        turned_away = types.SimpleNamespace(
            structuredContent={
                "success": True, "print_start": "accepted",
                "evidence": {"adapter_reported_success": False, "adapter_message": "not ready for the printer"},
            },
            isError=False, content=[],
        )
        consent_window_note._attach(turned_away, None, "start_print", {"printer_name": "garage"})
        block = turned_away.structuredContent[consent_window_note.RESULT_KEY]
        assert block["note"] == 'Always allow is on for garage. Say "ask me first" to turn it off.'
        assert "bed_check" not in block
        # The control: the adapter reported the start, and the line says so.
        took_it = types.SimpleNamespace(
            structuredContent={"success": True, "print_start": "accepted", "evidence": {"adapter_reported_success": True}},
            isError=False, content=[],
        )
        consent_window_note._attach(took_it, None, "start_print", {"printer_name": "garage"})
        assert took_it.structuredContent[consent_window_note.RESULT_KEY]["note"] == ON_LINE

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

    def test_a_stored_clearance_is_asked_again_wherever_it_is_re_granted(self, garage, at_terminal):
        """A paused pipeline, like a queued job, re-grants the clearance it
        stored.  Under always allow that re-grant asks the gate's question
        again, and a start with nothing granted is turned away by the
        adapter itself.  A/B: with the re-check removed from
        ``grant_from_record`` the replacement printer starts the file."""
        entry = _turn_on()
        record = {"source": SOURCE_ALWAYS, "door": "stage", "printer_name": "garage", "window_id": entry.id}
        # While it stands: granted, and the start goes through.
        assert print_signoff.grant_from_record(record, tool="pipeline", file_name="part.gcode", printer_name="garage")
        assert garage.start_print("part.gcode").success is True
        # A different machine under the name: not granted, and not started.
        replacement = self._swap()
        assert print_signoff.grant_from_record(
            record, tool="pipeline", file_name="part.gcode", printer_name="garage",
        ) is None
        assert print_signoff.current() is None
        assert replacement.start_print("part.gcode").success is False and replacement.started == []
        # A record that names no entry rests on nothing.
        nameless = {"source": SOURCE_ALWAYS, "door": "stage", "printer_name": "garage"}
        assert print_signoff.record_refusal(nameless, "garage")

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
        assert _call("safety_settings")["always_allow"]["on_for"] == ["garage"]
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


# ---------------------------------------------------------------------------
# Two printers of one make are two machines
# ---------------------------------------------------------------------------


class TestWhichMachine:
    def test_two_of_the_same_model_are_told_apart_by_serial(self, at_terminal, tmp_path, no_rate_limit):
        """Same make, same model, both set up: the permission for one is
        not a permission for its twin."""
        left, right = _Printer("SERIAL-L"), _Printer("SERIAL-R")
        for name, printer in (("left", left), ("right", right)):
            server._get_registry().register(name, printer)
            printer.set_safety_profile("bambu_a1")
        _turn_on("left")
        assert _start(tmp_path, printer_name="left")["success"] is True
        assert _start(tmp_path, printer_name="right")["error"]["code"] == "PREVIEW_NOT_CONFIRMED"
        assert left.started == ["part.gcode"] and right.started == []

    def test_printers_without_a_serial_are_told_apart_by_address(self, at_terminal, tmp_path, no_rate_limit):
        one, other = _Printer("", host="http://192.168.1.50"), _Printer("", host="http://192.168.1.51")
        server._get_registry().register("one", one)
        server._get_registry().register("other", other)
        entry = _turn_on("one")
        assert entry.machine == "fake:host:192.168.1.50"
        assert _start(tmp_path, printer_name="one")["success"] is True
        assert _start(tmp_path, printer_name="other")["error"]["code"] == "PREVIEW_NOT_CONFIRMED"
        assert other.started == []

    def test_a_new_address_under_the_name_turns_it_off(self, at_terminal, tmp_path):
        """Known by address only: a printer that comes back at another
        address is, to Kiln, not provably the same machine — so it asks."""
        server._get_registry().register("one", _Printer("", host="192.168.1.50"))
        entry = _turn_on("one")
        moved = _Printer("", host="192.168.1.77")
        server._get_registry().register("one", moved)
        out = _start(tmp_path, printer_name="one")
        assert out["error"]["code"] == "PREVIEW_NOT_CONFIRMED" and moved.started == []
        assert consent_windows.get_window(entry.id).revoked_reason == consent_windows.REASON_MACHINE_CHANGED


# ---------------------------------------------------------------------------
# Several named printers at once
# ---------------------------------------------------------------------------


class TestSeveralAtOnce:
    @pytest.fixture
    def three(self, at_terminal, monkeypatch):
        """Three printers, on the tier that runs several at once."""
        monkeypatch.setattr(consent_windows, "_fleet_tier_allows", lambda: True)
        printers = {"garage": _Printer("SERIAL-A"), "workshop": _Printer("SERIAL-W"), "attic": _Printer("SERIAL-T")}
        for name, printer in printers.items():
            server._get_registry().register(name, printer)
        return printers

    def _several(self, typed: str, names: str = "garage,workshop,attic"):
        return _kiln("consent", "window", "--always", "--printers", names, typed=typed)

    def test_each_named_printer_gets_its_own_entry(self, three, tmp_path, no_rate_limit):
        result = self._several("3")
        assert result.exit_code == 0, result.output
        said = _said(result)
        # Every printer is listed before the person confirms.
        assert "Always allow prints on these 3 printers?" in said
        assert all(name in said for name in three)
        assert "Type the number of printers listed (3)" in said
        entries = {w.scope[0]: w for w in _always_entries()}
        assert set(entries) == set(three)
        assert {w.machine for w in entries.values()} == {"fake:serial:serial-a", "fake:serial:serial-w", "fake:serial:serial-t"}
        # Each covers its own machine...
        assert _start(tmp_path, printer_name="workshop")["success"] is True
        assert three["workshop"].started == ["part.gcode"] and three["garage"].started == []
        # ...and is turned off on its own.
        _call("revoke_consent_window", printer_name="workshop")
        assert {w.scope[0] for w in _always_entries()} == {"garage", "attic"}

    @pytest.mark.parametrize("typed", ["2", "4", "", "y", "yes", "garage", "three"])
    def test_anything_but_the_count_turns_none_on(self, three, typed):
        """A/B: with the count comparison removed from
        ``open_always_for_several`` this fails."""
        result = self._several(typed)
        assert result.exit_code != 0, result.output
        assert "not turned on" in _said(result)
        assert consent_windows.live_windows() == []

    def test_below_the_fleet_tier_it_is_one_at_a_time(self, three, monkeypatch):
        """A/B: with the tier check removed this fails."""
        monkeypatch.setattr(consent_windows, "_fleet_tier_allows", lambda: False)
        result = self._several("3")
        assert result.exit_code != 0 and "Business" in _said(result)
        assert consent_windows.live_windows() == []
        # One printer is every tier's.
        assert _kiln("consent", "window", "--always", "--printer", "garage", typed="garage").exit_code == 0

    def test_one_printer_kiln_cannot_tell_apart_stops_all_of_them(self, three):
        server._get_registry().register("shed", _Printer(""))
        result = self._several("3", names="garage,shed,attic")
        assert result.exit_code != 0 and "cannot tell which machine shed is" in _said(result)
        assert consent_windows.live_windows() == []

    def test_one_machine_named_twice_is_refused(self, three):
        server._get_registry().register("default", three["garage"])
        result = self._several("3", names="garage,default,attic")
        assert result.exit_code != 0 and "same machine" in _said(result)
        result = self._several("3", names="garage,Garage,attic")
        assert result.exit_code != 0 and "named twice" in _said(result)
        assert consent_windows.live_windows() == []

    def test_off_a_terminal_it_refuses(self, three, monkeypatch):
        monkeypatch.setattr(consent_windows, "person_at_terminal", lambda: False)
        assert self._several("3").exit_code != 0
        with pytest.raises(consent_windows.NotAPerson):
            consent_windows.open_always_for_several(printer_names=list(three), typed_count="3")
        assert consent_windows.live_windows() == []

    def test_one_of_them_swapped_turns_only_that_one_off(self, three, tmp_path, no_rate_limit):
        assert self._several("3").exit_code == 0
        server._get_registry().register("attic", _Printer("SERIAL-NEW"))
        assert _start(tmp_path, printer_name="attic")["error"]["code"] == "PREVIEW_NOT_CONFIRMED"
        assert {w.scope[0] for w in _always_entries()} == {"garage", "workshop"}


# ---------------------------------------------------------------------------
# Nobody is asked, so the bed is looked at
# ---------------------------------------------------------------------------


class TestTheBedIsLookedAt:
    @pytest.fixture
    def camera(self, at_terminal):
        """``garage`` with a working camera, always allow on."""
        printer = _CameraPrinter("SERIAL-A")
        server._get_registry().register("garage", printer)
        _turn_on()
        return printer

    def test_the_start_waits_for_eyes_on_a_fresh_frame(self, camera, tmp_path, audits, no_rate_limit):
        """A/B: with the held start removed from the gate this fails — the
        first call starts the print with nobody having looked."""
        token = _previewed(_gcode(tmp_path))
        held = _call("start_print", file_name="part.gcode", printer_name="garage", preview_token=token)
        assert held["error"]["code"] == "ALWAYS_ALLOW_LOOK_FIRST", held
        assert camera.started == []
        assert pathlib.Path(held["snapshot_path"]).read_bytes() == _FRAME
        assert "look_at_plate" in held["error"]["message"]

        # The assistant looks at the frame it was handed and says what it saw.
        seen = _call("look_at_plate", printer_name="garage", seen="clear")
        assert seen["success"] is True, seen

        # The same call again — the token was not spent by the held start.
        out = _call("start_print", file_name="part.gcode", printer_name="garage", preview_token=token)
        assert out["success"] is True, out
        assert camera.started == ["part.gcode"]
        # The picture the clear rested on is kept, and named on the result.
        check = out[consent_window_note.RESULT_KEY]["bed_check"]
        kept = pathlib.Path(check["frame"])
        assert check["checked"] is True and check["judged_by"] == "agent"
        assert kept.read_bytes() == _FRAME and kept.parent.name == "plate_looks"
        assert str(kept) in check["note"] and "your assistant checked the bed" in check["note"]
        assert out[consent_window_note.RESULT_KEY]["note"] == ON_LINE
        # And on the audit line.
        [details] = [d for _, action, d in audits if action == "preview_gate_satisfied"]
        assert details["bed_look"] == {
            "checked": True, "camera": "printer", "frame": str(kept),
            "frame_at": check["frame_at"], "judged_by": "agent",
        }

    def test_one_frame_is_fetched_per_start(self, camera, tmp_path, no_rate_limit):
        """The asker, the gate and the result line share one look."""
        token = _previewed(_gcode(tmp_path))
        _call("start_print", file_name="part.gcode", printer_name="garage", preview_token=token)
        assert camera.frames_fetched == 1
        _call("look_at_plate", printer_name="garage", seen="clear")
        _call("start_print", file_name="part.gcode", printer_name="garage", preview_token=token)
        assert camera.frames_fetched == 1 and camera.started == ["part.gcode"]

    def test_a_clear_nobody_looked_for_is_not_enough(self, camera, tmp_path, monkeypatch):
        """A record that says clear — a print taken off this morning, a
        reset — is not a look.  A/B: with ``look_for_unasked_start``
        accepting any clear record this fails."""
        from kiln import plate_state

        plate_state.mark_clear(camera, "human")
        monkeypatch.setattr(plate_state, "LOOK_GOOD_FOR_SECONDS", -1.0)  # said long ago
        out = _start(tmp_path)
        assert out["error"]["code"] == "ALWAYS_ALLOW_LOOK_FIRST", out
        assert camera.started == []

    def test_the_persons_own_word_settles_a_picture_nobody_could_judge(self, camera, tmp_path, no_rate_limit):
        """The assistant could not tell and said so; the person says the
        bed is clear.  Their word, just given, is what the start rests on
        — and the result says that, not that a picture was checked."""
        from kiln import plate_state

        assert _start(tmp_path)["error"]["code"] == "ALWAYS_ALLOW_LOOK_FIRST"
        _call("look_at_plate", printer_name="garage", seen="occupied")
        assert _start(tmp_path)["error"]["code"] == plate_state.START_NOT_YET_CODE
        plate_state.mark_clear(camera, "human", note="the person says the plate is empty")
        out = _start(tmp_path)
        assert out["success"] is True and camera.started == ["part.gcode"]
        check = out[consent_window_note.RESULT_KEY]["bed_check"]
        assert check["checked"] is True and check["judged_by"] == "person" and check["frame"] is None
        assert check["note"] == "Before it started, you said the bed was clear."

    def test_an_old_look_is_not_enough(self, camera, tmp_path, monkeypatch, no_rate_limit):
        from kiln import plate_state

        assert _start(tmp_path)["error"]["code"] == "ALWAYS_ALLOW_LOOK_FIRST"
        _call("look_at_plate", printer_name="garage", seen="clear")
        assert plate_state.read(camera).fresh_look() is not None
        # Time passes: the look is about a plate that may have changed.
        monkeypatch.setattr(plate_state, "LOOK_GOOD_FOR_SECONDS", -1.0)
        assert _start(tmp_path)["error"]["code"] == "ALWAYS_ALLOW_LOOK_FIRST"
        assert camera.started == []

    def test_a_verdict_with_no_frame_behind_it_is_not_a_look(self, camera, tmp_path):
        """``seen="clear"`` with no picture handed over first: recorded,
        as before, but nothing a start may rest on."""
        from kiln import plate_state

        _call("look_at_plate", printer_name="garage", seen="clear")
        assert plate_state.read(camera).clear and plate_state.read(camera).fresh_look() is None
        assert _start(tmp_path)["error"]["code"] == "ALWAYS_ALLOW_LOOK_FIRST"

    def test_seen_occupied_stops_the_start(self, camera, tmp_path, no_rate_limit):
        from kiln import plate_state

        assert _start(tmp_path)["error"]["code"] == "ALWAYS_ALLOW_LOOK_FIRST"
        _call("look_at_plate", printer_name="garage", seen="occupied")
        out = _start(tmp_path)
        assert out["error"]["code"] == plate_state.START_NOT_YET_CODE, out
        assert camera.started == []

    @pytest.mark.parametrize(
        "frame",
        [b"", RuntimeError("camera timed out"), _png(width=32, height=24)],
        ids=["no image", "camera error", "too small to judge"],
    )
    def test_a_camera_that_cannot_see_means_the_person_is_asked(self, at_terminal, tmp_path, frame):
        """Nothing usable from the camera: always allow is not used for
        this print, and the refusal says why.  A/B: with the blind
        fallback removed from ``consent_for`` this fails — the print
        starts with the bed unseen."""
        printer = _CameraPrinter("SERIAL-A", frame=frame)
        server._get_registry().register("garage", printer)
        entry = _turn_on()
        out = _start(tmp_path)
        assert out["error"]["code"] == "PREVIEW_NOT_CONFIRMED", out
        assert printer.started == []
        message = out["error"]["message"]
        assert "Always allow is on for garage, but Kiln could not see the bed through the camera" in message
        assert "asking you this time" in message
        # It is still on: one blind look turns nothing off.
        assert [w.id for w in _always_entries()] == [entry.id]

    def test_the_dialog_says_why_it_is_asking(self, at_terminal, monkeypatch):
        printer = _CameraPrinter("SERIAL-A", frame=b"")
        server._get_registry().register("garage", printer)
        _turn_on()
        monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: True)
        asked: list[str] = []

        class _RecordingHost(_Host):
            async def elicit(self, message, schema):
                asked.append(message)
                return await super().elicit(message, schema)

        async def _one_ask():
            token = await server._obtain_print_consent(
                "start_print", {"file_name": "part.gcode", "printer_name": "garage"}, _RecordingHost(CHOICE_THIS_PRINT),
            )
            if token is not None:
                print_consent.reset_consent(token)

        asyncio.run(_one_ask())
        [message] = asked
        assert "Always allow: on, but Kiln could not see the bed through the camera" in message

    def test_no_camera_starts_and_says_the_bed_was_not_checked(self, garage, at_terminal, tmp_path, audits):
        _turn_on()
        out = _start(tmp_path)
        assert out["success"] is True and garage.started == ["part.gcode"]
        check = out[consent_window_note.RESULT_KEY]["bed_check"]
        assert check["checked"] is False and check["camera"] is None
        assert check["note"] == "This printer has no camera Kiln can use, so the bed was not checked first."
        [details] = [d for _, action, d in audits if action == "preview_gate_satisfied"]
        assert details["bed_look"]["checked"] is False

    def test_a_persons_own_yes_needs_no_look(self, tmp_path, monkeypatch):
        """The rule is for a start nobody was asked about.  A person who
        said yes to this print is not held for a frame."""
        printer = _CameraPrinter("SERIAL-A")
        server._get_registry().register("garage", printer)
        token = _previewed(_gcode(tmp_path))
        reset = print_consent.set_consent(print_consent.PrintConsent(
            tool="start_print", file_name="part.gcode", printer_name="garage", source=SOURCE_ELICITED,
        ))
        try:
            assert server._preview_gate_error("start_print", "part.gcode", token, printer_name="garage") is None
        finally:
            print_consent.reset_consent(reset)
        assert printer.frames_fetched == 0

    def test_a_timed_window_needs_no_look(self, at_terminal, tmp_path):
        """Unchanged on purpose: the look is always allow's."""
        printer = _CameraPrinter("SERIAL-A")
        server._get_registry().register("garage", printer)
        consent_windows.open_window(seconds=3600, scope=("garage",))
        assert _start(tmp_path)["success"] is True
        assert printer.frames_fetched == 0

    def test_the_screen_says_what_kiln_does_about_the_bed(self, at_terminal):
        server._get_registry().register("garage", _CameraPrinter("SERIAL-A"))
        server._get_registry().register("shed", _Printer("SERIAL-S"))
        with_camera = _said(_kiln("consent", "window", "--always", "--printer", "garage", typed="garage"))
        assert "Kiln looks at the bed through the camera before every print" in with_camera
        without = _said(_kiln("consent", "window", "--always", "--printer", "shed", typed="shed"))
        assert "shed has no camera Kiln can use, so Kiln can't check the bed before it prints" in without
        assert "looks at the bed" not in without

    def test_a_queued_job_is_not_sent_to_a_bed_nobody_looked_at(self, camera, tmp_path):
        from kiln.events import EventBus
        from kiln.queue import PrintQueue
        from kiln.scheduler import JobScheduler

        [entry] = _always_entries()
        queue = PrintQueue(db_path=str(tmp_path / "q.db"))
        scheduler = JobScheduler(queue, server._get_registry(), EventBus(), poll_interval=0.01)
        record = {"source": SOURCE_ALWAYS, "door": "stage", "printer_name": "garage", "window_id": entry.id}
        job = queue.submit("part.gcode", "garage", "test", metadata={"preview_signoff": record})
        assert scheduler.tick()["dispatched"] == [] and camera.started == []
        assert "seen clear through the camera" in (queue.get_job(job).error or "")

    def test_at_a_terminal_the_person_is_asked_instead_of_handed_a_frame(self, camera, tmp_path, monkeypatch):
        """``kiln print`` typed by a person: they are here, so they are
        asked; an agent's shell gets the held start."""
        import click

        from kiln.cli.main import cli_gate

        monkeypatch.setattr("kiln.cli.print_gate._audit", lambda *a, **k: None)
        path = _gcode(tmp_path)
        token = _previewed(path)
        answers: list[str] = []
        monkeypatch.setattr(click, "confirm", lambda question, **_k: answers.append(question) or True)
        assert cli_gate("print", path, token, printer_name="garage", json_mode=True) is None
        assert len(answers) == 1
        # Nobody at the terminal: the refusal, and no question.
        monkeypatch.setattr(consent_windows, "person_at_terminal", lambda: False)
        print_consent.drop_consent()
        with pytest.raises(SystemExit):
            cli_gate("print", path, _previewed(path), printer_name="garage", json_mode=True)
        assert len(answers) == 1


# ---------------------------------------------------------------------------
# "Bed clear", typed in a chat
# ---------------------------------------------------------------------------


class TestThePersonSaysTheBedIsClear:
    """The assistant looked and could not tell, so the print is held.  The
    person, in a chat, says the bed is clear.  Their words reach the plate
    record through ``look_at_plate(person_says=...)`` — recorded as theirs
    when Kiln could ask them itself, as passed on when it could not."""

    @pytest.fixture
    def held(self, at_terminal, tmp_path, no_rate_limit):
        """``garage`` with a camera and always allow on; the assistant has
        looked, could not tell, and said so: a start is refused."""
        from kiln import plate_state

        printer = _CameraPrinter("SERIAL-A")
        server._get_registry().register("garage", printer)
        _turn_on()
        assert _start(tmp_path)["error"]["code"] == "ALWAYS_ALLOW_LOOK_FIRST"
        said = _call("look_at_plate", printer_name="garage", seen="occupied")
        assert "person_says" in said["next"]
        assert _start(tmp_path)["error"]["code"] == plate_state.START_NOT_YET_CODE
        return printer

    def test_in_a_chat_app_the_words_are_recorded_as_passed_on(self, held, tmp_path, audits):
        from kiln import plate_state

        out = _call("look_at_plate", printer_name="garage", person_says="  Bed clear ")
        assert out["success"] is True, out
        assert out["recorded_as"] == "the person's words, passed on by you"
        state = plate_state.read(held)
        assert state.clear and state.source == plate_state.SAID_IN_CHAT
        assert 'the person typed: "Bed clear"' in state.note
        assert "passed on by their assistant" in state.describe()
        [details] = [d for _, action, d in audits if action == "plate_cleared_by_persons_word"]
        assert details == {"printer": "garage", "words": "Bed clear", "through": "assistant"}
        # The print now starts, and says what it rested on.
        started = _start(tmp_path)
        assert started["success"] is True and held.started == ["part.gcode"]
        check = started[consent_window_note.RESULT_KEY]["bed_check"]
        assert check["judged_by"] == "person_via_assistant" and check["frame"] is None
        assert check["note"] == "Before it started, you told your assistant the bed was clear."

    def test_where_the_app_can_ask_the_person_is_asked_and_the_answer_is_theirs(self, held, tmp_path, audits):
        """A/B: with the dialog removed from the wrapper this fails — the
        words are recorded as passed on and nobody is asked."""
        from kiln import plate_state

        person = _Person("yes")
        out = _call("look_at_plate", _person=person, printer_name="garage", person_says="bed clear")
        assert out["success"] is True, out
        [question] = person.asked
        assert question == server.bed_question("garage")
        assert out["recorded_as"] == "the person's own answer, in a dialog this app showed them"
        assert plate_state.read(held).source == plate_state.SAID_DIRECTLY
        [details] = [d for _, action, d in audits if action == "plate_cleared_by_persons_word"]
        assert details["through"] == "dialog"
        started = _start(tmp_path)
        assert started["success"] is True
        assert started[consent_window_note.RESULT_KEY]["bed_check"]["note"] == "Before it started, you said the bed was clear."

    def test_a_person_who_answers_no_clears_nothing(self, held, tmp_path):
        """The assistant passed on "bed clear"; asked directly, the person
        did not say yes.  A/B: with the declined check removed from the
        tool this fails — the relayed words clear the bed anyway."""
        from kiln import plate_state

        person = _Person("no")
        out = _call("look_at_plate", _person=person, printer_name="garage", person_says="bed clear")
        assert out["error"]["code"] == "PLATE_WORD_NOT_CONFIRMED", out
        assert len(person.asked) == 1
        assert plate_state.read(held).occupied
        assert _start(tmp_path)["error"]["code"] == plate_state.START_NOT_YET_CODE
        assert held.started == []

    @pytest.mark.parametrize(
        "words",
        ["should be clear", "is it clear?", "not clear", "it isn't empty", "yes", "", "I think it's empty",
         "clear, maybe", "go ahead", "probably clear", "clear if you move the purge line"],
    )
    def test_words_that_do_not_say_it_record_nothing_and_ask_nobody(self, held, words):
        from kiln import plate_state

        person = _Person("yes")
        out = _call("look_at_plate", _person=person, printer_name="garage", person_says=words)
        assert out["error"]["code"] == "INVALID_INPUT", (words, out)
        assert person.asked == [] and plate_state.read(held).occupied

    @pytest.mark.parametrize(
        "words", ["bed clear", "printbed clear", "The plate is empty.", "it's clear", "all clear", "nothing on it",
                  "I cleared the bed", "bed's empty now"],
    )
    def test_the_ways_a_person_says_it(self, words):
        from kiln import plate_state

        assert plate_state.says_plate_is_clear(words), words

    def test_a_word_cannot_stand_in_for_the_look(self, at_terminal, tmp_path, no_rate_limit):
        """Nothing on record says a part is there, so there is nothing for
        a word to settle: the held start still wants eyes on the frame.
        A/B: with the occupied-record rule removed from the tool this fails
        — the words clear the plate and the print starts unseen."""
        printer = _CameraPrinter("SERIAL-A")
        server._get_registry().register("garage", printer)
        _turn_on()
        assert _start(tmp_path)["error"]["code"] == "ALWAYS_ALLOW_LOOK_FIRST"
        person = _Person("yes")
        out = _call("look_at_plate", _person=person, printer_name="garage", person_says="bed clear")
        assert out["error"]["code"] == "PLATE_NOTHING_TO_SETTLE", out
        assert person.asked == []
        assert _start(tmp_path)["error"]["code"] == "ALWAYS_ALLOW_LOOK_FIRST"
        assert printer.started == []

    def test_an_old_word_is_not_enough(self, held, tmp_path, monkeypatch):
        from kiln import plate_state

        assert _call("look_at_plate", printer_name="garage", person_says="bed clear")["success"] is True
        monkeypatch.setattr(plate_state, "LOOK_GOOD_FOR_SECONDS", -1.0)  # said long ago
        assert _start(tmp_path)["error"]["code"] == "ALWAYS_ALLOW_LOOK_FIRST"
        assert held.started == []

    def test_what_was_seen_and_what_was_said_are_two_calls(self, held):
        out = _call("look_at_plate", printer_name="garage", seen="clear", person_says="bed clear")
        assert out["error"]["code"] == "INVALID_INPUT"

    def test_without_a_camera_the_word_clears_a_finished_print(self, garage, at_terminal, tmp_path, no_rate_limit):
        """No camera at all: after a print, the part on the bed is the
        record, and the person's word is what clears it."""
        from kiln import plate_state

        _turn_on()
        plate_state.mark_occupied(garage, plate_state.PlateJob(file="earlier.gcode"))
        assert _start(tmp_path)["error"]["code"] == plate_state.START_NOT_YET_CODE
        assert _call("look_at_plate", printer_name="garage", person_says="plate is empty")["success"] is True
        assert _start(tmp_path)["success"] is True and garage.started == ["part.gcode"]

    def test_the_refusal_over_a_part_names_the_chat_door(self, held, tmp_path):
        message = _start(tmp_path)["error"]["message"]
        assert "look_at_plate(person_says=" in message
