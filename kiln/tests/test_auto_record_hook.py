"""Tests for the terminal-state auto-record hook (Bug #10)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiln import auto_record_hook as hook


@pytest.fixture(autouse=True)
def _reset_hook_state():
    """Reset the module-level _HOOK_STATE between tests so debounce
    and idempotency ledgers don't leak across test boundaries."""
    hook._HOOK_STATE = hook._HookState()
    yield


@pytest.fixture
def mock_record():
    """Intercept record_print_outcome so tests can inspect the call
    without touching a real learning DB."""
    with patch(
        "kiln.plugins.learning_tools.record_print_outcome",
        return_value={"success": True, "recorded": True},
    ) as m:
        yield m


# ---------------------------------------------------------------------------
# fire_terminal_state_hook
# ---------------------------------------------------------------------------


def test_fires_success_on_printing_to_finish(mock_record):
    result = hook.fire_terminal_state_hook(
        prev_state="running",
        new_state="finish",
        print_error_code=0,
        printer_name="bambu-a1",
        job_id="job-123",
    )
    assert result is not None
    mock_record.assert_called_once()
    kwargs = mock_record.call_args.kwargs
    assert kwargs["outcome"] == "success"
    assert kwargs["auto_recorded"] is True
    assert kwargs["job_id"] == "job-123"
    assert kwargs["printer_name"] == "bambu-a1"


def test_fires_failed_with_hms_inferred_mode(mock_record):
    # HMS family 0x07 → filament_runout
    result = hook.fire_terminal_state_hook(
        prev_state="running",
        new_state="failed",
        print_error_code=0x07_00_02_00,
        printer_name="bambu-a1",
        job_id="job-456",
    )
    assert result is not None
    kwargs = mock_record.call_args.kwargs
    assert kwargs["outcome"] == "failed"
    assert kwargs["failure_mode"] == "filament_runout"


def test_fires_failed_with_unknown_hms_falls_back_to_other(mock_record):
    result = hook.fire_terminal_state_hook(
        prev_state="paused",
        new_state="failed",
        print_error_code=0xAB_CD_EF_00,  # arbitrary unknown family
        printer_name="bambu-a1",
        job_id="job-789",
    )
    assert result is not None
    kwargs = mock_record.call_args.kwargs
    assert kwargs["outcome"] == "failed"
    assert kwargs["failure_mode"] == "other"


def test_fires_failed_servo_overload_maps_to_mechanical(mock_record):
    # HMS 0300-0900 (P2S servo extruder overload / clog) -> mechanical, not
    # the generic "other" bucket.  Narrow: a different 0300 code stays "other".
    result = hook.fire_terminal_state_hook(
        prev_state="running",
        new_state="failed",
        print_error_code=0x03_00_09_00,
        printer_name="bambu-p2s",
        job_id="job-p2s",
    )
    assert result is not None
    kwargs = mock_record.call_args.kwargs
    assert kwargs["outcome"] == "failed"
    assert kwargs["failure_mode"] == "mechanical"


def test_fires_failed_other_0300_code_stays_other(mock_record):
    # A non-servo 0300-family code is NOT swept into "mechanical".
    result = hook.fire_terminal_state_hook(
        prev_state="running",
        new_state="failed",
        print_error_code=0x03_00_8014,
        printer_name="bambu-a1",
        job_id="job-clump",
    )
    assert result is not None
    assert mock_record.call_args.kwargs["failure_mode"] == "other"


def test_fires_cancelled_when_cancel_intent_registered(mock_record):
    hook.register_cancel_intent("bambu-a1")
    result = hook.fire_terminal_state_hook(
        prev_state="running",
        new_state="idle",
        print_error_code=0,
        printer_name="bambu-a1",
        job_id="job-cancel-1",
    )
    assert result is not None
    kwargs = mock_record.call_args.kwargs
    assert kwargs["outcome"] == "cancelled"


def test_idle_without_cancel_intent_records_success(mock_record):
    """Some Bambu firmware versions jump printing→idle (skipping
    the "finish" state) for a natural finish.  Without a cancel
    intent, that's a success."""
    result = hook.fire_terminal_state_hook(
        prev_state="running",
        new_state="idle",
        print_error_code=0,
        printer_name="bambu-a1",
        job_id="job-natural-finish",
    )
    assert result is not None
    kwargs = mock_record.call_args.kwargs
    assert kwargs["outcome"] == "success"


