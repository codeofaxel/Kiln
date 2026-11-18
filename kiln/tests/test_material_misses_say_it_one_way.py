"""Every material answer Kiln's servers did not give says so the same way.

The material doors (the recommendation and its list, the substitutes, the
pairing check and the multi-material check, the material pick, the tuned
slicing settings) each word a miss in their own line.  These pin what the
lines share: someone who never signed in is told a free account is enough,
and an account whose sign-in ran out or was not taken is not; a basic reply
carries ``retryable`` and ``count`` the way its siblings do; a fault on Kiln's
servers is never called a fault on this computer.
"""

from __future__ import annotations

import sys

import pytest

from kiln.served_answer import FREE_ACCOUNT_NOTE, Miss

NEVER_SIGNED_IN = Miss("signed_out", "KILN_ACCOUNT_NOT_PAIRED")
HAS_AN_ACCOUNT = (
    Miss("signed_out", "KILN_SESSION_EXPIRED"),
    Miss("signed_out", "KILN_AUTH_REJECTED"),
)


@pytest.fixture(autouse=True)
def _plain_install(monkeypatch):
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


def _lines(miss: Miss) -> dict[str, str]:
    from kiln import _pro_adaptive_bridge, _pro_substitution_bridge, material_routing
    from kiln.design_intelligence import _basic_pairing_report
    from kiln.material_basics import basic_pick_line
    from kiln.material_limits import miss_sentence, sliced_miss_sentence
    from kiln.material_safety import _basic_mix_check

    return {
        "pick": basic_pick_line(miss, feature="material pick"),
        "shelf": material_routing._shelf_line(miss),
        "list": material_routing.basic_material_list(miss)["message"],
        "substitutes": _pro_substitution_bridge.basic_line(miss, "PLA", found=True),
        "pairing": _basic_pairing_report("pla", "petg", miss).caution,
        "mix": _basic_mix_check(["PLA", "PETG"], miss)["caution"],
        "slicing": _pro_adaptive_bridge.basic(miss, "PLA")["message"],
        "design limits": miss_sentence(miss),
        "slicer check": sliced_miss_sentence(miss, 0.8),
    }


def test_someone_who_never_signed_in_is_told_a_free_account_is_enough_on_every_material_line():
    for door, line in _lines(NEVER_SIGNED_IN).items():
        assert line.count(FREE_ACCOUNT_NOTE.strip()) == 1, (door, line)


@pytest.mark.parametrize("miss", HAS_AN_ACCOUNT, ids=lambda m: m.code)
def test_an_account_whose_sign_in_lapsed_is_not_told_to_make_one(miss):
    for door, line in _lines(miss).items():
        assert FREE_ACCOUNT_NOTE.strip() not in line, (door, line)


def test_a_miss_that_is_not_a_sign_in_never_mentions_an_account():
    for door, line in _lines(Miss("unanswered", "SERVER_UNREACHABLE")).items():
        assert "account" not in line, (door, line)


def test_both_substitution_doors_say_whether_asking_again_may_help_and_how_many_they_list(monkeypatch):
    import kiln.server as srv
    from kiln import _pro_substitution_bridge as bridge

    monkeypatch.setattr(bridge, "_local", lambda *a, **k: bridge._NOT_HERE)
    monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: {
        "status": "error", "success": False, "code": "SERVER_UNREACHABLE", "why": "unanswered",
    })
    listed = srv.find_material_substitute("PLA")
    best = srv.get_best_material_substitute("PLA")
    for reply in (listed, best):
        assert reply["basic"] is True and reply["retryable"] is True, reply
        assert reply["count"] == len(reply["substitutes"]), reply


def test_a_family_member_the_catalogue_spells_two_ways_is_listed_once():
    from kiln._pro_substitution_bridge import basic_substitutes
    from kiln.catalog_keys import same_material_spelling
    from kiln.design_intelligence import list_public_material_profiles

    for profile in list_public_material_profiles():
        family = [m["substitute_material"] for m in basic_substitutes(profile.material_id)]
        twins = [(a, b) for i, a in enumerate(family) for b in family[i + 1:] if same_material_spelling(a, b)]
        assert not twins, (profile.material_id, twins)


def test_a_fault_in_the_substitution_ranking_is_never_called_this_computers(monkeypatch):
    import types

    from kiln import _pro_substitution_bridge as bridge

    def broken(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setitem(sys.modules, bridge.LOCAL_MODULE, types.SimpleNamespace(substitutes_for_the_wire=broken))
    miss = bridge.ask("PLA").miss
    assert miss is not None and miss.cause == "unanswered"
    assert "this computer" not in miss.detail and "boom" not in miss.detail
    assert miss.detail == "Kiln's substitution ranking stopped"
