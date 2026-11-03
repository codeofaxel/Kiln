"""On an install without kiln-pro, every screw into a printed part gets its hole.

The served joint tool used to take no screw kind, so the bridge asked only
about machine screws: a Pro customer driving a wood screw or a sheet-metal
screw into a printed bracket got no hole and no reason.  Now the bridge
asks for every kind that threads into a printed part, sending the kind
(``fastener_family``) unless it is a machine screw, so servers from before
the argument still answer machine screws as before; a server that does not
read the kind (it refuses the argument by name, HTTP 400 ``Unknown
parameters: fastener_family``; or, a proxy having dropped it, answers a
machine-screw hole and says no kind back) is said once, and that joint gets
no hole -- but nothing else is lost: the glue, the gaps and every other joint
are still asked.

Kiln's servers are stood in at the one served door
(``kiln.server._pro_api_call``); kiln-pro is removed from the import system.

A/B: ``test_a_wood_screw...``, ``test_a_sheet_metal_screw...`` and
``test_a_server_that_does_not_know...`` fail on the tree before this change
(nothing was asked for a wood screw); the machine-screw and heat-set-insert
tests pass on both.
"""

from __future__ import annotations

import copy
import importlib
import sys

import pytest

import kiln.server as srv
from kiln import _pro_assembly_bridge as bridge
from kiln import assembly as A
from kiln.assembly import Assembly, AssemblyPart, FastenerSpec, MatingInterface, validate_assembly, validate_joint

_HOLE = {
    "fastener_size": "M3",
    "thread_form": "self_tap_bt",
    "tier": "generic_methodology",
    "hole_diameter_horizontal_mm": 2.4,
    "hole_diameter_vertical_mm": 2.4,
    "engagement_length_mm": None,
    "install_torque_nm": None,
}
_JOINT = {"status": "success", "recommended_joint": "PT/Plastite self-tap into sized boss", "rationale": "r", "hardware": _HOLE}
#: An older server refuses an argument the tool does not take, by name: Kiln's
#: tool route answers HTTP 400 with this body, and the served door passes a
#: body carrying ``success`` and ``error`` through as the server wrote it.
_OLDER_SERVER = {"success": False, "error": "Unknown parameters: fastener_family"}
#: A proxy between this install and the servers could drop the argument
#: instead; then a machine-screw hole comes back, and the server's age shows
#: in what it does NOT say: no ``fastener_family_read`` for a kind that was
#: sent.
_KIND_DROPPED = {**_JOINT, "hardware": {**_HOLE, "thread_form": "machine_self_thread", "hole_diameter_horizontal_mm": 2.9}}
_GLUE = {"status": "success", "recommendations": [{"name": "Medium CA glue"}]}


@pytest.fixture
def plain_install(monkeypatch):
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


@pytest.fixture
def pro_plan(monkeypatch):
    licensing = importlib.import_module("kiln.licensing")
    monkeypatch.setattr(licensing, "check_tier", lambda _tier: (True, None))
    from kiln import auth_session

    monkeypatch.setattr(auth_session, "lapsed_signin_message", lambda stored=None: "")


class _Servers:
    """Kiln's servers stood in: one answer per tool.  A current server says
    a sent kind back (``fastener_family_read``); ``reads_kind=False`` is a
    server from before the argument, which drops it and says nothing."""

    def __init__(self, answers: dict[str, dict], *, reads_kind: bool = True):
        self.answers = answers
        self.reads_kind = reads_kind
        self.asks: list[tuple[str, dict]] = []

    def call(self, tool, /, _timeout=None, _asked_by_user=True, _http=None, **kwargs):
        self.asks.append((tool, dict(kwargs)))
        answer = copy.deepcopy(self.answers[tool])
        if self.reads_kind and "fastener_family" in kwargs:
            # The wire key, spelled out so this file runs unchanged on the
            # tree before the bridge knew it (the A/B below).
            answer["fastener_family_read"] = kwargs["fastener_family"]
        return answer


