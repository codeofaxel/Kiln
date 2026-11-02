"""Kiln Pro's answers for an assembly's joints, on an install without kiln-pro.

Validating an assembly checks every joint against the standard rules on
every install.  Kiln Pro adds three answers per joint -- the glue for a glued
joint, the hole a machine screw threads into, the gap for parts that move --
and on a customer's computer (no kiln-pro) all three used to come back empty
for every plan, while the glued joint pitched Kiln Pro to people who already
pay for it.  Now each is asked of Kiln's servers when the signed-in plan
includes Kiln Pro, once per question in one validation, and a miss is said
once.

Kiln's servers are stood in at the one served door
(``kiln.server._pro_api_call``); kiln-pro is removed from the import system.
"""

from __future__ import annotations

import copy
import importlib
import re
import socket
import sys
import types
import urllib.error

import pytest

import kiln.server as srv
from kiln import assembly as A
from kiln.assembly import (
    Assembly,
    AssemblyPart,
    FastenerSpec,
    MatingInterface,
    get_clearance_recommendation,
    validate_assembly,
    validate_joint,
)
from kiln.served_answer import SESSION_EXPIRED_CODE, Miss

_PITCH = "Bonding two printed plastics?"
_PRICING = "kiln3d.com/pricing"

_GLUE = {
    "status": "success",
    "summary": "Two picks for this pair.",
    "query": {"material_a": "PLA", "material_b": "PETG"},
    "recommendations": [{"adhesive_id": "glue_x", "name": "Test Glue X"}, {"adhesive_id": "glue_y", "name": "Test Glue Y"}],
    "warnings": [],
}
_HOLE = {
    "fastener_size": "M3",
    "tier": "generic_methodology",
    "hole_diameter_horizontal_mm": 2.5,
    "hole_diameter_vertical_mm": 2.5,
    "engagement_length_mm": 6.0,
    "install_torque_nm": None,
}
_JOINT = {"status": "success", "recommended_joint": "self-tap into a boss", "rationale": "r", "hardware": _HOLE}
_GAP = {
    "status": "ok",
    "tool": "recommend_running_clearance",
    "mating": "pin_in_bore",
    "material": "PETG",
    "recommended_mm": 0.62,
    "modeled_quantity": "diametral clearance",
    "confidence": "low",
    "binding_term": "derived",
    "expected_as_printed_mm": 0.21,
    "cautions": ["c"],
}
_ALL = {
    "recommend_adhesive": _GLUE,
    "recommend_joint_strategy": _JOINT,
    "recommend_running_clearance": _GAP,
}


@pytest.fixture
def plain_install(monkeypatch):
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


def _plan(monkeypatch, *, pro: bool, lapsed: str = "") -> None:
    """The signed-in account's plan, read where every gate reads it."""
    licensing = importlib.import_module("kiln.licensing")
    monkeypatch.setattr(
        licensing, "check_tier", lambda _tier: (True, None) if pro else (False, "This feature needs Kiln Pro."),
    )
    from kiln import auth_session

    monkeypatch.setattr(auth_session, "lapsed_signin_message", lambda stored=None: lapsed)


class _Servers:
    """Kiln's servers at the served door: records each ask, answers by tool."""

    def __init__(self, answers):
        self.answers = answers
        self.asks: list[tuple[str, dict, bool]] = []

    def call(self, tool, /, _timeout=None, _asked_by_user=True, _http=None, **kwargs):
        self.asks.append((tool, dict(kwargs), _asked_by_user))
        answer = self.answers.get(tool, {"status": "error", "code": "NOT_IN_TEST", "error": "unexpected ask"})
        if isinstance(answer, BaseException):
            raise answer
        return copy.deepcopy(answer)

    def tools(self) -> list[str]:
        return [tool for tool, _kw, _by in self.asks]


@pytest.fixture
def servers(monkeypatch):
    def install(answers):
        s = _Servers(answers)
        monkeypatch.setattr(srv, "_served_bearer", lambda: ("bearer", None))
        monkeypatch.setattr(srv, "_pro_api_call", s.call)
        return s

    return install


