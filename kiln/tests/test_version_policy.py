"""The version-awareness evaluator: verdicts, precedence, and safe defaults."""

from __future__ import annotations

from kiln.version_policy import (
    AVAILABLE,
    OK,
    RECOMMENDED,
    REQUIRED,
    evaluate,
)


def test_unknown_current_is_never_nudged():
    for bad in ("", "unknown", None):
        v = evaluate(bad)  # type: ignore[arg-type]
        assert v.state == OK
        assert v.blocking is False
        assert v.offer is None


def test_up_to_date_is_ok():
    v = evaluate("1.2.0", latest="1.2.0", recommended="1.1.0", floor="1.0.0")
    assert v.state == OK
    assert v.target is None
    assert v.blocking is False


def test_merely_behind_latest_is_available_and_optional():
    v = evaluate("1.2.0", latest="1.3.0")
    assert v.state == AVAILABLE
    assert v.target == "1.3.0"
    assert v.blocking is False
    assert v.offer and "whenever you like" in v.offer.lower()


def test_below_recommended_is_recommended():
    v = evaluate("1.1.0", latest="1.3.0", recommended="1.2.0")
    assert v.state == RECOMMENDED
    assert v.blocking is False
    # steer toward the newest thing we know, not just the recommended floor.
    assert v.target == "1.3.0"


def test_below_floor_is_required_and_blocking():
    v = evaluate("1.0.0", floor="1.2.0", reason="the updated terms of use")
    assert v.state == REQUIRED
    assert v.blocking is True
    assert v.target == "1.2.0"
    assert v.reason == "the updated terms of use"
    # Apple-grade: an OFFER to do it, not a command to run.
    assert v.offer and "want me to update it for you" in v.offer.lower()
    assert "pip install" not in v.offer  # the command never leaks into the offer


def test_floor_steers_to_latest_when_latest_is_newer_than_floor():
    v = evaluate("1.0.0", latest="1.5.0", floor="1.2.0")
    assert v.state == REQUIRED
    assert v.target == "1.5.0"  # land them on the newest, not just the floor


def test_required_precedence_over_recommended_and_available():
    v = evaluate("1.0.0", latest="1.4.0", recommended="1.3.0", floor="1.2.0")
    assert v.state == REQUIRED  # strictest applicable verdict wins
    assert v.blocking is True


def test_to_block_shape_carries_offer_and_command():
    block = evaluate("1.0.0", floor="1.2.0").to_block()
    assert block["state"] == REQUIRED
    assert block["blocking"] is True
    assert block["target"] == "1.2.0"
    assert block["command"].startswith("pip install --upgrade")
    assert "offer" in block
    ok_block = evaluate("1.2.0", latest="1.2.0").to_block()
    assert ok_block["state"] == OK
    assert "offer" not in ok_block  # nothing to offer when current
