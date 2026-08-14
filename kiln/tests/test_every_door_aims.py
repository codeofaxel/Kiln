"""Every door that starts a print aims the same way.

``upload_file``, ``start_print`` and ``set_temperature`` learned to take a
``printer_name``.  They are not the only way to reach a printer: seven
sibling tools slice-and-print, generate-and-print, retry-with-fix,
monitor-a-print, download-and-upload, print one plate object.  Five of
them have accepted a ``printer_name`` for longer than the aimed verbs
have existed — and each one resolved it, aimed its emergency latch, and
then asked the DEFAULT printer two questions that decide whether the
print is safe to start:

  * ``preflight_check()`` with no name, so a tool aimed at the second
    machine was cleared to print by the first machine being idle; and
  * ``notify_print_started()`` on the heater watchdog, one process-wide
    instance around the default adapter, which then believed itself busy
    and stopped its idle tick from ever cooling a default printer left
    sitting hot.

Both were live, not hypothetical: no new parameter was needed to reach
them, only a bench with two printers.

The fix is a shared path rather than a guard per door — a guard per door
is the same bug with more places to forget it.  ``_note_print_started``
owns the ownership rule, and the pre-flight verdict follows the name the
caller already passed.  The structural tests here are what stop the next
tool from growing its own copy: they read the source, so a door added
next year is covered without anyone remembering this file exists.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from kiln import server
from kiln.printers.base import PrinterState, PrinterStatus, PrintResult, UploadResult
from kiln.registry import PrinterRegistry

# ---------------------------------------------------------------------------
# Source under inspection
# ---------------------------------------------------------------------------

_SRC = pathlib.Path(server.__file__).parent
_MODULES = [_SRC / "server.py", *sorted((_SRC / "plugins").glob("*.py"))]


def _functions(path: pathlib.Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _call_name(node: ast.Call) -> str:
    """``a.b.c(...)`` -> ``"c"``; ``f(...)`` -> ``"f"``."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _params(fn) -> set[str]:
    args = fn.args
    return {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}


# ---------------------------------------------------------------------------
# The heater watchdog has exactly one caller
# ---------------------------------------------------------------------------


def test_only_the_shared_helper_tells_the_heater_watchdog_a_print_started():
    """One owner for the ownership rule.

    Every tool that started a print used to call this itself, and each
    call was a separate chance to tell the default printer's watchdog
    about a machine it does not watch.  A new start-side tool should get
    the guard by calling ``_note_print_started``, not by remembering that
    the guard exists.
    """
    # One exemption, named rather than silent: the PRINT_STARTED event-bus
    # handler gets an Event, not an adapter, so it has no machine to check.
    # Nothing publishes that event today, so the handler never runs; the
    # comment at the subscription says what wiring a publisher would owe.
    exempt = {"_get_event_bus"}
    offenders: list[str] = []
    for path in _MODULES:
        for fn in _functions(path):
            if fn.name == "_note_print_started" or fn.name in exempt:
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and _call_name(node) == "notify_print_started":
                    offenders.append(f"{path.name}::{fn.name}:{node.lineno}")
    assert offenders == [], (
        "notify_print_started must be reached through _note_print_started, "
        f"which guards it by machine.  Direct callers: {offenders}"
    )


def test_the_shared_helper_only_speaks_for_its_own_machine(monkeypatch):
    garage, workshop = _two_printers(monkeypatch)
    notified: list[str] = []

    class _Watchdog:
        @staticmethod
        def notify_print_started() -> None:
            notified.append("started")

    monkeypatch.setattr(server, "_get_heater_watchdog", lambda: _Watchdog)

    server._note_print_started(workshop)
    assert notified == []

    server._note_print_started(garage)
    assert notified == ["started"]


def test_the_shared_helper_never_fails_a_start(monkeypatch):
    """Bookkeeping must not be able to abort a print that already began."""
    garage, _ = _two_printers(monkeypatch)

    def _boom():
        raise RuntimeError("watchdog exploded")

    monkeypatch.setattr(server, "_get_heater_watchdog", _boom)

    server._note_print_started(garage)  # must not raise