def test_no_fire_when_prev_not_active(mock_record):
    """idle→idle, idle→busy, busy→idle during boot should NOT record."""
    result = hook.fire_terminal_state_hook(
        prev_state="idle",
        new_state="idle",
        print_error_code=0,
        printer_name="bambu-a1",
        job_id="job-noop",
    )
    assert result is None
    mock_record.assert_not_called()


def test_no_fire_without_job_id(mock_record):
    result = hook.fire_terminal_state_hook(
        prev_state="running",
        new_state="finish",
        print_error_code=0,
        printer_name="bambu-a1",
        job_id="",
    )
    assert result is None
    mock_record.assert_not_called()


def test_idempotent_per_job(mock_record):
    """Repeat terminal transitions for the same (printer, job_id) only
    record once.  Protects against MQTT message replay."""
    hook.fire_terminal_state_hook(
        prev_state="running",
        new_state="finish",
        print_error_code=0,
        printer_name="bambu-a1",
        job_id="job-idempotent",
    )
    result_2 = hook.fire_terminal_state_hook(
        prev_state="running",
        new_state="finish",
        print_error_code=0,
        printer_name="bambu-a1",
        job_id="job-idempotent",
    )
    assert result_2 is None
    assert mock_record.call_count == 1


def test_cancel_intent_is_single_consumer(mock_record):
    """First terminal-idle transition after register_cancel_intent
    claims the intent; subsequent idle transitions on the same printer
    are treated as natural finish until a new intent is registered."""
    hook.register_cancel_intent("bambu-a1")
    hook.fire_terminal_state_hook(
        prev_state="running",
        new_state="idle",
        print_error_code=0,
        printer_name="bambu-a1",
        job_id="job-a",
    )
    hook.fire_terminal_state_hook(
        prev_state="running",
        new_state="idle",
        print_error_code=0,
        printer_name="bambu-a1",
        job_id="job-b",
    )
    outcomes = [c.kwargs["outcome"] for c in mock_record.call_args_list]
    assert outcomes == ["cancelled", "success"]


def test_observe_state_tracks_previous():
    """observe_state returns the previously-recorded state for a
    printer and updates the store in place."""
    assert hook.observe_state("bambu-a1", "idle") is None
    assert hook.observe_state("bambu-a1", "running") == "idle"
    assert hook.observe_state("bambu-a1", "running") == "running"
    # Per-printer tracking
    assert hook.observe_state("bambu-x1c", "running") is None


def test_hms_family_0xc_is_adhesion(mock_record):
    # Bambu HMS codes are 32-bit; 0x0C000100 is family 0x0C (adhesion)
    hook.fire_terminal_state_hook(
        prev_state="running",
        new_state="failed",
        print_error_code=0x0C_00_01_00,
        printer_name="bambu-a1",
        job_id="hms-0c",
    )
    kwargs = mock_record.call_args.kwargs
    assert kwargs["failure_mode"] == "adhesion"


def test_hms_family_0x5_is_mechanical(mock_record):
    hook.fire_terminal_state_hook(
        prev_state="running",
        new_state="failed",
        print_error_code=0x05_04_00_00,
        printer_name="bambu-a1",
        job_id="hms-05",
    )
    kwargs = mock_record.call_args.kwargs
    assert kwargs["failure_mode"] == "mechanical"


def test_expired_cancel_intent_does_not_leak_past_ttl():
    """An intent cannot bleed into an unrelated subsequent print.

    That is the invariant.  It used to be enforced by the in-memory TTL
    alone, and this test asserted the TTL directly — but five seconds was a
    guess at how long a printer takes to stop moving, retract, park and
    report idle, and a cancel that took longer expired before its own
    ending arrived and was recorded a success.  The intent now also has a
    durable half so it can cross a process boundary, and it is bounded by
    ``_CANCEL_INTENT_MAX_AGE_S`` plus an exact clear when the next print
    starts.  Both bounds are asserted here; the clear is covered in
    ``test_every_stop_says_so``.
    """
    import time as _time

    base = _time.monotonic()
    with patch.object(hook.time, "monotonic", side_effect=[
        base,              # register_cancel_intent's capture
        base + hook._CANCEL_INTENT_TTL_S + 1,  # consume attempt, post-TTL
    ]):
        hook.register_cancel_intent("bambu-a1")
        # The durable half is what answers now, so age it past its own bound
        # rather than asserting the in-memory window in isolation.
        hook._write_durable_cancel_intent(
            "bambu-a1", _time.time() - (hook._CANCEL_INTENT_MAX_AGE_S + 60)
        )
        assert hook._HOOK_STATE.consume_cancel_intent("bambu-a1") is False
