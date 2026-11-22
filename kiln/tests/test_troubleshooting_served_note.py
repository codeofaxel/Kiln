"""On an install without kiln-pro, a troubleshooting answer made on this
computer says why the step-by-step fixes are not in it, and names no plan.

The fixes come from Kiln's servers for any signed-in account (the tools run
there through :mod:`kiln.served_public`).  When that ask misses, the answer
made here carries one sentence for the miss's cause and the miss's fields,
and never the paid-plan pitch: no plan holds these fixes back.  The storage
and drying advice kept in the public tables is said whatever happens.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiln import served_public
from kiln.design_intelligence import get_print_diagnostic, troubleshoot_print_issue
from kiln.served_answer import Miss


@pytest.fixture
def plain_install():
    """kiln-pro not in the process, and its overlay not merged."""
    with (
        patch("kiln.design_intelligence._kiln_pro_in_process", return_value=False),
        patch("kiln.design_intelligence._engineering_overlay_loaded", return_value=False),
    ):
        yield


@pytest.mark.parametrize(
    ("miss", "cause_words", "fix_words"),
    [
        (Miss("offline"), "this computer is offline", "Reconnect to the internet and ask again"),
        (Miss("signed_out", "KILN_ACCOUNT_NOT_PAIRED"), "Kiln is signed out", "Sign in and ask again"),
        (Miss("unanswered", "SERVER_UNREACHABLE"), "Kiln's servers didn't answer", "Wait a minute and ask again"),
        (Miss("refused", "SOME_REFUSAL", "Not today."), "Kiln's servers said no", "Not today."),
    ],
)
def test_a_missed_ask_is_said_once_with_its_cause(plain_install, miss, cause_words, fix_words):
    with served_public.here_after(miss):
        out = troubleshoot_print_issue("petg", "stringing").to_dict()
    assert out["upgrade_hint"] == ""
    assert "kiln3d.com/pricing" not in str(out)
    assert out["fixes_note"].startswith("Kiln's step-by-step fixes for this print problem come from Kiln's servers")
    assert cause_words in out["fixes_note"]
    assert fix_words in out["fixes_note"]
    assert out["why"] == miss.cause
    assert out["why_code"] == miss.code
    # The safety floor kept here is still said.
    assert out["storage_requirements"]["drying_temp_c"] is not None


def test_the_diagnostic_says_it_too_and_keeps_its_storage_line(plain_install):
    with served_public.here_after(Miss("offline")):
        out = get_print_diagnostic("petg", symptom="stringing").to_dict()
    assert "this computer is offline" in out["fixes_note"]
    assert out["why"] == "offline"
    assert any(line.startswith("Storage:") for line in out["combined_guidance"])


def test_a_served_tool_adds_no_second_note():
    """``served_public.say_once`` adds nothing to an answer already carrying
    its miss, so the cause is said once, not twice."""
    with (
        patch("kiln.design_intelligence._kiln_pro_in_process", return_value=False),
        patch("kiln.design_intelligence._engineering_overlay_loaded", return_value=False),
    ):
        miss = Miss("offline")
        with served_public.here_after(miss):
            out = troubleshoot_print_issue("petg", "stringing").to_dict()
    said = served_public.say_once("troubleshoot_print_issue", out, miss)
    assert served_public.NOTE_KEY not in said


def test_called_directly_it_names_where_the_fixes_come_from(plain_install):
    out = troubleshoot_print_issue("petg", "stringing").to_dict()
    assert out["upgrade_hint"] == ""
    assert "free with any Kiln account" in out["fixes_note"]
    assert "why" not in out


def test_where_the_fixes_merged_nothing_is_added():
    with patch("kiln.design_intelligence._engineering_overlay_loaded", return_value=True):
        out = troubleshoot_print_issue("petg", "stringing").to_dict()
        diag = get_print_diagnostic("petg", symptom="stringing").to_dict()
    assert "fixes_note" not in out and "why" not in out
    assert "fixes_note" not in diag