# ---------------------------------------------------------------------------
# A tool that knows the machine asks that machine whether it is ready
# ---------------------------------------------------------------------------


def test_a_tool_that_can_be_aimed_aims_its_preflight():
    """The pre-flight verdict is about one printer.

    A tool holding a ``printer_name`` that calls ``preflight_check()``
    bare is asking the default printer whether some other machine is
    ready to print — and then starting the print on the strength of that
    answer.
    """
    offenders: list[str] = []
    for path in _MODULES:
        for fn in _functions(path):
            if "printer_name" not in _params(fn):
                continue
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call) and _call_name(node) == "preflight_check"):
                    continue
                passed = {kw.arg for kw in node.keywords}
                if "printer_name" not in passed:
                    offenders.append(f"{path.name}::{fn.name}:{node.lineno}")
    assert offenders == [], (
        "these tools know which printer they were aimed at but ask the "
        f"default one whether it is ready: {offenders}"
    )


# ---------------------------------------------------------------------------
# The door that could not be aimed at all
# ---------------------------------------------------------------------------


def test_print_plate_object_aims_both_of_its_steps(monkeypatch):
    """Upload and start have to land on the same machine.

    It reaches the printer through the aimed verbs, so threading the name
    is all it needs — but threading it to only one of the two would put
    the file on one printer and start it on another.
    """
    import inspect

    fn = getattr(server.print_plate_object, "fn", server.print_plate_object)
    assert inspect.signature(fn).parameters["printer_name"].default is None

    seen: dict[str, str | None] = {}
    monkeypatch.setattr(
        server,
        "upload_file",
        lambda path, printer_name=None: (
            seen.__setitem__("upload", printer_name)
            or {"success": True, "file_name": "part.gcode"}
        ),
    )
    monkeypatch.setattr(
        server,
        "start_print",
        lambda **kw: (
            seen.__setitem__("start", kw.get("printer_name"))
            or {"success": True, "print_start": "started"}
        ),
    )
    monkeypatch.setattr(server, "_check_auth", lambda scope: None)
    monkeypatch.setattr(
        server,
        "_safe_filename",
        lambda name: "obj",
    )

    def _extract(file_path, object_name, output_path=None, plate_number=1):
        pathlib.Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(output_path).write_text("G28\n")
        return {
            "output_path": output_path,
            "matched_object": {"name": object_name},
            "all_objects": [{"name": object_name}],
            "skipped_lines": 0,
            "estimated_time_minutes": 1,
        }

    import kiln.generation.validation as _val

    monkeypatch.setattr(_val, "extract_plate_object_gcode", _extract, raising=False)

    server.print_plate_object(
        file_path="/tmp/plate.3mf", object_name="widget", printer_name="workshop",
    )

    assert seen.get("upload") == "workshop"
    assert seen.get("start") == "workshop"


# ---------------------------------------------------------------------------
# A print ending on one machine is not news about another
# ---------------------------------------------------------------------------


def _ended_event(printer_name: str | None):
    """A PRINT_FAILED event shaped like the one kiln.print_recovery sends.

    With no name it carries neither ``data["printer_name"]`` nor a
    ``"<kind>:<name>"`` source — the "names nobody" case, which has to keep
    meaning the default printer.
    """
    from kiln.events import Event, EventType

    if printer_name is None:
        return Event(type=EventType.PRINT_FAILED)
    return Event(
        type=EventType.PRINT_FAILED,
        data={"printer_name": printer_name},
        source=f"recovery:{printer_name}",
    )


