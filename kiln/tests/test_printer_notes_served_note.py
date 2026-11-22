"""On an install without kiln-pro, a printer model's answer made on this
computer says why what Kiln's servers add to it is missing, and names no plan.

A printer model's known failures, its note on each plastic and its settings
notes come from Kiln's servers for any signed-in account: the four doors run
there through :mod:`kiln.served_public`.  When that ask misses, the answer
made here carries one sentence for the miss's cause and the miss's fields,
and never a plan pitch: no plan holds these back.  The verdict and the
settings kept here are said whatever happens.

Each test calls the registered tool, through the same dispatch an agent's
call takes, with Kiln's servers' answer replaced by the miss under test.
"""

from __future__ import annotations

import sys

import pytest

import kiln.server as srv
from kiln import served_public
from kiln.design_intelligence import PrinterCompatibilityReport
from kiln.served_answer import Miss
from tests import _real_exports as rx

_MISSES = [
    (Miss("offline"), "this computer is offline", "Reconnect to the internet and ask again"),
    (Miss("signed_out", "KILN_ACCOUNT_NOT_PAIRED"), "Kiln is signed out", "Sign in and ask again"),
    (Miss("unanswered", "SERVER_UNREACHABLE"), "Kiln's servers didn't answer", "Wait a minute and ask again"),
    (Miss("refused", "SOME_REFUSAL", "Not today."), "Kiln's servers said no", "Not today."),
]


@pytest.fixture
def plain_install(monkeypatch):
    """An install without kiln-pro whose bundled manifest names the four
    doors as served; ``missed(miss)`` makes Kiln's servers miss with *miss*."""
    import kiln.printer_intelligence as pi
    from kiln import _pro_catalogue_bridge
    from kiln.mcp_compat import FastMCP

    srv._register_pro_tool_stubs(FastMCP("printer-notes-test"))
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    monkeypatch.setattr(served_public, "_said", set())
    # No printer record and no curated rows, whatever another test left in
    # this process: what an install that never reached Kiln's servers holds.
    monkeypatch.setattr(_pro_catalogue_bridge, "printer_record", lambda *a, **k: None)
    monkeypatch.setattr(pi, "_overlay_for_caller", lambda: None)
    pi._reset_caches()
    for door in ("troubleshoot_printer", "check_printer_material_compatibility",
                 "check_printer_material_support", "get_material_recommendation"):
        assert served_public.plan(door, {}) is not None, f"{door} is not served to a signed-in install"

    def missed(miss: Miss) -> None:
        monkeypatch.setattr(served_public, "ask", lambda name, arguments, entry: (None, miss))

    yield missed
    served_public._reset_for_tests()
    pi._reset_caches()


def _call(name: str, **args) -> dict:
    return rx.call(srv.mcp, name, args)


@pytest.fixture
def own_verdict_without_notes(monkeypatch):
    """The A1's own nylon verdict, held from an earlier session, with no note:
    what an install that once fetched the printer's record answers offline."""
    report = PrinterCompatibilityReport(
        printer_id="bambu_a1",
        materials={"nylon": {"status": "needs_upgrade", "upgrades_needed": ["enclosure"]}},
        resolved_from="exact",
    )
    monkeypatch.setattr(
        "kiln.design_intelligence.check_printer_material_compatibility", lambda *a, **k: report,
    )
    return report


@pytest.mark.parametrize(("miss", "cause_words", "fix_words"), _MISSES)
def test_printer_troubleshooting_says_why_the_known_failures_are_missing(
    plain_install, miss, cause_words, fix_words,
):
    plain_install(miss)
    out = _call("troubleshoot_printer", printer_id="bambu_a1", symptom="stringing")
    assert out["success"] is True and out["matches"] == []
    assert out["upgrade_hint"] == ""
    assert "kiln3d.com/pricing" not in str(out)
    assert out["matches_note"].startswith("Kiln's known failures for this printer model")
    assert cause_words in out["matches_note"] and fix_words in out["matches_note"]
    assert out["why"] == miss.cause and out["why_code"] == miss.code
    # Said once: the dispatcher's own note does not repeat the cause.
    assert served_public.NOTE_KEY not in out


def test_a_named_wizard_step_is_not_answered_with_a_plan_either(plain_install):
    plain_install(Miss("signed_out", "KILN_ACCOUNT_NOT_PAIRED"))
    out = _call("troubleshoot_printer", printer_id="bambu_a1", symptom="load fails at step 5")
    assert out["upgrade_hint"] == ""
    assert "Kiln Pro" not in str(out) and "kiln3d.com/pricing" not in str(out)
    assert "Kiln is signed out" in out["matches_note"]