@pytest.fixture(autouse=True)
def _no_mesh_pass(monkeypatch):
    """The geometry pass needs meshes on disk; these tests are about joints."""
    monkeypatch.setattr(A, "check_all_clearances", lambda asm, **_kw: setattr(asm, "clearance_checks", []))


def _parts(a: str = "PLA", b: str = "PETG") -> list[AssemblyPart]:
    return [
        AssemblyPart(part_id="a", file_path="/tmp/a.stl", material=a),
        AssemblyPart(part_id="b", file_path="/tmp/b.stl", material=b),
    ]


def _glued() -> MatingInterface:
    return MatingInterface("a", "b", "glued", clearance_mm=0.1)


def _screwed(family: str = "metric_machine_screw", surface: str | None = "printed") -> MatingInterface:
    return MatingInterface(
        "a", "b", "snap_fit", clearance_mm=0.2,
        fastener_spec=FastenerSpec(size="M3", family=family, surface_type=surface),
    )


def _moving(clearance_mm: float = 0.45) -> MatingInterface:
    return MatingInterface("a", "b", "clearance_fit", clearance_mm=clearance_mm)


def _assembly(*interfaces: MatingInterface, parts=None) -> Assembly:
    asm = Assembly(assembly_id="x", name="x")
    asm.parts.extend(parts or _parts())
    asm.interfaces.extend(interfaces)
    return asm


def _pitched(lines: list[str]) -> bool:
    return any(_PITCH in line or _PRICING in line for line in lines)


# ---------------------------------------------------------------------------
# A plan that includes Kiln Pro gets Kiln Pro's answers
# ---------------------------------------------------------------------------


def test_a_pro_caller_gets_the_glue_and_is_not_pitched(plain_install, servers, monkeypatch):
    _plan(monkeypatch, pro=True)
    s = servers(_ALL)
    jv = validate_joint(_glued(), _parts())

    assert any("Test Glue X" in line for line in jv.recommendations), jv.recommendations
    assert not _pitched(jv.recommendations)
    assert s.asks == [("recommend_adhesive", {"material_a": "PLA", "material_b": "PETG"}, False)]


def test_a_pro_caller_gets_the_hole_a_machine_screw_threads_into(plain_install, servers, monkeypatch):
    _plan(monkeypatch, pro=True)
    s = servers(_ALL)
    jv = validate_joint(_screwed(), _parts(), printer_id="bambu_a1")

    assert jv.screw_hole == _HOLE
    assert "compensated_screw_hole" in jv.design_rules_checked
    tool, sent, by_user = s.asks[0]
    assert tool == "recommend_joint_strategy" and by_user is False
    assert sent == {"fastener_size": "M3", "material": "PLA", "printer_id": "bambu_a1", "substrate": "plastic_print"}


@pytest.mark.parametrize("family,surface", [("wood_screw", "printed"), ("metric_machine_screw", "metal")])
def test_a_fastener_the_served_hole_does_not_cover_is_not_asked_about(plain_install, servers, monkeypatch, family, surface):
    """The served answer sizes a machine screw into a printed part; a wood
    screw or a bolt into metal is not that question, so it is not asked."""
    _plan(monkeypatch, pro=True)
    s = servers(_ALL)
    jv = validate_joint(_screwed(family=family, surface=surface), _parts(), printer_id="bambu_a1")
    assert jv.screw_hole is None
    assert s.asks == []


def test_a_pro_caller_is_held_to_the_gap_this_printer_needs(plain_install, servers, monkeypatch):
    """0.45 mm clears the standard 0.3 mm minimum; Kiln Pro's gap for this
    printer and material is 0.62 mm, so the parts are likely to fuse."""
    _plan(monkeypatch, pro=True)
    s = servers(_ALL)
    jv = validate_joint(_moving(0.45), _parts("PETG", "PETG"), printer_id="bambu_a1")

    assert jv.valid is False
    assert any("0.62 mm" in issue for issue in jv.issues), jv.issues
    tool, sent, _by = s.asks[0]
    assert tool == "recommend_running_clearance"
    assert sent == {"mating": "pin_in_bore", "material": "PETG", "printer_id": "bambu_a1"}


