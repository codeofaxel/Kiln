"""When the host cannot draw the approval dialog, Kiln shows a code on the
machine's own screen and the person types it in the chat.

The threat these pin: the agent is the one relaying the words, so nothing
the agent can call may reveal the code, a guess must cost, an answer that
lands before a person could have read the banner is no answer, and a host
whose own hooks answer dialogs is not a host that asked anyone.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
import subprocess
import sys
import time
import types

import pytest

from kiln import consent_windows, mcp_compat, preview_evidence, print_consent, print_signoff, screen_code, server
from kiln.preview_gate import PreviewGate
from kiln.print_consent import (
    CHOICE_THIS_PRINT,
    FIELD_ANSWER,
    NOT_ASKED_CODE_SHOWN,
    NOT_ASKED_HOST_CANNOT,
    SOURCE_CODE,
    consent_for,
    grade_of,
    reset_consent,
    why_not_asked,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    monkeypatch.setenv("KILN_EMERGENCY_PERSIST", "0")
    preview_evidence._reset_for_tests()
    print_signoff._reset_for_tests()
    print_consent._reset_for_tests()
    consent_windows._reset_for_tests()
    screen_code._reset_for_tests()
    import kiln.preview_gate as pg

    monkeypatch.setattr(pg, "_gate", PreviewGate())
    monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: False)
    monkeypatch.setattr(server, "_resolve_effective_printer_name", lambda name=None: name or "bench")
    monkeypatch.setattr(consent_windows, "person_at_terminal", lambda: False)
    monkeypatch.setattr(consent_windows, "_fleet_tier_allows", lambda: False)
    monkeypatch.setattr(consent_windows, "_path", lambda: tmp_path / "consent_windows.json")
    monkeypatch.setattr(screen_code, "screen_available", lambda: True)
    monkeypatch.setattr(screen_code, "MIN_READ_S", 0.0)
    # Never a real banner from a test: a test that wants to see one installs
    # the ``banners`` fixture, which replaces this.
    monkeypatch.setattr(screen_code, "_show_hook", lambda issued: False)
    yield
    screen_code._reset_for_tests()
    preview_evidence._reset_for_tests()
    print_signoff._reset_for_tests()
    print_consent._reset_for_tests()
    consent_windows._reset_for_tests()


@pytest.fixture
def banners(monkeypatch):
    """The screen: records every banner, and hands the test the code the
    way a person's eyes would."""
    shown: list[screen_code.Issued] = []

    def show(issued):
        shown.append(issued)
        return True

    monkeypatch.setattr(screen_code, "_show_hook", show)
    return shown


@pytest.fixture
def audits(monkeypatch):
    seen: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(server, "_audit", lambda tool, action, details=None: seen.append((tool, action, details or {})))
    return seen


def _ask(tool="start_print", file_name="benchy.3mf", printer_name="bench", ctx=None):
    """One start attempt, judged inside its own call the way the wrapper's
    gate reads it: what was recorded, the yes the gate sees, the refusal it
    would give.  The context the answer lives in ends with the call."""

    async def run():
        token = await server._obtain_print_consent(
            tool, {"file_name": file_name, "printer_name": printer_name}, ctx or types.SimpleNamespace(),
        )
        try:
            return types.SimpleNamespace(
                why=why_not_asked(),
                consent=consent_for(file_name=file_name, printer_name=printer_name),
                text=server._no_yes_message(tool, file_name, printer_name),
                block=server._preview_gate_error(tool, file_name, None, printer_name=printer_name),
            )
        finally:
            reset_consent(token)

    return asyncio.run(run())


def _give(words):
    fn = server.mcp._tool_manager._tools["give_print_code"].fn
    return fn(words=words)


# ---------------------------------------------------------------------------
# The door opens where the dialog cannot
# ---------------------------------------------------------------------------