@pytest.fixture
def servers(monkeypatch):
    def install(answer, *, reads_kind: bool = True):
        s = _Servers({bridge.SCREW_HOLE_TOOL: answer, bridge.ADHESIVE_TOOL: _GLUE}, reads_kind=reads_kind)
        monkeypatch.setattr(srv, "_served_bearer", lambda: ("bearer", None))
        monkeypatch.setattr(srv, "_pro_api_call", s.call)
        return s

    return install


@pytest.fixture(autouse=True)
def _no_mesh_pass(monkeypatch):
    monkeypatch.setattr(A, "check_all_clearances", lambda asm, **_kw: setattr(asm, "clearance_checks", []))


def _parts() -> list[AssemblyPart]:
    return [
        AssemblyPart(part_id="a", file_path="/tmp/a.stl", material="PLA"),
        AssemblyPart(part_id="b", file_path="/tmp/b.stl", material="PLA"),
    ]


def _screwed(family: str, surface: str | None = "printed") -> MatingInterface:
    return MatingInterface(
        "a", "b", "snap_fit", clearance_mm=0.2,
        fastener_spec=FastenerSpec(size="M3", family=family, surface_type=surface),
    )


@pytest.mark.parametrize("family", ["wood_screw", "sheet_metal_screw", "self_tapping_screw"])
def test_a_thread_forming_screw_into_a_printed_part_is_asked_about_by_kind(plain_install, pro_plan, servers, family):
    s = servers(_JOINT)
    jv = validate_joint(_screwed(family), _parts(), printer_id="bambu_a1")

    assert jv.screw_hole == _HOLE
    assert "compensated_screw_hole" in jv.design_rules_checked
    assert s.asks == [("recommend_joint_strategy", {
        "fastener_size": "M3", "material": "PLA", "printer_id": "bambu_a1",
        "substrate": "plastic_print", "fastener_family": family,
    })]


def test_a_wood_screw_through_to_wood_is_a_clearance_hole_and_is_not_asked_about(plain_install, pro_plan, servers):
    s = servers(_JOINT)
    jv = validate_joint(_screwed("wood_screw", surface="wood"), _parts(), printer_id="bambu_a1")
    assert jv.screw_hole is None and s.asks == []


def test_a_machine_screw_is_asked_about_as_before_with_no_kind(plain_install, pro_plan, servers):
    s = servers(_JOINT)
    jv = validate_joint(_screwed("metric_machine_screw"), _parts(), printer_id="bambu_a1")

    assert jv.screw_hole == _HOLE
    [(_tool, sent)] = s.asks
    assert bridge.SCREW_KIND_ARG not in sent
    assert sent == {"fastener_size": "M3", "material": "PLA", "printer_id": "bambu_a1", "substrate": "plastic_print"}


def test_a_heat_set_insert_asks_nothing(plain_install, pro_plan, servers):
    s = servers(_JOINT)
    jv = validate_joint(_screwed("heat_set_insert"), _parts(), printer_id="bambu_a1")
    assert jv.screw_hole is None and s.asks == []


@pytest.mark.parametrize("older", [_OLDER_SERVER, _KIND_DROPPED], ids=["refuses_the_kind", "kind_dropped_by_a_proxy"])
def test_a_server_that_does_not_know_the_kind_is_said_once_as_newer_than_its_answer(plain_install, pro_plan, servers, older):
    s = servers(older, reads_kind=False)
    asm = Assembly(assembly_id="x", name="x")
    asm.parts.extend(_parts())
    asm.interfaces.extend([_screwed("wood_screw"), _screwed("wood_screw")])
    validate_assembly(asm, printer_id="bambu_a1")

    assert len(s.asks) == 1  # the same question is asked once per validation
    assert all(j.screw_hole is None for j in asm.joint_validations)
    said = [line for line in asm.recommendations if "Kiln Pro's joint checks" in line]
    assert len(said) == 1, asm.recommendations
    assert "This computer's Kiln is newer than Kiln's servers' answer for a wood screw" in said[0]
    assert "kiln3d.com/pricing" not in said[0]


