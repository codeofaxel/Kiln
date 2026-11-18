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
import os
import pathlib
import sys

import pytest

from kiln import print_consent, server
from kiln.print_consent import (
    CHOICE_THIS_PRINT,
    SOURCE_ELICITED,
    DialogAnswer,
    PrintConsent,
    consent_for,
    describe_print_request,
    reset_consent,
    set_consent,
    why_not_asked,
)
from tests.test_watchdog_attached_to_every_print import _Bench

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
# The gate takes a human answer alongside the token — never one for the other
# ---------------------------------------------------------------------------


def test_a_person_saying_yes_is_the_half_the_token_never_was(monkeypatch, tmp_path):
    """Two facts.  The token says a door showed the file; the yes says a
    person agreed.  The gate wants both — a yes alone is a yes to a
    description, a token alone is a print started unseen.  (The A/B for
    this rewrite lives in test_a_person_says_go.py.)"""
    monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
    from kiln import preview_evidence

    mesh = tmp_path / "benchy.3mf"
    mesh.write_bytes(b"PK\x03\x04 not really a 3mf")
    preview_evidence.record("stage", str(mesh), via="panel_fetch")

    def _token():
        return server.issue_preview_token(str(mesh), door="stage")["token"]

    # Nothing at all — refused.
    assert server._preview_gate_error("start_print", str(mesh), None, printer_name="garage") is not None
    # A yes alone — refused: no preview on record.
    token = set_consent(_granted(file_name=str(mesh)))
    try:
        blocked = server._preview_gate_error("start_print", str(mesh), None, printer_name="garage")
        assert blocked is not None and "no preview" in blocked["error"]["message"]
    finally:
        reset_consent(token)
    # A token alone — refused: nobody said go.
    blocked = server._preview_gate_error("start_print", str(mesh), _token(), printer_name="garage")
    assert blocked is not None and "nobody said go" in blocked["error"]["message"]
    # Both — the print may start.
    token = set_consent(_granted(file_name=str(mesh)))
    try:
        assert server._preview_gate_error("start_print", str(mesh), _token(), printer_name="garage") is None
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

    async def _fake(ctx, message, **_kw):
        _ask.last_message = message
        return DialogAnswer(action, "", choice=CHOICE_THIS_PRINT if action == "accept" else "")

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
    have no dialog.  Neither is a yes, and neither may be refused outright.
    What IS recorded is that nobody was asked, and why — so the gate's
    refusal can say no dialog is coming (see test_a_window_from_the_dialog)."""
    token, (granted, reason) = _ask(
        monkeypatch, "accept", can_ask=False,
        observe=lambda: (consent_for(file_name="benchy.3mf", printer_name="garage"), why_not_asked()),
    )
    assert granted is None
    assert reason == "host_cannot_ask"
    # The note lives exactly as long as the call, like a consent does.
    assert token is not None
    assert why_not_asked() == ""


def test_a_host_that_errors_mid_question_is_not_a_yes(monkeypatch):
    monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
    monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: True)

    async def _boom(ctx, message, **_kw):
        return DialogAnswer("unavailable", "Timeout")

    monkeypatch.setattr(server, "ask_user_to_confirm", _boom)

    async def _run():
        token = await server._obtain_print_consent("start_print", {"file_name": "b.3mf"}, _Ctx())
        try:
            return consent_for(file_name="b.3mf", printer_name=None), why_not_asked()
        finally:
            if token is not None:
                reset_consent(token)

    granted, reason = asyncio.run(_run())
    assert granted is None
    assert reason == "unavailable:Timeout"


def test_the_ci_bypass_silences_the_prompt_too(monkeypatch):
    """One switch turns off consent, as it always did.  Otherwise a CI run
    on a host that CAN ask would hang waiting for a human."""
    monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
    monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: True)

    async def _never(ctx, message, **_kw):
        raise AssertionError("must not ask when the bypass is set")

    monkeypatch.setattr(server, "ask_user_to_confirm", _never)
    assert asyncio.run(
        server._obtain_print_consent("start_print", {"file_name": "b.3mf"}, _Ctx())
    ) is None


def test_a_tool_that_does_not_start_a_print_is_never_asked_about(monkeypatch):
    monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: True)

    async def _never(ctx, message, **_kw):
        raise AssertionError("must not ask about a read-only tool")

    monkeypatch.setattr(server, "ask_user_to_confirm", _never)
    assert asyncio.run(
        server._obtain_print_consent("get_printer_status", {}, _Ctx())
    ) is None


def _ask_about_the_multi_material_plate(monkeypatch, arguments: dict) -> tuple[list[str], object]:
    """Drive the wrapper for ``multi_material_print``: ``(the questions put,
    the consent on record for its plate inside the call)``."""
    monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
    monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: True)
    asked: list[str] = []

    async def _accept(ctx, message, **_kw):
        asked.append(message)
        return DialogAnswer("accept", "", choice=CHOICE_THIS_PRINT)

    monkeypatch.setattr(server, "ask_user_to_confirm", _accept)

    async def _run():
        token = await server._obtain_print_consent("multi_material_print", arguments, _Ctx())
        try:
            return consent_for(file_name="kiln_multi_material.3mf", printer_name=None)
        finally:
            if token is not None:
                reset_consent(token)

    return asked, asyncio.run(_run())


def test_the_call_that_prints_a_built_plate_asks_about_that_plate(monkeypatch):
    """multi_material_print names no file: the plate is one it builds.  The
    call carrying the plate's preview token is the one that prints it, so
    that call puts the question, about that plate."""
    asked, granted = _ask_about_the_multi_material_plate(
        monkeypatch, {"objects_json": "[]", "preview_token": "kp_shown"},
    )
    assert len(asked) == 1 and "kiln multi material" in asked[0], asked
    assert granted is not None and granted.source == SOURCE_ELICITED


def test_the_call_that_builds_the_plate_asks_nobody(monkeypatch):
    """Without a token the call builds the plate for preview and starts
    nothing; there is nothing yet to say go to."""
    asked, granted = _ask_about_the_multi_material_plate(monkeypatch, {"objects_json": "[]"})
    assert asked == [] and granted is None


# ---------------------------------------------------------------------------
# A plate built from an approved input starts on that input's yes, never on
# a preview check alone
# ---------------------------------------------------------------------------


class TestADerivedPlateRidesAYes:
    """``_covered_by_approval`` records that a plate the call built is the
    print its input was approved for.  It carries a person's yes forward; a
    clearance that is only a preview check has no yes in it to carry."""

    @pytest.fixture(autouse=True)
    def _no_clearance_either_side(self):
        from kiln import print_signoff

        print_signoff.clear()
        yield
        print_signoff.clear()

    @staticmethod
    def _consent_inside(approved: str = "input.stl", derived: str = "plate.3mf"):
        with server._covered_by_approval("a_plate_door", approved, derived, None):
            return consent_for(file_name=derived, printer_name=None)

    def test_a_preview_check_alone_carries_no_yes(self):
        from kiln import print_signoff

        print_signoff.grant("a_plate_door", "input.stl", None, source=print_signoff.SOURCE_PREVIEW_TOKEN, door="stage")
        assert self._consent_inside() is None

    def test_no_clearance_carries_no_yes(self):
        assert self._consent_inside() is None

    def test_a_yes_for_another_file_is_not_carried(self):
        from kiln import print_signoff

        print_signoff.grant("a_plate_door", "other.stl", None, source=SOURCE_ELICITED, door="stage")
        assert self._consent_inside() is None

    def test_a_persons_yes_is_carried_to_the_plate_for_the_inner_call_only(self):
        from kiln import print_signoff
        from kiln.print_consent import SOURCE_DERIVED

        print_signoff.grant("a_plate_door", "input.stl", None, source=SOURCE_ELICITED, door="stage")
        carried = self._consent_inside()
        assert carried is not None and carried.source == SOURCE_DERIVED and carried.door == "stage"
        assert consent_for(file_name="plate.3mf", printer_name=None) is None

    def test_a_window_carried_to_the_plate_covers_the_printer_it_names_and_no_other(self):
        """A start that names no printer goes to the printer it is aimed at;
        a window carried to the plate covers that start when it names that
        printer, keeps its own list of printers, and covers no other."""
        from kiln import print_signoff
        from kiln.print_consent import SOURCE_WINDOW

        print_signoff.grant(
            "a_plate_door", "input.stl", None,
            source=SOURCE_WINDOW, door="stage", scope=("garage",), window_id="w_1",
        )
        with server._covered_by_approval("a_plate_door", "input.stl", "plate.3mf", None):
            here = consent_for(file_name="plate.3mf", printer_name=None, aimed_at="garage")
            elsewhere = consent_for(file_name="plate.3mf", printer_name=None, aimed_at="workshop")
            named_elsewhere = consent_for(file_name="plate.3mf", printer_name="workshop", aimed_at="workshop")
        assert here is not None and here.scope == ("garage",) and here.window_id == "w_1", here
        assert elsewhere is None and named_elsewhere is None

    @staticmethod
    def _plate_object_bench(monkeypatch, tmp_path) -> tuple[object, list[object]]:
        """``print_plate_object`` with extraction, upload and the printer stood
        in, and the inner ``start_print`` gate real: ``(the plate, what that
        gate said)``."""
        monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
        plate = tmp_path / "plate.gcode.3mf"
        plate.write_bytes(b"PK\x03\x04 the plate")
        gcode = tmp_path / "cube.gcode"
        gcode.write_text("G28\n")
        monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
        monkeypatch.setattr(server, "_resolve_target_printer_type", lambda *_a, **_k: "octoprint")
        monkeypatch.setattr(
            "kiln.generation.validation.extract_plate_object_gcode",
            lambda *_a, **_k: {
                "output_path": str(gcode), "matched_object": {"name": "cube"}, "all_objects": [{"name": "cube"}],
                "skipped_lines": 0, "estimated_time_minutes": 1,
            },
        )
        monkeypatch.setattr(server, "upload_file", lambda *_a, **_k: {"success": True, "file_name": "cube.gcode"})
        inner: list[object] = []

        def _start(*, file_name, printer_name=None, **_kw):
            inner.append(server._preview_gate_error("start_print", file_name, None, printer_name=printer_name))
            return {"success": True}

        monkeypatch.setattr(server, "start_print", _start)
        return plate, inner

    def test_print_plate_object_with_no_printer_named_starts_inside_a_window_for_its_printer(
        self, monkeypatch, tmp_path,
    ):
        from kiln import consent_windows
        from kiln.preview_gate import get_preview_gate
        from kiln.print_consent import SOURCE_TERMINAL

        monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
        aimed = server._resolve_effective_printer_name(None)
        consent_windows._open_window(seconds=600, scope=(aimed,), source=SOURCE_TERMINAL)
        plate, inner = self._plate_object_bench(monkeypatch, tmp_path)
        token = get_preview_gate().issue(str(plate), door="stage").token
        server.print_plate_object(file_path=str(plate), object_name="cube", preview_token=token)
        assert inner == [None], inner

    def test_print_plate_object_starts_the_object_on_the_yes_its_plate_got(self, monkeypatch, tmp_path):
        """The object is cut from the plate the person saw and said go to;
        the start's own gate accepts the object on that yes.  Extraction,
        upload and the printer are stood in; both gates are real."""
        from kiln.preview_gate import get_preview_gate

        plate, inner = self._plate_object_bench(monkeypatch, tmp_path)
        token = get_preview_gate().issue(str(plate), door="stage").token
        said_go = set_consent(PrintConsent(tool="print_plate_object", file_name=str(plate), printer_name=None))
        try:
            server.print_plate_object(file_path=str(plate), object_name="cube", preview_token=token)
        finally:
            reset_consent(said_go)
        assert inner == [None], inner


# ---------------------------------------------------------------------------
# A retry is a print like any other
# ---------------------------------------------------------------------------


class TestARetryIsAPrintLikeAnyOther:
    """``retry_print_with_fix`` starts the model it is handed, whichever
    model that is, so it needs the person's yes and the preview like every
    other start.  The real gate and the real adapter template; the slicer,
    the pre-flight and the printer are stood in."""

    @pytest.fixture(autouse=True)
    def _bench(self, monkeypatch, tmp_path):
        from kiln import print_signoff
        from kiln.print_watchdog import PrintWatchdog
        from kiln.printers import base
        from kiln.registry import PrinterRegistry
        from tests.test_watchdog_attached_to_every_print import _Bench, _NullHeaterWatchdog, _on_the_server

        monkeypatch.setattr(base, "_PRINT_STARTED_HOOKS", ())
        monkeypatch.setattr(base, "_PRINT_ENDED_HOOKS", ())
        monkeypatch.setattr(server, "_print_lifecycle_hooks_installed", False)
        monkeypatch.setattr(server, "_registry", PrinterRegistry())
        monkeypatch.setattr(server, "_adapter", None)
        monkeypatch.setattr(server, "_print_watchdogs", {})
        monkeypatch.setattr(server, "_tool_limiter", server._ToolRateLimiter())
        monkeypatch.setattr(server, "_TOOL_RATE_LIMITS", {})
        monkeypatch.setattr(server, "_get_heater_watchdog", lambda: _NullHeaterWatchdog)
        monkeypatch.setattr(PrintWatchdog, "start", lambda self: None)
        monkeypatch.setattr(base, "_preflight_upload_or_raise", lambda adapter, path: path)
        monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
        monkeypatch.setenv("KILN_SKIP_PREFLIGHT", "1")
        monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
        self.printer = _Bench()
        self.uploaded: list[str] = []
        upload = self.printer.upload_file
        monkeypatch.setattr(self.printer, "upload_file", lambda path: self.uploaded.append(path) or upload(path))
        _on_the_server(workshop=self.printer)
        self.cleared: list[object] = []
        take = print_signoff.take
        monkeypatch.setattr(
            print_signoff, "take", lambda *a, **k: self.cleared.append(c := take(*a, **k)) or c,
        )
        self.model = tmp_path / "anything.stl"
        self.model.write_text("solid part\nendsolid part\n")
        gcode = tmp_path / "anything.gcode"
        gcode.write_text("G28\nG1 X10 Y10 Z0.2 E1\n")
        print_signoff.clear()
        yield
        print_signoff.clear()

    def _retry(self, monkeypatch, **kwargs):
        from kiln.monitor_twin import note_sliced
        from kiln.plugins import smart_print_tools
        from tests.test_watchdog_attached_to_every_print import _plugin_tools, _ready, _sliced

        gcode = self.model.with_suffix(".gcode")
        sliced = _sliced(gcode)

        def _slice(path, **kw):
            # What the real slicer notes, so the printer-side file traces
            # back to the model the yes was given for.
            note_sliced(path, str(gcode))
            return sliced(path, **kw)

        monkeypatch.setattr("kiln.slicer.slice_file", _slice)
        monkeypatch.setattr("kiln.slicer_profiles.resolve_slicer_profile", lambda *a, **k: None)
        monkeypatch.setattr(server, "_map_printer_hint_to_profile_id", lambda *a, **k: None)
        monkeypatch.setattr(server, "_PRINTER_MODEL", None)
        monkeypatch.setattr(server, "preflight_check", _ready)
        retry = _plugin_tools(smart_print_tools)["retry_print_with_fix"]
        return retry(
            model_path=str(self.model), printer_name="workshop",
            skip_diagnosis=True, skip_validation=True, **kwargs,
        )

    def _token(self) -> str:
        from kiln.preview_gate import get_preview_gate

        return get_preview_gate().issue(str(self.model), door="stage").token

    def _said_go(self):
        return set_consent(PrintConsent(
            tool="retry_print_with_fix", file_name=str(self.model), printer_name="workshop", source=SOURCE_ELICITED,
        ))

    def test_the_same_model_with_nobody_asked_starts_nothing_and_sends_nothing(self, monkeypatch):
        out = self._retry(monkeypatch, preview_token=self._token())
        assert self.printer.started == [], "a retry started with nobody's yes"
        assert self.uploaded == [], "a refused retry left its file on the printer"
        assert out["error"]["code"] == "PREVIEW_NOT_CONFIRMED", out

    def test_a_host_that_cannot_ask_starts_nothing(self, monkeypatch):
        """No dialog, no code on a screen, no account: nobody was asked, and
        the retry is refused rather than leaning on an earlier yes."""
        from kiln import screen_code

        async def _no_account(*_a, **_k):
            return ""

        monkeypatch.setattr(server, "host_can_ask_the_user", lambda *_a: False)
        monkeypatch.setattr(screen_code, "issue", lambda **_k: {"outcome": "unavailable", "reason": "no screen"})
        monkeypatch.setattr(server, "_ask_the_account", _no_account)

        async def _the_call():
            # The wrapper, then the tool, in one call's context, as MCP runs them.
            await server._obtain_print_consent(
                "retry_print_with_fix", {"model_path": str(self.model), "printer_name": "workshop"}, object(),
            )
            return why_not_asked(), self._retry(monkeypatch, preview_token=self._token())

        why, out = asyncio.run(_the_call())
        assert why.startswith("host_cannot_ask"), why
        assert self.printer.started == [], "a retry started on a host that asked nobody"
        assert out["error"]["code"] == "PREVIEW_NOT_CONFIRMED", out

    def test_the_persons_yes_and_the_preview_print_it_on_that_yes(self, monkeypatch):
        said_go = self._said_go()
        try:
            out = self._retry(monkeypatch, preview_token=self._token())
        finally:
            reset_consent(said_go)
        assert out.get("success") is True, out
        assert self.printer.started == ["anything.gcode"]
        assert [(c.source, c.door) for c in self.cleared if c] == [(SOURCE_ELICITED, "stage")], self.cleared

    def test_a_yes_without_the_preview_starts_nothing(self, monkeypatch):
        """The model is whatever file the call names: nothing on record says
        the person ever saw it, so the yes alone does not print it."""
        said_go = self._said_go()
        try:
            out = self._retry(monkeypatch)
        finally:
            reset_consent(said_go)
        assert self.printer.started == []
        assert out["error"]["code"] == "PREVIEW_NOT_CONFIRMED", out

    def test_inside_a_window_the_retry_is_still_previewed(self, monkeypatch):
        from kiln import consent_windows
        from kiln.print_consent import SOURCE_TERMINAL

        window = consent_windows._open_window(seconds=600, scope=("workshop",), source=SOURCE_TERMINAL)
        unseen = self._retry(monkeypatch)
        assert self.printer.started == [], "a retry inside a window started a model nobody was shown"
        assert unseen["error"]["code"] == "PREVIEW_NOT_CONFIRMED", unseen
        shown = self._retry(monkeypatch, preview_token=self._token())
        assert shown.get("success") is True, shown
        assert [c.window_id for c in self.cleared if c] == [window.id]

    def test_the_ci_bypass_still_prints(self, monkeypatch):
        monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
        out = self._retry(monkeypatch)
        assert out.get("success") is True, out
        assert self.printer.started == ["anything.gcode"]


class TestARetryBesideTheFailedPart:
    """A retry placed beside the part that failed is the same part, moved on
    the plate: the person's yes and the preview for the model print it with
    no new question, and the plate is still checked before it starts.  A
    mesh Kiln repairs or rescales is another part: it is refused in plain
    words before anything is sent, and printed once the person is asked
    about the file the refusal names.

    The real gate, the real wrapper asking a scripted host, the real
    adapter template and its start judge, the real pre-print check; the
    slicer, the placement verdict and the printer are stood in."""

    @pytest.fixture(autouse=True)
    def _bench(self, monkeypatch, tmp_path):
        from kiln import consent_windows, monitor_twin, print_signoff, screen_code
        from kiln.print_watchdog import PrintWatchdog
        from kiln.printers import base
        from kiln.registry import PrinterRegistry
        from tests.test_quiet_start import JAR
        from tests.test_watchdog_attached_to_every_print import _NullHeaterWatchdog, _on_the_server

        monkeypatch.setattr(base, "_PRINT_STARTED_HOOKS", ())
        monkeypatch.setattr(base, "_PRINT_ENDED_HOOKS", ())
        monkeypatch.setattr(server, "_print_lifecycle_hooks_installed", False)
        monkeypatch.setattr(server, "_registry", PrinterRegistry())
        monkeypatch.setattr(server, "_adapter", None)
        monkeypatch.setattr(server, "_print_watchdogs", {})
        monkeypatch.setattr(server, "_tool_limiter", server._ToolRateLimiter())
        monkeypatch.setattr(server, "_TOOL_RATE_LIMITS", {})
        monkeypatch.setattr(server, "_get_heater_watchdog", lambda: _NullHeaterWatchdog)
        monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
        monkeypatch.setattr(server, "_pro_api_call", lambda tool, **kw: {"status": "error", "error": "no network in tests"})
        monkeypatch.setattr(PrintWatchdog, "start", lambda self: None)
        monkeypatch.setattr(base, "_preflight_upload_or_raise", lambda adapter, path: path)
        monkeypatch.setattr("kiln.printers.bed_fit.get_build_volume", lambda pid: (256.0, 256.0, 256.0))
        monkeypatch.setattr("kiln.emergency.get_emergency_coordinator", lambda: _Unlatched())
        for var in ("KILN_SKIP_PREVIEW_GATE", "KILN_HOSTED_MULTITENANT"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("KILN_SKIP_PREFLIGHT", "1")
        monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
        monkeypatch.setitem(sys.modules, "kiln_pro", None)
        twin = tmp_path / "twin"
        twin.mkdir()
        monkeypatch.setattr(monitor_twin, "_TWIN_DIR", twin)
        monkeypatch.setattr(monitor_twin, "_SLICES_FILE", twin / "slices.json")
        monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", twin / "active.json")
        monkeypatch.setattr(screen_code, "_show_hook", lambda issued: False)
        monkeypatch.setattr(consent_windows, "person_at_terminal", lambda: False)
        print_signoff._reset_for_tests()
        print_consent._reset_for_tests()
        consent_windows._reset_for_tests()
        screen_code._reset_for_tests()
        self.printer = _BesideBench()
        _on_the_server(workshop=self.printer)
        self.cleared: list[object] = []
        take = print_signoff.take
        monkeypatch.setattr(
            print_signoff, "take", lambda *a, **k: self.cleared.append(c := take(*a, **k)) or c,
        )
        self.asked: list[str] = []
        self.jar = JAR
        yield
        print_signoff._reset_for_tests()
        print_consent._reset_for_tests()
        consent_windows._reset_for_tests()
        screen_code._reset_for_tests()

    def _occupied(self, monkeypatch):
        """The failed part is still on the plate, and the placement verdict
        finds room beside it with a plan for this machine."""
        from kiln import _pro_placement_bridge as bridge
        from kiln.plate_state import mark_occupied
        from tests.test_plate_placement_doors import _Bridge, _verdict
        from tests.test_quiet_start import _plan_verdict

        mark_occupied(self.printer, self.jar)
        monkeypatch.setattr(bridge, "ask", _Bridge((_verdict(ok=True, at=(200.0, 20.0)), None), (_plan_verdict(self.printer), None)))

    def _slicer(self, monkeypatch, tmp_path, *, then=None):
        from kiln.monitor_twin import note_sliced
        from kiln.slicer import SliceResult
        from tests.test_quiet_start import _body

        self.sliced: list[str] = []

        def _slice(path, **kw):
            # What the real slicer writes and notes, so the printer-side
            # file traces back to the mesh it was sliced from.
            self.sliced.append(path)
            gcode = tmp_path / (os.path.splitext(os.path.basename(path))[0] + ".gcode")
            gcode.write_text(_body())
            note_sliced(path, str(gcode))
            if then is not None:
                then()
            return SliceResult(success=True, output_path=str(gcode), slicer="bench", message="sliced")

        monkeypatch.setattr("kiln.slicer.slice_file", _slice)
        monkeypatch.setattr("kiln.slicer_profiles.resolve_slicer_profile", lambda *a, **k: None)
        monkeypatch.setattr(server, "_map_printer_hint_to_profile_id", lambda *a, **k: None)
        monkeypatch.setattr(server, "_PRINTER_MODEL", None)

    def _retry(self, model: str, **kwargs):
        from kiln.plugins import smart_print_tools
        from tests.test_watchdog_attached_to_every_print import _plugin_tools

        retry = _plugin_tools(smart_print_tools)["retry_print_with_fix"]
        kwargs.setdefault("skip_validation", True)
        return retry(model_path=model, printer_name="workshop", skip_diagnosis=True, **kwargs)

    def _asked_then(self, monkeypatch, model: str, call, *, tool: str = "retry_print_with_fix", arg: str = "model_path"):
        """The wrapper asks the person about *model* on a host that draws a
        dialog, they say yes, and *call* runs in that same call's context."""
        async def _person(ctx, message, **_kw):
            self.asked.append(message)
            return DialogAnswer("accept", "", choice=CHOICE_THIS_PRINT)

        async def _no_account(*_a, **_k):
            return None

        monkeypatch.setattr(server, "host_can_ask_the_user", lambda *_a: True)
        monkeypatch.setattr(server, "ask_user_to_confirm", _person)
        monkeypatch.setattr(server, "_consent_from_account", _no_account)

        async def _the_call():
            await server._obtain_print_consent(tool, {arg: model, "printer_name": "workshop"}, object())
            return call()

        return asyncio.run(_the_call())

    @staticmethod
    def _token(path: str) -> str:
        from kiln.preview_gate import get_preview_gate

        return get_preview_gate().issue(path, door="stage").token

    def test_the_persons_yes_for_the_model_prints_it_beside_the_failed_part(self, monkeypatch, tmp_path):
        from tests.test_plate_placement_doors import _cube

        monkeypatch.setattr(server, "preflight_check", lambda *a, **k: {"ready": True, "summary": "ready"})
        self._occupied(monkeypatch)
        self._slicer(monkeypatch, tmp_path)
        model = _cube(tmp_path / "bracket.stl")
        out = self._asked_then(
            monkeypatch, model,
            lambda: self._retry(model, placement=[200, 20], preview_token=self._token(model)),
        )
        assert out.get("success") is True, out
        assert len(self.asked) == 1 and "bracket" in self.asked[0], "one question, about the model"
        assert [os.path.basename(p) for p in self.sliced] == ["bracket_placed.stl"], "the part was moved, not changed"
        assert self.printer.started == ["bracket_placed.gcode.3mf"]
        # The start rode the person's yes for the model and the stage it was
        # shown on -- carried to the moved copy, never a yes nobody gave.
        assert [(c.source, c.door, os.path.basename(c.file_name)) for c in self.cleared if c] == [
            (SOURCE_ELICITED, "stage", "bracket.stl"),
        ], self.cleared
        assert out["approval_carries"] is True and "carries" in out["approval_note"]
        # The plate was judged at the start: the file started the quiet way,
        # every switch the plan names off, and it is recorded beside the jar.
        (_name, kwargs), = self.printer.impl_calls
        assert kwargs.get("bed_leveling") is False and kwargs.get("timelapse") is False
        from kiln.plate_state import read

        assert [j.file for j in read(self.printer).jobs] == ["jar_v2.gcode.3mf", "bracket_placed.gcode.3mf"]

    def test_the_plate_is_still_checked_when_the_yes_carries(self, monkeypatch, tmp_path):
        """The yes carried to the moved copy, and the plate check still had
        the last word: a part put down beside the jar after the plan was made
        stops the start, whoever said yes."""
        from kiln.plate_state import PLATE_CHANGED_CODE, mark_occupied
        from tests.test_plate_placement_doors import _cube
        from tests.test_quiet_start import COASTER

        monkeypatch.setattr(server, "preflight_check", lambda *a, **k: {"ready": True, "summary": "ready"})
        self._occupied(monkeypatch)
        self._slicer(monkeypatch, tmp_path, then=lambda: mark_occupied(self.printer, COASTER, keep_previous=True))
        model = _cube(tmp_path / "bracket.stl")
        out = self._asked_then(
            monkeypatch, model,
            lambda: self._retry(model, placement=[200, 20], preview_token=self._token(model)),
        )
        assert self.printer.started == [], out
        assert out["error"]["code"] == PLATE_CHANGED_CODE, out

    @pytest.mark.parametrize("yes", ["window", "always_allow", "ci_bypass"])
    def test_every_other_kind_of_yes_prints_it_beside_the_failed_part(self, monkeypatch, tmp_path, yes):
        from kiln import consent_windows
        from kiln.print_consent import SOURCE_ALWAYS, SOURCE_TERMINAL, SOURCE_WINDOW
        from tests.test_plate_placement_doors import _cube

        monkeypatch.setattr(server, "preflight_check", lambda *a, **k: {"ready": True, "summary": "ready"})
        if yes == "window":
            window = consent_windows._open_window(seconds=600, scope=("workshop",), source=SOURCE_TERMINAL)
            expected = (SOURCE_WINDOW, window.id)
        elif yes == "always_allow":
            monkeypatch.setattr(consent_windows, "person_at_terminal", lambda: True)
            monkeypatch.setattr(consent_windows, "always_allow_is_yours", lambda: True)
            window = consent_windows.open_always(printer_name="workshop", typed_name="workshop", told_no_camera=True)
            expected = (SOURCE_ALWAYS, window.id)
        else:
            monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
            expected = None
        self._occupied(monkeypatch)
        self._slicer(monkeypatch, tmp_path)
        model = _cube(tmp_path / "bracket.stl")
        token = None if yes == "ci_bypass" else self._token(model)
        out = self._retry(model, placement=[200, 20], preview_token=token)
        assert out.get("success") is True, out
        assert self.printer.started == ["bracket_placed.gcode.3mf"]
        if expected is not None:
            assert [(c.source, c.window_id) for c in self.cleared if c] == [expected], self.cleared

    def _standing_yes(self, monkeypatch, yes: str):
        from kiln import consent_windows
        from kiln.print_consent import SOURCE_ALWAYS, SOURCE_TERMINAL, SOURCE_WINDOW

        if yes == "window":
            return SOURCE_WINDOW, consent_windows._open_window(seconds=600, scope=("workshop",), source=SOURCE_TERMINAL)
        monkeypatch.setattr(consent_windows, "person_at_terminal", lambda: True)
        monkeypatch.setattr(consent_windows, "always_allow_is_yours", lambda: True)
        told_no_camera = self.printer.frame is None
        window = consent_windows.open_always(printer_name="workshop", typed_name="workshop", told_no_camera=told_no_camera)
        return SOURCE_ALWAYS, window

    def _print_beside(self, door: str, model: str, token: str):
        """One call of *door*, placing *model* beside the failed part, in a
        context of its own as every MCP call is; returns ``(result, context)``."""
        import contextvars

        from kiln.plugins import slicer_tools
        from tests.test_watchdog_attached_to_every_print import _plugin_tools

        ctx = contextvars.copy_context()
        if door == "retry":
            return ctx.run(lambda: self._retry(model, placement=[200, 20], preview_token=token)), ctx
        sap = _plugin_tools(slicer_tools)["slice_and_print"]
        out = ctx.run(lambda: sap(
            input_path=model, printer_name="workshop", placement=[200, 20], skip_validation=True, preview_token=token,
        ))
        return out, ctx

    @staticmethod
    def _bed_line(ctx) -> str:
        from kiln import consent_window_note

        block = ctx.run(lambda: consent_window_note.for_outcome(
            consent_window_note.note_for("workshop"), consent_window_note.STARTED, "workshop",
        ))
        return str((block.get("bed_check") or {}).get("note") or "")

    @pytest.mark.parametrize("door", ["retry", "slice_and_print"])
    @pytest.mark.parametrize("yes", ["window", "always_allow"])
    def test_the_bed_is_looked_at_before_a_print_beside_the_failed_part(self, monkeypatch, tmp_path, yes, door):
        """Nobody is asked, the printer has a camera and the failed part is on
        the plate: the print is held with a fresh frame of the bed, and starts
        beside the part, still unasked, once eyes say that part is all there is."""
        import contextvars

        from kiln.plugins import homing_tools
        from tests.test_always_allow_on_one_printer import _FRAME
        from tests.test_plate_placement_doors import _cube

        monkeypatch.setattr(server, "preflight_check", lambda *a, **k: {"ready": True, "summary": "ready"})
        self.printer.frame = _FRAME
        source, window = self._standing_yes(monkeypatch, yes)
        self._occupied(monkeypatch)
        self._slicer(monkeypatch, tmp_path)
        model = _cube(tmp_path / "bracket.stl")
        token = self._token(model)

        held, _ = self._print_beside(door, model, token)
        assert self.printer.started == [], held
        assert held["error"]["code"] == "ALWAYS_ALLOW_LOOK_FIRST", held
        assert self.printer.frames_fetched >= 1
        assert pathlib.Path(held["snapshot_path"]).read_bytes() == _FRAME
        assert "jar v2 is still on the plate" in held["error"]["message"]
        assert 'seen="as_recorded"' in held["error"]["message"]

        judged = contextvars.copy_context().run(
            lambda: homing_tools.look_at_plate(printer_name="workshop", seen="as_recorded"),
        )
        assert judged.get("success") is True, judged

        out, ctx = self._print_beside(door, model, token)
        assert out.get("success") is True, out
        assert self.printer.started == ["bracket_placed.gcode.3mf"]
        assert [(c.source, c.window_id) for c in self.cleared if c][-1:] == [(source, window.id)]
        assert "only jar v2 was on it" in self._bed_line(ctx)

    @pytest.mark.parametrize("yes", ["window", "always_allow"])
    def test_a_bed_seen_with_more_on_it_asks_the_person_once(self, monkeypatch, tmp_path, yes):
        """Eyes saw more than the failed part, or could not tell: nothing
        starts unasked, the person is told why they are asked, and their yes
        prints it beside the part with one question."""
        import contextvars

        from kiln.plugins import homing_tools
        from tests.test_always_allow_on_one_printer import _FRAME
        from tests.test_plate_placement_doors import _cube

        monkeypatch.setattr(server, "preflight_check", lambda *a, **k: {"ready": True, "summary": "ready"})
        self.printer.frame = _FRAME
        self._standing_yes(monkeypatch, yes)
        self._occupied(monkeypatch)
        self._slicer(monkeypatch, tmp_path)
        model = _cube(tmp_path / "bracket.stl")
        token = self._token(model)
        held, _ = self._print_beside("retry", model, token)
        assert held["error"]["code"] == "ALWAYS_ALLOW_LOOK_FIRST", held
        contextvars.copy_context().run(lambda: homing_tools.look_at_plate(printer_name="workshop", seen="occupied"))

        refused, _ = self._print_beside("retry", model, token)
        assert self.printer.started == [], refused
        assert refused["error"]["code"] == "PREVIEW_NOT_CONFIRMED", refused
        assert "besides jar v2" in refused["error"]["message"]

        out = contextvars.copy_context().run(lambda: self._asked_then(
            monkeypatch, model, lambda: self._retry(model, placement=[200, 20], preview_token=token),
        ))
        assert out.get("success") is True, out
        assert len(self.asked) == 1 and "besides jar v2" in self.asked[0]
        assert self.printer.started == ["bracket_placed.gcode.3mf"]

    @pytest.mark.parametrize("yes", ["window", "always_allow"])
    def test_a_printer_with_no_camera_prints_beside_the_failed_part_and_says_so(self, monkeypatch, tmp_path, yes):
        from tests.test_plate_placement_doors import _cube

        monkeypatch.setattr(server, "preflight_check", lambda *a, **k: {"ready": True, "summary": "ready"})
        self._standing_yes(monkeypatch, yes)
        self._occupied(monkeypatch)
        self._slicer(monkeypatch, tmp_path)
        model = _cube(tmp_path / "bracket.stl")
        out, ctx = self._print_beside("retry", model, self._token(model))
        assert out.get("success") is True, out
        assert self.printer.started == ["bracket_placed.gcode.3mf"]
        assert self._bed_line(ctx) == "This printer has no camera Kiln can use, so the bed was not checked first."

    def test_a_rescaled_mesh_is_another_part_asked_about_once_on_the_file_named(self, monkeypatch, tmp_path):
        """The pre-print check rescales a part drawn in metres: what would
        print is not what the person was shown.  Refused in plain words with
        nothing sent, and the file the refusal names prints once the person
        is shown it and asked about it -- one question, never a dead end."""
        from tests.test_plate_placement_doors import _cube

        monkeypatch.setattr(server, "preflight_check", lambda *a, **k: {"ready": True, "summary": "ready"})
        self._slicer(monkeypatch, tmp_path)
        model = _cube(tmp_path / "bracket.stl", size=0.02)
        refused = self._asked_then(
            monkeypatch, model,
            lambda: self._retry(model, skip_validation=False, preview_token=self._token(model)),
        )
        assert self.printer.started == [] and self.printer.uploaded == [], "nothing sent for a part nobody saw"
        assert refused["error"]["code"] == "PREVIEW_NOT_CONFIRMED", refused
        changed = refused["changed_part"]
        assert changed["how"] == "rescaled" and changed["was"] == os.path.realpath(model)
        message = refused["error"]["message"]
        assert "Kiln rescaled bracket.stl before printing it" in message
        assert "not the part the person was shown and said yes to" in message
        assert f'model_path="{changed["now"]}"' in message and "asked about it once" in message
        assert "was shown, and nobody said go" not in message, "the rescaled file was never shown"

        rescaled = changed["now"]
        self.asked.clear()
        out = self._asked_then(
            monkeypatch, rescaled,
            lambda: self._retry(rescaled, skip_validation=False, preview_token=self._token(rescaled)),
        )
        assert out.get("success") is True, out
        assert len(self.asked) == 1 and print_consent.display_name(rescaled) in self.asked[0]
        assert len(self.printer.started) == 1
        assert [os.path.basename(c.file_name) for c in self.cleared if c] == [os.path.basename(rescaled)]

    @pytest.mark.parametrize("token", ["none", "for another part"])
    @pytest.mark.parametrize("door, arg", [("retry", "model_path"), ("slice_and_print", "input_path")])
    def test_a_yes_with_no_preview_of_this_part_sends_nothing(self, monkeypatch, tmp_path, door, arg, token):
        """The person said go, and no preview of this part is on this call:
        refused before anything is sent, so the printer keeps no file."""
        from tests.test_plate_placement_doors import _cube

        monkeypatch.setattr(server, "preflight_check", lambda *a, **k: {"ready": True, "summary": "ready"})
        self._slicer(monkeypatch, tmp_path)
        model = _cube(tmp_path / "bracket.stl")
        other = self._token(_cube(tmp_path / "other.stl", size=10.0)) if token != "none" else None
        tool = "retry_print_with_fix" if door == "retry" else "slice_and_print"

        def _print():
            if door == "retry":
                return self._retry(model, preview_token=other)
            from kiln.plugins import slicer_tools
            from tests.test_watchdog_attached_to_every_print import _plugin_tools

            sap = _plugin_tools(slicer_tools)["slice_and_print"]
            return sap(input_path=model, printer_name="workshop", skip_validation=True, preview_token=other)

        out = self._asked_then(monkeypatch, model, _print, tool=tool, arg=arg)
        assert self.printer.uploaded == [] and self.printer.started == [], out
        expected = "PREVIEW_NOT_CONFIRMED" if token == "none" else "PREVIEW_TOKEN_INVALID"
        assert out["error"]["code"] == expected, out

    @staticmethod
    def _step(path, size: float = 20.0) -> str:
        pytest.importorskip("OCP", reason="a real STEP file needs the CAD kernel to write and convert")
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

        writer = STEPControl_Writer()
        writer.Transfer(BRepPrimAPI_MakeBox(size, size, size).Shape(), STEPControl_StepModelType.STEPControl_AsIs)
        assert writer.Write(str(path)) == IFSelect_RetDone
        return str(path)

    def _print_step(self, door: str, model: str, token: str):
        from kiln.plugins import slicer_tools
        from tests.test_watchdog_attached_to_every_print import _plugin_tools

        if door == "retry":
            return self._retry(model, skip_validation=False, preview_token=token)
        sap = _plugin_tools(slicer_tools)["slice_and_print"]
        return sap(input_path=model, printer_name="workshop", preview_token=token)

    @pytest.mark.parametrize("door, arg", [("retry", "model_path"), ("slice_and_print", "input_path")])
    def test_a_step_file_only_converted_prints_on_the_yes_for_it(self, monkeypatch, tmp_path, door, arg):
        """The check turns a STEP file into a mesh and changes nothing else:
        the same part, so the person's one yes for the STEP prints it, and
        nothing says Kiln repaired it."""
        monkeypatch.setattr(server, "preflight_check", lambda *a, **k: {"ready": True, "summary": "ready"})
        self._slicer(monkeypatch, tmp_path)
        model = self._step(tmp_path / "bracket.step")
        tool = "retry_print_with_fix" if door == "retry" else "slice_and_print"
        out = self._asked_then(
            monkeypatch, model, lambda: self._print_step(door, model, self._token(model)), tool=tool, arg=arg,
        )
        assert out.get("success") is True, out
        assert len(self.asked) == 1 and "bracket" in self.asked[0], "one question, about the STEP"
        assert len(self.printer.started) == 1
        assert [(c.source, os.path.basename(c.file_name)) for c in self.cleared if c] == [(SOURCE_ELICITED, "bracket.step")]
        assert "repaired" not in str(out.get("error") or "")

    def test_a_step_file_converted_and_rescaled_is_another_part(self, monkeypatch, tmp_path):
        monkeypatch.setattr(server, "preflight_check", lambda *a, **k: {"ready": True, "summary": "ready"})
        self._slicer(monkeypatch, tmp_path)
        model = self._step(tmp_path / "bracket.step", size=0.02)
        out = self._asked_then(monkeypatch, model, lambda: self._print_step("retry", model, self._token(model)))
        assert self.printer.started == [] and self.printer.uploaded == [], out
        assert out["changed_part"]["how"] == "rescaled", out
        assert "Kiln rescaled bracket.step before printing it" in out["error"]["message"]

    def test_a_window_does_not_let_the_old_preview_stand_for_a_changed_part(self, monkeypatch, tmp_path):
        """Inside a window nobody is asked, and the preview is still the
        part's: a token for the model as drawn does not cover the rescaled
        one, and the refusal says why in the same plain words."""
        from kiln import consent_windows
        from kiln.print_consent import SOURCE_TERMINAL
        from tests.test_plate_placement_doors import _cube

        monkeypatch.setattr(server, "preflight_check", lambda *a, **k: {"ready": True, "summary": "ready"})
        consent_windows._open_window(seconds=600, scope=("workshop",), source=SOURCE_TERMINAL)
        self._slicer(monkeypatch, tmp_path)
        model = _cube(tmp_path / "bracket.stl", size=0.02)
        out = self._retry(model, skip_validation=False, preview_token=self._token(model))
        assert self.printer.started == [], out
        assert out["changed_part"]["how"] == "rescaled", out
        assert "Kiln rescaled bracket.stl before printing it" in out["error"]["message"]


