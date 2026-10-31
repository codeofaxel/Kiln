"""A printer door that runs into the one-printer rule answers with the rule.

Below the fleet tier Kiln works with one printer at a time, and a command
aimed at a second machine is refused with words written for a person: what
Kiln is doing, that it is not watching the other machine, that machine's own
controls, how to move Kiln over, the tier once.  The printer tools caught
that refusal in a broad ``except`` and re-worded it -- "Failed to get printer
status: ... Check that the printer is online and KILN_PRINTER_HOST is
correct." -- and three went further: the status door called the machine
offline and said to check its power, the print-health door graded the print
"critical", and bed leveling recorded a failed leveling for a command that
was never sent.

The fix lives in the envelope every door already builds its failure with
(``server._error_dict``, and ``cli.output.format_error`` on the command
line): built while the refusal is the exception being handled, it answers
with the refusal's own words and code.  A door whose answer is not that
envelope asks ``refusal_in_flight`` first.  Pinned here:

* every public tool that can reach a printer adapter is found by reading the
  code, not from a list -- a tool added tomorrow is in the roster;
* each one is READ: an ``except`` that can catch the refusal and answers
  either answers through the envelope or asks first, so a door the walk
  cannot drive is still held to the rule;
* each one that can be called with plain arguments is CALLED against a
  machine Kiln is not working with; a refused answer is the refusal, word for
  word, and still a failure, and a door that answers as if nothing was
  refused must be a recorded optional read;
* any other failure keeps today's words, byte for byte.
"""

from __future__ import annotations

import ast
import inspect
import threading
from functools import cache
from pathlib import Path
from typing import Any

import pytest

import kiln
import kiln.server as srv
from kiln import hardware_stops as hs
from kiln.cli.output import format_error
from kiln.printers import engagement
from kiln.printers.base import (
    PrinterCapabilities,
    PrinterEngagementError,
    PrinterError,
    PrinterStatus,
    PrintResult,
    UploadResult,
)
from kiln.registry import PrinterRegistry
from tests.test_a_machine_at_a_stop_is_not_printing import _Machine
from tests.test_hardware_stops import _job_file

_PACKAGE = Path(kiln.__file__).resolve().parent
_SERVER = _PACKAGE / "server.py"
_PLUGINS = _PACKAGE / "plugins"

# The connection advice a refusal used to arrive wrapped in, in every
# spelling the doors used.
_WRONG_ADVICE = ("online", "KILN_PRINTER_HOST", "credentials are correct")


# ---------------------------------------------------------------------------
# The bench: voron is the machine Kiln is working with, ender is not.
# ---------------------------------------------------------------------------


class _Printer(_Machine):
    """A whole printer, so every command a door can send is a real, gated one.

    ``_Machine`` answers status only; a door that pauses, heats or sends
    G-code would otherwise reach an unimplemented method and fail for a
    reason that is not the rule.
    """

    def pause_print(self) -> PrintResult:
        return PrintResult(success=True, message="Paused.")

    def _resume_print_impl(self) -> PrintResult:
        return PrintResult(success=True, message="Resumed.")

    def cancel_print(self) -> PrintResult:
        return PrintResult(success=True, message="Cancelled.")

    def emergency_stop(self) -> PrintResult:
        return PrintResult(success=True, message="Stopped.")

    def send_gcode(self, commands: list[str]) -> bool:
        return True

    def set_tool_temp(self, target: float) -> bool:
        return True

    def set_bed_temp(self, target: float) -> bool:
        return True

    def upload_file(self, file_path: str) -> UploadResult:
        return UploadResult(success=True, file_name=Path(file_path).name, message="Uploaded.")

    def list_files(self) -> list:
        return []

    def delete_file(self, file_path: str) -> bool:
        return True

    def _load_filament_impl(self, **kwargs: Any) -> PrintResult:
        return PrintResult(success=True, message="Loaded.")

    def _unload_filament_impl(self, **kwargs: Any) -> PrintResult:
        return PrintResult(success=True, message="Unloaded.")

    def _purge_filament_impl(self, **kwargs: Any) -> PrintResult:
        return PrintResult(success=True, message="Purged.")


_Printer.__abstractmethods__ = frozenset()


