"""A person gives their agent a standing window from the approval dialog,
without a terminal — and the agent still cannot give itself one.

The window feature landed with one door: ``kiln consent window --for 2h``,
refused unless a person is at a terminal.  Honest, and unusable by anyone
who never opens a terminal.  The owner's objection, verbatim: "the normie
should be able to give their agent a consent window inline without
touching a terminal."

The constraint that has to survive: a window is opened by a PERSON through
a door the agent does not hold.  The second door is the dialog the host
already draws before a print.  Its answer comes back through MCP's
elicitation channel — the server sends ``elicitation/create`` to the
CLIENT and only the client's JSON-RPC response answers it; the agent's
only channel to this server is ``tools/call`` — so a "yes, and for the
next 2 hours" picked there is the person's, and can open a window.  What
is pinned here:

* the dialog offers this print / the next 2 hours / the rest of today /
  no, and nothing wider or longer — several printers and the fleet stay
  a terminal command;
* a window with ``source=user_elicited`` exists only when a real
  elicitation response carried a window choice: not for "this print",
  not for a no, not for an answer the form never offered, not when the
  host could not be asked, and never through anything an agent can call;
* inside a window nobody is asked, at every door, and the gate still
  wants the preview; outside it the next print asks again;
* the person closes the window from the same chat (a tool — closing is
  the safe direction), and every print result names the window while it
  is open, so nobody is left in a window they cannot see the edge of;
* a host that cannot show a dialog is named plainly at the refusal, so
  the agent tells the person instead of promising a dialog it cannot show.

A/B: every test here fails on the tree before this change (8e11faf3) in
the direction that matters — the dialog offered no window, and a window
could be opened at a terminal only.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import pathlib
import time
import types
import typing

import pytest
from click.testing import CliRunner

from kiln import consent_window_note, consent_windows, preview_evidence, print_consent, print_signoff, server
from kiln.preview_gate import PreviewGate
from kiln.print_consent import (
    CHOICE_NEXT_TWO_HOURS,
    CHOICE_NO,
    CHOICE_REST_OF_TODAY,
    CHOICE_THIS_PRINT,
    DIALOG_CHOICES,
    FIELD_ANSWER,
    FIELD_FOR_HOW_LONG,
    FIELD_WHERE,
    MAX_WINDOW_SECONDS,
    NOT_ASKED_HOST_CANNOT,
    SOURCE_ELICITED,
    SOURCE_HOSTED_APPROVAL,
    SOURCE_TERMINAL,
    SOURCE_WINDOW,
    WHERE_EVERY_PRINTER,
    WHERE_THIS_PRINTER,
    DialogAnswer,
    PrintConsent,
    answer_from_content,
    consent_for,
    dialog_schema,
    register_hosted_approval_hook,
    reset_consent,
    why_not_asked,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Fresh home, fresh gate, no bypass, not hosted, nobody at a terminal,
    a host that can ask (each test says what it answers)."""
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    monkeypatch.setenv("KILN_EMERGENCY_PERSIST", "0")
    preview_evidence._reset_for_tests()
    print_signoff._reset_for_tests()
    print_consent._reset_for_tests()
    consent_windows._reset_for_tests()
    import kiln.preview_gate as pg

    monkeypatch.setattr(pg, "_gate", PreviewGate())
    monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: True)
    monkeypatch.setattr(server, "_resolve_effective_printer_name", lambda name=None: name or "bench")
    monkeypatch.setattr(consent_windows, "person_at_terminal", lambda: False)
    monkeypatch.setattr(consent_windows, "_fleet_tier_allows", lambda: False)
    monkeypatch.setattr("kiln.local_stage.host_renders_apps", lambda *a, **k: False)
    yield
    preview_evidence._reset_for_tests()
    print_signoff._reset_for_tests()
    print_consent._reset_for_tests()
    consent_windows._reset_for_tests()


@pytest.fixture
def audits(monkeypatch):
    seen: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        server, "_audit", lambda tool, action, details=None: seen.append((tool, action, details or {})),
    )
    return seen


@pytest.fixture
def at_terminal(monkeypatch):
    monkeypatch.setattr(consent_windows, "person_at_terminal", lambda: True)


@pytest.fixture
def business(monkeypatch):
    """An install whose tier runs several printers at once."""
    monkeypatch.setattr(consent_windows, "_fleet_tier_allows", lambda: True)


class _Host:
    """The CLIENT side of an elicitation: what a host does with the
    server's ``elicitation/create`` request.  ``answer`` is the value the
    person picked on the form; ``action`` the envelope they sent it in.
    Records the message and schema it was shown, like a host would draw."""

    def __init__(
        self, answer: str | None = None, action: str = "accept", raises: Exception | None = None,
        typed: str = "", where: str | None = None,
    ):
        self.answer, self.action, self.raises = answer, action, raises
        self.typed, self.where = typed, where
        self.asked: list[tuple[str, dict]] = []

    async def elicit(self, message, schema):
        self.asked.append((message, schema.model_json_schema()))
        if self.raises is not None:
            raise self.raises
        if self.action != "accept":
            return types.SimpleNamespace(action=self.action)
        # A real host validates against the schema it was sent; a value
        # off the form still has to reach the server as data, which is
        # the case the server must refuse on its own.
        data = {FIELD_ANSWER: self.answer, FIELD_FOR_HOW_LONG: self.typed}
        if self.where is not None:
            data[FIELD_WHERE] = self.where
        return types.SimpleNamespace(action="accept", data=types.SimpleNamespace(**data))


class _NeverAsks(_Host):
    """A host that must not be asked.  It RECORDS being asked rather than
    raising: ``ask_user_to_confirm`` swallows a host's exception as "could
    not ask", so a raise here would be read as unavailable and the test
    would pass for the wrong reason (measured on the pre-change tree).
    Tests assert ``asked == []`` after the call."""

    def __init__(self):
        super().__init__(action="decline")


def _stl(path: pathlib.Path) -> str:
    path.write_bytes(b"solid t\nendsolid t\n")
    return str(path)


def _token_for(path: str) -> str:
    preview_evidence.record("stage", path, via="panel_fetch")
    out = server.issue_preview_token(path, door="stage")
    assert out["success"], out
    return out["token"]


def _obtain(tool: str, arguments: dict, host, observe=None):
    """Drive the real asker with a scripted host; observe inside the same
    context (a ContextVar set in a task is not visible outside it)."""

    async def _run():
        token = await server._obtain_print_consent(tool, arguments, host)
        try:
            return token, (observe() if observe else None)
        finally:
            if token is not None:
                reset_consent(token)

    return asyncio.run(_run())


def _one_window() -> consent_windows.Window:
    live = consent_windows.live_windows()
    assert len(live) == 1, live
    return live[0]


# ---------------------------------------------------------------------------
# The dialog offers the choice
# ---------------------------------------------------------------------------