def test_the_clearance_advice_carries_the_servers_gap(plain_install, servers, monkeypatch):
    _plan(monkeypatch, pro=True)
    servers(_ALL)
    r = get_clearance_recommendation("clearance_fit", "PETG", "PETG", printer_id="bambu_a1")

    assert r["recommended_clearance_mm"] == pytest.approx(0.62)
    assert r["running_clearance"]["recommended_mm"] == pytest.approx(0.62)
    assert r["running_clearance"]["mating_assumed"] is True
    assert r["warnings"] == []


def test_each_question_is_asked_once_in_a_validation(plain_install, servers, monkeypatch):
    """Twenty joints are not twenty round trips: one per distinct question."""
    _plan(monkeypatch, pro=True)
    s = servers(_ALL)
    asm = _assembly(*([_glued()] * 6 + [_moving(0.9)] * 4 + [_screwed()] * 3))
    validate_assembly(asm, printer_id="bambu_a1")

    assert sorted(s.tools()) == ["recommend_adhesive", "recommend_joint_strategy", "recommend_running_clearance"]
    assert all(j.screw_hole == _HOLE for j in asm.joint_validations[-3:])


# ---------------------------------------------------------------------------
# A miss: said once, never read as passing, never a pitch
# ---------------------------------------------------------------------------


def test_offline_the_joints_are_checked_by_the_standard_rules_and_it_says_so_once(plain_install, servers, monkeypatch):
    _plan(monkeypatch, pro=True)
    offline = urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided"))
    s = servers({tool: offline for tool in _ALL})
    asm = _assembly(_glued(), _screwed(), _moving(0.45), _glued())
    validate_assembly(asm, printer_id="bambu_a1")

    # One ask, then nothing more this validation.
    assert len(s.asks) == 1
    said = [line for line in asm.recommendations if "Kiln Pro's joint checks" in line]
    assert len(said) == 1, asm.recommendations
    assert "this computer is offline" in said[0] and "standard rules only" in said[0]
    assert not _pitched(asm.recommendations)
    assert all(j.screw_hole is None for j in asm.joint_validations)
    # The standard minimum stands for the moving joint (0.45 clears 0.3).
    assert asm.joint_validations[2].valid is True


def test_a_refusal_rides_in_the_servers_own_words_and_nothing_more_is_asked(plain_install, servers, monkeypatch):
    _plan(monkeypatch, pro=True)
    refusal = {
        "status": "error", "code": "PLAN_ENDED", "required_tier": "pro",
        "error": "Your Kiln Pro plan ended on 1 October; renew it on your account page.",
    }
    s = servers({"recommend_adhesive": refusal})
    asm = _assembly(
        _glued(), MatingInterface("b", "a", "glued", clearance_mm=0.1), _moving(0.9),
        parts=_parts("ABS", "ASA"),
    )
    validate_assembly(asm, printer_id="bambu_a1")

    assert s.tools() == ["recommend_adhesive"]
    said = [line for line in asm.recommendations if "Kiln Pro's joint checks" in line]
    assert len(said) == 1 and "Your Kiln Pro plan ended on 1 October" in said[0]
    assert not _pitched(asm.recommendations)


def test_a_missed_gap_is_said_in_the_clearance_advice(plain_install, servers, monkeypatch):
    _plan(monkeypatch, pro=True)
    servers({"recommend_running_clearance": {"status": "error", "code": "SERVER_UNREACHABLE", "error": "timed out"}})
    r = get_clearance_recommendation("clearance_fit", "PETG", "PETG", printer_id="bambu_a1")

    assert r["running_clearance"] == {}
    assert len(r["warnings"]) == 1 and "didn't answer" in r["warnings"][0]