@pytest.fixture
def bench(monkeypatch, tmp_path):
    """voron printing (or paused at its planned stop) and engaged; ender idle."""

    def _build(*, plan: bool = False, engaged: bool = True):
        voron = _Printer("AAA111", PrinterStatus.PAUSED if plan else PrinterStatus.PRINTING, layer=29)
        ender = _Printer("BBB222", file=None)
        voron._capabilities = ender._capabilities = PrinterCapabilities(
            can_upload=True,
            can_set_temp=True,
            can_send_gcode=True,
            can_pause=True,
            can_handle_filament=True,
        )
        if plan:
            hs.note_print_started(voron, _job_file(tmp_path))
        registry = PrinterRegistry()
        registry.register("voron", voron)
        registry.register("ender", ender)
        monkeypatch.setattr("kiln.registry.get_registry", lambda: registry)
        monkeypatch.setattr(srv, "_get_registry", lambda: registry)
        # A door that takes no printer name aims at the default printer.
        monkeypatch.setattr(srv, "_adapter", ender)
        monkeypatch.setattr(engagement, "_multi_machine_tier", lambda: False)
        monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setattr(srv, "_tool_limiter", srv._ToolRateLimiter())
        if engaged:
            engagement.engage(voron, voron.get_job(), reason="started", label="voron")
        return voron, ender

    return _build


@pytest.fixture
def refusals(monkeypatch):
    """Every refusal the gate hands out during the test, in order: a look's
    (``check_look``) and an action's (``check_command``)."""
    seen: list[dict[str, Any]] = []
    for name in ("check_command", "check_look"):
        real = getattr(engagement, name)

        def _recording(adapter, action, _real=real):
            verdict = _real(adapter, action)
            if verdict is not None:
                seen.append(verdict)
            return verdict

        monkeypatch.setattr(engagement, name, _recording)
    return seen


def _tool(fn):
    return getattr(fn, "fn", fn)


def _assert_is_the_refusal(answer: dict[str, Any], fired: list[dict[str, Any]], door: str) -> None:
    """The answer is the refusal: its words whole, its code, its verdict."""
    assert answer.get("success") is False, f"{door}: a refused command answered {answer!r}"
    error = answer.get("error")
    assert isinstance(error, dict), f"{door}: a refused command answered {answer!r}"
    message = str(error.get("message") or "")
    assert message in {engagement.refusal_words(v) for v in fired}, f"{door}: {message}"
    for advice in _WRONG_ADVICE:
        assert advice not in message, f"{door}: still says {advice!r}: {message}"
    # The start door reports the pre-flight that refused, with the refusal
    # carried inside it; every other door answers with the refusal's code.
    verdict = answer.get("refusal") or (answer.get("preflight") or {}).get("refusal") or {}
    assert verdict.get("code") == "TIER_SINGLE_PRINTER_LIMIT", door
    expected = "PREFLIGHT_FAILED" if "preflight" in answer else "TIER_SINGLE_PRINTER_LIMIT"
    assert error.get("code") == expected, f"{door}: code {error.get('code')!r}"


# ---------------------------------------------------------------------------
# The roster: every public tool whose code can reach a printer adapter.
# ---------------------------------------------------------------------------


@cache
def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text())