class _Unlatched:
    def get_latch_status(self, printer_id: str) -> dict:
        return {"latched": False, "critical_interlocks_pending": []}

    def __getattr__(self, name: str):
        return lambda *a, **k: None


class _BesideBench(_Bench):
    """A real adapter that talks to nothing, with what a start beside a part
    asks of a machine: a serial, homed axes, and the real 3MF wrap."""
    printer_id = "bambu_a1"

    def __init__(self) -> None:
        super().__init__()
        self.serial = "01P00A000000001"
        self._printer_model = "bambu_a1"
        self.homed = {"x", "y", "z"}
        self.uploaded: list[str] = []
        self.impl_calls: list[tuple[str, dict]] = []
        self.frame: bytes | None = None
        self.frames_fetched = 0

    @property
    def name(self) -> str:
        return "bambu"

    @property
    def snapshot_source(self):
        return "printer" if self.frame is not None else None

    def get_snapshot(self) -> bytes:
        self.frames_fetched += 1
        return self.frame or b""

    def homed_axes_now(self):
        return set(self.homed)

    def homed_axes_field(self) -> str:
        return "print.home_flag[bits 0-2]"

    def upload_file(self, file_path: str):
        self.uploaded.append(file_path)
        return super().upload_file(file_path)

    def wrap_gcode_as_3mf(self, gcode_path, *, stl_paths=None, source_3mf_path=None, quiet_start=None, lift_floor_mm=None):
        from kiln.monitor_twin import note_wrapped
        from kiln.printers.bambu_3mf import BambuPrintSettings, build_bambu_3mf

        out = os.path.splitext(gcode_path)[0] + ".gcode.3mf"
        build_bambu_3mf(
            pathlib.Path(gcode_path).read_text(), out, settings=BambuPrintSettings(),
            quiet_start=quiet_start, printer_model="bambu_a1",
        )
        note_wrapped(gcode_path, out)
        return out

    def _start_print_impl(self, file_name: str, **kwargs):
        self.impl_calls.append((file_name, dict(kwargs)))
        return super()._start_print_impl(file_name, **kwargs)


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
    # Named the way a person names it: the suffix they never typed is gone.
    assert "“dragon”" in msg and "dragon.stl" not in msg
    assert "workshop" in msg
    assert "PLA" in msg
    assert "slice_and_print" in msg


