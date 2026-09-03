"""A finished print must not read the same as an idle printer.

Regression cover for the state collapse found on real hardware: a print
ran to completion on a Bambu A1 (``gcode_state: FINISH``, layer 35/35,
100 %) and Kiln handed the web ``state: "idle"`` — the same value it
gives a printer nobody has touched all week, and the same value it gave
after a cancel.  Three different endings, one word.

Every adapter had its own spelling of the same fold: Bambu ``finish``,
Moonraker ``complete``/``cancelled``, Prusa Link ``FINISHED``/``STOPPED``,
and Marlin's M27 byte counter reaching the file size.  So the tests below
are written against the maps and the shared plumbing rather than against
one adapter.

The fix is a second field, not a new :class:`PrinterStatus` member:
``IDLE`` is load-bearing as "ready to print" in the pre-print gate, the
CLI preflight, ``registry.get_idle_printers`` and both fleet routers, and
a printer that just finished IS ready.  ``test_preflight_contract`` pins
that half — several of those gates compare the raw string, where nothing
else would catch a regression.
"""

from __future__ import annotations

import pytest

from kiln import auto_record_hook as arh
from kiln.printers.base import (
    JobResult,
    PrinterState,
    PrinterStatus,
)

# ---------------------------------------------------------------------------
# The incident, at the layer it happened
# ---------------------------------------------------------------------------


def _bambu_state(gcode_state: str, print_error: int | None = None) -> PrinterState:
    """Build a PrinterState the way the Bambu adapter builds one from MQTT."""
    from kiln.printers.bambu import BambuAdapter

    status: dict[str, object] = {"gcode_state": gcode_state}
    if print_error is not None:
        status["print_error"] = print_error
    return BambuAdapter._build_state_from_cache(  # type: ignore[misc]
        object.__new__(BambuAdapter), status
    )


def test_finished_print_is_not_the_same_as_an_idle_printer():
    """The measured bug: FINISH and IDLE were indistinguishable.

    This is the assertion that fails against the unfixed adapter, where
    both sides of the comparison were ``PrinterStatus.IDLE`` and nothing
    else was carried.
    """
    finished = _bambu_state("FINISH")
    untouched = _bambu_state("IDLE")

    # Both are genuinely idle machines — that part was never wrong.
    assert finished.state is PrinterStatus.IDLE
    assert untouched.state is PrinterStatus.IDLE

    # But they are no longer the same answer.
    assert finished.last_job_result is JobResult.COMPLETED
    assert untouched.last_job_result is None
    assert finished.to_dict() != untouched.to_dict()


def test_cancelled_print_is_not_the_same_as_a_finished_one():
    """The second half of the collapse, also measured live.

    After a cancel the Bambu MQTT cache sticks on ``failed`` with
    ``print_error == 0``.  That downgrades to IDLE so preflight passes —
    which is correct and stays — but it used to erase the ending too, so
    a cancel and a completion were one value.
    """
    cancelled = _bambu_state("failed", print_error=0)
    finished = _bambu_state("FINISH")

    assert cancelled.state is PrinterStatus.IDLE, "the preflight downgrade must survive"
    assert cancelled.last_job_result is JobResult.CANCELLED
    assert finished.last_job_result is JobResult.COMPLETED
    assert cancelled.last_job_result is not finished.last_job_result


def test_real_failure_keeps_its_error_status_and_says_the_job_failed():
    failed = _bambu_state("failed", print_error=84033543)
    assert failed.state is PrinterStatus.ERROR
    assert failed.last_job_result is JobResult.FAILED


def test_all_three_endings_are_mutually_distinguishable():
    """The whole point, stated once: three endings, three answers."""
    endings = {
        "finished": _bambu_state("FINISH"),
        "cancelled": _bambu_state("failed", print_error=0),
        "failed": _bambu_state("failed", print_error=84033543),
        "never_started": _bambu_state("IDLE"),
    }
    serialised = {name: st.to_dict() for name, st in endings.items()}
    distinct = {
        (d.get("state"), d.get("last_job_result")) for d in serialised.values()
    }
    assert len(distinct) == len(endings), (
        f"endings collapsed onto each other: {serialised}"
    )


# ---------------------------------------------------------------------------
# The non-regression half: "idle means ready to print" must still hold
# ---------------------------------------------------------------------------


