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
        elif kind == "free_account":
            _sign_in(tmp_path, tier="free")
            answer = {"tier": "free"}
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


# ---------------------------------------------------------------------------
# every door that names a plan beside an answer or a refusal
# ---------------------------------------------------------------------------


def _material_that_bonds_badly():
    from kiln.design_intelligence import MaterialProfile

    return MaterialProfile(
        material_id="tpu", display_name="TPU", category="flexible", thermal={}, chemical={},
        bonding={"hard_to_bond": True},
    )


def _a_window_over_two_printers() -> str:
    from kiln import consent_windows

    with pytest.raises(consent_windows.NotTheFleetTier) as refused:
        consent_windows._checked_scope(["ender", "voron"])
    return str(refused.value)


def _say(door: str) -> str:
    """What *door* says about its plan, as text."""
    from kiln import consent_windows
    from kiln.decoration_quota import QuotaStatus, _quota_exceeded_error
    from kiln.design_intelligence import _format_upgrade_nudge, _upgrade_hint, unestablished_caution
    from kiln.design_reasoning import _print_aware_note
    from kiln.emboss_generator import _qr_unavailable_message
    from kiln.load_bearing_detector import _build_upgrade_recommendation
    from kiln.plugins.slicer_tools import _spots_clause
    from kiln.plugins.tier_diagnostic_tools import _build_response
    from kiln.scheduler import turn_plan_line
    from kiln.server import _drying_advisor_line, get_material_properties
    from kiln.slicer_profiles import get_slicer_profile, slicer_profile_to_dict

    return {
        "troubleshooting hint": lambda: _upgrade_hint("material_troubleshooting"),
        "post-processing hint": lambda: _upgrade_hint("post_processing"),
        "environment hint": lambda: _upgrade_hint("environment_compatibility"),
        "unestablished caution": lambda: unestablished_caution("load_tables", "No load figure was established here."),
        "load-bearing warning": lambda: _format_upgrade_nudge(None),
        "load-bearing block": lambda: json.dumps(_build_upgrade_recommendation(["engineering-grade material"])),
        "bonding caveat": lambda: _material_that_bonds_badly().bonding_caveat(),
        "edge finishing note": lambda: _print_aware_note({"edges": {"cautions": ["one"]}}, False, "M3 bolt"),
        "decoration allowance": lambda: json.dumps(_quota_exceeded_error(QuotaStatus(3, 3, 0, "free", "2026-11", False))),
        "slicer profile pointer": lambda: json.dumps(slicer_profile_to_dict(get_slicer_profile("default"))),
        "spots beside a part": lambda: _spots_clause({"spots": [], "spots_found": 2, "record": {"quiet_start": True}}),
        "always allow": consent_windows._always_allow_refusal,
        "plan diagnostic": lambda: _build_response("free", [], "account_plan", "the account is on free")["agent_summary"],
        "chemical pointer": lambda: get_material_properties("pla")["chemical_resistance"]["note"],
        "QR code": _qr_unavailable_message,
        "queue held to one printer": turn_plan_line,
        "window over two printers": _a_window_over_two_printers,
        "wet filament tip": _drying_advisor_line,
    }[door]()


#: The plan each door names, where it is not Kiln Pro.
PLAN_OF = {
    "QR code": "business",
    "queue held to one printer": "business",
    "window over two printers": "business",
}
DOORS = (
    "troubleshooting hint", "post-processing hint", "environment hint", "unestablished caution",
    "load-bearing warning", "load-bearing block", "bonding caveat", "edge finishing note",
    "decoration allowance", "slicer profile pointer", "spots beside a part", "always allow",
    "plan diagnostic", "chemical pointer", "QR code", "queue held to one printer", "window over two printers",
    "wet filament tip",
)


@pytest.fixture
def no_curated_tables(monkeypatch):
    """The curated tables were not read here, as on an install without kiln-pro."""
    from kiln import design_intelligence

    monkeypatch.setattr(design_intelligence, "_engineering_overlay_loaded", lambda _kind: False)


@pytest.mark.parametrize("kind", ("signed_out", "below_the_plan", *OWNERS))
@pytest.mark.parametrize("door", DOORS)
def test_a_door_sells_its_plan_only_to_someone_who_would_buy_it(caller, no_curated_tables, door, kind):
    plan = PLAN_OF.get(door, "pro")
    if kind == "below_the_plan":
        caller("smaller_plan" if plan == "business" else "free_account")
    else:
        caller(kind)
    said = _say(door)
    if kind in ("signed_out", "below_the_plan"):
        assert "kiln3d.com/pricing" in said, said
    else:
        assert "kiln3d.com/pricing" not in said and _says_the_step_back(kind, said), said