class TestTheDialog:
    def test_it_offers_this_print_two_hours_rest_of_today_or_no_and_a_typed_length(self):
        host = _Host(CHOICE_THIS_PRINT)
        _obtain("start_print", {"file_name": "benchy.3mf", "printer_name": "garage"}, host)
        [(message, schema)] = host.asked
        field = schema["properties"][FIELD_ANSWER]
        assert field["enum"] == [CHOICE_THIS_PRINT, CHOICE_NEXT_TWO_HOURS, CHOICE_REST_OF_TODAY, CHOICE_NO]
        assert field["enumNames"] == [label for _, label in DIALOG_CHOICES]
        # The safe default: a reflexive accept starts nothing.
        assert field["default"] == CHOICE_NO
        # A length the person types, with the cap in the help; nothing required.
        typed = schema["properties"][FIELD_FOR_HOW_LONG]
        assert typed["type"] == "string" and typed["default"] == "" and "24 hours" in typed["description"]
        assert list(schema["properties"]) == [FIELD_ANSWER, FIELD_FOR_HOW_LONG]
        assert not schema.get("required")
        # The person is told which machine a window would cover and how it closes.
        assert "garage" in message and "24 hours" in message
        assert "kiln consent revoke" in message
        # Every printer is NOT on a plain install's form.
        blob = json.dumps(schema).lower()
        assert "every printer" not in blob and "fleet" not in blob

    def test_the_fleet_tier_is_offered_every_printer(self, business):
        host = _Host(CHOICE_THIS_PRINT)
        _obtain("start_print", {"file_name": "benchy.3mf", "printer_name": "garage"}, host)
        [(message, schema)] = host.asked
        assert list(schema["properties"]) == [FIELD_ANSWER, FIELD_FOR_HOW_LONG, FIELD_WHERE]
        where = schema["properties"][FIELD_WHERE]
        assert where["enum"] == [WHERE_THIS_PRINTER, WHERE_EVERY_PRINTER]
        assert where["default"] == WHERE_THIS_PRINTER
        assert "every printer" in message

    def test_the_form_is_the_same_object_every_surface_draws(self, business):
        """The hosted wire and a native sheet build the form from
        ``dialog_schema``; the MCP host is handed the same properties."""
        host = _Host(CHOICE_THIS_PRINT)
        _obtain("start_print", {"file_name": "benchy.3mf", "printer_name": "garage"}, host)
        [(_, shown)] = host.asked
        assert shown["properties"] == dialog_schema(offer_window=True, offer_fleet=True)["properties"]
        assert list(dialog_schema(offer_window=True, offer_fleet=False)["properties"]) == [FIELD_ANSWER, FIELD_FOR_HOW_LONG]
        assert list(dialog_schema(offer_window=False)["properties"]) == [FIELD_ANSWER]

    def test_nothing_on_the_form_explains_the_implementation(self):
        host = _Host(CHOICE_THIS_PRINT)
        _obtain("start_print", {"file_name": "benchy.3mf"}, host)
        [(_, schema)] = host.asked
        blob = " ".join(
            [schema.get("title", ""), schema.get("description", ""),
             *[f"{v.get('title', '')} {v.get('description', '')} {' '.join(v.get('enumNames', []))}"
               for v in schema["properties"].values()]]
        ).lower()
        for leak in ("schema", "primitive", "spec", "enum", "elicit", "pydantic", "window_seconds", "field"):
            assert leak not in blob, f"the dialog says {leak!r} to the person"

    def test_the_form_is_valid_form_mode_on_the_installed_sdk(self):
        """The enum shape has to be one the SDK's own validator accepts —
        1.x refuses Literal and Enum fields outright; 2.x checks the
        rendered schema against the spec's PrimitiveSchemaDefinition."""
        captured: dict = {}

        class _Ctx:
            async def elicit(self, message, schema):
                captured["schema"] = schema
                raise RuntimeError("stop here")

        from kiln.mcp_compat import ask_user_to_confirm

        asyncio.run(ask_user_to_confirm(_Ctx(), "Start printing x.3mf?", offer_window=True, offer_fleet=True))
        schema = captured["schema"]
        try:
            from mcp.server.elicitation import render_elicitation_schema  # SDK 2

            render_elicitation_schema(schema)
        except ImportError:
            from mcp.server.elicitation import _validate_elicitation_schema  # SDK 1

            _validate_elicitation_schema(schema)

    def test_where_a_window_cannot_be_honoured_none_is_offered(self, monkeypatch):
        """The hosted server keeps no windows.  A dialog that offered one
        there would be a choice that silently shrinks to one print."""
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        host = _Host(CHOICE_THIS_PRINT)
        _obtain("start_print", {"file_name": "benchy.3mf", "printer_name": "garage"}, host)
        [(message, schema)] = host.asked
        assert schema["properties"][FIELD_ANSWER]["enum"] == [CHOICE_THIS_PRINT, CHOICE_NO]
        assert list(schema["properties"]) == [FIELD_ANSWER]
        assert "kiln consent revoke" not in message


# ---------------------------------------------------------------------------
# A window exists only when a real elicitation response asked for one
# ---------------------------------------------------------------------------


class TestOnlyARealAnswerOpensOne:
    def test_yes_for_two_hours_opens_a_window_on_the_aimed_printer_only(self, tmp_path, audits):
        path = _stl(tmp_path / "jar.stl")
        before = time.time()
        host = _Host(CHOICE_NEXT_TWO_HOURS)
        _, granted = _obtain(
            "start_print", {"file_name": path, "printer_name": "garage"}, host,
            observe=lambda: consent_for(file_name=path, printer_name="garage"),
        )
        # THIS print: the person's yes, grade A, as before.
        assert granted is not None and granted.source == SOURCE_ELICITED
        # AND a window: the person's, through the dialog door.
        w = _one_window()
        assert w.source == SOURCE_ELICITED
        assert w.scope == ("garage",)
        assert w.set_by == consent_windows.local_identity()
        assert before + 7200 - 2 <= w.until <= time.time() + 7200
        rec = next(d for _, a, d in audits if a == "consent_window_opened")
        assert rec["window_id"] == w.id and rec["printer"] == "garage" and rec["source"] == SOURCE_ELICITED
        # It does not cover the other machine.
        assert consent_windows.covering("workshop") is None
        assert consent_windows.covering("garage") is w or consent_windows.covering("garage").id == w.id
        # The record on disk says which door.
        raw = json.loads(consent_windows._path().read_text())
        assert raw["windows"][0]["source"] == SOURCE_ELICITED

    def test_rest_of_today_ends_at_local_midnight(self, tmp_path):
        host = _Host(CHOICE_REST_OF_TODAY)
        _obtain("start_print", {"file_name": "jar.stl", "printer_name": "garage"}, host)
        w = _one_window()
        end = time.localtime(w.until)
        assert (end.tm_hour, end.tm_min) == (0, 0)
        assert 0 < w.until - time.time() <= 24 * 3600

    def test_yes_this_print_opens_nothing(self, tmp_path):
        host = _Host(CHOICE_THIS_PRINT)
        _, granted = _obtain(
            "start_print", {"file_name": "jar.stl", "printer_name": "garage"}, host,
            observe=lambda: consent_for(file_name="jar.stl", printer_name="garage"),
        )
        assert granted is not None
        assert consent_windows.live_windows() == []
        assert not consent_windows._path().exists()

    @pytest.mark.parametrize("host", [_Host(CHOICE_NO), _Host(action="decline"), _Host(action="cancel")])
    def test_a_no_opens_nothing_and_starts_nothing(self, host):
        with pytest.raises(RuntimeError, match="Nothing was sent to the printer"):
            _obtain("start_print", {"file_name": "jar.stl", "printer_name": "garage"}, host)
        assert consent_windows.live_windows() == []

    @pytest.mark.parametrize("answer", ["fleet", "next_48_hours", "7200", "", None])
    def test_an_answer_the_form_never_offered_is_not_a_yes_to_anything(self, answer):
        """The one gap a longer or wider window could come through: a
        host handing back a value the person could not have picked.  Not
        asked, so no consent and no window; the gate then refuses."""
        host = _Host(answer)
        _, (granted, reason) = _obtain(
            "start_print", {"file_name": "jar.stl", "printer_name": "garage"}, host,
            observe=lambda: (consent_for(file_name="jar.stl", printer_name="garage"), why_not_asked()),
        )
        assert granted is None
        assert reason.startswith("unavailable:unexpected_choice")
        assert consent_windows.live_windows() == []

    def test_a_host_that_cannot_be_asked_opens_nothing(self, monkeypatch):
        monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: False)
        never = _NeverAsks()
        _obtain("start_print", {"file_name": "jar.stl", "printer_name": "garage"}, never)
        assert never.asked == []
        assert consent_windows.live_windows() == []
        host = _Host(raises=TimeoutError("host went away"))
        monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: True)
        _obtain("start_print", {"file_name": "jar.stl", "printer_name": "garage"}, host)
        assert consent_windows.live_windows() == []

    def test_the_dialog_door_takes_only_a_yes_with_a_window_choice(self):
        """``open_window_from_dialog`` is the writer behind the dialog; it
        refuses everything that is not a person's 'for a while' yes.  And
        the terminal door is untouched: off a terminal it still refuses."""
        for bad in (
            None, "next_two_hours", {"choice": CHOICE_NEXT_TWO_HOURS},
            DialogAnswer("accept", "", choice=CHOICE_THIS_PRINT),
            DialogAnswer("decline", "", choice=CHOICE_NEXT_TWO_HOURS),
            DialogAnswer("unavailable", "", choice=CHOICE_NEXT_TWO_HOURS),
        ):
            with pytest.raises(ValueError):
                consent_windows.open_window_from_dialog(bad, printer_name="garage")
        with pytest.raises(ValueError):
            consent_windows.open_window_from_dialog(
                DialogAnswer("accept", "", choice=CHOICE_NEXT_TWO_HOURS), printer_name="",
            )
        with pytest.raises(consent_windows.NotAPerson):
            consent_windows.open_window(seconds=7200, scope=("garage",))
        assert consent_windows.live_windows() == []

    def test_the_dialog_door_is_shut_on_the_hosted_server(self, monkeypatch):
        """Even a real window answer writes nothing on the shared disk."""
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        with pytest.raises(consent_windows.NotAPerson):
            consent_windows.open_window_from_dialog(
                DialogAnswer("accept", "", choice=CHOICE_NEXT_TWO_HOURS), printer_name="garage",
            )
        assert not consent_windows._path().exists()

    def test_an_unnamed_call_opens_the_window_for_the_printer_it_resolves_to(self):
        """The same name the gate matches a window on."""
        host = _Host(CHOICE_NEXT_TWO_HOURS)
        _obtain("start_print", {"file_name": "jar.stl"}, host)
        assert _one_window().scope == ("bench",)

    def test_the_hosted_server_opens_none_even_for_a_window_answer(self, monkeypatch, audits):
        """A host that answers with a window choice anyway (off the form
        it was sent) has not asked; a host that answers 'this print' gets
        the single yes, and no window is written on the shared disk."""
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        _, granted = _obtain(
            "start_print", {"file_name": "jar.stl", "printer_name": "garage"}, _Host(CHOICE_NEXT_TWO_HOURS),
            observe=lambda: consent_for(file_name="jar.stl", printer_name="garage"),
        )
        assert granted is None
        _, granted = _obtain(
            "start_print", {"file_name": "jar.stl", "printer_name": "garage"}, _Host(CHOICE_THIS_PRINT),
            observe=lambda: consent_for(file_name="jar.stl", printer_name="garage"),
        )
        assert granted is not None and granted.source == SOURCE_ELICITED
        assert consent_windows.live_windows() == []
        assert not any(a == "consent_window_opened" for _, a, _ in audits)

    def test_a_window_that_cannot_be_written_does_not_withdraw_the_yes(self, monkeypatch, audits):
        def _boom(_windows):
            raise OSError("disk full")

        monkeypatch.setattr(consent_windows, "_write", _boom)
        _, granted = _obtain(
            "start_print", {"file_name": "jar.stl", "printer_name": "garage"}, _Host(CHOICE_NEXT_TWO_HOURS),
            observe=lambda: consent_for(file_name="jar.stl", printer_name="garage"),
        )
        assert granted is not None
        assert consent_windows.live_windows() == []
        rec = next(d for _, a, d in audits if a == "consent_window_not_opened")
        assert "disk full" in rec["reason"]