def test_a_sign_in_that_ended_is_told_to_sign_back_in_never_to_upgrade(plain_install, servers, monkeypatch):
    _plan(monkeypatch, pro=False, lapsed="Your Kiln session for maker@example.com ended; sign in again.")
    s = servers(_ALL)
    asm = _assembly(_glued(), _glued())
    validate_assembly(asm)

    assert s.asks == []
    assert not _pitched(asm.recommendations), asm.recommendations
    said = [line for line in asm.recommendations if "Kiln Pro's joint checks" in line]
    assert len(said) == 1 and "your Kiln session has expired" in said[0] and "sign back in" in said[0]


# ---------------------------------------------------------------------------
# Below Kiln Pro: nothing asked, the offer stands
# ---------------------------------------------------------------------------


def test_a_free_caller_asks_nothing_and_keeps_the_offer(plain_install, servers, monkeypatch):
    _plan(monkeypatch, pro=False)
    s = servers(_ALL)
    asm = _assembly(_glued(), _screwed(), _moving(0.45))
    validate_assembly(asm, printer_id="bambu_a1")

    assert s.asks == []
    assert _pitched(asm.joint_validations[0].recommendations)
    assert all(j.screw_hole is None for j in asm.joint_validations)
    assert not any("Kiln Pro's joint checks" in line for line in asm.recommendations)


# ---------------------------------------------------------------------------
# With kiln-pro in the process: a paying caller is not pitched either
# ---------------------------------------------------------------------------


@pytest.fixture
def kiln_pro_with_no_glue_for_the_pair(monkeypatch):
    """kiln-pro here, answering no glue for this pair (none confident)."""
    adhesives = types.ModuleType("kiln_pro.adhesives")
    adhesives.adhesive_assembly_hint = lambda material_a, material_b=None: None
    monkeypatch.setitem(sys.modules, "kiln_pro.adhesives", adhesives)


def test_with_kiln_pro_here_a_pro_caller_with_no_glue_named_is_not_pitched(kiln_pro_with_no_glue_for_the_pair, monkeypatch):
    _plan(monkeypatch, pro=True)
    jv = validate_joint(_glued(), _parts())
    assert not _pitched(jv.recommendations), jv.recommendations


def test_with_kiln_pro_here_a_free_caller_still_gets_the_offer(kiln_pro_with_no_glue_for_the_pair, monkeypatch):
    _plan(monkeypatch, pro=False)
    jv = validate_joint(_glued(), _parts())
    assert _pitched(jv.recommendations)


# ---------------------------------------------------------------------------
# The sentence
# ---------------------------------------------------------------------------

_CODE_TOKEN = re.compile(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b")
_SYSTEM_WORD = re.compile(r"\b(API|endpoint|bridge|kiln-pro|kiln_pro|HTTP|exception|payload)\b", re.IGNORECASE)


def test_every_cause_reads_as_one_sentence_for_a_person_and_is_said_once():
    bridge = importlib.import_module("kiln._pro_assembly_bridge")
    misses = [
        Miss("offline", "SERVER_UNREACHABLE", "no route"),
        Miss("signed_out", "KILN_ACCOUNT_NOT_PAIRED"),
        Miss("signed_out", SESSION_EXPIRED_CODE),
        Miss("unanswered", "SERVER_UNREACHABLE", "timed out"),
        Miss("refused", "SOME_CODE", "The servers turned this away."),
        Miss("refused", "SOME_CODE", ""),
    ]
    seen = set()
    for miss in misses:
        session = bridge.Asking()
        session.miss = miss
        text = session.note()
        assert session.note() is None  # once
        assert text == " ".join(text.split()) and text.endswith(".")
        assert not _CODE_TOKEN.search(text) and not _SYSTEM_WORD.search(text), text
        for anchor in ("Kiln can't", "right now because", ", so it", "standard rules only"):
            assert anchor in text, text
        seen.add(text)
    assert len(seen) == len(misses)
    assert bridge.Asking().note() is None