#: What a nudge beside an answer still says to someone whose plan includes
#: what it points at: the caution, and nothing about the plan.
STILL_SAID = {
    "troubleshooting hint": "",
    "unestablished caution": "No load figure was established here.",
    "load-bearing warning": "doesn't account for cross-section shape",
    "load-bearing block": "This part looks load-bearing",
    "bonding caveat": "TPU bonds poorly with common glues.",
    "edge finishing note": "will print badly as asked",
    "slicer profile pointer": "get_printer_profile",
    "chemical pointer": "check_chemical_resistance",
    "wet filament tip": "drying_advisor gives the safe drying temperature",
}


@pytest.mark.parametrize("door", sorted(STILL_SAID))
def test_a_nudge_says_only_its_caution_to_someone_whose_plan_includes_it(caller, no_curated_tables, door):
    caller("holds_it", owns="enterprise")
    said = _say(door)
    assert "pricing" not in said and "requires_tier" not in said and "Kiln Pro shows" not in said, said
    assert STILL_SAID[door] in said if STILL_SAID[door] else said == ""


@pytest.mark.parametrize("kind", ("signed_out", "smaller_plan", *OWNERS, "holds_it"))
def test_encryption_status_on_this_machine_names_no_plan(caller, kind):
    """No plan makes this machine encrypt the G-code it stores, so its answer
    sells none, to anyone."""
    from kiln.plugins.enterprise_tools import _encryption_not_installed

    caller(kind, owns="enterprise")
    message = _encryption_not_installed()["message"]
    assert message.startswith("G-code encryption at rest is not installed on this machine")
    assert "pricing" not in message and "Kiln Enterprise" not in message, message


@pytest.mark.parametrize("kind", SOLD + OWNERS)
def test_a_second_live_watch(caller, monkeypatch, kind):
    import kiln.registry as registry
    from kiln import server

    caller(kind)
    monkeypatch.setattr(server, "_watched_machines", lambda *_a, **_k: {"voron"})
    monkeypatch.setattr(registry, "machine_fingerprint", lambda _adapter: "ender")
    refusal = server._watch_capacity_error(object(), "ender")["error"]
    assert refusal["code"] == "TIER_CONCURRENT_WATCH_LIMIT" and "printer_status" in refusal["message"]
    if kind in SOLD:
        assert "kiln3d.com/pricing" in refusal["message"]
    else:
        assert "pricing" not in refusal["message"] and _says_the_step_back(kind, refusal["message"]), refusal


@pytest.mark.parametrize("kind", ("signed_out", "free_account", "offline"))
def test_the_free_recovery_plan(caller, kind):
    from kiln._pro_recovery_bridge import Asked, floor_note

    caller(kind)
    note = floor_note(Asked(), "the cause on your model, and the fix")
    assert note["floor"] is True and note["pro_adds"]
    if kind == "offline":
        assert "upgrade_url" not in note and _says_the_step_back(kind, note["plan_note"]), note
    else:
        assert note["upgrade_url"].startswith("https://kiln3d.com/pricing") and "plan_note" not in note


@pytest.mark.parametrize("kind", ("free_account", *OWNERS))
def test_the_plan_this_machine_shows(caller, capsys, kind):
    from kiln.cli.main import _echo_account_plan

    caller(kind)
    _echo_account_plan(json_mode=False)
    shown = capsys.readouterr().out
    if kind == "free_account":
        assert "kiln3d.com/pricing" in shown
    else:
        assert "pricing" not in shown, shown
    if kind == "offline":
        assert _says_the_step_back(kind, shown), shown


def test_the_qr_stub_says_what_the_qr_door_says(caller):
    from kiln.emboss_generator import _qr_unavailable_message
    from kiln.image_to_surface import generate_qr_data

    caller("offline")
    with pytest.raises(ImportError) as missing:
        generate_qr_data("https://example.com", "/nowhere")
    assert str(missing.value) == _qr_unavailable_message() and "pricing" not in str(missing.value)


def test_a_second_live_watch_on_the_plan_that_includes_it(caller, monkeypatch):
    import kiln.registry as registry
    from kiln import server

    caller("holds_it")
    monkeypatch.setattr(server, "_watched_machines", lambda *_a, **_k: {"voron"})
    monkeypatch.setattr(registry, "machine_fingerprint", lambda _adapter: "ender")
    assert server._watch_capacity_error(object(), "ender") is None