# ---------------------------------------------------------------------------
# A length the person types, and the one cap at every door
# ---------------------------------------------------------------------------


def _result():
    return types.SimpleNamespace(
        structuredContent=None, isError=False,
        content=[types.SimpleNamespace(type="text", text=json.dumps({"success": True, "job": "j1"}))],
    )


def _line_after(tool: str, arguments: dict, host):
    """Drive the asker, then the result line, in the one context a real
    call runs in (the line takes what the grant noted)."""

    async def _run():
        token = await server._obtain_print_consent(tool, arguments, host)
        try:
            granted = consent_for(file_name=arguments.get("file_name", ""), printer_name=arguments.get("printer_name"))
            r = _result()
            consent_window_note._attach(r, None, tool, arguments)
            return granted, (r.structuredContent or {}).get(consent_window_note.RESULT_KEY)
        finally:
            if token is not None:
                reset_consent(token)

    return asyncio.run(_run())


class TestATypedLength:
    @pytest.mark.parametrize("choice,typed,seconds", [
        (CHOICE_THIS_PRINT, "45m", 45 * 60),
        (CHOICE_NEXT_TWO_HOURS, "3h", 3 * 3600),   # typed wins over the choice
        (CHOICE_REST_OF_TODAY, "90s", 90),
        (CHOICE_THIS_PRINT, "1d", 24 * 3600),      # the cap, inclusive
        (CHOICE_THIS_PRINT, " 0.5 ", 1800),        # a bare number is hours
    ])
    def test_a_typed_length_opens_a_window_for_that_long(self, choice, typed, seconds):
        granted, line = _line_after(
            "start_print", {"file_name": "jar.stl", "printer_name": "garage"}, _Host(choice, typed=typed),
        )
        assert granted is not None
        w = _one_window()
        assert w.until - w.set_at == pytest.approx(seconds, abs=1)
        assert w.scope == ("garage",) and w.source == SOURCE_ELICITED
        assert line["opened"] is True and line["id"] == w.id

    def test_blank_keeps_the_choice(self):
        _obtain("start_print", {"file_name": "jar.stl", "printer_name": "garage"}, _Host(CHOICE_NEXT_TWO_HOURS, typed="  "))
        w = _one_window()
        assert w.until - w.set_at == pytest.approx(7200, abs=1)

    @pytest.mark.parametrize("typed,words", [
        ("2 hrs-ish", "could not read"),
        ("25h", "at most 24 hours"),
        ("2d", "at most 24 hours"),
        ("0m", "longer than nothing"),
    ])
    def test_a_length_kiln_cannot_honour_is_told_in_the_moment_and_the_yes_stands(self, typed, words, audits):
        """The person said yes to THIS print unambiguously; the window part
        failed.  The print goes ahead, no window exists, and the result
        line says so with what to type next time — not the next dialog."""
        granted, line = _line_after(
            "start_print", {"file_name": "jar.stl", "printer_name": "garage"}, _Host(CHOICE_THIS_PRINT, typed=typed),
        )
        assert granted is not None and granted.source == SOURCE_ELICITED
        assert consent_windows.live_windows() == []
        assert line["opened"] is False
        assert typed in line["asked_for"] and words in line["reason"]
        assert "next print will ask again" in line["note"] and "24 hours" in line["note"]
        rec = next(d for _, a, d in audits if a == "consent_window_not_opened")
        assert words in rec["reason"]

    def test_a_no_with_a_typed_length_is_a_no(self):
        with pytest.raises(RuntimeError, match="Nothing was sent"):
            _obtain("start_print", {"file_name": "jar.stl", "printer_name": "garage"}, _Host(CHOICE_NO, typed="3h"))
        assert consent_windows.live_windows() == []

    def test_the_failure_line_is_said_once_and_only_on_that_call(self, monkeypatch):
        def _boom(_windows):
            raise OSError("disk full")

        monkeypatch.setattr(consent_windows, "_write", _boom)
        _, line = _line_after("start_print", {"file_name": "jar.stl", "printer_name": "garage"}, _Host(CHOICE_NEXT_TWO_HOURS))
        assert line["opened"] is False and "disk full" in line["reason"]
        monkeypatch.undo()
        # The next call carries nothing stale — and a plain yes carries nothing at all.
        _, line = _line_after("start_print", {"file_name": "jar.stl", "printer_name": "garage"}, _Host(CHOICE_THIS_PRINT))
        assert line is None

    def test_the_cap_is_the_same_at_the_terminal_door(self, at_terminal):
        with pytest.raises(ValueError, match="at most 24 hours"):
            consent_windows.open_window(seconds=MAX_WINDOW_SECONDS + 1, scope=("garage",))
        w = consent_windows.open_window(seconds=MAX_WINDOW_SECONDS, scope=("garage",))
        with pytest.raises(ValueError, match="at most 24 hours"):
            consent_windows.extend_window(w.id, seconds=MAX_WINDOW_SECONDS + 1)
        from kiln.cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["consent", "window", "--for", "2d", "--printer", "garage"])
        assert result.exit_code != 0 and "24 hours" in result.output
        result = runner.invoke(cli, ["consent", "extend", w.id, "--for", "36h"])
        assert result.exit_code != 0 and "24 hours" in result.output
        assert len(consent_windows.live_windows()) == 1