class TestTheCodeIsOffered:
    def test_a_host_that_cannot_ask_gets_a_code_on_the_screen(self, banners):
        r = _ask()
        assert r.why.startswith(NOT_ASKED_CODE_SHOWN) and r.consent is None
        assert len(banners) == 1
        assert banners[0].file_name == "benchy.3mf" and banners[0].printer_name == "bench"
        assert re.fullmatch(r"\d{4}", banners[0].code)

    def test_the_refusal_says_a_code_was_shown_and_never_contains_it(self, banners):
        r = _ask()
        assert "notification" in r.text and "give_print_code" in r.text
        assert banners[0].code not in r.text
        assert "kiln print benchy.3mf" in r.text  # the terminal is still named

    def test_no_screen_means_the_old_terminal_refusal(self, monkeypatch):
        monkeypatch.setattr(screen_code, "screen_available", lambda: False)
        r = _ask()
        assert r.why == NOT_ASKED_HOST_CANNOT
        assert "no dialog is coming" in r.text and "notification" not in r.text

    def test_a_failed_banner_is_not_a_shown_banner(self, monkeypatch):
        monkeypatch.setattr(screen_code, "_show_hook", lambda issued: False)
        r = _ask()
        assert r.why == NOT_ASKED_HOST_CANNOT
        assert screen_code.status()["live"] == []

    def test_asking_twice_shows_the_same_code_not_a_second_one(self, banners):
        _ask()
        _ask()
        assert len(screen_code.status()["live"]) == 1

    def test_the_banner_words_carry_the_code_once_and_the_three_answers(self, banners):
        _ask()
        title, subtitle, message = screen_code.banner_text(banners[0])
        words = " ".join((title, subtitle, message))
        assert title.startswith("Kiln") and "benchy" in subtitle and "bench" in subtitle
        assert words.count(banners[0].code) == 1 and "2h" in message and "today" in message

    def test_no_tool_result_or_status_line_carries_a_live_code(self, banners):
        _ask()
        code = banners[0].code
        assert code not in json.dumps(screen_code.status())
        assert code not in json.dumps(server.mcp._tool_manager._tools["consent_window_status"].fn())


# ---------------------------------------------------------------------------
# The person's words
# ---------------------------------------------------------------------------


