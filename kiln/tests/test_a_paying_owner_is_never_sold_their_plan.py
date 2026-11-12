"""Someone who pays for a plan this install's sign-in no longer proves is told
how to get it back, never sold it.

A sign-in the server refused, or a paid plan this machine has not been able
to confirm for longer than the offline grace, reads as Free to every gate.
The plan check (``kiln.account_plan``) words each state with the step that
works.  Every door here takes its words, and its pitch, from
:func:`kiln.tiers_and_terms.plan_offer`, and is walked as each kind of
caller: signed out, signed in on a smaller plan, an owner whose sign-in was
refused, and an owner offline too long.
"""

from __future__ import annotations

import json
import time

import pytest

from kiln import account_plan, licensing
from kiln.tiers_and_terms import plan_offer

#: The callers a door holding back a plan's feature meets.  The first two are
#: sold it; the owners are told the step that brings it back.
SOLD = ("signed_out", "smaller_plan")
OWNERS = ("signin_refused", "offline")


@pytest.fixture
def caller(monkeypatch, tmp_path):
    """``caller(kind, owns)`` puts this machine in one sign-in state, with
    the plan check of an install that has no kiln-pro deciding."""
    from tests.test_account_plan import _sign_in

    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
    monkeypatch.setattr(account_plan, "_last_ask_at", 0.0, raising=False)
    monkeypatch.setattr(account_plan, "_last_ask_answered", True, raising=False)
    monkeypatch.setattr(licensing, "check_tier", account_plan.check_tier)
    monkeypatch.setattr(licensing, "get_tier", account_plan.get_tier, raising=False)
    monkeypatch.setattr(licensing, "max_printers_for_tier", account_plan.max_printers_for_tier, raising=False)

    def _as(kind: str, owns: str = "business") -> None:
        answer = None
        if kind == "smaller_plan":
            _sign_in(tmp_path, tier="pro")
            answer = {"tier": "pro"}
        elif kind == "signin_refused":
            _sign_in(tmp_path, tier=owns, refresh_token="", refresh_rejected_at=int(time.time()))
        elif kind == "offline":
            _sign_in(tmp_path, tier=owns, checked_ago_s=8 * 24 * 3600)
        elif kind == "holds_it":
            _sign_in(tmp_path, tier=owns)
            answer = {"tier": owns}
        monkeypatch.setattr(account_plan, "_ask_account", lambda: answer)

    return _as


def _says_the_step_back(kind: str, text: str) -> bool:
    """The plan check's own words for an owner: sign back in, or get online."""
    lowered = text.lower()
    return "signing in again" in lowered if kind == "signin_refused" else "reach kiln" in lowered


# ---------------------------------------------------------------------------
# the one helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", SOLD)
def test_a_caller_who_does_not_hold_the_plan_is_offered_it(caller, kind):
    caller(kind)
    offer = plan_offer("business", "Every printer at once")
    assert offer.sells is True and offer.covered is False
    assert "kiln3d.com/pricing" in offer.words and offer.words.startswith("Every printer at once")


@pytest.mark.parametrize("kind", OWNERS)
def test_an_owner_is_told_the_step_that_brings_it_back(caller, kind):
    caller(kind)
    offer = plan_offer("business", "Every printer at once")
    assert offer.sells is False and offer.covered is False
    assert "pricing" not in offer.words and _says_the_step_back(kind, offer.words), offer.words


def test_a_caller_who_holds_the_plan_is_covered(caller):
    caller("holds_it")
    offer = plan_offer("business", "Every printer at once")
    assert offer.covered is True and offer.words == ""