# ---------------------------------------------------------------------------
# Every printer, on the tier that runs several
# ---------------------------------------------------------------------------


class TestEveryPrinter:
    def test_business_can_say_every_printer_from_the_dialog(self, business, audits):
        _obtain("start_print", {"file_name": "jar.stl", "printer_name": "garage"},
                _Host(CHOICE_NEXT_TWO_HOURS, where=WHERE_EVERY_PRINTER))
        w = _one_window()
        assert w.scope == consent_windows.SCOPE_FLEET and w.source == SOURCE_ELICITED
        assert consent_windows.covering("workshop") is not None
        rec = next(d for _, a, d in audits if a == "consent_window_opened")
        assert rec["scope"] == "fleet" and "every printer" in rec["asked_for"]
        # And nobody is asked on the other machine now.
        never = _NeverAsks()
        _obtain("start_print", {"file_name": "jar.stl", "printer_name": "workshop"}, never)
        assert never.asked == []

    def test_this_printer_is_the_default_and_the_typed_length_rides_along(self, business):
        _obtain("start_print", {"file_name": "jar.stl", "printer_name": "garage"},
                _Host(CHOICE_THIS_PRINT, typed="3h", where=WHERE_THIS_PRINTER))
        w = _one_window()
        assert w.scope == ("garage",) and w.until - w.set_at == pytest.approx(3 * 3600, abs=1)
        _obtain("start_print", {"file_name": "jar.stl", "printer_name": "attic"},
                _Host(CHOICE_THIS_PRINT, typed="3h", where=WHERE_EVERY_PRINTER))
        assert consent_windows.covering("workshop").scope == consent_windows.SCOPE_FLEET

    def test_below_the_fleet_tier_every_printer_is_not_on_the_form_and_not_honoured(self, audits):
        """A host that sends it anyway is off the form: not a yes to anything."""
        _, granted = _obtain(
            "start_print", {"file_name": "jar.stl", "printer_name": "garage"},
            _Host(CHOICE_NEXT_TWO_HOURS, where=WHERE_EVERY_PRINTER),
            observe=lambda: consent_for(file_name="jar.stl", printer_name="garage"),
        )
        assert granted is None
        assert consent_windows.live_windows() == []
        # And the writer itself refuses it, whichever door: the tier changed
        # between offer and answer.
        with pytest.raises(consent_windows.NotTheFleetTier):
            consent_windows.open_window_from_dialog(
                DialogAnswer("accept", "", choice=CHOICE_NEXT_TWO_HOURS, where=WHERE_EVERY_PRINTER), printer_name="garage",
            )

    def test_a_named_list_of_printers_stays_the_terminals(self):
        """The form offers this printer or every printer, nothing a name
        can be typed into; a list of printers is the terminal command's."""
        props = dialog_schema(offer_window=True, offer_fleet=True)["properties"]
        assert props[FIELD_WHERE]["enum"] == [WHERE_THIS_PRINTER, WHERE_EVERY_PRINTER]
        assert not {"printers", "printer_names", "printer_name", "scope"} & set(props)


# ---------------------------------------------------------------------------
# The hosted server: the account's store, when kiln-pro registers one
# ---------------------------------------------------------------------------


class _FakeStore(consent_windows.WindowStore):
    """What kiln-pro's store looks like from here: the account's windows,
    the account as ``set_by``.  Keeps them in memory."""

    def __init__(self, account: str = "account:acct_123"):
        self.account = account
        self.rows: list[consent_windows.Window] = []
        self.calls: list[str] = []

    def covering(self, printer_name):
        self.calls.append("covering")
        return next((w for w in self.rows if w.live() and w.covers(printer_name)), None)

    def live(self):
        return [w for w in self.rows if w.live()]

    def open(self, *, seconds, scope, source):
        self.calls.append("open")
        now = time.time()
        w = consent_windows.Window(
            id=f"w_h{len(self.rows)}", set_by=self.account, set_at=now, until=now + seconds, scope=scope, source=source,
        )
        self.rows.append(w)
        return w

    def revoke(self, window_id):
        self.calls.append("revoke")
        for i, w in enumerate(self.rows):
            if w.id == window_id:
                closed = consent_windows.Window(
                    id=w.id, set_by=w.set_by, set_at=w.set_at, until=w.until, scope=w.scope,
                    revoked_at=time.time(), source=w.source,
                )
                self.rows[i] = closed
                return closed
        raise KeyError(window_id)