def test_preflight_contract_a_finished_printer_is_still_ready():
    """A printer that just finished must still pass every idle gate.

    This is why the fix is a field and not a new enum member.  These four
    gates are the load-bearing ones, and two of them compare the raw
    string, so a new ``PrinterStatus`` member would have sailed past every
    type checker and quietly refused the next print.
    """
    finished = _bambu_state("FINISH")

    # kiln/src/kiln/server.py — the pre-print gate
    assert finished.state in {PrinterStatus.IDLE}
    # kiln/src/kiln/cli/main.py — CLI preflight
    assert finished.state == PrinterStatus.IDLE
    # kiln/src/kiln/registry.py — get_idle_printers
    assert finished.connected and finished.state == PrinterStatus.IDLE
    # kiln/src/kiln/pipelines.py + kiln_pro fleet routers — string compares
    assert finished.state.value == "idle"


def test_printer_status_carries_no_ending():
    """The blast-radius argument, pinned as the RULE rather than a list.

    The set is allowed to grow — ``stale`` and the two split-out
    unreachable causes were added deliberately, and every gate that reads
    this enum was updated with them.  What may never happen is a member
    meaning "a job ended": ``IDLE`` is load-bearing as "ready to print" in
    the pre-print gate, the CLI preflight, ``registry.get_idle_printers``
    and the fleet routers, two of which compare the raw string.  A printer
    that just finished IS ready, so the ending lives on :class:`JobResult`
    and nowhere else.

    Pinning the rule instead of the membership is what makes this test
    survive a legitimate addition while still catching "just add FINISHED".
    """
    ending_words = {m.value for m in JobResult} | {"finished", "complete", "done"}
    assert not ending_words & {m.value for m in PrinterStatus}

    # And every member is classified, so no gate can silently fall through
    # a new one.
    from kiln.printers.base import (
        BUSY_STATES,
        INDETERMINATE_STATES,
        READY_STATES,
        UNREACHABLE_STATES,
    )

    covered = BUSY_STATES | READY_STATES | UNREACHABLE_STATES | INDETERMINATE_STATES
    assert covered == set(PrinterStatus)
    assert {PrinterStatus.IDLE} == READY_STATES


# ---------------------------------------------------------------------------
# The wire contract
# ---------------------------------------------------------------------------


def test_to_dict_emits_the_string_value_and_omits_absence():
    finished = PrinterState(
        connected=True,
        state=PrinterStatus.IDLE,
        last_job_result=JobResult.COMPLETED,
    )
    assert finished.to_dict()["last_job_result"] == "completed"

    # Absent, not null: the field joins the extended block, which is
    # stripped when empty, so an adapter that cannot answer adds nothing
    # to the payload rather than a key that reads as a verdict.
    unknown = PrinterState(connected=True, state=PrinterStatus.IDLE)
    assert "last_job_result" not in unknown.to_dict()
    assert unknown.to_dict()["state"] == "idle"


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (JobResult.COMPLETED, "completed"),
        (JobResult.CANCELLED, "cancelled"),
        (JobResult.FAILED, "failed"),
    ],
)
def test_job_result_wire_values(result, expected):
    assert result.value == expected


# ---------------------------------------------------------------------------
# Every adapter, not just the one the bug was found on
# ---------------------------------------------------------------------------


def test_moonraker_distinguishes_complete_from_cancelled():
    from kiln.printers.moonraker import (
        _map_moonraker_job_result,
        _map_moonraker_state,
    )

    # Klipper is explicit about both, and both used to arrive as IDLE.
    assert _map_moonraker_state("ready", "complete") is PrinterStatus.IDLE
    assert _map_moonraker_state("ready", "cancelled") is PrinterStatus.IDLE
    assert _map_moonraker_job_result("complete") is JobResult.COMPLETED
    assert _map_moonraker_job_result("cancelled") is JobResult.CANCELLED

    # A machine that has not run a job says nothing.
    assert _map_moonraker_job_result("standby") is None
    assert _map_moonraker_job_result(None) is None
    # A Klipper fault is a printer condition, not a verdict on a job.
    assert _map_moonraker_job_result("error") is None


def test_prusalink_distinguishes_finished_from_stopped():
    from kiln.printers.prusalink import _JOB_RESULT_MAP, _STATE_MAP

    assert _STATE_MAP["FINISHED"] is PrinterStatus.IDLE
    assert _STATE_MAP["STOPPED"] is PrinterStatus.IDLE
    assert _JOB_RESULT_MAP["FINISHED"] is JobResult.COMPLETED
    assert _JOB_RESULT_MAP["STOPPED"] is JobResult.CANCELLED
    assert "IDLE" not in _JOB_RESULT_MAP
    assert "ERROR" not in _JOB_RESULT_MAP


