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
from pathlib import Path

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
    from kiln.hotend_safety import MOLTEN_FILAMENT_WARNING as warning

    lowered = warning.lower()
    # The hazard, and what to do about it. A warning that names neither is
    # decoration.
    assert "molten" in lowered
    assert "glove" in lowered
    assert "spray" in lowered or "sprays" in lowered


def test_burn_warning_fires_on_the_same_trigger_as_the_next_step():
    from kiln.hotend_safety import needs_burn_warning

    # Everything a user says when they are already at the hot end with a tool
    # in their hand has to trip the warning, not just the word "clog".
    for phrase in ("cold pull", "heat creep", "extruder gear", "clog"):
        assert needs_burn_warning(phrase), phrase


# ---------------------------------------------------------------------------
# The safety floor reaches every door, and only the doors that need it
# ---------------------------------------------------------------------------


def test_the_clog_recovery_plan_leads_with_the_warning():
    """The worst gap found in the door audit, pinned.

    ``get_recovery_plan(failure_type="nozzle_clog")`` hands back a numbered
    bare-hands procedure. ``failure_type`` is an enum the user picks, so
    there is no symptom string for the troubleshooter's trigger list to
    match — this door could never have been covered by that route. It is the
    three-days-bare-handed scenario reached in a single call.
    """
    from kiln.failure_recovery import FailureType, _build_recovery
    from kiln.hotend_safety import MOLTEN_FILAMENT_WARNING

    steps = _build_recovery(FailureType.NOZZLE_CLOG).steps
    assert "glove" in steps[0].lower(), "the warning is not the first step"
    assert MOLTEN_FILAMENT_WARNING in steps[0]


def test_the_clog_plan_no_longer_asserts_a_material_blind_temperature():
    # It used to say "heat to 250C, cool to 90C" for every material. This
    # plan does not know the material, and 250C is well above an A1's PLA
    # range — a number it could not stand behind. The technique is what is
    # universal.
    from kiln.failure_recovery import FailureType, _build_recovery

    text = " ".join(_build_recovery(FailureType.NOZZLE_CLOG).steps)
    assert "250C" not in text and "90C" not in text
    assert "steady" in text and "never a jerk" in text


def test_the_recovery_gcode_carries_the_warning_into_the_file():
    # This script heats the nozzle and then a PERSON pulls. The warning has
    # to survive into the G-code itself, not just the chat around it.
    from kiln.hotend_safety import WARNING_GCODE_COMMENT

    source = (
        Path(__file__).resolve().parent.parent
        / "src" / "kiln" / "print_recovery.py"
    ).read_text(encoding="utf-8")
    assert "WARNING_GCODE_COMMENT" in source
    assert WARNING_GCODE_COMMENT.startswith("; ")


def test_the_trigger_list_matches_intent_not_anatomy():
    """A warning that fires on every mention of "nozzle" is wallpaper.

    Wallpaper is not a safety measure, it is the appearance of one — so the
    list must NOT fire on shopping and settings questions.
    """
    from kiln.hotend_safety import needs_burn_warning

    # Fires: someone is about to touch a blockage.
    for yes in (
        "my nozzle is clogged",
        "filament jam on the A1",
        "it will not extrude",
        "how do I do a cold pull",
        "load filament fails at step 5",
        "heat creep keeps coming back",
    ):
        assert needs_burn_warning(yes), f"should warn: {yes!r}"

    # Silent: nobody's hand is anywhere near the hot end.
    for no in (
        "which nozzle should I use for ABS",
        "what nozzle temp for PETG",
        "my purge tower is too big",
        "the bed levelling wizard failed",
        "0.4 nozzle or 0.6 nozzle",
    ):
        assert not needs_burn_warning(no), f"should stay silent: {no!r}"


def test_the_original_symptom_still_trips_the_warning():
    # The tightening dropped "wizard", "purge" and bare "nozzle". Check the
    # sentence that started all of this still fires, via "load filament".
    from kiln.hotend_safety import needs_burn_warning

    assert needs_burn_warning(
        "Load Filament wizard fails at step 5, HMS 1200-8007"
    )


