"""A person is asked before a print starts, and the agent cannot forge it.

Every consent Kiln had before this was the same shape: the server hands the
agent a token and trusts the agent to have shown a human something.
``issue_preview_token`` proves a preview was RENDERED for a file — never
that anyone saw it — and the agent is both the one who asks and the one who
reports the answer.  MCP elicitation moves the asking to the client, so the
agent is not holding the pen.

These tests are about the half that was missing, and about the seams where
it could quietly stop working: a consent that covers a different print, a
consent that outlives its call, a "we could not ask" that gets read as a
yes.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import pathlib

import pytest

from kiln import print_consent, server
from kiln.print_consent import (
    SOURCE_ELICITED,
    PrintConsent,
    consent_for,
    describe_print_request,
    reset_consent,
    set_consent,
)

# ---------------------------------------------------------------------------
# A consent is about one print
# ---------------------------------------------------------------------------


def _granted(file_name="benchy.3mf", printer_name="garage"):
    return PrintConsent(
        tool="start_print", file_name=file_name, printer_name=printer_name,
    )


def test_a_yes_covers_the_print_it_was_asked_about():
    token = set_consent(_granted())
    try:
        assert consent_for(file_name="benchy.3mf", printer_name="garage") is not None
    finally:
        reset_consent(token)


def test_a_yes_does_not_cover_the_next_file():
    """The whole failure this guards: 'approved' with nothing attached
    authorises whatever runs next."""
    token = set_consent(_granted())
    try:
        assert consent_for(file_name="something_else.3mf", printer_name="garage") is None
    finally:
        reset_consent(token)


def test_a_yes_does_not_cover_the_other_machine():
    token = set_consent(_granted())
    try:
        assert consent_for(file_name="benchy.3mf", printer_name="workshop") is None
    finally:
        reset_consent(token)


def test_the_same_print_is_recognised_through_a_path_or_a_case_change():
    """A tool is handed /tmp/Benchy.3MF and the printer reports benchy.3mf.

    Same print.  A consent that refused the second would teach users to
    approve twice, which is how people learn to click yes without reading.
    """
    token = set_consent(_granted(file_name="/tmp/Benchy.3MF"))
    try:
        assert consent_for(file_name="benchy.3mf", printer_name="garage") is not None
    finally:
        reset_consent(token)


def test_no_consent_is_not_consent():
    assert consent_for(file_name="benchy.3mf", printer_name="garage") is None


def test_consent_does_not_outlive_its_call():
    token = set_consent(_granted())
    reset_consent(token)
    assert consent_for(file_name="benchy.3mf", printer_name="garage") is None


# ---------------------------------------------------------------------------
# The gate takes a human answer in place of a token
# ---------------------------------------------------------------------------


def test_a_person_saying_yes_replaces_the_token(monkeypatch):
    monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
    # No token at all — this is refused today.
    assert server._preview_gate_error(
        "start_print", "benchy.3mf", None, printer_name="garage",
    ) is not None

    token = set_consent(_granted())
    try:
        assert server._preview_gate_error(
            "start_print", "benchy.3mf", None, printer_name="garage",
        ) is None
    finally:
        reset_consent(token)


def test_a_yes_about_another_file_does_not_open_the_gate(monkeypatch):
    monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
    token = set_consent(_granted(file_name="approved.3mf"))
    try:
        blocked = server._preview_gate_error(
            "start_print", "something_else.3mf", None, printer_name="garage",
        )
        assert blocked is not None
        assert blocked["error"]["code"] == "PREVIEW_NOT_CONFIRMED"
    finally:
        reset_consent(token)


# ---------------------------------------------------------------------------
# Asking: accept, decline, cancel, and could-not-ask
# ---------------------------------------------------------------------------


class _Ctx:
    """Stands in for the MCP request context the wrapper is handed."""


def _ask(monkeypatch, action: str, *, can_ask: bool = True, observe=None):
    """Drive _obtain_print_consent with a scripted host answer."""
    monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
    monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: can_ask)

    async def _fake(ctx, message):
        _ask.last_message = message
        return action, ""

    monkeypatch.setattr(server, "ask_user_to_confirm", _fake)

    async def _run():
        # Observed inside the same coroutine on purpose: a ContextVar set in
        # a task is not visible outside it, and in production the wrapper
        # awaits the tool in this very context.  Checking from outside would
        # test asyncio.run, not Kiln.
        token = await server._obtain_print_consent(
            "start_print",
            {"file_name": "benchy.3mf", "printer_name": "garage"},
            _Ctx(),
        )
        try:
            return token, (observe() if observe else None)
        finally:
            if token is not None:
                reset_consent(token)

    return asyncio.run(_run())


def test_yes_records_a_consent_bound_to_that_print(monkeypatch):
    def _observe():
        return (
            consent_for(file_name="benchy.3mf", printer_name="garage"),
            consent_for(file_name="other.3mf", printer_name="garage"),
        )

    token, (granted, other) = _ask(monkeypatch, "accept", observe=_observe)
    assert token is not None
    assert granted is not None
    assert granted.source == SOURCE_ELICITED
    # The same yes must not cover a different file.
    assert other is None


@pytest.mark.parametrize("action", ["decline", "cancel"])
def test_no_stops_the_call_before_the_tool_runs(monkeypatch, action):
    """A refused print is one that never started, not one that started and
    reported a failure."""
    with pytest.raises(RuntimeError, match="Nothing was sent to the printer"):
        _ask(monkeypatch, action)
    assert consent_for(file_name="benchy.3mf", printer_name="garage") is None


def test_a_host_that_cannot_be_asked_falls_back_rather_than_assuming(monkeypatch):
    """The REST proxy has no person attached; hosts predating elicitation
    have no dialog.  Neither is a yes, and neither may be refused outright."""
    token, granted = _ask(
        monkeypatch, "accept", can_ask=False,
        observe=lambda: consent_for(file_name="benchy.3mf", printer_name="garage"),
    )
    assert token is None
    assert granted is None


def test_a_host_that_errors_mid_question_is_not_a_yes(monkeypatch):
    monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
    monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: True)

    async def _boom(ctx, message):
        return "unavailable", "Timeout"

    monkeypatch.setattr(server, "ask_user_to_confirm", _boom)
    got = asyncio.run(
        server._obtain_print_consent(
            "start_print", {"file_name": "b.3mf"}, _Ctx(),
        )
    )
    assert got is None


def test_the_ci_bypass_silences_the_prompt_too(monkeypatch):
    """One switch turns off consent, as it always did.  Otherwise a CI run
    on a host that CAN ask would hang waiting for a human."""
    monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
    monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: True)

    async def _never(ctx, message):
        raise AssertionError("must not ask when the bypass is set")

    monkeypatch.setattr(server, "ask_user_to_confirm", _never)
    assert asyncio.run(
        server._obtain_print_consent("start_print", {"file_name": "b.3mf"}, _Ctx())
    ) is None


def test_a_tool_that_does_not_start_a_print_is_never_asked_about(monkeypatch):
    monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: True)

    async def _never(ctx, message):
        raise AssertionError("must not ask about a read-only tool")

    monkeypatch.setattr(server, "ask_user_to_confirm", _never)
    assert asyncio.run(
        server._obtain_print_consent("get_printer_status", {}, _Ctx())
    ) is None


# ---------------------------------------------------------------------------
# What the person is actually shown
# ---------------------------------------------------------------------------


def test_the_question_is_answerable_on_its_own():
    """A tool name and a token tell a reader nothing about what their
    printer is about to do."""
    msg = describe_print_request(
        "slice_and_print",
        file_name="dragon.stl",
        printer_name="workshop",
        extra={"material": "PLA"},
    )
    assert "dragon.stl" in msg
    assert "workshop" in msg
    assert "PLA" in msg
    assert "slice_and_print" in msg


def test_the_question_does_not_claim_to_show_the_model():
    """Form-mode elicitation carries text and a flat schema; it cannot
    render geometry.  A dialog implying a preview it never showed is the
    same lie the token tells."""
    msg = describe_print_request(
        "start_print", file_name="x.3mf", printer_name=None,
    )
    assert "not showing it" in msg


# ---------------------------------------------------------------------------
# The wiring, pinned
# ---------------------------------------------------------------------------


_SRC = pathlib.Path(server.__file__).parent
_MODULES = [_SRC / "server.py", *sorted((_SRC / "plugins").glob("*.py"))]


def _gate_callers() -> set[str]:
    """Every function that asks the preview gate for a verdict.

    This is the authoritative set of tools that must not print without
    consent — they said so themselves by calling the gate.
    """
    found: set[str] = set()
    for path in _MODULES:
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                name = (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else getattr(node.func, "id", "")
                )
                if name != "_preview_gate_error":
                    continue
                # Plugin tools are nested inside their register() function,
                # so attribute the call to the function that actually
                # contains it, not to every ancestor.
                owner = min(
                    (
                        f for f in ast.walk(tree)
                        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and f.lineno <= node.lineno <= (f.end_lineno or 0)
                    ),
                    key=lambda f: (f.end_lineno or 0) - f.lineno,
                )
                found.add(owner.name)
    return found


def test_every_gated_tool_is_asked_about():
    """The wrapper needs the answer BEFORE the tool runs, so it cannot read
    the gate calls at runtime and the mapping is written by hand.  This is
    the pin: a new print tool that gates itself but is missing here would
    never prompt anyone, and would look completely fine.
    """
    gated = _gate_callers() - {"_preview_gate_error"}
    missing = gated - set(server._CONSENT_FILE_ARG)
    assert missing == set(), (
        f"these tools gate their print but are never asked about: {missing}"
    )


def test_the_argument_each_tool_is_asked_about_still_exists():
    """The other half of the pin: the map names an argument per tool, and a
    renamed parameter would leave the prompt describing an empty file."""
    from kiln.plugins import monitoring_tools, slicer_tools, smart_print_tools

    sources = {
        "start_print": inspect.getsource(server),
        "start_monitored_print": inspect.getsource(monitoring_tools),
        "slice_and_print": inspect.getsource(slicer_tools),
        "retry_print_with_fix": inspect.getsource(smart_print_tools),
    }
    for tool, arg in server._CONSENT_FILE_ARG.items():
        src = sources[tool]
        sig = src[src.index(f"def {tool}(") :]
        sig = sig[: sig.index(") -> dict:")]
        assert f"{arg}:" in sig, f"{tool} has no argument named {arg!r}"


def test_the_consent_record_is_read_in_exactly_one_place():
    """One writer, one reader.  A second opinion on a question with one
    answer is how two callers come to disagree about who approved what."""
    readers: list[str] = []
    for path in _MODULES:
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    name = (
                        node.func.attr
                        if isinstance(node.func, ast.Attribute)
                        else getattr(node.func, "id", "")
                    )
                    if name == "consent_for":
                        readers.append(f"{path.name}::{fn.name}")
    assert readers == ["server.py::_preview_gate_error"], (
        f"consent should be read only by the gate; readers: {readers}"
    )


def test_the_module_does_not_reach_for_the_sdk():
    """``mcp_compat`` is the only door; the elicitation call goes through
    it like every other SDK use."""
    src = pathlib.Path(print_consent.__file__).read_text()
    assert "import mcp" not in src


# ---------------------------------------------------------------------------
# The dialog is written for the person reading it
# ---------------------------------------------------------------------------


def test_nothing_in_the_dialog_explains_the_implementation():
    """Hosts render the schema's class name as the title and its docstring
    as the description, so both are user-visible surface.

    Found live: the first version put "the spec allows only flat
    primitives" in front of the person being asked to approve a print.
    """
    captured: dict = {}

    class _Ctx:
        async def elicit(self, message, schema):
            captured["schema"] = schema.model_json_schema()
            raise RuntimeError("stop here — the schema is what is under test")

    asyncio.run(print_consent_ask(_Ctx()))

    schema = captured["schema"]
    blob = " ".join(
        [
            schema.get("title", ""),
            schema.get("description", ""),
            *[
                f"{v.get('title', '')} {v.get('description', '')}"
                for v in schema.get("properties", {}).values()
            ],
        ]
    ).lower()
    for leak in ("schema", "primitive", "spec", "boolean", "elicit", "pydantic"):
        assert leak not in blob, f"the approval dialog says {leak!r} to the user"
    # It still has to be a real question.
    assert "print" in blob


async def print_consent_ask(ctx):
    from kiln.mcp_compat import ask_user_to_confirm

    return await ask_user_to_confirm(ctx, "Start printing x.3mf?")