class TestTheHostedStore:
    """The hosted server, with kiln-pro's store registered.  The shape
    agreed with the print-authority work: the dialog door OPENS through
    the store (a standing permission the account grants the calling
    agent, Pro and above), the status tool and the result line READ
    through it, revoke CLOSES through it — and whether a hosted print may
    start is the hosted approval hook's answer, never the store's."""

    @pytest.fixture
    def hosted_store(self, monkeypatch):
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        store = _FakeStore()
        consent_windows.register_window_store(store)
        yield store
        consent_windows.register_window_store(None)
        register_hosted_approval_hook(None)

    def test_a_pro_web_user_is_offered_the_window_and_it_is_the_accounts(self, hosted_store, audits):
        host = _Host(CHOICE_NEXT_TWO_HOURS)
        _, granted = _obtain(
            "start_print", {"file_name": "jar.stl", "printer_name": "garage"}, host,
            observe=lambda: consent_for(file_name="jar.stl", printer_name="garage"),
        )
        [(message, schema)] = host.asked
        assert list(schema["properties"]) == [FIELD_ANSWER, FIELD_FOR_HOW_LONG]
        assert "telling your assistant" in message and "kiln consent" not in message
        assert granted is not None and granted.source == SOURCE_ELICITED
        [w] = hosted_store.live()
        assert w.set_by == "account:acct_123" and w.scope == ("garage",) and w.source == SOURCE_ELICITED
        assert hosted_store.calls.count("open") == 1
        # Nothing touched the shared disk.
        assert not consent_windows._path().exists()
        rec = next(d for _, a, d in audits if a == "consent_window_opened")
        assert rec["by"] == "account:acct_123"

    def test_the_hosted_start_is_the_hooks_answer_not_the_stores(self, hosted_store, tmp_path):
        """The store holds a window; the hook has not said yes.  The gate
        refuses: on the hosted server the store is read for what is open,
        never as a second opinion on whether this print may start."""
        path = _stl(tmp_path / "jar.stl")
        hosted_store.open(seconds=3600, scope=("garage",), source=SOURCE_ELICITED)
        assert consent_windows.covering("garage") is not None
        assert consent_for(file_name=path, printer_name="garage", aimed_at="garage") is None
        # And the asker asks — a window in the store alone silences nothing.
        host = _Host(CHOICE_THIS_PRINT)
        _obtain("start_print", {"file_name": path, "printer_name": "garage"}, host)
        assert len(host.asked) == 1
        # When the hook answers (kiln-pro: the account's approval or the
        # standing permission it granted this agent), nobody is asked and
        # the gate takes it as grade A.
        register_hosted_approval_hook(lambda **kw: PrintConsent(
            tool="start_print", file_name=kw["file_name"], printer_name=kw["printer_name"],
            source=SOURCE_HOSTED_APPROVAL, identity="agent:a1 under account:acct_123#d7",
        ))
        never = _NeverAsks()
        token, granted = _obtain(
            "start_print", {"file_name": path, "printer_name": "garage"}, never,
            observe=lambda: consent_for(file_name=path, printer_name="garage", aimed_at="garage"),
        )
        assert never.asked == [] and token is None
        assert granted.source == SOURCE_HOSTED_APPROVAL and granted.identity.startswith("agent:a1 under")

    def test_the_web_user_is_reminded_and_closes_it_through_the_agent(self, hosted_store):
        _obtain("start_print", {"file_name": "jar.stl", "printer_name": "garage"}, _Host(CHOICE_NEXT_TWO_HOURS))
        [w] = hosted_store.live()
        r = _result()
        consent_window_note._attach(r, None, "start_print", {"printer_name": "garage"})
        assert r.structuredContent[consent_window_note.RESULT_KEY]["id"] == w.id
        out = _tool("consent_window_status")()
        assert [x["id"] for x in out["windows"]] == [w.id] and out["windows"][0]["set_by"] == "account:acct_123"
        assert "Agent page in their Kiln settings" in out["note"]
        out = _tool("revoke_consent_window")(window_id=w.id)
        assert out["success"] and hosted_store.live() == [] and "revoke" in hosted_store.calls

    def test_every_printer_on_hosted_follows_the_accounts_tier(self, hosted_store, business):
        host = _Host(CHOICE_NEXT_TWO_HOURS, where=WHERE_EVERY_PRINTER)
        _obtain("start_print", {"file_name": "jar.stl", "printer_name": "garage"}, host)
        [(_, schema)] = host.asked
        assert FIELD_WHERE in schema["properties"]
        assert hosted_store.live()[0].scope == consent_windows.SCOPE_FLEET

    def test_a_free_account_is_offered_the_window_and_the_door_opens_it(self, monkeypatch):
        """One printer is every tier's: a hosted standing window over one
        printer has no tier gate at grant time (where a print may start
        from — at home, or away through the cloud — is judged at each
        start, by the relay).  Several printers or the fleet stays the
        fleet tier's, judged by the same writer as every door."""
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        try:
            import kiln.licensing as lic
        except ImportError:  # a plain install has no licence module, and reads as free already
            lic = None
        if lic is not None:
            monkeypatch.setattr(lic, "get_tier", lambda: "free", raising=False)
        assert consent_windows._fleet_tier_allows() is False
        store = _FakeStore()
        consent_windows.register_window_store(store)
        try:
            assert server.dialog_offers() == (True, False)
            host = _Host(CHOICE_NEXT_TWO_HOURS)
            _obtain("start_print", {"file_name": "jar.stl", "printer_name": "garage"}, host)
            assert list(host.asked[0][1]["properties"]) == [FIELD_ANSWER, FIELD_FOR_HOW_LONG]
            [w] = store.live()
            assert w.scope == ("garage",) and w.set_by == "account:acct_123"
            with pytest.raises(consent_windows.NotTheFleetTier):
                consent_windows.open_window_from_dialog(
                    DialogAnswer("accept", "", choice=CHOICE_NEXT_TWO_HOURS, where=WHERE_EVERY_PRINTER),
                    printer_name="garage",
                )
            assert store.calls.count("open") == 1
        finally:
            consent_windows.register_window_store(None)

    def test_without_a_store_hosted_is_as_before(self, monkeypatch):
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        assert consent_windows.window_store() is None
        assert server.dialog_offers() == (False, False)
        assert consent_windows.covering("garage") is None
        assert _tool("consent_window_status")()["windows"] == []

    def test_a_store_is_never_consulted_for_the_local_users_windows(self, at_terminal):
        """Locally the file is the store; a hook registered by mistake on a
        laptop must not become a second opinion."""
        store = _FakeStore()
        consent_windows.register_window_store(store)
        consent_windows.open_window(seconds=600, scope=("garage",))
        assert consent_windows.covering("garage") is not None
        assert consent_windows.window_store() is None and store.calls == []

    def test_a_store_that_fails_has_no_window(self, hosted_store, monkeypatch):
        def _boom(_printer):
            raise RuntimeError("db down")

        monkeypatch.setattr(hosted_store, "covering", _boom)
        assert consent_windows.covering("garage") is None

    def test_the_store_contract_every_method_kiln_pro_implements(self):
        """The signatures kiln-pro's implementation is written against."""
        sig = inspect.signature
        store = consent_windows.WindowStore
        assert list(sig(store.covering).parameters) == ["self", "printer_name"]
        assert list(sig(store.live).parameters) == ["self"]
        assert list(sig(store.open).parameters) == ["self", "seconds", "scope", "source"]
        assert list(sig(store.revoke).parameters) == ["self", "window_id"]


# ---------------------------------------------------------------------------
# The one parser every surface uses — pinned for the hosted wire and the app
# ---------------------------------------------------------------------------


class TestTheParserEverySurfaceShares:
    """kiln-pro's hosted 'input required' shape and the desktop app's sheet
    hand the person's filled form to ``answer_from_content`` as a dict.
    What it means there is what it means in the MCP dialog."""

    def test_the_field_names_are_the_contract(self):
        assert (FIELD_ANSWER, FIELD_FOR_HOW_LONG, FIELD_WHERE) == ("answer", "for_how_long", "where")

    @pytest.mark.parametrize("content,expect", [
        ({"answer": "this_print"}, ("accept", "this_print", "", "this_printer")),
        ({"answer": "next_two_hours", "for_how_long": "45m"}, ("accept", "next_two_hours", "45m", "this_printer")),
        ({"answer": "no", "for_how_long": "3h"}, ("decline", "", "", "")),
        ({"answer": "fleet"}, ("unavailable", "", "", "")),
        ({}, ("unavailable", "", "", "")),
        ({"answer": "this_print", "where": "every_printer"}, ("unavailable", "", "", "")),  # fleet not offered
    ])
    def test_a_dict_from_the_wire_means_the_same_as_the_dialog(self, content, expect):
        a = answer_from_content("accept", content, offer_window=True, offer_fleet=False)
        assert (a.action, a.choice, a.typed_duration, a.where) == expect

    def test_where_a_window_is_not_offered_a_length_is_off_the_form(self):
        assert answer_from_content("accept", {"answer": "this_print", "for_how_long": "3h"}, offer_window=False).action == "unavailable"
        assert answer_from_content("accept", {"answer": "next_two_hours"}, offer_window=False).action == "unavailable"
        assert answer_from_content("accept", {"answer": "this_print"}, offer_window=False).accepted

    def test_decline_and_cancel_envelopes(self):
        assert answer_from_content("decline", None).action == "decline"
        assert answer_from_content("cancel", {"answer": "this_print"}).action == "cancel"
        assert answer_from_content("", {"answer": "this_print"}).action == "unavailable"

    def test_the_rest_envelope_is_the_same_form(self):
        """A door with no dialog channel carries the question on the
        refused result; the desktop sheet and the hosted wire read this."""
        from kiln.print_consent import (
            CODE_CONSENT_REQUIRED,
            INPUT_KIND_PRINT_CONSENT,
            INPUT_REQUIRED_KEY,
            INPUT_RESPONSES_HEADER,
            input_required_block,
        )

        assert (INPUT_REQUIRED_KEY, CODE_CONSENT_REQUIRED, INPUT_RESPONSES_HEADER, INPUT_KIND_PRINT_CONSENT) == (
            "input_required", "CONSENT_REQUIRED", "X-Kiln-Input-Responses", "print_consent",
        )
        block = input_required_block(message="Start printing jar.stl on garage?", offer_window=True, offer_fleet=True, request_id="r1")
        assert block == {
            "kind": "print_consent", "request_id": "r1", "message": "Start printing jar.stl on garage?",
            "schema": dialog_schema(offer_window=True, offer_fleet=True),
        }
        # Data only: the properties present are the fields to show, and an
        # absent request id is absent, not an empty string.
        plain = input_required_block(message="m", offer_window=False)
        assert set(plain) == {"kind", "message", "schema"}
        assert list(plain["schema"]["properties"]) == [FIELD_ANSWER]
        json.dumps(block)  # it goes on the wire as JSON

    def test_the_grant_takes_only_an_accepted_answer(self):
        with pytest.raises(ValueError):
            server.consent_from_dialog_answer("start_print", "jar.stl", "garage", DialogAnswer("decline"), aimed="garage")
        with pytest.raises(ValueError):
            server.consent_from_dialog_answer("start_print", "jar.stl", "garage", {"action": "accept"}, aimed="garage")
        assert consent_windows.live_windows() == []

    def test_the_grant_is_what_the_hosted_wire_and_the_app_call(self):
        """A surface that parsed the person's response records the yes
        and the window through the one grant; nothing else to remember."""

        async def _run():
            a = answer_from_content("accept", {"answer": "this_print", "for_how_long": "1h"})
            token = server.consent_from_dialog_answer("start_print", "jar.stl", "garage", a, aimed="garage")
            try:
                return consent_for(file_name="jar.stl", printer_name="garage")
            finally:
                reset_consent(token)

        granted = asyncio.run(_run())
        assert granted is not None and granted.source == SOURCE_ELICITED
        w = _one_window()
        assert w.until - w.set_at == pytest.approx(3600, abs=1) and w.scope == ("garage",)