def test_the_ruled_out_line_carries_no_single_model_anatomy():
    """The engine must not hardcode one machine's parts.

    The first version listed the A1's spool, tube, cutter, gears and hot-end
    mount inside a string emitted for ANY model with a load sequence. On a
    machine that cuts at the AMS or mounts its hot end with screws, that
    sentence names parts the user does not have — confidently.
    """
    source = (
        Path(__file__).resolve().parent.parent
        / "src" / "kiln" / "printer_intelligence.py"
    ).read_text(encoding="utf-8")
    ruled_out = source[source.index('reading["ruled_out"] = ('):][:1200]
    for anatomy in ("cutter", "spool", "buckle", "seated in its mount"):
        assert anatomy not in ruled_out, (
            f"the generic elimination string hardcodes {anatomy!r}"
        )


def test_an_ams_unit_and_slot_variant_matches_the_same_fault():
    """Bambu files one fault under sixteen spellings.

    The first group's low digit is the AMS unit and the second group's
    second digit is the slot, so the same jam on unit B slot 3 arrives as
    0701_7200_… where unit A slot 1 arrives as 0700_7000_…  Comparing raw
    digits, a failure mode declaring the canonical code matched only the
    user whose filament happened to be in the first slot of the first unit.
    """
    from kiln.printer_intelligence import _normalize_code

    assert _normalize_code("0701_7200_0002_0002") == _normalize_code(
        "0700_7000_0002_0002"
    )
    # A print_error, which has no unit aliasing, is untouched.
    assert _normalize_code("1200-8007") == "12008007"


def test_the_clog_plan_forks_instead_of_guessing_the_temperature():
    """"It will not extrude" has two causes that want OPPOSITE moves.

    The plan used to answer with one of them unconditionally — raise 5-10C —
    which is right for debris in the nozzle bore and strictly wrong for heat
    creep, where the plug forms above the melt zone and more heat is what put
    it there. It was not even self-consistent: its own prevent_recurrence
    already named heat creep as something to avoid.
    """
    from kiln.failure_recovery import FailureType, _build_recovery

    plan = _build_recovery(FailureType.NOZZLE_CLOG)
    text = " ".join(plan.steps).lower()
    assert "which failure this is" in text, "the plan does not fork"
    assert "fan" in text, "no cheap check to tell the two apart"
    assert "makes this worse" in text, "does not warn against raising the heat"


def test_the_unautomatable_adjustment_is_not_in_the_automatable_map():
    """``settings_adjustments`` is consumed programmatically.

    Anything reading it applies the change, so a value whose correctness
    depends on which root cause is in play must not sit there — a conditional
    cannot be expressed as a key/value pair. Retraction stays, because
    reducing it helps BOTH causes and is safe to apply without knowing which.
    """
    from kiln.failure_recovery import FailureType, _build_recovery

    adjustments = _build_recovery(FailureType.NOZZLE_CLOG).settings_adjustments
    assert "print_temp" not in adjustments
    assert adjustments["retraction_distance"]


def test_the_discriminator_comes_before_the_physical_work():
    # A user cannot act on either branch until they know which one they are
    # in, so the check has to precede the heating and the pull.
    from kiln.failure_recovery import FailureType, _build_recovery

    steps = [s.lower() for s in _build_recovery(FailureType.NOZZLE_CLOG).steps]
    check = next(i for i, s in enumerate(steps) if "which failure this is" in s)
    # "perform a cold pull", not merely "cold pull" — the safety warning at
    # step 1 mentions the phrase too, and matching that would compare the
    # check against the warning instead of against the instruction.
    pull = next(i for i, s in enumerate(steps) if "perform a cold pull" in s)
    assert check < pull


# ---------------------------------------------------------------------------
# The free-tier nudge
# ---------------------------------------------------------------------------


def _free_troubleshoot(**kwargs):
    """Call the tool the way a free install would — kiln_pro unimportable."""
    import kiln.printer_intelligence as pi
    from kiln.server import troubleshoot_printer as tool

    class _Block:
        def find_spec(self, name, path=None, target=None):
            if name.startswith("kiln_pro"):
                raise ImportError("simulated free install")
            return None

    # Evict any already-imported kiln_pro as well as blocking the import.
    # A meta_path hook is only consulted on a MISS, and by the time this test
    # runs the suite has usually imported kiln_pro already — so the blocker
    # alone measures nothing and the test silently reads paid data. (That is
    # the same import-path trap that made the first two hand-run
    # measurements of this wrong.)
    blocker = _Block()
    evicted = {
        name: mod for name, mod in sys.modules.items()
        if name == "kiln_pro" or name.startswith("kiln_pro.")
    }
    for name in evicted:
        del sys.modules[name]
    sys.meta_path.insert(0, blocker)
    pi._merged_cache = None
    try:
        return getattr(tool, "fn", tool)(**kwargs)
    finally:
        sys.meta_path.remove(blocker)
        sys.modules.update(evicted)
        pi._merged_cache = None


