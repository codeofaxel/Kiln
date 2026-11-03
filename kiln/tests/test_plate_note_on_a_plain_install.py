"""Where the fleet's plates are not judged on this computer, Kiln's servers judge them, and the note says so plainly.

``suggest_printer_for_job`` carries ``has_room`` per suggestion from the fleet
plate survey.  On an install without the survey (every customer's) it used to
say "Plate room is not read on this server: fleet placement needs kiln-pro"
to someone on their own laptop, at every plan; later it pointed at
``route_print_job`` and left ``has_room`` null at every plan, a Business
account included.  Inverted 2026-11-02: the door now reads each plate here
and asks Kiln's servers for the plates alone, so the plan that answers is the
servers' own, per request.  A plan that holds it gets the room and no pitch;
below it, the servers' sentence is said once; signed out or a session that
ended is said in the shared voice, the account's own state decided before
anything leaves this computer.  The servers' side of the plates-only ask is
pinned in kiln-pro.

The plan is read the way a plain install reads it (``kiln.account_plan``,
the signed-in account's saved plan), with the account's own answer stood in
as "no answer"; the served call is stood in for every signed-in case and
nothing reaches the network.

And ``route_print_job``'s survey refusal, reached only where a survey older
than this release is on the computer, names no package and no server.
"""

from __future__ import annotations

import sys
import time
import types
from types import SimpleNamespace

import pytest

from tests.test_fleet_plate_doors import _block, _cube, doors  # noqa: F401 -- fixture

_PRICING = "kiln3d.com/pricing"
_BELOW_BUSINESS = (
    "Plate room across your printers (which of them can take a print now) needs Kiln Business. "
    "Upgrade at https://kiln3d.com/pricing."
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Nothing here may reach Kiln's servers: a call that tries fails the test."""
    import urllib.request

    def refuse(*_a, **_k):
        raise AssertionError("a plate-note test reached the network")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)


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
def servers(monkeypatch):
    """Kiln's servers, answering the plates-only ask with *answer*."""
    import kiln.server as srv

    def answer_with(answer: dict) -> list[dict]:
        sent: list[dict] = []
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: sent.append({"tool": tool, **kw}) or dict(answer))
        return sent

    return answer_with


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
    return result


def _plain_words(note: str) -> None:
    for word in ("kiln-pro", "kiln_pro", "this server", "on this server"):
        assert word not in note, f"{word!r} in the note: {note!r}"


@pytest.mark.parametrize("tier", ["business", "enterprise"])
def test_a_plan_with_the_fleet_gets_the_room_from_the_servers_and_is_pitched_nothing(
    doors, monkeypatch, account, servers, plain_install, tier,  # noqa: F811
):
    account(tier)
    sent = servers({"success": True, "plates": {"a": _block(), "b": _block(status="occupied", has_room=False)}})
    result = _suggest(doors, monkeypatch)
    assert [s["has_room"] for s in result["suggestions"]] == [True, False]
    assert result["plate_note"] is None
    assert sent[0]["plates_only"] is True


@pytest.mark.parametrize("tier", ["free", "pro"])
def test_below_the_fleet_plan_the_servers_sentence_is_said_once(
    doors, monkeypatch, account, servers, plain_install, tier,  # noqa: F811
):
    account(tier)
    servers({"status": "error", "error": _BELOW_BUSINESS, "code": "TIER_REQUIRED", "required_tier": "business"})
    result = _suggest(doors, monkeypatch)
    assert [s["has_room"] for s in result["suggestions"]] == [None, None]
    note = result["plate_note"]
    _plain_words(note)
    assert note == _BELOW_BUSINESS and note.count("needs Kiln Business") == 1 and note.count(_PRICING) == 1


def test_signed_out_is_told_to_sign_in_before_anything_leaves(doors, monkeypatch, account, plain_install):  # noqa: F811
    account(None)
    result = _suggest(doors, monkeypatch)
    assert [s["has_room"] for s in result["suggestions"]] == [None, None]
    note = result["plate_note"]
    _plain_words(note)
    assert "Kiln is signed out" in note and "sign in" in note, note
    assert "Business" not in note and _PRICING not in note


def test_a_session_that_ended_is_told_to_sign_back_in(doors, monkeypatch, account, plain_install):  # noqa: F811
    """Lapsed: a Business sign-in the server refused grants no plan here."""
    account("business", refresh_token="", refresh_rejected_at=time.time())
    note = _suggest(doors, monkeypatch)["plate_note"]
    _plain_words(note)
    assert "session has expired" in note and "sign back in" in note, note


def test_the_install_reads_no_plan_of_its_own_for_the_note(doors, monkeypatch, servers, plain_install):  # noqa: F811
    """The plan that answers is the servers', per request: a plan this
    computer cannot read changes nothing."""
    import kiln.licensing as licensing

    def broken(*_a, **_k):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(licensing, "check_tier", broken, raising=False)
    servers({"success": True, "plates": {"a": _block(), "b": _block()}})
    result = _suggest(doors, monkeypatch)
    assert [s["has_room"] for s in result["suggestions"]] == [True, True] and result["plate_note"] is None


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