@pytest.mark.parametrize(
    ("file_name", "person_reads"),
    [
        ("consent_test_cube.gcode.3mf", "consent test cube"),
        ("/Users/someone/prints/Benchy (1).3mf", "Benchy (1)"),
        ("plate_1.gcode", "plate 1"),
        ("bracket.STEP", "bracket"),
        ("my  part__v2.obj", "my part v2"),
        ("tab\there\x07.stl", "tab here"),
        # A name chosen to read backwards on the line a person approves.
        ("invoice\u202egnp.stl", "invoicegnp"),
        # Only a print file's own suffixes go.
        ("notes.txt", "notes.txt"),
        # Never empty: a name with nothing left is shown as it is.
        ("___.stl", "___.stl"),
        (".3mf", ".3mf"),
    ],
)
def test_a_print_file_is_named_the_way_a_person_names_it(file_name, person_reads):
    assert print_consent.display_name(file_name) == person_reads


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


#: What a function does on its way to a print start, however it is spelled:
#: asks the gate, checks a preview token, or runs an inner start under the
#: approval its input got.
_START_SIGNS = frozenset({"_preview_gate_error", "token_verdict", "_covered_by_approval"})
#: The names a plugin calls the server module by.
_SERVER_MODULE_NAMES = frozenset({"_srv", "server"})