# ---------------------------------------------------------------------------
# Inside a window nobody is asked — at every door — and the preview is still wanted
# ---------------------------------------------------------------------------


class TestInsideTheWindow:
    def test_nobody_is_asked_and_every_door_starts_on_the_window(self, tmp_path):
        """Before this, a window opened at a terminal did nothing for a
        person in a chat: the dialog still came up for every print."""
        path = _stl(tmp_path / "jar.stl")
        _obtain("start_print", {"file_name": path, "printer_name": "garage"}, _Host(CHOICE_NEXT_TWO_HOURS))
        w = _one_window()
        for tool, arg in server._CONSENT_FILE_ARG.items():
            never = _NeverAsks()
            token, granted = _obtain(
                tool, {arg: path, "printer_name": "garage"}, never,
                observe=lambda: consent_for(file_name=path, printer_name="garage", aimed_at="garage"),
            )
            assert never.asked == [], tool
            assert token is None, tool
            assert granted is not None and granted.source == SOURCE_WINDOW and granted.window_id == w.id, tool
            # The gate every door calls passes on the window — with a preview.
            print_signoff.clear()
            assert server._preview_gate_error(tool, path, None, printer_name="garage") is not None, tool
            assert server._preview_gate_error(tool, path, _token_for(path), printer_name="garage") is None, tool
            cleared = print_signoff.current()
            assert cleared.source == SOURCE_WINDOW and cleared.window_id == w.id, tool

    def test_a_window_for_one_printer_does_not_silence_the_dialog_for_another(self, tmp_path):
        _obtain("start_print", {"file_name": "jar.stl", "printer_name": "garage"}, _Host(CHOICE_NEXT_TWO_HOURS))
        host = _Host(CHOICE_THIS_PRINT)
        _obtain("start_print", {"file_name": "jar.stl", "printer_name": "workshop"}, host)
        assert len(host.asked) == 1

    def test_outside_the_window_the_next_print_asks_again(self, tmp_path, monkeypatch):
        _obtain("start_print", {"file_name": "jar.stl", "printer_name": "garage"}, _Host(CHOICE_NEXT_TWO_HOURS))
        w = _one_window()
        never = _NeverAsks()
        _obtain("start_print", {"file_name": "jar.stl", "printer_name": "garage"}, never)
        assert never.asked == []
        consent_windows.revoke_window(w.id)
        host = _Host(CHOICE_THIS_PRINT)
        _obtain("start_print", {"file_name": "jar.stl", "printer_name": "garage"}, host)
        assert len(host.asked) == 1
        # Run out, same thing.
        _obtain("start_print", {"file_name": "jar.stl", "printer_name": "garage"}, _Host(CHOICE_NEXT_TWO_HOURS))
        real = time.time()
        monkeypatch.setattr(consent_windows, "_now", lambda: real + 7201)
        host = _Host(CHOICE_THIS_PRINT)
        _obtain("start_print", {"file_name": "jar.stl", "printer_name": "garage"}, host)
        assert len(host.asked) == 1


# ---------------------------------------------------------------------------
# Closing it from the same chat, and being reminded it is open
# ---------------------------------------------------------------------------


def _tool(name: str):
    server._ensure_internal_tool_plugins_registered()
    return next(t.fn for t in server.mcp._tool_manager.list_tools() if t.name == name)