# --- defects a lap found (2026-11-02; each fails at 73f8ecc0 / 8b2a3cc69) ---


@pytest.mark.parametrize("spelling", ["machine screw", "machine-screw", "Machine Screw", "metric_machine"])
def test_a_machine_screw_spelled_any_way_is_sent_without_a_kind_and_keeps_its_hole(plain_install, pro_plan, servers, spelling):
    """The lap found a machine screw spelled ``"machine screw"``,
    ``"machine-screw"`` or ``"metric_machine"`` sent up as a kind; the
    current server sized it as a machine screw (correctly) and the bridge
    read that thread form as an older server, dropped the hole and ended the
    validation.  The suite only ever spelled it ``metric_machine_screw``."""
    s = servers(_JOINT)
    jv = validate_joint(_screwed(spelling), _parts(), printer_id="bambu_a1")

    assert jv.screw_hole == _HOLE
    [(_tool, sent)] = s.asks
    assert bridge.SCREW_KIND_ARG not in sent
    assert not jv.recommendations or all("newer than Kiln's servers" not in line for line in jv.recommendations)


def _two_joint_assembly(family: str) -> Assembly:
    asm = Assembly(assembly_id="x", name="x")
    asm.parts.extend([
        AssemblyPart(part_id="a", file_path="/tmp/a.stl", material="PLA"),
        AssemblyPart(part_id="b", file_path="/tmp/b.stl", material="PETG"),
    ])
    asm.interfaces.append(_screwed(family))
    asm.interfaces.append(MatingInterface("a", "b", "glued", clearance_mm=0.1))
    return asm


def _glue_lines(asm: Assembly) -> list[str]:
    return [line for line in asm.recommendations if "Medium CA glue" in line]


def test_a_machine_screw_spelled_with_a_space_keeps_the_glue_pick_for_the_rest_of_the_assembly(plain_install, pro_plan, servers):
    """R1 as the lap measured it: one ``"machine screw"`` joint beside a
    glued joint lost the glue pick too, because the wrongly read answer was
    a session-ending miss.  The suite validated one joint at a time, so a
    miss had nothing after it to lose."""
    s = servers(_JOINT)
    asm = _two_joint_assembly("machine screw")
    validate_assembly(asm, printer_id="bambu_a1")

    assert asm.joint_validations[0].screw_hole == _HOLE
    assert [t for t, _ in s.asks] == [bridge.SCREW_HOLE_TOOL, bridge.ADHESIVE_TOOL]
    assert len(_glue_lines(asm)) == 1, asm.recommendations
    assert not any("Kiln Pro's joint checks" in line for line in asm.recommendations)


def test_a_kind_the_servers_do_not_list_is_sent_as_spelled_and_its_hole_kept(plain_install, pro_plan, servers):
    """R2: ``concrete_anchor`` (named in :class:`FastenerSpec`'s own
    docstring) was refused by the server, the refusal listing its accepted
    kinds was pasted into the recommendations, and the glue pick was lost.
    The server now sizes it as the in-process path does; the bridge sends
    the kind spelled the way the servers read it and keeps what comes
    back.  The suite sent only the kinds the server listed."""
    s = servers(_JOINT)
    asm = _two_joint_assembly("Concrete Anchor")
    validate_assembly(asm, printer_id="bambu_a1")

    [(_tool, sent), _glue] = s.asks
    assert sent[bridge.SCREW_KIND_ARG] == "concrete_anchor"
    assert asm.joint_validations[0].screw_hole == _HOLE
    assert len(_glue_lines(asm)) == 1, asm.recommendations
    assert not any("must be one of" in line or "Kiln Pro's joint checks" in line for line in asm.recommendations)