#: Tools that reach a start and are never asked about, each with why that is
#: not a print started unasked.  Shrinks only: an entry that is asked about
#: now, or no longer reaches a start, fails as stale.
_REACHES_A_START_UNASKED = {
    "smart_reprint": (
        "hands reprint_with_material no preview token and takes none, so a "
        "start it reaches needs a yes already on record for that file, with "
        "the door it was shown through: never a print nobody said go to"
    ),
}


def _start_reachers(modules: list[pathlib.Path]) -> set[str]:
    """Every function in *modules* that reaches a print start.

    Found by what a function does rather than by one spelling: it calls one
    of :data:`_START_SIGNS`, or it calls by name (or through the server
    module) a function that does, at any depth.  A door that checks its
    preview its own way and starts through another door is found as surely
    as one that calls the gate.  A call through an adapter object is the
    printer's own start, guarded by the clearance backstop, and is not
    followed here.
    """
    signs: set[str] = set()
    calls: dict[str, set[str]] = {}

    class _Calls(ast.NodeVisitor):
        # Plugin tools are nested inside their register() function, so a call
        # belongs to the innermost function that contains it.
        def __init__(self) -> None:
            self.inside: list[str] = []

        def visit_FunctionDef(self, node: ast.AST) -> None:
            self.inside.append(node.name)
            self.generic_visit(node)
            self.inside.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if self.inside and isinstance(func, (ast.Name, ast.Attribute)):
                owner = self.inside[-1]
                if isinstance(func, ast.Name):
                    name, by_name = func.id, True
                else:
                    name = func.attr
                    by_name = isinstance(func.value, ast.Name) and func.value.id in _SERVER_MODULE_NAMES
                if name in _START_SIGNS:
                    signs.add(owner)
                if by_name:
                    calls.setdefault(owner, set()).add(name)
            self.generic_visit(node)

    for path in modules:
        _Calls().visit(ast.parse(path.read_text()))
    reach = set(signs)
    while more := {fn for fn, called in calls.items() if called & reach} - reach:
        reach |= more
    return reach