def test_the_nudge_is_specific_when_the_user_named_an_exact_signal():
    """The sharpest honest moment to mention Pro.

    Someone who typed a step number and a fault code just handed over the two
    most precise things they can say about a load failure, and a free install
    reads neither. Saying so is not a sales line — it is the one place the
    difference between the tiers is concrete.
    """
    hint = _free_troubleshoot(
        printer_id="bambu_a1", symptom="load fails at step 5, 1200-8007"
    )["upgrade_hint"]
    assert "fault code" in hint and "wizard step" in hint
    # The free floor is named too — a nudge that says only what you lack
    # reads as a wall; one that says what you keep reads as a door.
    assert "still tells you" in hint
    assert "kiln3d.com" in hint


def test_the_nudge_never_leaks_the_paid_answer():
    # Naming the shape of the answer sells it; naming the answer gives it
    # away. "Step 5 is upstream of the melt zone" is the product.
    hint = _free_troubleshoot(
        printer_id="bambu_a1", symptom="load fails at step 5, 1200-8007"
    )["upgrade_hint"].lower()
    for leak in ("upstream", "step 5 is", "cannot be the cause", "clog cannot",
                 "melt zone"):
        assert leak not in hint, f"the nudge gives away {leak!r}"


def test_the_nudge_does_not_promise_what_pro_cannot_deliver():
    # A Prusa owner who types "step 3" gets the generic line, because the
    # codes and load sequences only exist for Bambu in the paid data. A
    # promise the product cannot keep costs more than the sale.
    hint = _free_troubleshoot(
        printer_id="prusa_mk4", symptom="load fails at step 3"
    )["upgrade_hint"]
    assert "fault code" not in hint
    assert "kiln3d.com" in hint


def test_the_generic_nudge_says_what_it_means_in_plain_words():
    # It used to read "per-printer firmware quirks + failure-mode playbooks",
    # which is engineer-speak at the exact moment a stuck user is reading.
    hint = _free_troubleshoot(printer_id="bambu_a1", symptom="stringing")[
        "upgrade_hint"
    ]
    for jargon in ("firmware quirks", "playbook", "overlay"):
        assert jargon not in hint.lower()
    assert "what goes wrong" in hint


def test_the_nudge_is_said_once_per_session():
    """The tenth showing does not persuade — it teaches people to skip the field.

    A user debugging a stuck load calls this tool repeatedly. Repeating the
    same suggestion every time costs the nudge every FUTURE moment it would
    have been welcome, which is a worse trade than the impression gained.
    """
    from kiln.tiers_and_terms import reset_spoken_keys

    reset_spoken_keys()
    try:
        first = _free_troubleshoot(
            printer_id="bambu_a1", symptom="load fails at step 5, 1200-8007"
        )["upgrade_hint"]
        second = _free_troubleshoot(
            printer_id="bambu_a1", symptom="load fails at step 5, 1200-8007"
        )["upgrade_hint"]
        assert first, "the nudge never fired at all"
        assert second == "", "the nudge repeated within one session"
    finally:
        reset_spoken_keys()


def test_a_paid_caller_never_spends_the_session_claim():
    # A caller with the depth gets no nudge and must not consume the one
    # showing a later free caller in the same process would have had.
    from kiln.server import troubleshoot_printer as tool
    from kiln.tiers_and_terms import claim_once, reset_spoken_keys

    reset_spoken_keys()
    try:
        getattr(tool, "fn", tool)(printer_id="bambu_a1", symptom="stringing")
        assert claim_once("troubleshoot_printer.upgrade_hint") is True
    finally:
        reset_spoken_keys()


def test_every_rationed_line_counts_against_one_session_claim():
    # Two independent "once per session" counters say the same thing twice
    # per session. The fastener advice had the first copy of this mechanism
    # and now claims through the shared one.
    from kiln.fastener_advice import reset_emitted_content_keys
    from kiln.tiers_and_terms import claim_once, reset_spoken_keys

    reset_spoken_keys()
    try:
        assert claim_once("shared-key") is True
        assert claim_once("shared-key") is False
        reset_emitted_content_keys()  # must clear the SHARED set
        assert claim_once("shared-key") is True
    finally:
        reset_spoken_keys()