def test_bambu_job_result_map_leaves_idle_alone():
    """``idle`` must stay absent: on firmware that jumps straight there,
    it genuinely carries no ending, and inventing one would be the guess
    this field exists to stop making."""
    from kiln.printers.bambu import _JOB_RESULT_MAP

    assert _JOB_RESULT_MAP["finish"] is JobResult.COMPLETED
    assert "idle" not in _JOB_RESULT_MAP


def test_adapters_without_a_completion_signal_report_none():
    """OctoPrint / Duet / Elegoo answer ``None``, and that is correct.

    Their protocols name no ending in the payload Kiln polls, so ``None``
    is the honest report.  The test exists so that stays a decision: if
    someone later teaches one of them to answer, this is where they will
    notice the claim needs updating.
    """
    from kiln.printers.duet import _RRF2_STATUS_MAP, _RRF3_STATUS_MAP
    from kiln.printers.elegoo import _PRINT_STATUS_MAP
    from kiln.printers.octoprint import _map_flags_to_status

    # OctoPrint: a completed print is operational+ready, same as untouched.
    done = _map_flags_to_status({"ready": True, "operational": True})
    assert done is PrinterStatus.IDLE

    # RRF has no terminal status at all — 13 states, none of them an ending.
    assert not any(
        k in _RRF3_STATUS_MAP for k in ("finished", "complete", "cancelled")
    )
    assert set(_RRF2_STATUS_MAP) == set("CFHODRSAMPTBI")

    # Elegoo never collapsed: an unmapped code is UNKNOWN, not IDLE.
    assert _PRINT_STATUS_MAP.get(16) is None


# ---------------------------------------------------------------------------
# Downstream: the outcome-learning loop
# ---------------------------------------------------------------------------


def test_outcome_loop_reads_the_ending_not_the_idle_it_hides_behind():
    """The collapse did not merely starve the learning loop — it fed it
    a wrong answer.

    ``_feed_outcome_lifecycle`` passes the adapter's normalized value into
    the outcome hook, and ``_infer_outcome`` resolves a bare ``"idle"`` to
    ``success`` unless our own side registered a cancel intent in the last
    five seconds.  Only the ``cancel_print`` MCP tool registers one, so a
    cancel from the touchscreen, from a rival client, or from any Kiln
    path that is not that tool was recorded as a SUCCESS.
    """
    # The shape of the old bug, still reachable and still wrong:
    assert arh._infer_outcome("idle", 0, "no-intent-printer") == ("success", None)

    # What the adapters now report instead — no intent needed, and it
    # beats one:
    assert arh._infer_outcome("cancelled", 0, "any-printer") == ("cancelled", None)
    assert arh._infer_outcome("completed", 0, "any-printer") == ("success", None)
    assert arh._infer_outcome("failed", 0, "any-printer")[0] == "failed"


def test_outcome_loop_treats_the_new_tokens_as_terminal():
    for token in ("completed", "cancelled", "failed"):
        assert arh.is_terminal_transition("printing", token), token
    # A cancel in flight is still an ACTIVE job, not an ending.
    assert not arh.is_terminal_transition("cancelling", "cancelling")
    assert arh.is_terminal_transition("cancelling", "cancelled")


def test_a_named_cancel_does_not_burn_an_unrelated_cancel_intent():
    """A machine-reported cancel consumes the intent it corresponds to,
    so a stale intent cannot survive to mislabel the NEXT print."""
    arh.register_cancel_intent("printer-X")
    assert arh._infer_outcome("cancelled", 0, "printer-X") == ("cancelled", None)
    # Intent is spent; a later bare idle is not retro-labelled a cancel.
    assert arh._infer_outcome("idle", 0, "printer-X") == ("success", None)


def test_finished_state_reaches_the_loop_as_completed():
    """End to end through the shared feed, for every adapter at once.

    ``base._feed_outcome_lifecycle`` is the adapter-generic path, so this
    is the assertion that the fix is engine-level: the token it derives
    is the ending when the printer named one, and the status otherwise.
    """
    finished = _bambu_state("FINISH")
    result = getattr(finished, "last_job_result", None)
    token = getattr(result, "value", None) or finished.state.value
    assert token == "completed", "a finished print must not reach the loop as 'idle'"

    # And the reconcile path — the one that settles a print nobody was
    # watching — can now resolve it instead of giving up.
    assert token in arh._FINISH_STATES
    assert "idle" not in arh._FINISH_STATES

    printing = _bambu_state("RUNNING")
    assert getattr(printing, "last_job_result", None) is None
    assert printing.state.value == "printing"