class TestGivingTheCode:
    def test_the_right_code_is_the_yes_for_the_next_start(self, banners, audits):
        _ask()
        out = _give(banners[0].code)
        assert out["success"] and out["approved"]["choice"] == CHOICE_THIS_PRINT
        r = _ask()
        assert r.consent is not None and r.consent.source == SOURCE_CODE and grade_of(r.consent.source) == "B"
        assert r.consent.door == ""  # a code shows nothing: the preview is still the token's to prove
        granted = [d for _, a, d in audits if a == "consent_granted"]
        assert granted and granted[0]["words"] == banners[0].code and granted[0]["door"] == "screen_code"
        assert granted[0]["code_shown_at"] <= granted[0]["answered_at"]

    def test_a_yes_is_spent_by_one_start(self, banners):
        _ask()
        _give(banners[0].code)
        assert _ask().consent is not None  # spent here
        r = _ask()
        assert r.consent is None and r.why.startswith(NOT_ASKED_CODE_SHOWN)  # a new banner

    def test_a_yes_covers_the_print_it_was_shown_for_not_another(self, banners):
        _ask()
        _give(banners[0].code)
        assert _ask(file_name="other.3mf").consent is None

    def test_a_wrong_code_counts_and_three_void_everything(self, banners):
        _ask()
        wrong = "0000" if banners[0].code != "0000" else "0001"
        assert _give(wrong)["error"]["code"] == "WRONG_CODE"
        assert _give(wrong)["error"]["code"] == "WRONG_CODE"
        assert _give(wrong)["error"]["code"] == "CODES_VOIDED"
        assert _give(banners[0].code)["error"]["code"] == "NO_CODE"  # voided: the real code is gone too
        r = _ask()
        assert r.why.startswith(print_consent.NOT_ASKED_CODE_COOLDOWN)
        assert "seconds" in r.text and len(banners) == 1  # no new banner during the wait

    def test_an_answer_faster_than_a_person_reads_is_refused_not_counted(self, banners, monkeypatch):
        monkeypatch.setattr(screen_code, "MIN_READ_S", 60.0)
        _ask()
        assert _give(banners[0].code)["error"]["code"] == "TOO_FAST"
        monkeypatch.setattr(screen_code, "MIN_READ_S", 0.0)
        assert _give(banners[0].code)["success"]  # the same code still stands: nothing was counted

    def test_an_expired_code_is_no_code(self, banners, monkeypatch):
        _ask()
        code = banners[0].code
        monkeypatch.setattr(screen_code.time, "time", lambda: 4_000_000_000.0)
        assert _give(code)["error"]["code"] == "NO_CODE"

    def test_two_hours_opens_a_window_on_the_one_printer(self, banners):
        _ask()
        out = _give(f"{banners[0].code} 2h")
        assert out["success"] and out["standing_window"]["opens"] == "when the print is started"
        assert consent_windows.covering("bench") is None  # no tool opens a window
        assert _ask().consent is not None  # the start does, where the dialog's opens
        w = consent_windows.covering("bench")
        assert w is not None and w.source == SOURCE_CODE and 7000 < w.until - time.time() <= 7200
        assert consent_windows.covering("other") is None

    def test_today_and_a_typed_length_are_the_dialogs_own_answers(self, banners):
        _ask()
        assert _give(f"{banners[0].code} today")["standing_window"]["for"] == "the rest of today on this printer"
        _ask()
        assert consent_windows.covering("bench").source == SOURCE_CODE
        _ask(file_name="other.3mf", printer_name="other")  # a second printer: the first now has a window
        out = _give(f"{banners[1].code} 45m")
        assert out["success"] and out["standing_window"]["for"] == "45m on this printer"
        _ask(file_name="other.3mf", printer_name="other")
        assert 2600 < consent_windows.covering("other").until - time.time() <= 2700

    def test_every_printer_is_not_a_code_answer(self, banners):
        _ask()
        out = _give(f"{banners[0].code} all")
        assert out["error"]["code"] == "NOT_HERE" and consent_windows.covering("bench") is None
        assert screen_code.status()["live"]  # nothing spent, nothing counted

    def test_words_that_are_not_a_code(self, banners):
        _ask()
        assert _give("yes")["error"]["code"] == "VALIDATION_ERROR"
        assert _give("")["error"]["code"] == "VALIDATION_ERROR"

    def test_the_hosted_server_has_no_screen(self, monkeypatch, banners):
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        assert _give("1234")["error"]["code"] == "NOT_HERE"


# ---------------------------------------------------------------------------
# The dialog, hardened
# ---------------------------------------------------------------------------


class _Host:
    def __init__(self, delay: float):
        self.delay, self.asked = delay, []

    async def elicit(self, message, schema):
        self.asked.append(message)
        await asyncio.sleep(self.delay)
        return types.SimpleNamespace(action="accept", data=types.SimpleNamespace(**{FIELD_ANSWER: CHOICE_THIS_PRINT}))