def test_a_failure_on_one_machine_leaves_the_others_watchdog_alone(monkeypatch):
    """``kiln.print_recovery`` publishes PRINT_FAILED with a printer in it.

    The handler used to drop the event and tear down the DEFAULT printer's
    watchdog, so a failure on the second machine silently removed Layer 5
    cover from a live print on the first — and left the failed machine's
    own watchdog running.
    """
    garage, workshop = _two_printers(monkeypatch)
    stopped: list[str | None] = []
    monkeypatch.setattr(server, "_stop_print_watchdog", lambda name=None: stopped.append(name))
    monkeypatch.setattr(server, "_get_heater_watchdog", lambda: _NullWatchdog)

    server._on_print_ended_event(_ended_event("workshop"))

    assert stopped == ["workshop"]


def test_a_failure_elsewhere_does_not_start_the_defaults_cooldown(monkeypatch):
    """The dangerous direction: the heater watchdog's idle tick sets hotend
    and bed to 0.  Told a print ended when another machine's did, it would
    start that timer against a default printer still printing."""
    garage, workshop = _two_printers(monkeypatch)
    ended: list[str] = []

    class _Watchdog:
        @staticmethod
        def notify_print_ended() -> None:
            ended.append("ended")

    monkeypatch.setattr(server, "_stop_print_watchdog", lambda name=None: None)
    monkeypatch.setattr(server, "_get_heater_watchdog", lambda: _Watchdog)

    server._on_print_ended_event(_ended_event("workshop"))
    assert ended == []

    # The default printer's own ending is still its news.
    server._on_print_ended_event(_ended_event("garage"))
    assert ended == ["ended"]


def test_an_event_naming_nobody_keeps_the_old_behaviour(monkeypatch):
    """An event with no printer in it is what every publisher sent before
    this handler could read one; it must still mean the default printer."""
    garage, workshop = _two_printers(monkeypatch)
    ended: list[str] = []

    class _Watchdog:
        @staticmethod
        def notify_print_ended() -> None:
            ended.append("ended")

    monkeypatch.setattr(server, "_stop_print_watchdog", lambda name=None: None)
    monkeypatch.setattr(server, "_get_heater_watchdog", lambda: _Watchdog)

    server._on_print_ended_event(_ended_event(None))

    assert ended == ["ended"]


def test_an_unknown_machine_is_skipped_not_guessed(monkeypatch):
    """Skipping costs a late cooldown; guessing costs a cold bed mid-print."""
    garage, workshop = _two_printers(monkeypatch)
    ended: list[str] = []

    class _Watchdog:
        @staticmethod
        def notify_print_ended() -> None:
            ended.append("ended")

    monkeypatch.setattr(server, "_stop_print_watchdog", lambda name=None: None)
    monkeypatch.setattr(server, "_get_heater_watchdog", lambda: _Watchdog)

    server._on_print_ended_event(_ended_event("a-printer-that-never-existed"))

    assert ended == []


def test_the_printer_name_is_read_from_either_place():
    from kiln.events import Event, EventType

    named = Event(type=EventType.PRINT_FAILED, data={"printer_name": "workshop"})
    assert server._event_printer_name(named) == "workshop"
    # Publishers repeat it in source; fall back to that.
    sourced = Event(type=EventType.PRINT_FAILED, source="recovery:garage")
    assert server._event_printer_name(sourced) == "garage"
    # Nothing to read is "unknown", never "the default".
    assert server._event_printer_name(Event(type=EventType.PRINT_FAILED)) is None


class _NullWatchdog:
    @staticmethod
    def notify_print_ended() -> None:
        pass

    @staticmethod
    def notify_print_started() -> None:
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakePrinter:
    name = "fake"

    def __init__(self, host: str) -> None:
        self.host = host
        self.serial = ""
        self.started: list[str] = []

    def get_state(self) -> PrinterState:
        return PrinterState(state=PrinterStatus.IDLE, connected=True)

    def upload_file(self, file_path: str) -> UploadResult:
        return UploadResult(success=True, file_name="part.gcode", message="ok")

    def start_print(self, file_name: str, **kwargs) -> PrintResult:
        self.started.append(file_name)
        return PrintResult(success=True, message="started")


