"""Structured diagnostic signals in ``troubleshoot_printer``.

A symptom string carries two kinds of information.  The prose is fuzzy and
belongs to word matching.  A fault code and a load-wizard step number are
exact, and word matching destroys them — which is how a user who said
"Load Filament wizard fails at step 5, HMS 1200-8007" got back one generic
AMS wear entry and nothing else.

Covers:
    - extract_codes / extract_load_step parse the exact signals out
    - a named code selects the mode that claims it, alone
    - a named step selects the modes that claim it, and DROPS the text noise
    - read_load_step says which side of the melt zone a step is on
    - a feed-zone step rules a nozzle clog out in as many words
    - text-only symptoms behave exactly as they did before
    - the molten-filament burn warning is free-tier, at the same trigger as
      the filament next-step hint
"""

from __future__ import annotations

import sys
import types

import pytest

from kiln.printer_intelligence import (
    FailureMode,
    diagnose_issue,
    extract_codes,
    extract_load_step,
    get_printer_intel,
    read_load_step,
)

# A profile shaped like the real A1 overlay: generic modes that share the word
# "filament" with everything, plus two that claim exact signals.  The generic
# ones are the noise the old matcher returned.
SIGNAL_DEPTH = {
    "bambu_a1": {
        "failure_modes": [
            {
                "symptom": "AMS Lite extrusion failures / filament not loading",
                "cause": "Cheap causes first; PTFE wear only with the hours.",
                "fix": "Cut a clean tip, free the spool, clear the shavings.",
            },
            {
                "symptom": "Stringing with PETG",
                "cause": "Moisture in the filament.",
                "fix": "Dry it.",
            },
            {
                "symptom": "Filament load fails at the push step (step 5)",
                "cause": "Upstream of the melt zone; a clog cannot explain it.",
                "fix": "Clean tip, clear the shavings, re-seat the hot end.",
                "codes": ["1200-8014"],
                "load_steps": [5],
            },
            {
                "symptom": "Filament load fails at the purge step (step 6)",
                "cause": "The melt zone; the feed path demonstrably worked.",
                "fix": "Heat and push by hand; clear the clog.",
                "codes": ["1200-8007"],
                "load_steps": [6],
            },
        ],
        "load_sequence": [
            {"step": 1, "name": "heat the nozzle", "zone": "feed"},
            {"step": 5, "name": "push the new filament into the extruder", "zone": "feed"},
            {"step": 6, "name": "purge through the nozzle", "zone": "melt"},
        ],
    }
}


@pytest.fixture
def signal_overlay(monkeypatch):
    """Install a fake kiln-pro serving the signal-carrying profile above."""
    import kiln.printer_intelligence as pi

    data_overlays = types.ModuleType("kiln_pro.data_overlays")
    data_overlays.load_overlay = lambda kind: SIGNAL_DEPTH
    package = types.ModuleType("kiln_pro")
    package.data_overlays = data_overlays
    monkeypatch.setitem(sys.modules, "kiln_pro", package)
    monkeypatch.setitem(sys.modules, "kiln_pro.data_overlays", data_overlays)
    monkeypatch.setattr(pi, "_merged_cache", None, raising=False)
    yield
    monkeypatch.setattr(pi, "_merged_cache", None, raising=False)


# ---------------------------------------------------------------------------
# Parsing the exact signals out of free text
# ---------------------------------------------------------------------------


def test_extracts_a_fault_code_in_any_spelling():
    for spelling in ("1200-8007", "1200_8007", "12008007", "1200-8007 031520"):
        assert "12008007" in extract_codes(f"load failed, {spelling} on screen")


def test_a_material_name_is_not_read_as_a_code():
    # "PLA-CF" is hex-ish only by accident; a false code match would send the
    # whole diagnosis to the wrong entry with total confidence.
    assert extract_codes("stringing with PLA-CF and PETG") == ()


def test_a_short_number_is_not_read_as_a_code():
    assert extract_codes("step 5 of 6 failed") == ()


def test_a_raw_decimal_print_error_is_not_read_as_a_code():
    """The bug this guard exists for, and it is not hypothetical.

    Bambu reports ``print_error`` over MQTT as a 32-bit DECIMAL -- 302022663
    is the screen's 1200-8007 -- and Kiln's own live diagnosis
    (``diagnose_print_failure_live``, ``smart_reprint``) feeds that field
    straight into this matcher as a symptom string.  Every digit of a decimal
    is a valid hex digit, so without a length rule the matcher would read
    302022663 as a hex code and answer confidently about a fault the printer
    never reported.
    """
    assert extract_codes("302022663") == ()
    # The same value in the form the screen shows IS a code.
    assert extract_codes("1200-8007") == ("12008007",)


def test_the_live_diagnosis_doors_pass_the_screen_form():
    # The other half of the fix: the doors convert before they ask, so the
    # code path works there too instead of merely failing safe.
    from kiln.printers.base import format_error_code

    assert format_error_code(302022663) == "1200-8007"
    assert extract_codes(format_error_code(302022663)) == ("12008007",)


def test_extracts_a_wizard_step():
    assert extract_load_step("fails at step 5") == 5
    assert extract_load_step("Step #5 of the load") == 5
    assert extract_load_step("dies at stage 6") == 6


def test_a_nozzle_size_is_not_read_as_a_step():
    # Nothing here says "step", so nothing here is a step.
    assert extract_load_step("under-extrusion with the 0.4mm nozzle") is None


def test_two_different_steps_are_no_signal_at_all():
    # An ambiguous reading is not a signal; guessing which one the user meant
    # turns a narrowing into a misdirection.
    assert extract_load_step("gets past step 4 but dies at step 6") is None