class TestTheDialogHardened:
    def test_an_instant_yes_is_asked_again_and_a_second_is_no_answer(self, monkeypatch):
        monkeypatch.setenv("KILN_DIALOG_MIN_READ_S", "0.5")
        host = _Host(delay=0.0)
        answer = asyncio.run(mcp_compat.ask_user_to_confirm(host, "Print benchy?"))
        assert answer.action == "unavailable" and answer.detail == "answered_too_fast"
        assert len(host.asked) == 2 and host.asked[1].startswith("(Asked again")

    def test_a_yes_that_took_a_moment_stands(self, monkeypatch):
        monkeypatch.setenv("KILN_DIALOG_MIN_READ_S", "0.01")
        host = _Host(delay=0.03)
        answer = asyncio.run(mcp_compat.ask_user_to_confirm(host, "Print benchy?"))
        assert answer.accepted and len(host.asked) == 1

    def test_an_unanswered_dialog_falls_to_the_code(self, monkeypatch, banners):
        monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: True)
        monkeypatch.setenv("KILN_DIALOG_MIN_READ_S", "0.5")
        monkeypatch.setattr(server, "ask_user_to_confirm", mcp_compat.ask_user_to_confirm)
        r = _ask(ctx=_Host(delay=0.0))
        assert r.why.startswith(NOT_ASKED_CODE_SHOWN) and len(banners) == 1

    def test_a_host_whose_hooks_answer_dialogs_is_not_asked(self, monkeypatch, tmp_path, banners, audits):
        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"hooks": {"Elicitation": [{"matcher": "kiln", "hooks": [{"type": "command", "command": "echo"}]}]}}))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: True)
        monkeypatch.setattr(server, "_host_client_name", lambda ctx: "claude-code")
        asked = []

        async def never(ctx, message, **kw):
            asked.append(message)
            return print_consent.DialogAnswer("accept", choice=CHOICE_THIS_PRINT)

        monkeypatch.setattr(server, "ask_user_to_confirm", never)
        r = _ask()
        assert asked == [] and len(banners) == 1
        assert r.why.startswith(NOT_ASKED_CODE_SHOWN) and ":hook=" in r.why
        assert "hooks answer its dialogs" in r.text and str(settings) in r.text
        assert any(a == "consent_dialog_skipped" for _, a, _ in audits)

    def test_a_hook_for_another_server_does_not_count(self, tmp_path):
        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"hooks": {"Elicitation": [{"matcher": "^github$", "hooks": []}]}}))
        assert screen_code.host_dialog_hook("claude-code", cwd=str(tmp_path)) is None
        settings.write_text(json.dumps({"hooks": {"Elicitation": [{"matcher": "", "hooks": []}]}}))
        assert screen_code.host_dialog_hook("claude-code", cwd=str(tmp_path)) == str(settings)
        assert screen_code.host_dialog_hook("cursor", cwd=str(tmp_path)) is None


# ---------------------------------------------------------------------------
# The gate reads the same yes
# ---------------------------------------------------------------------------


def test_the_preview_gate_takes_a_code_yes_like_a_terminals(banners):
    _ask()
    _give(banners[0].code)
    r = _ask()
    # A yes with no preview is refused for the preview, never for the yes.
    assert r.consent is not None and r.block is not None
    assert "nobody said go" not in json.dumps(r.block) and "preview" in json.dumps(r.block).lower()


# ---------------------------------------------------------------------------
# The banner, read by a person (live test 2026-09-25: the code sat mid-sentence
# and was cut off, the printer read "default", and the file kept its extension)
# ---------------------------------------------------------------------------


class TestTheBannerReadsForAPerson:
    def test_the_code_is_in_the_title_so_a_cut_off_banner_still_shows_it(self, banners, monkeypatch):
        monkeypatch.setattr(server, "_resolve_printer_model_live", lambda name=None: "bambu_a1")
        _ask(printer_name="default")
        title, _subtitle, _message = screen_code.banner_text(banners[0])
        assert banners[0].code in title

    def test_the_default_printer_is_named_by_its_model_not_by_kilns_alias(self, banners, monkeypatch):
        monkeypatch.setattr(server, "_resolve_printer_model_live", lambda name=None: "bambu_a1")
        _ask(printer_name="default")
        _title, subtitle, _message = screen_code.banner_text(banners[0])
        assert "default" not in subtitle and "Bambu Lab A1" in subtitle

    def test_a_printer_the_person_named_keeps_that_name(self, banners, monkeypatch):
        monkeypatch.setattr(server, "_resolve_printer_model_live", lambda name=None: "bambu_a1")
        _ask(printer_name="garage")
        _title, subtitle, _message = screen_code.banner_text(banners[0])
        assert "garage" in subtitle

    def test_an_unknown_model_is_your_printer(self, banners, monkeypatch):
        monkeypatch.setattr(server, "_resolve_printer_model_live", lambda name=None: "")
        _ask(printer_name="default")
        _title, subtitle, _message = screen_code.banner_text(banners[0])
        assert "your printer" in subtitle and "default" not in subtitle

    def test_the_file_is_named_without_its_extension(self, banners):
        _ask()
        _title, subtitle, _message = screen_code.banner_text(banners[0])
        assert "benchy" in subtitle and ".3mf" not in subtitle