def test_an_older_server_is_said_once_and_the_next_joints_glue_pick_still_arrives(plain_install, pro_plan, servers):
    """The older-server case was a session-ending miss: a wood screw the
    servers could not size took the glued joint's answer down with it.
    Now it is said once and nothing else is lost.  The suite's older-server
    case had two wood screws and no other kind of joint to lose."""
    s = servers(_KIND_DROPPED, reads_kind=False)
    asm = _two_joint_assembly("wood_screw")
    asm.interfaces.append(_screwed("wood_screw"))
    validate_assembly(asm, printer_id="bambu_a1")

    assert [t for t, _ in s.asks] == [bridge.SCREW_HOLE_TOOL, bridge.ADHESIVE_TOOL]
    assert all(j.screw_hole is None for j in asm.joint_validations)
    said = [line for line in asm.recommendations if "newer than Kiln's servers' answer for a wood screw" in line]
    assert len(said) == 1, asm.recommendations
    assert "said no" not in said[0] and "kiln3d.com/pricing" not in said[0]
    assert len(_glue_lines(asm)) == 1, asm.recommendations


def test_a_real_miss_still_ends_the_validation_and_is_said_once(plain_install, pro_plan, servers):
    """The standing rule is untouched: a refusal, not an older server, still
    stops every later ask."""
    s = servers({"status": "error", "code": "SOMETHING_ELSE", "error": "no"})
    asm = _two_joint_assembly("wood_screw")
    validate_assembly(asm, printer_id="bambu_a1")

    assert [t for t, _ in s.asks] == [bridge.SCREW_HOLE_TOOL]
    assert _glue_lines(asm) == []
    assert sum("Kiln Pro's joint checks" in line for line in asm.recommendations) == 1


# --- lap 2 (2026-11-02; each fails at f62be5ba) ---


def test_the_real_older_server_refuses_the_kind_by_name_and_the_rest_is_still_asked(plain_install, pro_plan, servers):
    """Lap 2 ran the bridge against what the hosted route really answers for
    an argument the tool does not take: HTTP 400 ``Unknown parameters:
    fastener_family``.  The bridge had modelled an older server as one that
    DROPS the argument (the fixture above said so, and the suite's every
    older-server case was built from it), so the real refusal read as an
    unanswered miss: the validation's asking ended, the glued joint's pick
    was never asked, the note said the servers did not answer and to wait a
    minute (a retry that could never succeed), and the older-server remark
    never fired.  The suite missed it because no fixture had ever answered
    the server's own 400 body."""
    s = servers(_OLDER_SERVER)
    asm = _two_joint_assembly("wood_screw")
    asm.interfaces.append(_screwed("wood_screw"))
    validate_assembly(asm, printer_id="bambu_a1")

    assert [t for t, _ in s.asks] == [bridge.SCREW_HOLE_TOOL, bridge.ADHESIVE_TOOL]
    assert all(bridge.SCREW_KIND_ARG in sent for t, sent in s.asks if t == bridge.SCREW_HOLE_TOOL)
    assert all(j.screw_hole is None for j in asm.joint_validations)
    said = [line for line in asm.recommendations if "newer than Kiln's servers' answer for a wood screw" in line]
    assert len(said) == 1, asm.recommendations
    assert sum("Kiln Pro's joint checks" in line for line in asm.recommendations) == 1  # the remark, no miss
    assert not any("wait a minute" in line or "come from Kiln's servers" in line for line in asm.recommendations)
    assert len(_glue_lines(asm)) == 1, asm.recommendations


def test_a_refusal_of_another_argument_is_still_a_miss(plain_install, pro_plan, servers):
    """The refusal is read narrowly: the servers refusing some other
    argument by the same words is a real miss, said once, ending the rest."""
    s = servers({"success": False, "error": "Unknown parameters: substrate"})
    asm = _two_joint_assembly("wood_screw")
    validate_assembly(asm, printer_id="bambu_a1")

    assert [t for t, _ in s.asks] == [bridge.SCREW_HOLE_TOOL]
    assert sum("Kiln Pro's joint checks" in line for line in asm.recommendations) == 1
    assert not any("newer than Kiln's servers" in line for line in asm.recommendations)