def _consent_doors() -> set[str]:
    """The registered tools that reach a print start."""
    server._ensure_internal_tool_plugins_registered()
    tools = {t.name for t in server.mcp._tool_manager.list_tools()}
    doors = _start_reachers(_MODULES) & tools
    # A sweep that silently found nothing would pass every check below.
    assert {"start_print", "print_plate_object", "multi_material_print"} <= doors, sorted(doors)
    return doors


def _asked_about() -> set[str]:
    """The tools the wrapper asks about: by an argument, or by the plate they build."""
    return set(server._CONSENT_FILE_ARG) | set(server._CONSENT_BUILT_PLATE)


def test_every_gated_tool_is_asked_about():
    """The wrapper needs the answer BEFORE the tool runs, so it cannot read
    the gate calls at runtime and the mapping is written by hand.  This is
    the pin: a new print tool that reaches a start but is missing here would
    never prompt anyone, and would look completely fine.
    """
    missing = _consent_doors() - _asked_about() - set(_REACHES_A_START_UNASKED)
    assert missing == set(), (
        f"these tools reach a print start but are never asked about: {sorted(missing)}"
    )


def test_every_door_left_unasked_still_says_why():
    """The exceptions stay true: each still reaches a start and is still
    not asked about, or it leaves the list."""
    doors, asked = _consent_doors(), _asked_about()
    stale = {t for t in _REACHES_A_START_UNASKED if t not in doors or t in asked}
    assert stale == set(), f"no longer an unasked door; remove from _REACHES_A_START_UNASKED: {sorted(stale)}"