def _functions(path: Path) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {node.name: node for node in _module(path).body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


@cache
def _adapter_sources() -> frozenset[str]:
    """What hands a door a printer, read from the server rather than listed.

    Every server helper declared to return an adapter, the registry that
    holds them, and every command the one-printer gate sits on.
    """
    returns_adapter = {
        name
        for name, node in _functions(_SERVER).items()
        if node.returns is not None and "PrinterAdapter" in ast.unparse(node.returns)
    }
    return frozenset(returns_adapter | {"get_registry", "_get_registry"} | set(engagement.GATED_ACTIONS))


def _called(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _reaches(nodes: list[ast.AST], path: Path, depth: int = 3, seen: frozenset[str] = frozenset()) -> bool:
    """Whether *nodes* can obtain or command a printer, through helpers too."""
    sources = _adapter_sources()
    own, server = _functions(path), _functions(_SERVER)
    for root in nodes:
        for node in ast.walk(root):
            if not isinstance(node, ast.Call):
                continue
            name = _called(node)
            if name in sources:
                return True
            if depth and name not in seen:
                helper, where = (own.get(name), path) if name in own else (server.get(name), _SERVER)
                if helper is not None and _reaches([helper], where, depth - 1, seen | {name}):
                    return True
    return False


def _tool_node(fn, path: Path) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    first = fn.__code__.co_firstlineno
    for node in ast.walk(_module(path)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == fn.__name__:
            start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            if start <= first <= (node.end_lineno or node.lineno):
                return node
    return None


@cache
def _roster() -> tuple[tuple[str, Any, Path, Any], ...]:
    srv._ensure_internal_tool_plugins_registered()
    rows = []
    for name, tool in sorted(srv.mcp._tool_manager._tools.items()):
        fn = inspect.unwrap(tool.fn)
        source = Path(inspect.getsourcefile(fn) or "").resolve()
        if source != _SERVER and source.parent != _PLUGINS:
            continue  # kiln-pro's own tools, when it is installed
        node = _tool_node(fn, source)
        if node is not None and _reaches([node], source):
            rows.append((name, fn, source, node))
    return tuple(rows)


_CATCHES_THE_REFUSAL = {"Exception", "BaseException", "PrinterError", "PrinterEngagementError"}


def _can_catch_the_refusal(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    kinds = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(ast.unparse(kind).rsplit(".", 1)[-1] in _CATCHES_THE_REFUSAL for kind in kinds)


def _answers_outside_the_envelope(name: str, node: ast.AST, source: Path) -> list[str]:
    """``except`` blocks that can catch a refusal from a printer and answer by hand.

    An answer built by ``_error_dict`` inside the block carries the refusal
    by itself.  Any other answer -- a softer success, a dict written out --
    has to ask ``refusal_in_flight`` before it is reached, or a refusal
    comes back dressed as whatever that answer says.
    """
    findings = []
    for tried in ast.walk(node):
        if not isinstance(tried, ast.Try) or not _reaches(list(tried.body), source):
            continue
        for handler in tried.handlers:
            if not _can_catch_the_refusal(handler):
                continue
            asked = [
                n.lineno
                for n in ast.walk(handler)
                if isinstance(n, ast.Call) and _called(n).endswith("refusal_in_flight")
            ]
            for answer in ast.walk(handler):
                if not isinstance(answer, ast.Return) or answer.value is None:
                    continue
                if isinstance(answer.value, ast.Call) and _called(answer.value) == "_error_dict":
                    continue
                if not any(line < answer.lineno for line in asked):
                    findings.append(f"{name} ({source.name}:{answer.lineno})")
    return findings


# What a door needs besides a printer name, so the walk can drive it.  A
# door asking for anything else is held to the reading instead.
_PLAIN_ARGUMENTS: dict[str, Any] = {
    "printer_name": "ender",
    "file_name": "gasket.gcode",
    "filename": "gasket.gcode",
    "commands": "M105",
    "tool_temp": 200,
    "percent": 50,
    "mode": "on",
    "object_ids": [1],
    "material": "PLA",
    "expected_material": "PLA",
    "timeout": 1,
    "timeout_seconds": 1,
    "poll_interval": 0.05,
    "max_wait_seconds": 1,
}

# Doors where a printer read is one optional input, not the subject: a
# refusal there is dropped and the answer says nothing about that printer.
# Each one is checked to still be a door, so the list cannot outlive it.
_OPTIONAL_READS = {
    "diagnose_print_failure_live": (
        "the live state is one optional input beside the model and the "
        "material; the diagnosis makes no claim about the printer it could "
        "not read"
    ),
}


def _arguments(fn, tmp_path: Path) -> dict[str, Any] | None:
    gcode = tmp_path / "gasket.gcode"
    gcode.write_text("G28\nG1 Z0.2 F600\nG1 X10 Y10 E1\n")
    plain = {**_PLAIN_ARGUMENTS, "file_path": str(gcode)}
    kwargs: dict[str, Any] = {}
    for param in inspect.signature(fn).parameters.values():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if param.name in plain:
            kwargs[param.name] = plain[param.name]
        elif param.default is inspect.Parameter.empty:
            return None
    return kwargs


def _call_bounded(fn, kwargs: dict[str, Any], seconds: float = 20.0) -> tuple[bool, Any]:
    """``(finished, answer)``: a door that loops is judged by reading instead."""
    box: list[Any] = []

    def _run() -> None:
        try:
            box.append(fn(**kwargs))
        except BaseException as exc:  # noqa: BLE001 -- the escape is the finding
            box.append(exc)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(seconds)
    return (not worker.is_alive(), box[0] if box else None)


def _drivable(name: str) -> bool:
    # An emergency stop keeps its own words on purpose (it is the one
    # refusal that leads with the printer's own controls), and its record
    # lives in a coordinator that outlives a test.  It is read, not driven.
    return srv._get_safety_level(name) != "emergency"


_ROSTER_NAMES = [row[0] for row in _roster()]


class TestTheRoster:
    # Doors that once answered a refusal with "check that the printer is
    # online", "offline -- check it is powered on", "health: critical" or
    # "bed leveling failed".  Named here only to prove the walk below is not
    # vacuous: the roster that finds them is read from the code.
    DRIVEN = (
        "printer_status",
        "start_print",
        "preflight_check",
        "set_temperature",
        "pause_print",
        "resume_print",
        "cancel_print",
        "send_gcode",
        "await_print_completion",
        "check_ambient_conditions",
        "check_print_health",
        "trigger_bed_level",
        "load_filament",
        "home_axes",
        "watch_print",
    )

    @pytest.mark.parametrize("door", DRIVEN)
    def test_the_walk_reaches_the_gate_through_the_doors_a_person_meets(
        self,
        door,
        bench,
        refusals,
        tmp_path,
    ):
        assert door in _ROSTER_NAMES, door
        _, fn, _, _ = next(row for row in _roster() if row[0] == door)
        kwargs = _arguments(fn, tmp_path)
        assert kwargs is not None, door
        bench()
        finished, answer = _call_bounded(fn, kwargs)
        assert finished and refusals, door
        _assert_is_the_refusal(answer, list(refusals), door)

    def test_the_optional_reads_are_still_doors(self):
        assert set(_OPTIONAL_READS) <= set(_ROSTER_NAMES)


@pytest.mark.parametrize("door", _ROSTER_NAMES)
def test_the_door_answers_a_refusal_with_it(door, bench, refusals, tmp_path):
    """Driven when the walk can reach ender's gate; read either way."""
    _, fn, source, node = next(row for row in _roster() if row[0] == door)
    findings = _answers_outside_the_envelope(door, node, source)
    assert not findings, (
        "This door catches a printer failure and builds its own answer, so a "
        "one-printer refusal would reach the person in the door's words: " + ", ".join(findings)
    )
    kwargs = _arguments(fn, tmp_path)
    if kwargs is None or not _drivable(door):
        return
    bench()
    finished, answer = _call_bounded(fn, kwargs)
    if not finished or not refusals:
        return  # never reached ender's gate with plain arguments
    assert not isinstance(answer, BaseException), f"{door} let the refusal escape: {answer!r}"
    assert isinstance(answer, dict), f"{door}: {answer!r}"
    if answer.get("success") is not False and "error" not in answer:
        assert door in _OPTIONAL_READS, f"{door} answered as if nothing was refused: {str(answer)[:300]}"
        return
    _assert_is_the_refusal(answer, list(refusals), door)


# ---------------------------------------------------------------------------
# The words, through the doors a person meets most.
# ---------------------------------------------------------------------------


class TestTheDoorsSayIt:
    @pytest.mark.parametrize(
        "door, kwargs",
        [
            ("printer_status", {}),
            ("set_temperature", {"tool_temp": 200}),
            ("send_gcode", {"commands": "M105"}),
            ("pause_print", {}),
            ("cancel_print", {}),
        ],
    )
    def test_the_refusal_reaches_the_person_whole(self, bench, refusals, door, kwargs):
        bench()
        fn = _tool(getattr(srv, door))
        if "printer_name" in inspect.signature(fn).parameters:
            kwargs = {**kwargs, "printer_name": "ender"}
        answer = fn(**kwargs)
        assert answer["success"] is False
        _assert_is_the_refusal(answer, refusals, door)
        assert "hand_back_printer" in answer["error"]["message"]
        assert answer["error"]["message"].lower().count("business") == 1

    def test_the_status_door_says_the_machine_is_waiting_at_its_stop(self, bench, refusals):
        bench(plan=True)
        answer = _tool(srv.printer_status)(printer_name="ender")
        message = answer["error"]["message"]
        assert message.startswith("At a planned hardware stop, voron has paused, still hot")
        _assert_is_the_refusal(answer, refusals, "printer_status")

    def test_the_engaged_machine_is_untouched(self, bench, refusals):
        bench()
        answer = _tool(srv.printer_status)(printer_name="voron")
        assert answer["success"] is True
        assert refusals == []


class TestEverythingElseKeepsTodaysWords:
    def test_a_printer_fault_on_the_status_door(self, bench, monkeypatch):
        _, ender = bench(engaged=False)

        def _boom():
            raise PrinterError("socket closed")

        monkeypatch.setattr(ender, "get_state", _boom)
        answer = _tool(srv.printer_status)(printer_name="ender")
        # A printer that does not answer is still a state, not a failure.
        assert answer["success"] is True
        assert answer["printer"]["state"] == "offline"
        assert answer["offline_reason"] == "socket closed"
        assert "refusal" not in answer and "error" not in answer

    def test_a_tool_fault_on_the_temperature_door(self, bench, monkeypatch):
        _, ender = bench(engaged=False)

        def _boom(target):
            raise PrinterError("socket closed")

        monkeypatch.setattr(ender, "set_tool_temp", _boom)
        answer = _tool(srv.set_temperature)(tool_temp=200, printer_name="ender")
        assert answer["error"] == {
            "code": "ERROR",
            "message": (
                "Failed to set temperature: socket closed. "
                "Check that the printer is online and KILN_PRINTER_HOST is correct."
            ),
            "retryable": True,
        }
        assert "refusal" not in answer

    def test_the_envelope_outside_an_except_is_unchanged(self):
        assert srv._error_dict("x", code="VALIDATION_ERROR") == {
            "success": False,
            "error": {"code": "VALIDATION_ERROR", "message": "x", "retryable": False},
        }

    def test_a_different_fault_raised_while_handling_a_refusal_is_that_fault(self):
        verdict = {"code": "TIER_SINGLE_PRINTER_LIMIT", "reason": "Kiln is working with voron."}
        try:
            try:
                raise PrinterEngagementError(verdict)
            except PrinterEngagementError:
                raise PrinterError("socket closed")  # noqa: B904 -- implicit context, on purpose
        except PrinterError:
            answer = srv._error_dict("Failed: socket closed", code="PRINTER_ERROR")
        assert answer["error"]["message"] == "Failed: socket closed"
        assert answer["error"]["code"] == "PRINTER_ERROR"

    def test_a_refusal_an_adapter_rewrapped_is_still_the_refusal(self):
        verdict = {"code": "TIER_SINGLE_PRINTER_LIMIT", "reason": "Kiln is working with voron."}
        try:
            try:
                raise PrinterEngagementError(verdict)
            except PrinterEngagementError as inner:
                raise PrinterError("fan command failed", cause=inner)  # noqa: B904
        except PrinterError:
            answer = srv._error_dict("Failed to set fan: fan command failed")
        assert answer["error"]["message"] == "Kiln is working with voron."
        assert answer["refusal"] is verdict


class TestTheCommandLine:
    @pytest.mark.parametrize("argv", [["status", "--json"], ["pause", "--json"], ["temp", "--tool", "200", "--json"]])
    def test_a_command_aimed_at_the_other_machine_prints_the_refusal(self, bench, refusals, monkeypatch, argv):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        _, ender = bench()
        monkeypatch.setattr("kiln.cli.main._get_adapter_from_ctx", lambda ctx: ender)
        out = CliRunner().invoke(cli, argv)
        assert refusals, out.output
        assert '"TIER_SINGLE_PRINTER_LIMIT"' in out.output, out.output
        assert "hand_back_printer" in out.output
        for advice in _WRONG_ADVICE:
            assert advice not in out.output, out.output

    def test_the_refusal_prints_in_its_own_words(self):
        verdict = {
            "code": "TIER_SINGLE_PRINTER_LIMIT",
            "reason": "Kiln is working with voron right now.",
            "suggestions": ["To move Kiln over, hand voron back with hand_back_printer."],
        }
        try:
            raise PrinterEngagementError(verdict)
        except PrinterError as exc:
            text = format_error(
                f"Failed to get printer status: {exc}. Verify the printer is online and credentials are correct.",
                json_mode=True,
            )
        assert '"TIER_SINGLE_PRINTER_LIMIT"' in text
        assert "hand_back_printer" in text
        assert "online" not in text and "credentials" not in text

    def test_any_other_failure_prints_as_today(self):
        try:
            raise PrinterError("socket closed")
        except PrinterError as exc:
            text = format_error(f"Failed to get printer status: {exc}. Verify the printer is online.", json_mode=True)
        assert "Verify the printer is online." in text and '"ERROR"' in text