class TestRevokeAndStatusInline:
    def test_the_person_closes_the_window_through_the_agent(self, audits):
        _obtain("start_print", {"file_name": "jar.stl", "printer_name": "garage"}, _Host(CHOICE_NEXT_TWO_HOURS))
        w = _one_window()
        out = _tool("revoke_consent_window")(window_id=w.id)
        assert out["success"] and [r["id"] for r in out["revoked"]] == [w.id]
        assert consent_windows.live_windows() == []
        assert any(a == "consent_window_revoked" for _, a, _ in audits)
        # And the next print asks again.
        host = _Host(CHOICE_THIS_PRINT)
        _obtain("start_print", {"file_name": "jar.stl", "printer_name": "garage"}, host)
        assert len(host.asked) == 1

    def test_revoke_by_printer_and_all(self, at_terminal, monkeypatch):
        monkeypatch.setattr(consent_windows, "_fleet_tier_allows", lambda: True)
        consent_windows.open_window(seconds=3600, scope=("garage",))
        consent_windows.open_window(seconds=3600, scope=("workshop",))
        consent_windows.open_window(seconds=3600, scope=consent_windows.SCOPE_FLEET)
        revoke = _tool("revoke_consent_window")
        out = revoke(printer_name="garage")
        # The garage window and the fleet window (it covers garage too).
        assert {r["scope"] for r in out["revoked"]} == {"garage", "the whole fleet"}
        assert [w.scope for w in consent_windows.live_windows()] == [("workshop",)]
        assert revoke(all_windows=True)["revoked"][0]["scope"] == "workshop"
        assert consent_windows.live_windows() == []
        assert revoke()["error"]["code"] == "VALIDATION_ERROR"
        assert revoke(window_id="w_nope")["error"]["code"] == "NOT_FOUND"

    def test_status_names_the_door_each_window_came_through(self, at_terminal):
        consent_windows.open_window(seconds=3600, scope=("workshop",))
        _obtain("start_print", {"file_name": "jar.stl", "printer_name": "garage"}, _Host(CHOICE_NEXT_TWO_HOURS))
        out = _tool("consent_window_status")()
        doors = {r["scope"]: r["opened_via"] for r in out["windows"]}
        assert doors == {"workshop": "terminal", "garage": "host_dialog"}
        assert "revoke_consent_window" in out["note"]
        only = _tool("consent_window_status")(printer_name="garage")["windows"]
        assert [r["scope"] for r in only] == ["garage"]
        from kiln.cli.main import cli

        result = CliRunner().invoke(cli, ["consent", "status"])
        assert result.exit_code == 0, result.output
        assert "via host_dialog" in result.output and "via terminal" in result.output

    def test_the_status_tool_opens_nothing(self):
        """No argument of either tool is a duration, a scope or a yes."""
        for name in ("consent_window_status", "revoke_consent_window"):
            params = set(inspect.signature(_tool(name)).parameters)
            assert not params & {"seconds", "duration", "hours", "scope", "fleet", "for_", "extend"}, name

    def test_every_print_result_names_the_window_while_it_is_open(self, monkeypatch):
        _obtain("start_print", {"file_name": "jar.stl", "printer_name": "garage"}, _Host(CHOICE_NEXT_TWO_HOURS))
        w = _one_window()

        for tool in server._CONSENT_FILE_ARG:
            r = _result()
            consent_window_note._attach(r, None, tool, {"printer_name": "garage"})
            block = r.structuredContent[consent_window_note.RESULT_KEY]
            assert block["id"] == w.id and block["printer"] == "garage", tool
            assert "revoke_consent_window" in block["note"] and w.id in block["note"], tool
            assert block["until"].endswith(time.strftime("%H:%M", time.localtime(w.until)))
            # The result it rode in on is intact.
            assert r.structuredContent["job"] == "j1"
        # Not on a tool that starts nothing; not for a printer it does not cover.
        r = _result()
        consent_window_note._attach(r, None, "printer_status", {"printer_name": "garage"})
        assert r.structuredContent is None
        r = _result()
        consent_window_note._attach(r, None, "start_print", {"printer_name": "workshop"})
        assert r.structuredContent is None
        # An unnamed call is the printer it resolves to.
        _obtain("start_print", {"file_name": "jar.stl"}, _Host(CHOICE_NEXT_TWO_HOURS))
        r = _result()
        consent_window_note._attach(r, None, "start_print", None)
        assert r.structuredContent[consent_window_note.RESULT_KEY]["printer"] == "bench"
        # Closed: gone from the next result.
        consent_windows.revoke_all()
        r = _result()
        consent_window_note._attach(r, None, "start_print", {"printer_name": "garage"})
        assert r.structuredContent is None

    def test_the_line_rides_the_real_result_object(self):
        """Through the lowlevel wrapper, with the request shape the SDK
        hands it — the one attach path measured to work."""
        from kiln.mcp_compat import MCP_SDK_MAJOR, wrap_call_tool_result

        _obtain("start_print", {"file_name": "jar.stl", "printer_name": "garage"}, _Host(CHOICE_NEXT_TWO_HOURS))
        result = types.SimpleNamespace(
            structuredContent={"success": True}, isError=False, content=[],
        )

        async def _handler(*_args):
            return types.SimpleNamespace(root=result)

        params = types.SimpleNamespace(name="start_print", arguments={"printer_name": "garage"})
        if MCP_SDK_MAJOR >= 2:
            entries: dict = {}
            srv = types.SimpleNamespace(
                get_request_handler=lambda m: entries.get(m),
                add_request_handler=lambda m, p, h: entries.__setitem__(
                    m, types.SimpleNamespace(handler=h, params_type=p)
                ),
            )
            srv.add_request_handler("tools/call", object, _handler)
            mcp = types.SimpleNamespace(_lowlevel_server=srv)
            assert wrap_call_tool_result(mcp, consent_window_note._attach)
            asyncio.run(entries["tools/call"].handler(None, params))
        else:
            from mcp.types import CallToolRequest

            srv = types.SimpleNamespace(request_handlers={CallToolRequest: _handler})
            mcp = types.SimpleNamespace(_mcp_server=srv)
            assert wrap_call_tool_result(mcp, consent_window_note._attach)
            asyncio.run(srv.request_handlers[CallToolRequest](types.SimpleNamespace(params=params)))
        assert result.structuredContent[consent_window_note.RESULT_KEY]["printer"] == "garage"

    def test_the_hook_is_installed_at_startup(self):
        src = inspect.getsource(server._start)
        assert "consent_window_note.install(mcp)" in src

    def test_the_cli_says_which_window_a_start_rests_on(self, tmp_path, monkeypatch, at_terminal):
        from kiln.cli.main import cli, cli_gate

        gcode = tmp_path / "part.gcode"
        gcode.write_text("G28\n")
        preview_evidence.record("png", str(gcode), renderer="stage_paint", shown_sha="abc")
        preview_evidence.record_url_refusal(str(gcode), "signed_out")
        token = server.issue_preview_token(str(gcode), door="png")["token"]
        w = consent_windows.open_window(seconds=3600, scope=("garage",))
        monkeypatch.setattr(consent_windows, "person_at_terminal", lambda: False)

        @cli.command("gate_probe")
        def _probe():
            cli_gate("start_print", str(gcode), token, printer_name="garage", json_mode=False)

        result = CliRunner().invoke(cli, ["gate_probe"])
        assert result.exit_code == 0, result.output
        assert w.id in result.output and "kiln consent revoke" in result.output and "terminal" in result.output

    def test_the_cli_json_carries_the_same_block_as_the_mcp_result(self, tmp_path, monkeypatch, at_terminal):
        """Every door: a script reading `kiln print --json` sees the window
        the way an agent reading a tool result does — same keys, the
        command that closes it instead of the tool."""
        from kiln.cli.main import cli
        from tests.test_a_person_says_go import _Printer

        printer = _Printer()
        monkeypatch.setattr("kiln.cli.main._make_adapter", lambda cfg: printer)
        monkeypatch.setattr(
            "kiln.cli.main.load_printer_config",
            lambda *_a, **_k: {"type": "moonraker", "host": "http://t.local", "timeout": 1, "retries": 0},
        )
        monkeypatch.setattr("kiln.cli.main.validate_printer_config", lambda cfg: (True, None))
        monkeypatch.setattr("kiln.cli.print_gate._audit", lambda *a, **k: None)
        gcode = tmp_path / "part.gcode"
        gcode.write_text("G28\n")
        preview_evidence.record("png", str(gcode), renderer="stage_paint", shown_sha="abc")
        preview_evidence.record_url_refusal(str(gcode), "signed_out")
        token = server.issue_preview_token(str(gcode), door="png")["token"]
        w = consent_windows.open_window(seconds=3600, scope=("garage",))
        monkeypatch.setattr(consent_windows, "person_at_terminal", lambda: False)
        result = CliRunner().invoke(cli, ["--printer", "garage", "print", str(gcode), "--json", "--preview-token", token])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        block = data["standing_window"]
        assert block["id"] == w.id and block["printer"] == "garage" and block["opened"] is True
        assert f"kiln consent revoke {w.id}" in block["note"]
        # Same keys as the MCP result's block, and the printer really started.
        assert set(block) == set(consent_window_note.note_for("garage"))
        assert printer.started == ["part.gcode"]


# ---------------------------------------------------------------------------
# A host that cannot ask is named plainly
# ---------------------------------------------------------------------------


class TestAHostThatCannotAsk:
    def test_the_refusal_says_no_dialog_is_coming_and_names_the_terminal_door(self, tmp_path, monkeypatch):
        path = _stl(tmp_path / "jar.stl")
        token = _token_for(path)
        monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: False)

        def _gate_inside():
            assert why_not_asked() == NOT_ASKED_HOST_CANNOT
            return server._preview_gate_error("start_print", path, token, printer_name="garage")

        never = _NeverAsks()
        _, block = _obtain("start_print", {"file_name": path, "printer_name": "garage"}, never, observe=_gate_inside)
        assert never.asked == []
        message = block["error"]["message"]
        assert "cannot show an approval dialog" in message
        assert "no dialog is coming" in message
        assert "kiln consent window --for 2h --printer garage" in message
        assert "Kiln cannot open a window from here" in message
        # Outside that call the note is gone, and the generic refusal is back.
        assert why_not_asked() == ""
        generic = server._preview_gate_error("start_print", path, token, printer_name="garage")
        assert "cannot show an approval dialog" not in generic["error"]["message"]

    def test_the_ci_bypass_and_a_window_still_leave_nothing_to_say(self, tmp_path, monkeypatch, at_terminal):
        path = _stl(tmp_path / "jar.stl")
        monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: False)
        consent_windows.open_window(seconds=3600, scope=("garage",))
        never = _NeverAsks()
        _, reason = _obtain(
            "start_print", {"file_name": path, "printer_name": "garage"}, never, observe=why_not_asked,
        )
        assert never.asked == []
        assert reason == ""


# ---------------------------------------------------------------------------
# Every surface that reports safety state says whether a window is open
# ---------------------------------------------------------------------------