def test_a_plan_check_that_cannot_be_read_leaves_the_door_its_own_words(monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("no plan here")

    monkeypatch.setattr(licensing, "check_tier", broken)
    offer = plan_offer("business")
    assert (offer.words, offer.sells, offer.covered) == ("", True, False)


def test_a_plan_check_that_takes_no_subject_is_still_asked(monkeypatch):
    monkeypatch.setattr(licensing, "check_tier", lambda required: (False, "This feature needs Kiln Business. "
                                                                    "See https://kiln3d.com/pricing"))
    offer = plan_offer("business", "Running printers side by side")
    assert offer.sells is True and offer.words.startswith("Running printers side by side needs Kiln Business")


# ---------------------------------------------------------------------------
# the doors
# ---------------------------------------------------------------------------


@pytest.fixture
def two_printers_one_busy(monkeypatch):
    import kiln.registry as registry_mod
    from kiln.printers.base import PrinterStatus
    from tests.test_printer_identity_and_fleet_gate import _adapter, _FakeRegistry

    busy = _adapter(host="192.0.2.10", state=PrinterStatus.PRINTING)
    idle = _adapter(host="192.0.2.11")
    monkeypatch.setattr(registry_mod, "get_registry", lambda: _FakeRegistry({"voron": busy, "ender": idle}))
    return idle


@pytest.mark.parametrize("kind", SOLD + OWNERS)
def test_starting_a_second_printer(caller, two_printers_one_busy, kind):
    from kiln.printers.print_gate import _concurrent_fleet_verdict

    caller(kind)
    verdict = _concurrent_fleet_verdict(two_printers_one_busy)
    assert verdict is not None and verdict["code"] == "TIER_CONCURRENT_PRINT_LIMIT"
    assert "voron" in verdict["reason"]
    if kind in SOLD:
        assert "pricing" in verdict["override_hint"] and "upgrade_nudge" in verdict
    else:
        assert "pricing" not in json.dumps(verdict) and "upgrade_nudge" not in verdict
        assert _says_the_step_back(kind, verdict["override_hint"]), verdict["override_hint"]
        assert verdict["override_hint"].startswith("Wait for it to finish"), "the free path still comes first"


@pytest.mark.parametrize("kind", SOLD + OWNERS)
def test_the_one_printer_rule_with_nothing_engaged(caller, kind):
    from kiln.printers import engagement

    caller(kind)
    verdict = engagement._refusal(None, "status", "machine-b")
    assert verdict["code"] == "TIER_SINGLE_PRINTER_LIMIT"
    if kind in SOLD:
        assert verdict["upgrade_nudge"]["cta"]["tier"] == "business"
    else:
        assert "upgrade_nudge" not in verdict and "pricing" not in json.dumps(verdict)
        assert _says_the_step_back(kind, verdict["suggestions"][-1]), verdict["suggestions"]


@pytest.mark.parametrize("kind", SOLD + OWNERS)
def test_the_one_printer_rule_while_another_printer_is_engaged(caller, kind):
    from kiln.printers import engagement

    caller(kind)
    engaged = engagement.Engagement(machine="machine-a", label="garage", job=None, since=time.time(), reason="started")
    verdict = engagement._refusal(engaged, "pause", "machine-b")
    assert verdict["code"] == "TIER_SINGLE_PRINTER_LIMIT" and verdict["engaged_with"] == "garage"
    if kind in SOLD:
        assert verdict["upgrade_nudge"]["cta"]["tier"] == "business"
    else:
        assert "upgrade_nudge" not in verdict and "pricing" not in json.dumps(verdict)
        assert _says_the_step_back(kind, verdict["suggestions"][-1]), verdict["suggestions"]


@pytest.mark.parametrize("kind", SOLD + OWNERS)
def test_a_folder_watcher_over_every_printer(caller, capsys, kind):
    from kiln.cli.main import _refuse_a_watcher_over_every_printer

    caller(kind)
    with pytest.raises(SystemExit):
        _refuse_a_watcher_over_every_printer(["ender", "voron"], json_mode=True)
    said = capsys.readouterr().out
    assert "--printer NAME" in said and "ender, voron" in said
    if kind in SOLD:
        assert "kiln3d.com/pricing" in said
    else:
        assert "pricing" not in said and _says_the_step_back(kind, said), said


@pytest.mark.parametrize("kind", SOLD + OWNERS)
def test_grouping_printers_by_site(caller, kind):
    from kiln.server import _attach_sites

    caller(kind, owns="enterprise")
    result = {"success": True, "printers": [{"name": "voron"}]}
    _attach_sites(result, result["printers"], asked_through="printer_status")
    assert "sites" not in result and result["note"].endswith("Here is the whole fleet in one list.")
    if kind in SOLD:
        assert "Kiln Enterprise" in result["note"] and result["upgrade_url"].endswith("tool=printer_status")
        assert result["upgrade_nudge"]["cta"]["tier"] == "enterprise"
    else:
        assert "upgrade_url" not in result and "upgrade_nudge" not in result
        assert _says_the_step_back(kind, result["note"]), result["note"]