def test_the_asked_rosters_do_not_overlap():
    """A tool is asked about one way: by the argument naming its file, or by
    the plate it builds, never both."""
    assert set(server._CONSENT_FILE_ARG).isdisjoint(server._CONSENT_BUILT_PLATE)


@pytest.mark.parametrize(
    "body",
    [
        # The preview checked its own way, the start run under the plate's
        # approval through another door: every sign at once.
        "verdict = print_signoff.token_verdict('door', plate, preview_token)\n"
        "    with _covered_by_approval('door', plate, plate, None):\n"
        "        return run_reslice_and_print(model_path=plate)\n",
        "verdict = print_signoff.token_verdict('door', plate, preview_token)\n",
        "with _covered_by_approval('door', plate, plate, None):\n        return 1\n",
        "return run_reslice_and_print(model_path=plate)\n",
        "return _srv.run_reslice_and_print(model_path=plate)\n",
        "return _print_the_plate(plate)\n",
    ],
    ids=["all_signs", "token_only", "covered_only", "inner_door", "inner_door_via_server", "through_a_helper"],
)
def test_a_door_is_found_by_any_way_it_reaches_a_start(tmp_path, body):
    """Each way a tool can reach a start is enough on its own for the sweep
    to find it, including a door that never names the gate."""
    module = tmp_path / "doors.py"
    module.write_text(
        "def run_reslice_and_print(model_path, preview_token=None):\n"
        "    return _preview_gate_error('run_reslice_and_print', model_path, preview_token)\n"
        "\n"
        "def _print_the_plate(plate):\n"
        "    return run_reslice_and_print(model_path=plate)\n"
        "\n"
        "def door(plate, preview_token=None):\n"
        f"    {body}"
        "\n"
        "def adapter_door(adapter, plate):\n"
        "    return adapter.run_reslice_and_print(plate)\n"
    )
    found = _start_reachers([module])
    assert "door" in found
    assert "adapter_door" not in found


