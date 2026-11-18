"""``find_material_substitute`` and ``get_best_material_substitute`` ask Kiln's
servers which filaments can stand in for another.

The ranking and its trade-offs are Kiln's and are served, free on every plan
for a signed-in account.  These tests pin the doors' side of that wire with
the servers stood in (public tests cannot import kiln-pro, and the stand-in
answers carry made-up figures):

* what is sent: the material with the reason and score floor, or best-only;
* the served substitutes are the reply, with the food-safety check this
  package makes over them;
* a reason the doors do not take is refused before anything is asked, and a
  request the servers could not read is relayed as their own ruling;
* a miss (offline, signed out, no answer, refused) answers the basic list:
  the same family from the bundled catalogue, alphabetical, with no score,
  reason, trade-off or price, ``basic: true`` and one line saying what it is,
  why and how to get Kiln's ranking; the best door names no best on a miss;
* the substitution rules are not in this package.
"""

from __future__ import annotations

import errno
import importlib.util
import json
import sys
import urllib.error
from pathlib import Path
from typing import Any

import pytest

WIRE = "material_substitutes"
PLA_FAMILY = ["pla_matte", "pla_plus", "pla_tough", "silk_pla"]
RANKING_KEYS = ("compatibility_score", "reasons", "trade_offs", "cost_delta_pct")

#: A served answer with made-up figures: the doors relay whatever comes back.
SERVED = [
    {
        "original_material": "petg", "substitute_material": "pla", "device_type": "fdm",
        "compatibility_score": 0.31, "reasons": ["unavailable"], "trade_offs": "stand-in one", "cost_delta_pct": 3.0,
    },
    {
        "original_material": "petg", "substitute_material": "abs", "device_type": "fdm",
        "compatibility_score": 0.21, "reasons": ["strength"], "trade_offs": "stand-in two", "cost_delta_pct": -2.0,
    },
]


@pytest.fixture(autouse=True)
def _plain_install(monkeypatch):
    """No kiln-pro on this machine, whatever the test runner has on its path."""
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


@pytest.fixture
def served(monkeypatch):
    import kiln.server as srv

    calls: list[tuple[str, dict, bool]] = []
    answers: list[Any] = []

    def fake(tool_name, _timeout=None, _asked_by_user=True, **kwargs):
        calls.append((tool_name, kwargs, _asked_by_user))
        answer = answers.pop(0) if answers else {}
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(srv, "_pro_api_call", fake)
    return calls, answers


def _find(*args, **kwargs):
    from kiln.server import find_material_substitute

    return find_material_substitute(*args, **kwargs)


def _best(material):
    from kiln.server import get_best_material_substitute

    return get_best_material_substitute(material)


def _ok(substitutes):
    return {"success": True, "status": "ok", "substitutes": [dict(s) for s in substitutes]}


class TestWhatIsSent:
    def test_the_list_sends_the_material_reason_and_floor(self, served):
        calls, answers = served
        answers.append(_ok([]))
        _find("PETG", reason="strength", min_score=0.6)
        assert calls == [(WIRE, {"material": "PETG", "reason": "strength", "min_score": 0.6}, True)]

    def test_the_best_sends_the_material_and_best_only(self, served):
        calls, answers = served
        answers.append(_ok([]))
        _best("PETG")
        assert calls == [(WIRE, {"material": "PETG", "best": True}, True)]

    def test_a_reason_the_doors_do_not_take_sends_nothing(self, served):
        calls, answers = served
        out = _find("pla", reason="cheap")
        assert calls == []
        assert out["success"] is False and out["error"]["code"] == "VALIDATION_ERROR"
        assert out["error"]["message"] == (
            "Invalid reason: 'cheap'. Valid: ['unavailable', 'cost', 'lead_time', 'strength', "
            "'finish_quality', 'heat_resistance']"
        )
        # A reason the doors take is asked, in any case.
        answers.append(_ok([]))
        _find("pla", reason="COST")
        assert calls == [(WIRE, {"material": "pla", "reason": "COST", "min_score": 0.5}, True)]


class TestWhatIsRefusedBeforeAsking:
    """Kiln's servers refuse the same requests in the same words; the doors
    refuse them first, so an offline install answers the same refusal."""

    LIMIT = 64

    @pytest.mark.parametrize("door", ["find", "best"])
    def test_a_material_longer_than_the_limit(self, served, door):
        calls, _ = served
        long = "x" * (self.LIMIT + 1)
        out = _find(long) if door == "find" else _best(long)
        assert calls == []
        assert out["success"] is False and out["error"]["code"] == "VALIDATION_ERROR"
        assert long not in out["error"]["message"]

    def test_a_refused_reason_repeats_at_most_the_limit(self, served):
        calls, _ = served
        out = _find("pla", reason="z" * 500)
        assert calls == [] and out["error"]["code"] == "VALIDATION_ERROR"
        message = out["error"]["message"]
        assert "z" * (self.LIMIT + 1) not in message and "z" * self.LIMIT in message

    @pytest.mark.parametrize("min_score", [float("nan"), float("inf"), -0.1, 1.5, True])
    def test_min_score_must_be_a_finite_number_from_zero_to_one(self, served, min_score):
        calls, _ = served
        out = _find("pla", min_score=min_score)
        assert calls == [] and out["error"]["code"] == "VALIDATION_ERROR"
        assert out["error"]["message"] == "Invalid min_score: give a number from 0.0 to 1.0."