def _two_printers(monkeypatch) -> tuple[_FakePrinter, _FakePrinter]:
    garage = _FakePrinter("192.0.2.10")
    workshop = _FakePrinter("192.0.2.11")
    registry = server._get_registry()
    registry.register("garage", garage)
    registry.register("workshop", workshop)
    monkeypatch.setattr(server, "_get_adapter", lambda: garage)
    return garage, workshop


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    monkeypatch.setattr(server, "_registry", PrinterRegistry())
    monkeypatch.setattr(server, "_tool_limiter", server._ToolRateLimiter())
    monkeypatch.setattr(server, "_TOOL_RATE_LIMITS", {})


# ---------------------------------------------------------------------------
# The consent rule covers every command that starts a print
# ---------------------------------------------------------------------------


def test_every_command_that_starts_a_print_can_carry_consent():
    """A rule wired into one of six commands is not a rule.

    ``start_print`` asked for a preview token and the other five did not, so
    an agent that did not want to render a preview only had to call a
    different command.  Each of these now takes the token.
    """
    import inspect

    from kiln.plugins import monitoring_tools, slicer_tools, smart_print_tools

    for mod, tool in (
        (None, "start_print"),
        (slicer_tools, "slice_and_print"),
        (monitoring_tools, "start_monitored_print"),
        (smart_print_tools, "retry_print_with_fix"),
    ):
        src = inspect.getsource(mod) if mod is not None else inspect.getsource(server)
        assert f"def {tool}(" in src
        fn_src = src[src.index(f"def {tool}(") :]
        sig = fn_src[: fn_src.index(") -> dict:")]
        assert "preview_token" in sig, f"{tool} cannot carry a preview token"


def test_one_helper_owns_the_consent_rule():
    """Six copies of a consent check is five chances to write it differently."""
    # Exempt, named rather than silent: fulfillment confirms an order placed
    # with an external manufacturer, not a print on a machine in the room.
    # Different consent (money, a vendor), different semantics — it validates
    # against an explicit rendered file and controls token consumption itself.
    exempt = {"_preview_gate_error", "_validate_preview_confirmation"}
    offenders: list[str] = []
    for path in _MODULES:
        for fn in _functions(path):
            if fn.name in exempt:
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and _call_name(node) == "validate":
                    seg = ast.get_source_segment(path.read_text(), node) or ""
                    if "preview" in seg.lower():
                        offenders.append(f"{path.name}::{fn.name}:{node.lineno}")
    assert offenders == [], f"preview gate validated outside the helper: {offenders}"


# ---------------------------------------------------------------------------
# A retry is gated on whether the object changed, not on whether it retried
# ---------------------------------------------------------------------------


def test_a_reprint_of_the_same_object_is_not_re_asked():
    """Hotter nozzle, same shape: this is the print already approved."""
    assert server._retry_changes_the_object({"temperature": "215"}, False) is False
    assert server._retry_changes_the_object({}, False) is False
    assert server._retry_changes_the_object(None, False) is False


def test_a_repaired_mesh_is_a_different_object():
    assert server._retry_changes_the_object({"temperature": "215"}, True) is True


def test_an_override_we_do_not_recognise_counts_as_a_change():
    """The allowlist is deliberately the safe way round.

    Naming the geometry-changing keys means an incomplete list prints an
    unapproved shape.  Naming the harmless ones means an incomplete list
    asks one extra question.
    """
    assert server._retry_changes_the_object({"support_material": "1"}, False) is True
    assert server._retry_changes_the_object({"a_key_from_2027": "x"}, False) is True


def test_the_allowlist_holds_only_appearance_neutral_settings():
    """Guard against someone adding a geometry key to the safe list."""
    banned = ("support", "raft", "brim", "orient", "rotate", "scale", "layer_height")
    for key in server._APPEARANCE_NEUTRAL_OVERRIDES:
        assert not any(b in key for b in banned), f"{key} changes the object"