class TestTheWindowIsNeverInvisible:
    def test_the_agent_is_told_the_rules_on_connect(self, monkeypatch):
        monkeypatch.setattr(server, "_get_registry", lambda: types.SimpleNamespace(list_names=lambda: ["garage"]))
        text = server._build_instructions()
        block = text[text.index("CONSENT:"):]
        for phrase in ("you never answer it", "revoke_consent_window", "consent_window_status",
                       "Nothing you can call opens or extends one", "kiln consent window --for 2h"):
            assert phrase in block, phrase

    def test_safety_status_names_an_open_window(self, at_terminal):
        out = _tool("safety_status")()
        assert out["standing_windows"] == [] and "every print asks" in out["summary"]
        w = consent_windows.open_window(seconds=3600, scope=("garage",))
        out = _tool("safety_status")()
        assert [x["id"] for x in out["standing_windows"]] == [w.id]
        assert w.id in out["summary"] and "revoke_consent_window" in out["summary"]

    def test_kiln_doctor_names_an_open_window(self, at_terminal, monkeypatch):
        from kiln.cli.main import cli

        w = consent_windows.open_window(seconds=3600, scope=("garage",))
        result = CliRunner().invoke(cli, ["doctor", "--json"])
        assert result.exit_code in (0, 1), result.output
        checks = {c["name"]: c for c in json.loads(result.output)["checks"]}
        line = checks["standing_consent_windows"]
        assert line["ok"] is True and w.id in line["detail"] and "kiln consent revoke" in line["detail"]

    def test_the_question_names_the_default_printer_and_the_right_way_to_close(self, monkeypatch):
        host = _Host(CHOICE_THIS_PRINT)
        monkeypatch.setattr(server, "_resolve_effective_printer_name", lambda name=None: name or "default")
        _obtain("start_print", {"file_name": "jar.stl"}, host)
        [(message, _)] = host.asked
        assert "start on the default printer" in message and "kiln consent revoke" in message
        # On the hosted server the person has no terminal: the only close is to say so.
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        consent_windows.register_window_store(_FakeStore())
        try:
            host = _Host(CHOICE_THIS_PRINT)
            _obtain("start_print", {"file_name": "jar.stl", "printer_name": "garage"}, host)
            [(message, _)] = host.asked
            assert "telling your assistant" in message and "kiln consent" not in message
        finally:
            consent_windows.register_window_store(None)

    def test_the_hosted_refusal_never_sends_the_person_to_a_terminal(self, tmp_path, monkeypatch):
        path = _stl(tmp_path / "jar.stl")
        preview = _token_for(path)
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        block = server._preview_gate_error("start_print", path, preview, printer_name="garage")
        message = block["error"]["message"]
        # The account's doors — Approve on the print page, or a delegation
        # (in this app's dialog, or the Agent page in Settings) — never a terminal.
        assert "kiln consent window" not in message and "kiln print" not in message
        assert "signed-in person" in message and "delegation" in message
        assert "approval dialog" in message and "Tell the person that plainly" in message

    def test_the_status_tool_note_fits_the_surface(self, at_terminal, monkeypatch):
        consent_windows.open_window(seconds=3600, scope=("garage",))
        assert "kiln consent window" in _tool("consent_window_status")()["note"]
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        store = _FakeStore()
        consent_windows.register_window_store(store)
        try:
            store.open(seconds=3600, scope=("garage",), source=consent_windows.SOURCE_WEB)
            note = _tool("consent_window_status")()["note"]
            assert "Agent page in their Kiln settings" in note and "kiln consent window" not in note
            assert _tool("consent_window_status")()["windows"][0]["opened_via"] == "web"
        finally:
            consent_windows.register_window_store(None)


# ---------------------------------------------------------------------------
# The agent holds no door — pinned in the source and in the SDK
# ---------------------------------------------------------------------------

_SRC = pathlib.Path(server.__file__).parent
_MODULES = [*sorted(_SRC.glob("*.py")), *sorted((_SRC / "plugins").glob("*.py")), *sorted((_SRC / "cli").glob("*.py"))]

_OPENERS = ("open_window", "open_window_from_dialog", "extend_window", "_open_window")


def _innermost_owner(tree: ast.AST, node: ast.AST) -> str | None:
    holders = [
        f for f in ast.walk(tree)
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
        and f.lineno <= node.lineno <= (f.end_lineno or 0)
    ]
    return min(holders, key=lambda f: (f.end_lineno or 0) - f.lineno).name if holders else None


def _callers_of(names: set[str], modules=_MODULES) -> dict[str, set[str]]:
    """``{callee: {module::function}}`` for every call by one of *names*."""
    found: dict[str, set[str]] = {n: set() for n in names}
    for path in modules:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name in names:
                found[name].add(f"{path.name}::{_innermost_owner(tree, node)}")
    return found


def _is_tool_decorator(d: ast.expr) -> bool:
    target = d.func if isinstance(d, ast.Call) else d
    return isinstance(target, ast.Attribute) and target.attr == "tool"


def test_no_tool_opens_or_extends_a_window_or_answers_the_dialog():
    """Every ``@mcp.tool()`` body, in server.py and every plugin: none
    reaches an opener, the dialog, or the consent record.  The agent's
    channel is tools/call; if no tool does it, the agent cannot."""
    offenders: list[str] = []
    forbidden = {
        *_OPENERS, "ask_user_to_confirm", "set_consent", "DialogAnswer", "_obtain_print_consent",
        "consent_from_dialog_answer", "answer_from_content", "register_window_store",
    }
    for path in _MODULES:
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(_is_tool_decorator(d) for d in fn.decorator_list):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                    if name in forbidden:
                        offenders.append(f"{path.name}::{fn.name} -> {name}")
    assert offenders == [], offenders


def test_the_doors_are_held_by_exactly_the_functions_that_should_hold_them():
    callers = _callers_of({*_OPENERS, "ask_user_to_confirm", "_obtain_print_consent", "DialogAnswer", "consent_from_dialog_answer"})
    # The terminal door: the command, and nothing else.
    assert callers["open_window"] == {"consent_commands.py::window"}
    assert callers["extend_window"] == {"consent_commands.py::extend"}
    # The dialog door: the asker's helper, and nothing else.
    assert callers["open_window_from_dialog"] == {"server.py::_open_window_from_answer"}
    assert callers["_open_window"] == {"consent_windows.py::open_window", "consent_windows.py::open_window_from_dialog"}
    # The dialog itself: asked by the asker, which is called by the wrapper.
    assert callers["ask_user_to_confirm"] == {"server.py::_obtain_print_consent"}
    assert callers["_obtain_print_consent"] == {"server.py::_call_tool_with_context"}
    # An answer is built by the one parser — from what a host, the hosted
    # wire or a native sheet handed back — and by the shim only for its own
    # could-not-ask outcomes.  Nowhere else, and never in a tool.
    assert callers["DialogAnswer"] == {"print_consent.py::answer_from_content", "mcp_compat.py::ask_user_to_confirm"}
    # The grant every surface calls is called by the MCP asker alone in this repo.
    assert callers["consent_from_dialog_answer"] == {"server.py::_obtain_print_consent"}


def test_a_flag_or_variable_still_does_not_open_one():
    import re

    src = pathlib.Path(consent_windows.__file__).read_text()
    assert set(re.findall(r"environ(?:\.get)?\s*[\[(]\s*[\"']([A-Z_]+)", src)) <= {"KILN_HOME"}
    from kiln.cli.consent_commands import window

    assert not {"yes", "force", "no-tty", "dialog"} & {p.name for p in window.params}


def test_the_answer_travels_the_elicitation_channel_not_the_tool_channel():
    """From the installed SDK: the dialog is a request the SERVER sends
    to the client (``elicitation/create`` is a ServerRequest, never a
    ClientRequest), and the shim asks through ``ctx.elicit``.  The agent
    speaks ``tools/call``; it cannot send the response to a request the
    client received."""
    from mcp import types as mcp_types

    from kiln import mcp_compat

    def members(union) -> set[str]:
        ann = getattr(getattr(union, "model_fields", {}).get("root"), "annotation", union)
        return {getattr(a, "__name__", str(a)) for a in typing.get_args(ann)}

    assert "ElicitRequest" in members(mcp_types.ServerRequest)
    assert "ElicitRequest" not in members(mcp_types.ClientRequest)
    assert "CallToolRequest" in members(mcp_types.ClientRequest)
    assert mcp_types.ElicitRequest.model_fields["method"].default == "elicitation/create"
    src = inspect.getsource(mcp_compat.ask_user_to_confirm)
    assert "ctx.elicit(" in src
    # And no Kiln tool is named for it.
    server._ensure_internal_tool_plugins_registered()
    registered = {t.name for t in server.mcp._tool_manager.list_tools()}
    assert not {n for n in registered if "elicit" in n or "open_window" in n or "consent_window" in n} - {
        "consent_window_status", "revoke_consent_window",
    }


def test_old_records_read_as_terminal_windows():
    """A window file written before the dialog door existed has no
    ``source``; it came through a terminal, and reads as one."""
    path = consent_windows._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"windows": [{
        "id": "w_old", "set_by": "os_user:x", "set_at": time.time(), "until": time.time() + 600, "scope": ["garage"],
    }, {
        "id": "w_odd", "set_by": "os_user:x", "set_at": time.time(), "until": time.time() + 600, "scope": ["garage"],
        "source": "agent_tool",
    }]}))
    sources = {w.id: w.source for w in consent_windows.all_windows()}
    assert sources == {"w_old": SOURCE_TERMINAL, "w_odd": SOURCE_TERMINAL}