def test_every_door_a_window_opens_through_has_its_own_label():
    """A window opened by a typed code once read "opened via terminal" —
    every door that was not the dialog or the web fell into that name."""
    from kiln.consent_windows import SOURCE_DOORS, Window, describe

    now = time.time()
    labels = {
        source: describe(Window(id="w_t", set_by="os_user:t", set_at=now, until=now + 60, scope=("bench",), source=source))["opened_via"]
        for source in SOURCE_DOORS
    }
    assert len(set(labels.values())) == len(SOURCE_DOORS), labels
    assert labels[SOURCE_CODE] == "screen_code"


def test_the_desktop_apps_code_tab_reads_the_same_hooks(tmp_path):
    """The desktop app's Code tab runs Claude Code under its own client name
    (``local-agent-mode-kiln``, read off a live audit line) and honours the
    same settings files, so its hooks are checked too."""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"hooks": {"Elicitation": [{"matcher": "", "hooks": []}]}}))
    assert screen_code.host_dialog_hook("local-agent-mode-kiln", cwd=str(tmp_path)) == str(settings)


# ---------------------------------------------------------------------------
# The code never rides a command line
# ---------------------------------------------------------------------------


def _issued(code="4821"):
    now = time.time()
    return screen_code.Issued(
        code=code, tool="start_print", file_name="benchy.3mf", file_sha256="", printer_name="bench",
        host="", issued_at=now, issued_mono=time.monotonic(), expires_at=now + 600, shown_at=now,
    )


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_the_code_never_rides_a_command_line(monkeypatch, platform):
    """Any program on the machine can list every process's arguments, so a
    code handed to the notifier as an argument is readable by the agent it
    exists to keep out.  It travels on the notifier's standard input."""
    seen = []

    def fake_run(argv, **kw):
        seen.append((list(argv), kw.get("input")))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(screen_code, "_show_hook", None)
    monkeypatch.setattr(screen_code.sys, "platform", platform)
    monkeypatch.setattr(screen_code.subprocess, "run", fake_run)
    monkeypatch.setattr(screen_code, "_show_as_kiln", lambda issued: False)  # the fallback route
    assert screen_code._show(_issued("4821"))
    argv, stdin = seen[-1]
    assert not any("4821" in str(a) for a in argv), argv
    assert "4821" in (stdin or "")



# ---------------------------------------------------------------------------
# On a Mac, the banner comes from Kiln (the helper app), asked for once
# ---------------------------------------------------------------------------


class _FakeHelper:
    """The helper's three verbs, answered from a script, with every call kept."""

    def __init__(self, status="authorized", request_answer="authorized", request_hangs=False, post_code=0):
        self.status, self.request_answer = status, request_answer
        self.request_hangs, self.post_code = request_hangs, post_code
        self.calls: list[tuple[list[str], str | None]] = []

    def run(self, argv, **kw):
        self.calls.append((list(argv), kw.get("input")))
        verb = argv[1] if len(argv) > 1 else ""
        if verb == "status":
            return types.SimpleNamespace(returncode=0, stdout=self.status + "\n")
        if verb == "post":
            return types.SimpleNamespace(returncode=self.post_code, stdout="")
        # anything else is the fallback notifier
        return types.SimpleNamespace(returncode=0, stdout="")

    def popen(self, argv, **kw):
        helper = self
        self.calls.append((list(argv), None))

        class _Ask:
            killed = False

            def communicate(self, timeout=None):
                if helper.request_hangs:
                    raise screen_code.subprocess.TimeoutExpired(argv, timeout)
                helper.status = helper.request_answer
                return helper.request_answer + "\n", ""

            def kill(self):
                self.killed = True

        return _Ask()

    def verbs(self):
        return [argv[1] if argv[0].endswith("kiln-notifier") else argv[0] for argv, _ in self.calls]