@pytest.mark.parametrize(("miss", "cause_words", "fix_words"), _MISSES)
def test_material_settings_say_why_the_notes_are_missing(plain_install, miss, cause_words, fix_words):
    plain_install(miss)
    out = _call("get_material_recommendation", printer_id="bambu_a1", material="PETG")
    assert out["success"] is True and out["hotend_temp"] and out["notes"] == ""
    assert out["notes_note"].startswith("Kiln's notes on printing this plastic on this printer")
    assert cause_words in out["notes_note"] and fix_words in out["notes_note"]
    assert out["why"] == miss.cause
    assert served_public.NOTE_KEY not in out
    assert "kiln3d.com/pricing" not in str(out)


@pytest.mark.parametrize("door", ["check_printer_material_compatibility", "check_printer_material_support"])
@pytest.mark.parametrize(("miss", "cause_words", "fix_words"), _MISSES)
def test_a_verdict_held_without_its_note_says_why(
    plain_install, own_verdict_without_notes, door, miss, cause_words, fix_words,
):
    plain_install(miss)
    args = {"printer": "bambu_a1", "material": "nylon"} if door.endswith("compatibility") else {
        "printer_id": "bambu_a1", "material_id": "nylon",
    }
    out = _call(door, **args)
    assert out["success"] is True
    assert out["materials"]["nylon"]["status"] == "needs_upgrade"
    assert out["notes_note"].startswith("Kiln's notes on how this printer handles each plastic")
    assert cause_words in out["notes_note"] and fix_words in out["notes_note"]
    assert out["why"] == miss.cause
    assert served_public.NOTE_KEY not in out
    assert "kiln3d.com/pricing" not in str(out)


@pytest.mark.parametrize("door", ["check_printer_material_compatibility", "check_printer_material_support"])
def test_a_generic_answer_says_its_cause_once(plain_install, door, monkeypatch):
    """Answered from the generic floor, the door's own note names the cause;
    the dispatcher adds no second note for the same miss."""
    plain_install(Miss("offline"))
    report = PrinterCompatibilityReport(
        printer_id="default",
        materials={"nylon": {"status": "needs_upgrade", "upgrades_needed": ["enclosure"]}},
        resolved_from="default",
    )
    monkeypatch.setattr("kiln.design_intelligence.check_printer_material_compatibility", lambda *a, **k: report)
    monkeypatch.setattr(
        "kiln.design_intelligence.compat_floor_reason",
        lambda printer: {"roster_id": "bambu_a1", "line": "floor line", "why_generic": "offline", "why_code": ""},
    )
    args = {"printer": "bambu_a1", "material": "nylon"} if door.endswith("compatibility") else {
        "printer_id": "bambu_a1", "material_id": "nylon",
    }
    out = _call(door, **args)
    assert out["note"] == "floor line" and out["why"] == "offline"
    assert "notes_note" not in out
    assert served_public.NOTE_KEY not in out


def test_a_served_answer_is_the_servers_own(plain_install, monkeypatch):
    served = {"success": True, "printer": "Bambu Lab A1", "symptom": "stringing", "matches": [
        {"symptom": "s", "cause": "c", "fix": "f", "matched_on": "text"}], "count": 1, "upgrade_hint": ""}
    monkeypatch.setattr(served_public, "ask", lambda name, arguments, entry: (dict(served), None))
    out = _call("troubleshoot_printer", printer_id="bambu_a1", symptom="stringing")
    assert out == served


def test_called_directly_it_names_where_the_rest_comes_from(own_verdict_without_notes, monkeypatch):
    """No served ask was made (the tool's function called, not the dispatch):
    no cause to name, so the note says where the rest comes from."""
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    tool = srv.mcp._tool_manager._tools["check_printer_material_support"]
    out = tool.fn(printer_id="bambu_a1", material_id="nylon")
    assert "free with any Kiln account" in out["notes_note"]
    assert "ask through Kiln's printer tools while signed in" in out["notes_note"]
    assert "why" not in out


def test_with_kiln_pro_in_the_process_nothing_is_added(own_verdict_without_notes, monkeypatch):
    monkeypatch.setattr(served_public, "_kiln_pro_here", lambda: True)
    tool = srv.mcp._tool_manager._tools["check_printer_material_support"]
    out = tool.fn(printer_id="bambu_a1", material_id="nylon")
    assert "notes_note" not in out and "why" not in out