def test_the_argument_each_tool_is_asked_about_still_exists():
    """The other half of the pin: the map names an argument per tool, and a
    renamed parameter would leave the prompt describing an empty file."""
    from kiln.plugins import monitoring_tools, queue_tools, recovery_tools, slicer_tools, smart_print_tools

    sources = {
        "start_print": inspect.getsource(server),
        "start_monitored_print": inspect.getsource(monitoring_tools),
        "slice_and_print": inspect.getsource(slicer_tools),
        "retry_print_with_fix": inspect.getsource(smart_print_tools),
        "submit_job": inspect.getsource(queue_tools),
        "plan_multi_copy_split": inspect.getsource(recovery_tools),
    }
    for tool, arg in server._CONSENT_FILE_ARG.items():
        src = sources.get(tool) or inspect.getsource(server)
        sig = src[src.index(f"def {tool}(") :]
        sig = sig[: sig.index(") -> dict:")]
        assert f"{arg}:" in sig, f"{tool} has no argument named {arg!r}"


def _innermost_owner(tree: ast.AST, node: ast.AST) -> str | None:
    """The function a node actually sits in, not every function above it.

    Plugin tools are nested inside their ``register()``, so the widest
    enclosing function is almost never the interesting one.
    """
    holders = [
        f for f in ast.walk(tree)
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
        and f.lineno <= node.lineno <= (f.end_lineno or 0)
    ]
    if not holders:
        return None
    return min(holders, key=lambda f: (f.end_lineno or 0) - f.lineno).name


def _preview_token_refusers() -> tuple[set[str], set[str]]:
    """``(functions that refuse for want of a token, the tools that reach them)``.

    Both halves are read out of the source rather than listed here.  The
    refusal identifies itself — a function returning
    ``PREVIEW_NOT_CONFIRMED`` is a gate — so a third gate written next
    year is picked up without anyone remembering this file.  Two shapes
    exist today and both have to work: ``start_print`` and friends call a
    shared helper that emits the refusal, while ``fulfillment_order``
    emits it inline and is its own gate.
    """
    refusers: set[str] = set()
    for path in _MODULES:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if (
                    kw.arg == "code"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value == "PREVIEW_NOT_CONFIRMED"
                    and (owner := _innermost_owner(tree, node))
                ):
                    refusers.add(owner)

    reachers: set[str] = set()
    for path in _MODULES:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", "")
            )
            if name in refusers and (owner := _innermost_owner(tree, node)):
                reachers.add(owner)
    return refusers, reachers


def _gated_tools() -> dict:
    """``{tool name: registered tool}`` for every tool behind a preview gate."""
    server._ensure_internal_tool_plugins_registered()
    registered = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    refusers, reachers = _preview_token_refusers()

    # A silently-empty sweep would pass every test below for ever while
    # checking nothing, which is the failure they exist to catch.
    assert refusers, "no preview-token gate found — the sweep is broken"
    gated = {n: registered[n] for n in (refusers | reachers) if n in registered}
    assert "slice_and_print" in gated, (
        f"the sweep no longer sees a known gated tool; saw: {sorted(gated)}"
    )
    return gated


def test_a_tool_that_demands_a_token_can_be_handed_one():
    """A gate on a parameter the schema does not offer is a dead end.

    The refusal names ``preview_token``, so an agent reads it and passes
    one — and MCP drops the value on the way in, because the registered
    schema has no such property.  The call refuses again, identically,
    for ever.  Every part looks correct in isolation: the signature has
    the parameter, the gate is wired, the error tells the truth about
    what it wants.

    The same symptom comes from a client holding a tool list captured
    before the parameter existed rather than a defect in the tree — and
    nothing in the tree says so; the two are indistinguishable from
    outside.  This is the check that can tell them apart.

    The tools are derived from the refusal rather than listed, because a
    hand-maintained list is the same bug wearing a different hat.
    """
    missing = sorted(
        name for name, tool in _gated_tools().items()
        if "preview_token" not in (tool.parameters or {}).get("properties", {})
    )
    assert missing == [], (
        "these tools refuse without a preview token but give no way to pass "
        f"one — the schema has no preview_token property: {missing}"
    )


def test_no_preview_argument_is_lost_on_the_way_to_the_schema():
    """The general form: what the gate reads, the door must accept.

    ``preview_token`` is the parameter every refusal happens to name, so
    checking only that name would leave the same dead end open one
    argument over — ``fulfillment_order`` also refuses without
    ``preview_file_path``, and a token alone can never satisfy it.  So
    the rule is about the whole family rather than the one member that
    was reported: every ``preview_*`` argument a gated tool accepts in
    Python has to survive into the schema an agent is handed.
    """
    lost = {}
    for name, tool in _gated_tools().items():
        exposed = set((tool.parameters or {}).get("properties", {}))
        accepted = {
            p for p in inspect.signature(tool.fn).parameters
            if p.startswith("preview_")
        }
        # A gated tool that takes no preview argument at all cannot ever
        # be satisfied — the same dead end, reached from the other side.
        assert accepted, f"{name} is preview-gated but accepts no preview_* argument"
        if dropped := sorted(accepted - exposed):
            lost[name] = dropped
    assert lost == {}, (
        f"these preview arguments exist in Python but not in the schema: {lost}"
    )


def test_the_consent_record_is_read_in_exactly_one_place():
    """One function answers "is there a yes for this print", and two
    callers ask it: the gate, to decide whether the print may start, and
    the asker, to decide whether to put the question at all (a print
    inside a standing window is not asked about — that is what a window
    is).  Both read the SAME function, so they cannot disagree about who
    approved what.  A third caller, or a second function, is the second
    opinion this pin exists to refuse."""
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
    # The gate reads it through ``_said_go``, which the question asked before
    # a door sends a file to the printer shares, so those two cannot
    # disagree either.
    assert sorted(readers) == ["server.py::_obtain_print_consent", "server.py::_said_go"], (
        f"consent should be read only by the gate and the asker; readers: {readers}"
    )


# ---------------------------------------------------------------------------
# A yes is written only where a person gives it
# ---------------------------------------------------------------------------

#: The calls that write a print's clearance or a person's consent.
_YES_WRITES = frozenset({"grant", "grant_from_record", "set_consent"})
#: How a source read at run time is named below: a yes some gate already
#: read, passed on.
_CARRIED = "<carried>"

#: Where a person's own yes is written, and the person behind each.  A new
#: place fails until it is named here; a place that no longer writes one
#: fails as stale.
_WHERE_A_PERSON_SAYS_GO = {
    "server.py::consent_from_dialog_answer": "the answer the host's dialog brought back",
    "server.py::_consent_from_account_always": "always allow the account turned on, confirmed for this start",
    "server.py::_consent_from_code_answer": "the code a person typed after this machine's screen showed it",
    "server.py::_covered_by_approval": "the plate this call built, under the yes its input got on this call",
    "cli/print_gate.py::confirm_print_at_terminal": "the yes a person typed at this terminal",
}

#: Every other write, and what stands behind it in place of a person saying
#: go right now: a yes some gate already read, passed on, or a switch or a
#: check a person set up.  Nothing here is a door's own word that someone
#: once said yes.
_STANDS_IN_FOR_A_PERSON = {
    ("server.py::_preview_gate_error", _CARRIED): "the consent the gate just read, with the preview it checked",
    ("server.py::_consent_from_account", _CARRIED): "the account's approval or delegation, confirmed for this start",
    ("bridge_client.py::handle_relay_request", _CARRIED): "the account's approval or delegation the relay carried",
    ("cli/main.py::generate_and_print_cmd", _CARRIED): "the clearance the terminal's gate just granted, aimed at the slice",
    ("scheduler.py::tick", _CARRIED): "the yes the job stored when its door queued it",
    ("pipelines.py::_start_print", _CARRIED): "the yes a pipeline stored when its door started it",
    ("print_signoff.py::grant_from_record", _CARRIED): "a stored clearance, re-granted for the start it was stored for",
    ("print_signoff.py::grant_from_record", "SOURCE_QUEUED"): (
        "a queued job whose stored yes names no source: every door that queues asks the person "
        "before it queues, so someone said yes to it before it was queued"
    ),
    ("print_signoff.py::exemption_verdict", "SOURCE_CI_BYPASS"): "the audited KILN_SKIP_PREVIEW_GATE switch",
    ("print_signoff.py::exemption_verdict", "SOURCE_RESUME"): "a resume Kiln made, of the job the printer reports paused",
    ("print_signoff.py::token_verdict", "SOURCE_PREVIEW_TOKEN"): (
        "the preview half alone: the gate replaces it with the person's yes, or clears it, before any start"
    ),
    ("server.py::download_and_upload", "SOURCE_STANDING_OPT_IN"): "KILN_AUTO_PRINT_MARKETPLACE, the operator's switch",
    ("plugins/generation_ai_tools.py::generate_and_print", "SOURCE_STANDING_OPT_IN"): (
        "KILN_AUTO_PRINT_GENERATED, the operator's switch"
    ),
    ("cli/main.py::_dispatch_pending", "SOURCE_STANDING_OPT_IN"): (
        "the yes a person gave in person when they armed the watched folder"
    ),
}