@pytest.fixture
def mac_helper(monkeypatch, tmp_path):
    def make(**kw):
        helper = _FakeHelper(**kw)
        exe = tmp_path / "Kiln.app" / "Contents" / "MacOS" / "kiln-notifier"
        monkeypatch.setattr(screen_code, "_show_hook", None)
        monkeypatch.setattr(screen_code.sys, "platform", "darwin")
        monkeypatch.setattr(screen_code, "_notifier_path", lambda: exe)
        monkeypatch.setattr(screen_code.subprocess, "run", helper.run)
        monkeypatch.setattr(screen_code.subprocess, "Popen", helper.popen)
        return helper

    return make


class TestTheBannerComesFromKiln:
    def test_allowed_posts_as_kiln_with_the_words_on_standard_input(self, mac_helper):
        helper = mac_helper(status="authorized")
        assert screen_code._show(_issued("4821"))
        assert helper.verbs() == ["status", "post"]
        argv, stdin = helper.calls[-1]
        assert not any("4821" in a for a in argv)
        words = json.loads(stdin)
        assert "4821" in words["title"] and set(words) == {"title", "subtitle", "body"}

    def test_the_first_time_kiln_asks_and_then_posts(self, mac_helper):
        helper = mac_helper(status="not_determined", request_answer="authorized")
        assert screen_code._show(_issued())
        assert helper.verbs() == ["status", "request", "post"]
        assert screen_code.last_kiln_status() == screen_code.KILN_ALLOWED

    def test_an_unanswered_ask_leaves_the_code_to_the_old_route_and_keeps_asking(self, mac_helper):
        helper = mac_helper(status="not_determined", request_hangs=True)
        assert screen_code._show(_issued())
        assert helper.verbs() == ["status", "request", "osascript"]
        assert screen_code.last_kiln_status() == screen_code.KILN_NOT_ASKED

    def test_kiln_turned_off_falls_back_and_is_remembered(self, mac_helper):
        helper = mac_helper(status="denied")
        assert screen_code._show(_issued())
        assert helper.verbs() == ["status", "osascript"]
        assert screen_code.last_kiln_status() == screen_code.KILN_OFF

    def test_a_post_the_helper_refuses_falls_back(self, mac_helper):
        helper = mac_helper(status="authorized", post_code=3)
        assert screen_code._show(_issued())
        assert helper.verbs() == ["status", "post", "osascript"]

    def test_no_helper_is_the_old_route(self, monkeypatch):
        seen = []
        monkeypatch.setattr(screen_code, "_show_hook", None)
        monkeypatch.setattr(screen_code.sys, "platform", "darwin")
        monkeypatch.setattr(screen_code, "_notifier_path", lambda: None)
        monkeypatch.setattr(screen_code.subprocess, "run", lambda argv, **kw: seen.append(argv) or types.SimpleNamespace(returncode=0))
        assert screen_code._show(_issued())
        assert [a[0] for a in seen] == ["osascript"]

    def test_the_refusal_names_the_switch_when_kiln_is_turned_off(self, mac_helper, monkeypatch):
        mac_helper(status="denied")
        monkeypatch.setattr(screen_code, "_show_hook", None)
        r = _ask()
        assert r.why.startswith(NOT_ASKED_CODE_SHOWN)
        assert "Notifications from Kiln are turned off" in r.text and "System Settings" in r.text


