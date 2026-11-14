"""A start skips the person's yes and the preview as a resume only when the
printer itself reports the paused job it continues.

``resume_from_paused`` is an argument whoever starts the print sets, and a
``*_resume_*.3mf`` name is whatever the file's maker chose.  Each once let a
new print skip the approval dialog and the preview on an idle printer.  These
tests drive every gate that grants a resume through its real function, on an
idle printer and on a paused one, and a structural scan finds every early
return on a resume claim in the approval and preview gates and requires that
the gate asks the printer.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiln import print_signoff, server
from kiln.printers.base import PrinterState, PrinterStatus

RESUME_FILE = "transformed_resume_ab12.3mf"


class _Machine:
    def __init__(self, status: PrinterStatus) -> None:
        self.status = status

    def get_state(self) -> PrinterState:
        return PrinterState(state=self.status, connected=True)


@pytest.fixture()
def printer(monkeypatch):
    machine = _Machine(PrinterStatus.IDLE)
    monkeypatch.setattr(server, "_resolve_adapter", lambda *_a, **_k: machine)
    monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
    print_signoff.clear()
    yield machine
    print_signoff.clear()


CLAIMS = [
    pytest.param(RESUME_FILE, {}, id="resume-file-name"),
    pytest.param("benchy.3mf", {"resume_from_paused": True}, id="resume-flag"),
]


# ---------------------------------------------------------------------------
# The approval dialog
# ---------------------------------------------------------------------------


def _dialog(monkeypatch, file_name: str, extra: dict) -> list[str]:
    asked: list[str] = []
    monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: True)

    async def _ask(ctx, message, **_kw):
        asked.append(message)
        from kiln.print_consent import DialogAnswer

        return DialogAnswer("decline", "")

    monkeypatch.setattr(server, "ask_user_to_confirm", _ask)

    async def _run():
        return await server._obtain_print_consent(
            "start_print", {"file_name": file_name, "printer_name": "garage", **extra}, SimpleNamespace(),
        )

    with contextlib.suppress(RuntimeError):  # the person said no: the print never starts
        asyncio.run(_run())
    return asked


@pytest.mark.parametrize(("file_name", "extra"), CLAIMS)
def test_a_claimed_resume_on_an_idle_printer_is_asked_about(printer, monkeypatch, file_name, extra):
    assert len(_dialog(monkeypatch, file_name, extra)) == 1


@pytest.mark.parametrize(("file_name", "extra"), CLAIMS)
def test_a_resume_the_printer_backs_is_not_asked_again(printer, monkeypatch, file_name, extra):
    printer.status = PrinterStatus.PAUSED
    assert _dialog(monkeypatch, file_name, extra) == []


# ---------------------------------------------------------------------------
# The preview gate, its exemption, and the adapter backstop
# ---------------------------------------------------------------------------


def test_the_preview_gate_refuses_a_claimed_resume_on_an_idle_printer(printer):
    block = server._preview_gate_error("start_print", RESUME_FILE, None, printer_name="garage", is_resume=True)
    assert block is not None and block["success"] is False


def test_the_preview_gate_lets_a_backed_resume_through(printer):
    printer.status = PrinterStatus.PAUSED
    assert server._preview_gate_error("start_print", RESUME_FILE, None, printer_name="garage", is_resume=True) is None


def test_the_exemption_is_granted_only_on_a_paused_printer(printer):
    assert print_signoff.exemption_verdict("start_print", RESUME_FILE, "garage", is_resume=True) is None
    printer.status = PrinterStatus.PAUSED
    verdict = print_signoff.exemption_verdict("start_print", RESUME_FILE, "garage", is_resume=True)
    assert verdict is not None and verdict.source == print_signoff.SOURCE_RESUME


@pytest.mark.parametrize(("file_name", "extra"), CLAIMS)
def test_the_adapter_backstop_asks_the_printer(printer, file_name, extra):
    printer._kiln_signoff_required = True  # the mark a registry sets
    assert print_signoff.adapter_verdict(printer, file_name, extra) is not None
    printer.status = PrinterStatus.PAUSED
    assert print_signoff.adapter_verdict(printer, file_name, extra) is None


@pytest.mark.parametrize(("file_name", "extra"), CLAIMS)
def test_the_pre_print_safety_gate_judges_a_claimed_resume_on_an_idle_printer(printer, monkeypatch, file_name, extra):
    """The gate every adapter start meets skips its checks only for a resume
    the printer backs; a claim alone is judged like any new print."""
    from kiln.printers import print_gate

    judged = {"reason": "judged as a new print"}
    monkeypatch.setattr(print_gate, "_concurrent_fleet_verdict", lambda _adapter: judged)
    assert print_gate.run_adapter_gate(printer, file_name, dict(extra)) is judged
    printer.status = PrinterStatus.PAUSED
    assert print_gate.run_adapter_gate(printer, file_name, dict(extra)) is None


@pytest.mark.parametrize("status", [PrinterStatus.IDLE, PrinterStatus.PRINTING, PrinterStatus.OFFLINE])
def test_anything_but_a_paused_printer_is_not_a_resume(printer, status):
    printer.status = status
    assert print_signoff.verified_resume(RESUME_FILE, {"resume_from_paused": True}, printer_name="garage") is False


def test_an_unreadable_printer_is_not_a_resume(monkeypatch):
    def _gone(*_a, **_k):
        raise RuntimeError("no such printer")

    monkeypatch.setattr(server, "_resolve_adapter", _gone)
    assert print_signoff.printer_reports_paused("garage") is False


# ---------------------------------------------------------------------------
# The pre-flight
# ---------------------------------------------------------------------------


def test_the_pre_flight_wants_a_paused_printer_for_a_resume():
    from unittest.mock import MagicMock, patch

    state = MagicMock()
    state.connected = True
    state.state = PrinterStatus.IDLE
    state.tool_temp_actual = state.bed_temp_actual = 25.0
    state.tool_temp_target = state.bed_temp_target = 0.0
    with (
        patch("kiln.server._get_adapter") as adapter,
        patch("kiln.server._get_temp_limits", return_value=(280.0, 120.0)),
        patch("kiln.server.get_db") as db,
        patch("kiln.server._registry") as registry,
    ):
        adapter.return_value.get_state.return_value = state
        registry.count = 1
        registry.list_names.return_value = ["garage"]
        db.return_value.get_printer_learning_insights.return_value = {"total_outcomes": 0}

        def idle_check(**kw):
            return next(c for c in server.preflight_check(**kw)["checks"] if c["name"] == "printer_idle")

        assert idle_check(accept_paused=True)["passed"] is False, "an idle printer has nothing to resume"
        assert idle_check()["passed"] is True
        state.state = PrinterStatus.PAUSED
        assert idle_check(accept_paused=True)["passed"] is True
        assert idle_check()["passed"] is False


# ---------------------------------------------------------------------------
# The class: every early return on a resume claim asks the printer
# ---------------------------------------------------------------------------

SRC = Path(server.__file__).resolve().parent

#: Calls that make a function an approval or preview gate.
GATE_CALLS = frozenset({"consent_for", "exemption_verdict", "adapter_verdict", "token_verdict"})
#: Calls that read a resume CLAIM (a flag or a name the caller chose).
CLAIM_CALLS = frozenset({"is_resume_mode_3mf", "_is_resume_mode_3mf", "claims_resume", "_is_resume"})
CLAIM_NAMES = frozenset({"resume_from_paused", "is_resume", "_is_resume", "is_resume_3mf"})
#: Calls that ask the printer.
VERIFIERS = frozenset({"verified_resume", "printer_reports_paused"})
#: The helpers that define the claim and the check themselves.
HELPERS = frozenset({"claims_resume", "verified_resume", "printer_reports_paused"})


def _called(node: ast.AST) -> set[str]:
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            out.add(func.id if isinstance(func, ast.Name) else getattr(func, "attr", ""))
    return out


def _refers_to(node: ast.AST, names: frozenset[str]) -> bool:
    """Whether *node* calls or hands on (``asyncio.to_thread(fn, ...)``) one of *names*."""
    return any(
        (isinstance(sub, ast.Name) and sub.id in names) or (isinstance(sub, ast.Attribute) and sub.attr in names)
        for sub in ast.walk(node)
    )


def _reads_a_claim(test: ast.expr) -> bool:
    for sub in ast.walk(test):
        if isinstance(sub, ast.Call) and (_called(sub) & CLAIM_CALLS):
            return True
        if isinstance(sub, ast.Name) and sub.id in CLAIM_NAMES:
            return True
        if isinstance(sub, ast.Constant) and sub.value == "resume_from_paused":
            return True
    return False


def _early_returns_on_a_claim(fn: ast.AST) -> list[int]:
    lines = []
    for sub in ast.walk(fn):
        if isinstance(sub, ast.If) and _reads_a_claim(sub.test) and any(
            isinstance(s, ast.Return) for s in ast.walk(ast.Module(body=sub.body, type_ignores=[]))
        ):
            lines.append(sub.lineno)
    return lines


def resume_gates(src: Path) -> dict[str, tuple[list[int], bool]]:
    """``file::function`` -> (lines of early returns on a resume claim,
    whether the function asks the printer), for every approval or preview
    gate under *src*."""
    found: dict[str, tuple[list[int], bool]] = {}
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        in_signoff = path.name == "print_signoff.py"
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.name in HELPERS:
                continue
            calls = _called(fn)
            if not (in_signoff or calls & GATE_CALLS):
                continue
            lines = _early_returns_on_a_claim(fn)
            if lines:
                found[f"{path.relative_to(src).as_posix()}::{fn.name}"] = (lines, _refers_to(fn, VERIFIERS))
    return found


def test_every_early_return_on_a_resume_claim_asks_the_printer():
    gates = resume_gates(SRC)
    # Not vacuous: the gates that read a resume claim are found.  (The adapter
    # backstop asks verified_resume directly, so it reads no bare claim.)
    assert {"server.py::_obtain_print_consent", "print_signoff.py::exemption_verdict"} <= set(gates)
    unverified = {name: lines for name, (lines, asks) in gates.items() if not asks}
    assert unverified == {}, (
        "a gate returns early on a resume flag or file name without asking the printer "
        "(print_signoff.verified_resume / printer_reports_paused): " + repr(unverified)
    )


def test_the_scan_catches_the_old_shape(tmp_path):
    (tmp_path / "gate.py").write_text(
        "def ask(arguments, file_value):\n"
        "    if _is_resume_mode_3mf(file_value) or arguments.get('resume_from_paused'):\n"
        "        return None\n"
        "    return consent_for(file_name=file_value)\n"
    )
    assert resume_gates(tmp_path) == {"gate.py::ask": ([2], False)}