def _sources_named(expr: ast.AST | None) -> set[str]:
    """Each source *expr* can write: a ``SOURCE_*`` constant by name, a
    literal as written, and :data:`_CARRIED` for anything read at run time."""
    if expr is None:
        return {"SOURCE_ELICITED"}  # PrintConsent's default
    if isinstance(expr, ast.Constant):
        return {repr(expr.value)}
    name = expr.id if isinstance(expr, ast.Name) else expr.attr if isinstance(expr, ast.Attribute) else ""
    if name.startswith("SOURCE_"):
        return {name}
    if isinstance(expr, ast.BoolOp):
        return set().union(*(_sources_named(v) for v in expr.values))
    if isinstance(expr, ast.IfExp):
        return _sources_named(expr.body) | _sources_named(expr.orelse)
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == "str" and expr.args:
        return _sources_named(expr.args[0])
    return {_CARRIED}


def _keyword(call: ast.Call, name: str) -> ast.AST | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _yes_writes() -> dict[str, set[str]]:
    """``{"<module>::<function>": {each source it writes}}`` for every place
    in public Kiln that writes a print's clearance or a consent, read from
    the source by what the call is, however it is imported."""
    writes: dict[str, set[str]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        own = path.name in ("print_signoff.py", "print_consent.py")
        imported = {
            alias.asname or alias.name: alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and (node.module or "").rsplit(".", 1)[-1] in ("print_signoff", "print_consent")
            for alias in node.names
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in (
                "print_signoff", "print_consent",
            ):
                call = func.attr
            elif isinstance(func, ast.Name):
                call = imported.get(func.id, func.id if own else "")
            else:
                continue
            if call not in _YES_WRITES or (owner := _innermost_owner(tree, node)) is None:
                continue
            if call == "grant":
                sources = _sources_named(_keyword(node, "source"))
            elif call == "set_consent" and node.args and isinstance(node.args[0], ast.Call):
                sources = _sources_named(_keyword(node.args[0], "source"))
            else:
                sources = {_CARRIED}
            writes.setdefault(f"{path.relative_to(_SRC).as_posix()}::{owner}", set()).update(sources)
    # A sweep that silently found nothing would pass every check below.
    assert "server.py::_preview_gate_error" in writes and "print_signoff.py::exemption_verdict" in writes, writes
    return writes


def _a_persons_yes(source: str) -> bool:
    """Whether *source* names a yes a person gave (or a plate riding one)."""
    from kiln import print_signoff
    from kiln.print_consent import SOURCE_DERIVED, grade_of

    if source == _CARRIED:
        return False
    value = getattr(print_consent, source, None) or getattr(print_signoff, source, None)
    if value is None and source.startswith("'"):
        value = ast.literal_eval(source)
    return isinstance(value, str) and (grade_of(value) is not None or value == SOURCE_DERIVED)


def test_nothing_writes_a_yes_nobody_gave():
    """A clearance or a consent is a person's yes, or names what stands in
    for one.  A door that writes its own word that someone once said yes
    (a "prior approval" of a print nobody named) starts that print with
    nobody asked, and every gate that only asks whether a door CLEARS its
    start reads it as cleared."""
    unnamed = sorted(
        f"{writer} writes {source}"
        for writer, sources in _yes_writes().items()
        for source in sources
        if not _a_persons_yes(source) and (writer, source) not in _STANDS_IN_FOR_A_PERSON
    )
    assert unnamed == [], f"a clearance or consent with no person behind it: {unnamed}"


def test_a_persons_yes_is_written_only_where_a_person_gives_it():
    """Writing a person's yes anywhere else is a forged one."""
    elsewhere = sorted(
        f"{writer} writes {source}"
        for writer, sources in _yes_writes().items()
        for source in sources
        if _a_persons_yes(source) and writer not in _WHERE_A_PERSON_SAYS_GO
    )
    assert elsewhere == [], f"a person's yes written where no person gives it: {elsewhere}"


def test_every_named_writer_still_writes():
    writes = _yes_writes()
    stale = sorted(
        [w for w in _WHERE_A_PERSON_SAYS_GO if not any(_a_persons_yes(s) for s in writes.get(w, ()))]
        + [f"{w} {s}" for (w, s) in _STANDS_IN_FOR_A_PERSON if s not in writes.get(w, set())]
    )
    assert stale == [], f"no longer written; remove from the lists above: {stale}"


def test_a_prior_approval_never_stands_in_for_a_yes():
    """A door's word that an earlier print was approved names no print and
    no person, so it is never on the list of what may stand in."""
    assert not any(source == "SOURCE_PRIOR_APPROVAL" for _writer, source in _STANDS_IN_FOR_A_PERSON)


@pytest.mark.parametrize(
    "source, expected",
    [
        ("print_signoff.SOURCE_PRIOR_APPROVAL", {"SOURCE_PRIOR_APPROVAL"}),
        ("SOURCE_ELICITED", {"SOURCE_ELICITED"}),
        ("granted.source", {_CARRIED}),
        ("cleared.source if cleared else 'unrecorded'", {_CARRIED, "'unrecorded'"}),
        ("str(record.get('source') or SOURCE_QUEUED)", {_CARRIED, "SOURCE_QUEUED"}),
    ],
)
def test_a_write_is_read_for_every_source_it_can_name(source, expected):
    assert _sources_named(ast.parse(source, mode="eval").body) == expected


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


# ---------------------------------------------------------------------------
# What the file IS: the served side's line, shown and never trusted to open
# ---------------------------------------------------------------------------


def test_the_served_sides_line_about_the_file_reaches_the_person(monkeypatch):
    """A design drawn as a sketch can say what it is — outline, holes,
    thickness — and the dialog shows that line when the served side hands
    it one.  The dialog only displays; the words are not its business."""
    seen: dict = {}

    def _hook(*, file_name, local_path):
        seen["asked_about"] = (file_name, local_path)
        return "40×20 outline, 2 holes ⌀3.4, 3 mm thick, fully constrained, STEP exact"

    print_consent.register_print_description_hook(_hook)
    try:
        _ask(monkeypatch, "accept")
    finally:
        print_consent.register_print_description_hook(None)
    assert seen["asked_about"][0] == "benchy.3mf"
    assert "Built: 40×20 outline, 2 holes ⌀3.4, 3 mm thick, fully constrained, STEP exact" in _ask.last_message


def test_a_file_the_served_side_cannot_describe_gets_no_built_row(monkeypatch):
    print_consent.register_print_description_hook(lambda *, file_name, local_path: None)
    try:
        _ask(monkeypatch, "accept")
    finally:
        print_consent.register_print_description_hook(None)
    assert "Built:" not in _ask.last_message
    assert "“benchy”" in _ask.last_message


def test_a_describing_hook_that_fails_does_not_stop_the_question(monkeypatch):
    """A dialog that fails to open would be worse than one with a row
    missing: the person still has to be asked."""

    def _boom(*, file_name, local_path):
        raise RuntimeError("no record")

    print_consent.register_print_description_hook(_boom)
    try:
        token, _ = _ask(monkeypatch, "accept")
    finally:
        print_consent.register_print_description_hook(None)
    assert token is not None
    assert "Built:" not in _ask.last_message


def test_the_served_line_is_folded_onto_one_row():
    """The dialog's rows are single lines; a hook that hands back a
    paragraph is shown as one line, not as a second dialog."""
    print_consent.register_print_description_hook(
        lambda *, file_name, local_path: "  40×20 outline,\n  2 holes  \n"
    )
    try:
        assert print_consent.describe_file_for_approval(file_name="x.3mf") == "40×20 outline, 2 holes"
        print_consent.register_print_description_hook(lambda *, file_name, local_path: 42)
        assert print_consent.describe_file_for_approval(file_name="x.3mf") is None
    finally:
        print_consent.register_print_description_hook(None)
    assert print_consent.describe_file_for_approval(file_name="x.3mf") is None