# ---------------------------------------------------------------------------
# The signals actually steer the diagnosis
# ---------------------------------------------------------------------------


def test_a_named_code_returns_only_the_mode_that_claims_it(signal_overlay):
    matches = diagnose_issue("bambu_a1", "load failed with 1200-8007 on screen")
    assert [m["symptom"] for m in matches] == [
        "Filament load fails at the purge step (step 6)"
    ]
    assert matches[0]["matched_on"] == "code"


def test_a_named_step_returns_only_the_modes_that_claim_it(signal_overlay):
    # The exact shape of the reported failure: the sentence contains
    # "filament", which used to drag in the AMS entry and bury the answer.
    matches = diagnose_issue(
        "bambu_a1", "Load Filament wizard fails at step 5 pushing into the extruder"
    )
    assert [m["symptom"] for m in matches] == [
        "Filament load fails at the push step (step 5)"
    ]
    assert matches[0]["matched_on"] == "load_step"


def test_a_structured_signal_drops_the_text_noise(signal_overlay):
    generic = "AMS Lite extrusion failures / filament not loading"
    assert generic in [
        m["symptom"] for m in diagnose_issue("bambu_a1", "filament not loading")
    ]
    assert generic not in [
        m["symptom"]
        for m in diagnose_issue("bambu_a1", "filament not loading at step 5")
    ]


def test_text_only_symptoms_are_unchanged(signal_overlay):
    matches = diagnose_issue("bambu_a1", "stringing")
    assert [m["symptom"] for m in matches] == ["Stringing with PETG"]
    assert matches[0]["matched_on"] == "text"


def test_the_narrower_claim_leads(signal_overlay):
    """A mode claiming one step beats one claiming three, and neither lies.

    The real A1 data has both: an entry specific to the push step, and the
    general AMS entry whose cheap causes can also fail there.  Both are
    honest matches, so the fix is ranking, not pruning the broad entry's
    claim — pruning would make the data lie about its own scope to game the
    display, and the broad entry would then miss the cases it does explain.
    """
    import kiln.printer_intelligence as pi

    broad = {
        "symptom": "General AMS feed trouble",
        "cause": "Several cheap causes.",
        "fix": "Work down the path.",
        "load_steps": [2, 3, 5],
    }
    profile = dict(SIGNAL_DEPTH["bambu_a1"])
    profile["failure_modes"] = [broad, *SIGNAL_DEPTH["bambu_a1"]["failure_modes"]]
    pi._merged_cache = None
    import sys

    sys.modules["kiln_pro.data_overlays"].load_overlay = lambda kind: {
        "bambu_a1": profile
    }
    try:
        matches = diagnose_issue("bambu_a1", "load fails at step 5")
        assert matches[0]["symptom"] == "Filament load fails at the push step (step 5)"
        assert broad["symptom"] in [m["symptom"] for m in matches]
    finally:
        pi._merged_cache = None


def test_a_code_outranks_a_step_when_both_are_named(signal_overlay):
    # "step 5" and a step-6 code: the code is the printer's own word for what
    # happened, so it leads.
    matches = diagnose_issue("bambu_a1", "step 5, then 1200-8007")
    assert matches[0]["matched_on"] == "code"
    assert matches[0]["symptom"] == "Filament load fails at the purge step (step 6)"


# ---------------------------------------------------------------------------
# The step reading — the part that stops a needless nozzle replacement
# ---------------------------------------------------------------------------


def test_a_feed_step_rules_the_nozzle_out(signal_overlay):
    reading = read_load_step("bambu_a1", 5)
    assert reading is not None
    assert reading["zone"] == "feed"
    assert "clog cannot be the cause" in reading["ruled_out"]


def test_a_melt_step_rules_the_nozzle_in(signal_overlay):
    reading = read_load_step("bambu_a1", 6)
    assert reading is not None
    assert reading["zone"] == "melt"
    assert "feed path above it is working" in reading["ruled_out"]


def test_an_unknown_step_reads_as_nothing_rather_than_a_guess(signal_overlay):
    assert read_load_step("bambu_a1", 99) is None


def test_a_printer_with_no_sequence_reads_as_nothing(signal_overlay):
    assert read_load_step("ender3", 5) is None


def test_load_sequence_is_absent_without_the_overlay():
    # Public floor: no curated load sequence ships in this repo.
    assert get_printer_intel("bambu_a1").load_sequence == []


def test_failure_mode_signals_default_to_empty():
    fm = FailureMode(symptom="s", cause="c", fix="f")
    assert fm.codes == ()
    assert fm.load_steps == ()


# ---------------------------------------------------------------------------
# The safety floor
# ---------------------------------------------------------------------------


def test_burn_warning_is_public_and_names_the_hazard_and_the_action():
    from kiln.server import _MOLTEN_FILAMENT_WARNING as warning

    lowered = warning.lower()
    # The hazard, and what to do about it. A warning that names neither is
    # decoration.
    assert "molten" in lowered
    assert "glove" in lowered
    assert "spray" in lowered or "sprays" in lowered


def test_burn_warning_fires_on_the_same_trigger_as_the_next_step():
    from kiln.server import _FILAMENT_PATH_SYMPTOMS

    # Everything a user says when they are already at the hot end with a tool
    # in their hand has to trip the warning, not just the word "clog".
    for phrase in ("cold pull", "heat creep", "extruder gear", "nozzle"):
        assert phrase in _FILAMENT_PATH_SYMPTOMS