class TestTheServedAnswer:
    def test_the_list_is_the_servers_with_the_food_safety_check(self, served):
        _, answers = served
        answers.append(_ok(SERVED))
        out = _find("PETG")
        assert out["success"] is True and "basic" not in out and "note" not in out
        assert [s["substitute_material"] for s in out["substitutes"]] == ["pla", "abs"]
        assert out["substitutes"][0]["compatibility_score"] == 0.31 and out["count"] == 2
        assert out["food_safety"]["source_food_safe"] == "yes"
        assert set(out["food_safety"]["regressions"]) == {"pla", "abs"}

    def test_the_best_is_the_servers_one(self, served):
        _, answers = served
        answers.append(_ok(SERVED[:1]))
        assert _best("PETG") == {"success": True, "material": "PETG", "substitute": SERVED[0]}

    def test_no_substitute_is_said_as_it_always_was(self, served):
        calls, answers = served
        answers.append(_ok([]))
        assert _best("PLA") == {
            "success": True, "material": "PLA", "substitute": None, "message": "No substitutes found",
        }
        assert len(calls) == 1, "the best door answered without asking"

    def test_the_servers_ruling_on_a_request_is_relayed(self, served):
        _, answers = served
        answers.append({"success": False, "status": "error", "code": "VALIDATION_ERROR", "error": "Invalid min_score: 'x'."})
        out = _find("pla")
        assert out == {
            "success": False,
            "error": {"code": "VALIDATION_ERROR", "message": "Invalid min_score: 'x'.", "retryable": False},
        }


def _misses() -> dict[str, Any]:
    return {
        "offline": urllib.error.URLError(OSError(errno.ENETUNREACH, "Network is unreachable")),
        "unanswered": ConnectionRefusedError("refused"),
        "refused": {"status": "error", "success": False, "code": "NOT_TODAY", "error": "Not today."},
    }


def _assert_basic_line(note: str, label: str) -> None:
    assert note == " ".join(note.split()) and note.endswith(".")
    for anchor in ("Kiln can't", "right now because", ", so it"):
        assert anchor in note, (anchor, note)
    assert label in note


class TestAMissIsTheBasicList:
    @pytest.mark.parametrize("cause", sorted(_misses()))
    def test_the_list_is_the_family_unranked_with_one_line(self, served, cause):
        _, answers = served
        answers.append(_misses()[cause])
        out = _find("PLA")
        assert out["success"] is True and out["basic"] is True and out["why"] == cause
        assert [s["substitute_material"] for s in out["substitutes"]] == PLA_FAMILY
        assert out["count"] == len(PLA_FAMILY)
        assert all(set(s) == {"substitute_material", "display_name", "category"} for s in out["substitutes"])
        for key in RANKING_KEYS:
            assert f'"{key}"' not in json.dumps(out), key
        _assert_basic_line(out["note"], "PLA")
        assert "same family" in out["note"] and "not ranked" in out["note"]

    @pytest.mark.parametrize("cause", sorted(_misses()))
    def test_the_best_door_names_no_best(self, served, cause):
        _, answers = served
        answers.append(_misses()[cause])
        out = _best("PLA")
        assert out["success"] is True and out["basic"] is True and out["why"] == cause
        assert "substitute" not in out, "a miss crowned a best"
        assert [s["substitute_material"] for s in out["substitutes"]] == PLA_FAMILY
        _assert_basic_line(out["note"], "PLA")

    def test_signed_out_is_told_to_sign_in_and_that_a_free_account_is_enough(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
        monkeypatch.delenv("KILN_API_URL", raising=False)
        monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
        out = _find("PLA")
        assert out["basic"] is True and out["why"] == "signed_out"
        assert [s["substitute_material"] for s in out["substitutes"]] == PLA_FAMILY
        assert "sign in" in out["note"] and "a free account is enough" in out["note"]
        best = _best("PLA")
        assert best["basic"] is True and "substitute" not in best and best["why"] == "signed_out"

    def test_a_reason_given_is_said_to_be_unanswered_by_the_list(self, served):
        _, answers = served
        answers.append(ConnectionRefusedError("refused"))
        out = _find("pla", reason="heat_resistance")
        assert out["basic"] is True and "not chosen for heat resistance" in out["note"]

    @pytest.mark.parametrize("material", ["abs", "tpe", "unobtainium", ""])
    def test_no_family_says_there_is_no_basic_substitute(self, served, material):
        _, answers = served
        answers.append(ConnectionRefusedError("refused"))
        out = _find(material)
        assert out["basic"] is True and out["substitutes"] == [] and out["count"] == 0
        assert out["note"].startswith("There is no basic substitute for ")

    def test_the_food_safety_check_runs_over_the_basic_list(self, served):
        _, answers = served
        answers.append(ConnectionRefusedError("refused"))
        out = _find("PETG")
        assert out["basic"] is True and out["food_safety"]["source_food_safe"] == "yes"
        assert all("food_safe" in s for s in out["substitutes"])


class TestTheRulesAreNotInThisPackage:
    def test_the_rules_module_is_gone(self):
        assert importlib.util.find_spec("kiln.material_substitution") is None

    def test_no_public_source_builds_substitution_rules(self):
        src = Path(__file__).resolve().parents[1] / "src" / "kiln"
        offenders = [
            str(path.relative_to(src))
            for path in src.rglob("*.py")
            if any(word in path.read_text(encoding="utf-8", errors="replace")
                   for word in ("_add_bidirectional", "SubstitutionMatrix", "_build_builtin_rules"))
        ]
        assert offenders == []
