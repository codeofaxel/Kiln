"""Where the fleet's plates are not read on this computer, the note says so plainly.

``suggest_printer_for_job`` carries ``has_room`` per suggestion from the fleet
plate survey.  On an install without the survey (every customer's) it used to
say "Plate room is not read on this server: fleet placement needs kiln-pro"
to someone on their own laptop, at every plan: a package nobody can install
and a server that is not there.  Now the note points at the door that does
read every plate (``route_print_job``, which asks Kiln's servers on such an
install) and, below the plan that runs several printers at once, adds once
the plan that includes it, in the words the account's own gate uses for the
state this machine is in.  A plan that holds it is never pitched.

The plan is read the way a plain install reads it (``kiln.account_plan``,
the signed-in account's saved plan), with the account's own answer stood in
as "no answer", so nothing reaches the network.

And ``route_print_job``'s survey refusal, reached only where a survey older
than this release is on the computer, names no package and no server.
"""

from __future__ import annotations

import sys
import time
import types
from types import SimpleNamespace

import pytest

from tests.test_fleet_plate_doors import _cube, doors  # noqa: F401 -- fixture

_PRICING = "kiln3d.com/pricing"


@pytest.fixture
def account(monkeypatch):
    """The plan this machine's saved sign-in holds, read as a plain install reads it."""
    import kiln.auth_session as auth_session
    import kiln.licensing as licensing
    from kiln import account_plan

    monkeypatch.setattr(licensing, "LicenseTier", account_plan.LicenseTier, raising=False)
    monkeypatch.setattr(licensing, "check_tier", account_plan.check_tier, raising=False)
    monkeypatch.setattr(account_plan, "_ask_account", lambda: None)
    monkeypatch.setattr(account_plan, "_last_ask_at", 0.0)
    monkeypatch.setattr(account_plan, "_last_ask_answered", True)

    def signed_in(tier: str | None, **extra) -> None:
        stored: dict = {}
        if tier is not None:
            stored = {"access_token": "tok", "refresh_token": "ref", "tier": tier,
                      "email": "maker@example.com", "plan_checked_at": time.time(), **extra}
        monkeypatch.setattr(auth_session, "_read_tokens", lambda: dict(stored))

    return signed_in


@pytest.fixture
def plain_install(monkeypatch):
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


def _suggest(doors, monkeypatch) -> dict:  # noqa: F811 -- the fixture, passed in
    import kiln.persistence as persistence

    ranked = [{"printer_name": "a", "success_rate": 0.9, "total_prints": 4},
              {"printer_name": "b", "success_rate": 0.8, "total_prints": 2}]
    monkeypatch.setattr(persistence, "get_db", lambda: SimpleNamespace(suggest_printer_for_outcome=lambda **kw: list(ranked)))
    d = doors(["a", "b"], survey_missing=True)
    result = d.tools["suggest_printer_for_job"](material_type="PLA")
    assert result["success"] is True, result
    assert [s["has_room"] for s in result["suggestions"]] == [None, None]
    assert [s["plate"] for s in result["suggestions"]] == [None, None]
    return result


def _plain_words(note: str) -> None:
    for word in ("kiln-pro", "kiln_pro", "this server", "on this server"):
        assert word not in note, f"{word!r} in the note: {note!r}"
    assert "route_print_job" in note, note


@pytest.mark.parametrize("tier", ["business", "enterprise"])
def test_a_plan_with_the_fleet_is_pointed_at_the_door_that_reads_the_plates_and_pitched_nothing(
    doors, monkeypatch, account, plain_install, tier,  # noqa: F811
):
    account(tier)
    note = _suggest(doors, monkeypatch)["plate_note"]
    _plain_words(note)
    for pitch in ("Business", "Enterprise", "Pro", _PRICING, "upgrade"):
        assert pitch not in note, f"a {tier} caller was pitched {pitch!r}: {note!r}"


@pytest.mark.parametrize(
    ("tier", "also"),
    [
        (None, "Already subscribed?"),  # signed out
        ("free", "on the Free plan"),
        ("pro", "on the Pro plan"),
    ],
)
def test_below_the_fleet_plan_the_plan_that_includes_it_is_said_once(
    doors, monkeypatch, account, plain_install, tier, also,  # noqa: F811
):
    account(tier)
    note = _suggest(doors, monkeypatch)["plate_note"]
    _plain_words(note)
    assert note.startswith("These suggestions do not read the printers' plates on this computer.")
    assert note.count("needs Kiln Business") == 1, note
    assert also in note and _PRICING in note, note


def test_a_session_that_ended_is_told_to_sign_back_in(doors, monkeypatch, account, plain_install):  # noqa: F811
    """Lapsed: a Business sign-in the server refused grants no plan here."""
    account("business", refresh_token="", refresh_rejected_at=time.time())
    note = _suggest(doors, monkeypatch)["plate_note"]
    _plain_words(note)
    assert "session" in note and "expired" in note and "Signing in again" in note, note


def test_a_plan_that_cannot_be_read_names_none(doors, monkeypatch, plain_install):  # noqa: F811
    import kiln.licensing as licensing

    def broken(*_a, **_k):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(licensing, "check_tier", broken, raising=False)
    note = _suggest(doors, monkeypatch)["plate_note"]
    _plain_words(note)
    assert "Business" not in note and _PRICING not in note


def test_an_older_survey_on_this_computer_is_refused_in_plain_words(doors, monkeypatch, tmp_path):  # noqa: F811
    """route_print_job found the survey module, and it has no fleet survey."""
    d = doors(["a"])
    monkeypatch.setitem(sys.modules, "kiln.placement_fleet", types.ModuleType("kiln.placement_fleet"))
    result = d.tools["route_print_job"](_cube(tmp_path / "cube.stl"), material="PLA")
    assert result["success"] is False and result["error"]["code"] == "ROUTING_UNAVAILABLE"
    message = result["error"]["message"]
    for word in ("kiln-pro", "kiln_pro", "this server", "installed"):
        assert word not in message, f"{word!r} in the refusal: {message!r}"
    assert "nothing was routed" in message and "start_print" in message
    assert d.router.seen == [], "nothing is routed past a survey that did not run"