def _packaged_app(root, version):
    app = root / "Kiln.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "kiln-notifier").write_text("#!/bin/sh\n")
    import plistlib

    with open(app / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump({"CFBundleVersion": version}, fh)
    return app


class TestTheHelperIsInstalledOnce:
    def test_installed_from_the_package_into_kilns_home(self, monkeypatch, tmp_path):
        monkeypatch.setattr(screen_code.sys, "platform", "darwin")
        monkeypatch.setattr(screen_code, "_PACKAGED_APP", _packaged_app(tmp_path / "pkg", "1"))
        monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
        exe = screen_code._notifier_path()
        assert exe == tmp_path / "home" / "notifier" / "Kiln.app" / "Contents" / "MacOS" / "kiln-notifier"
        assert exe.is_file() and exe.stat().st_mode & 0o111

    def test_a_newer_helper_replaces_the_installed_one_and_the_same_one_is_left_alone(self, monkeypatch, tmp_path):
        monkeypatch.setattr(screen_code.sys, "platform", "darwin")
        monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
        monkeypatch.setattr(screen_code, "_PACKAGED_APP", _packaged_app(tmp_path / "v1", "1"))
        exe = screen_code._notifier_path()
        marker = exe.parent.parent / "marker"
        marker.write_text("x")
        assert screen_code._notifier_path() == exe and marker.exists()  # same version: untouched
        monkeypatch.setattr(screen_code, "_PACKAGED_APP", _packaged_app(tmp_path / "v2", "2"))
        screen_code._notifier_path()
        assert not marker.exists()  # a newer helper was copied over it

    def test_not_a_mac_has_no_helper(self, monkeypatch):
        monkeypatch.setattr(screen_code.sys, "platform", "win32")
        assert screen_code._notifier_path() is None


# ---------------------------------------------------------------------------
# On Windows, the toast comes from Kiln too — no prompt, nothing to install
# ---------------------------------------------------------------------------

_PS_QUOTES = "'\u2018\u2019\u201a\u201b"


def _powershell_code_outside_strings(script: str) -> str:
    """The script with every single-quoted literal removed, doubled quotes
    honoured: what PowerShell would actually run as code."""
    code, i, n = [], 0, len(script)
    while i < n:
        ch = script[i]
        if ch in _PS_QUOTES:
            i += 1
            while i < n:
                if script[i] in _PS_QUOTES and i + 1 < n and script[i + 1] in _PS_QUOTES:
                    i += 2
                    continue
                if script[i] in _PS_QUOTES:
                    i += 1
                    break
                i += 1
            code.append("''")
            continue
        code.append(ch)
        i += 1
    return "".join(code)


def _windows_script(monkeypatch, file_name="benchy.3mf", printer="bench"):
    monkeypatch.setattr(screen_code.sys, "platform", "win32")
    monkeypatch.setattr(screen_code, "_windows_icon", lambda: "C:\\Users\\a\\.kiln\\notifier\\Kiln.png")
    issued = _issued("4821")
    issued.file_name, issued.printer_name = file_name, printer
    argv, script = screen_code._show_command(issued)
    return argv, script


class TestTheWindowsToast:
    def test_it_is_filed_under_kilns_own_name_and_icon(self, monkeypatch):
        argv, script = _windows_script(monkeypatch)
        assert argv[0] == "powershell" and argv[-1] == "-"
        assert "HKCU:\\Software\\Classes\\AppUserModelId\\" in script
        assert "-Name DisplayName -Value 'Kiln'" in script and "-Name IconUri -Value 'C:\\Users" in script
        assert "CreateToastNotifier($id)" in script and "'Kiln3D.Kiln'" in script

    def test_the_code_is_in_the_toast_and_not_on_the_command_line(self, monkeypatch):
        argv, script = _windows_script(monkeypatch)
        assert not any("4821" in a for a in argv) and "Kiln print code 4821" in script

    @pytest.mark.parametrize("hostile", [
        "x'); Start-Process calc; ('y.stl",
        "x\u2019); Start-Process calc; (\u2019y.stl",
        "x\u2018); Start-Process calc; (\u201by.stl",
        "$(Start-Process calc).stl",
        "`$(Start-Process calc)`.stl",
    ])
    def test_a_file_name_can_never_become_a_command(self, monkeypatch, hostile):
        _argv, script = _windows_script(monkeypatch, file_name=hostile)
        code = _powershell_code_outside_strings(script)
        assert "Start-Process" not in code and "calc" not in code
        assert "$(" not in code.replace("$(-not", "")  # the one subexpression is Kiln's own Test-Path

    def test_a_printer_name_can_never_become_a_command_either(self, monkeypatch):
        _argv, script = _windows_script(monkeypatch, printer="p'); Start-Process calc; ('")
        assert "Start-Process" not in _powershell_code_outside_strings(script)

    def test_markup_in_a_name_is_text_not_toast_structure(self, monkeypatch):
        _argv, script = _windows_script(monkeypatch, file_name="a & b <text>c<text>.stl")
        assert "a &amp; b &lt;text&gt;c&lt;text&gt;" in script


def _applescript_code_outside_strings(script: str) -> str:
    code, i, n = [], 0, len(script)
    while i < n:
        if script[i] == '"':
            i += 1
            while i < n and script[i] != '"':
                i += 2 if script[i] == "\\" else 1
            i += 1
            code.append('""')
            continue
        code.append(script[i])
        i += 1
    return "".join(code)


def test_on_a_mac_a_file_name_can_never_become_a_command(monkeypatch):
    monkeypatch.setattr(screen_code.sys, "platform", "darwin")
    issued = _issued("4821")
    issued.file_name = 'x" & (do shell script "open -a Calculator") & "y.stl'
    _argv, script = screen_code._show_command(issued)
    code = _applescript_code_outside_strings(script)
    assert "do shell script" not in code and code.startswith("display notification")


# ---------------------------------------------------------------------------
# What the package carries
# ---------------------------------------------------------------------------


def test_the_package_carries_the_helper_and_the_windows_icon():
    import plistlib

    data = pathlib.Path(screen_code.__file__).parent / "data" / "notifier"
    app = data / "Kiln.app"
    assert (data / "Kiln.png").is_file()
    for rel in ("Contents/Info.plist", "Contents/MacOS/kiln-notifier", "Contents/Resources/Kiln.icns", "Contents/_CodeSignature/CodeResources"):
        assert (app / rel).is_file(), rel
    with open(app / "Contents" / "Info.plist", "rb") as fh:
        info = plistlib.load(fh)
    assert info["CFBundleName"] == "Kiln" and info["CFBundleIdentifier"] == "com.kiln3d.notifier"
    assert info["LSUIElement"] is True and info["CFBundleExecutable"] == "kiln-notifier"
    pyproject = (pathlib.Path(screen_code.__file__).parents[2] / "pyproject.toml").read_text()
    for rel in ("data/notifier/Kiln.png", "data/notifier/Kiln.app/Contents/MacOS/kiln-notifier",
                "data/notifier/Kiln.app/Contents/_CodeSignature/CodeResources"):
        assert f'"{rel}"' in pyproject, rel


@pytest.mark.skipif(sys.platform != "darwin", reason="reads a Mac binary with the Mac's own tools")
def test_the_helper_runs_on_both_mac_chip_families():
    exe = pathlib.Path(screen_code.__file__).parent / "data" / "notifier" / "Kiln.app" / "Contents" / "MacOS" / "kiln-notifier"
    archs = subprocess.run(["lipo", "-archs", str(exe)], capture_output=True, text=True, check=True).stdout.split()
    assert {"arm64", "x86_64"} <= set(archs)


def test_on_windows_no_console_window_flashes_up(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(screen_code, "_show_hook", None)
    monkeypatch.setattr(screen_code.sys, "platform", "win32")
    monkeypatch.setattr(screen_code, "_windows_icon", lambda: "")
    monkeypatch.setattr(screen_code.subprocess, "run", fake_run)
    assert screen_code._show(_issued())
    assert seen.get("creationflags") == 0x08000000
    assert seen["input"].endswith("\n") and seen["input"].count("\n") == 1
