"""A pitch for a plan is attached only after the plan check has been asked.

Someone who pays for a plan this install's sign-in no longer proves (a
sign-in the server refused, a paid plan unconfirmed while offline) reads as
Free to every gate.  The plan check words each of those states with the step
that works; a door that writes its own pitch sells them the plan they
already pay for.  So every function that builds a pitch (an upgrade link, a
nudge, the pricing page) asks :func:`kiln.tiers_and_terms.plan_offer`, the
one place that knows the caller's state, and pitches only when its words
send the caller to buy.  A function that rewords a refusal it was handed
relays that refusal's own pitch: it reads the pitch field, and pitches only
where the refusal already did.

:data:`NOT_YET` lists the functions that still pitch on their own; the list
only shrinks.  A function that pitches and is on neither path fails, and so
does a listed one that no longer pitches or now asks.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import kiln

SRC = Path(kiln.__file__).resolve().parent

#: Where the pitch is defined and where the plan check words a refusal: the
#: helpers themselves, not doors.
_HELPERS = {"tiers_and_terms.py", "account_plan.py"}

#: The calls and fields that pitch a plan.
_PITCH_CALLS = {"upgrade_nudge_block", "upgrade_link"}
_PITCH_KEYS = {"upgrade_url", "upgrade_nudge", "upgrade_hint"}
_PRICING = "kiln3d.com/pricing"

#: Functions that still build a pitch without asking the plan check.  Each
#: sells a person who pays for the plan it names, when their sign-in no
#: longer shows it; route it through ``plan_offer`` and delete its line.
NOT_YET: frozenset[str] = frozenset({
    "_pro_recovery_bridge.py::_upgrade_url",
    "_pro_recovery_bridge.py::floor_note",
    "assembly.py::_validate_joint",
    "cli/main.py::_echo_account_plan",
    "cli/main.py::upgrade",
    "consent_windows.py::_checked_scope",
    "consent_windows.py::open_always_for_several",
    "decoration_quota.py::_quota_exceeded_error",
    "design_intelligence.py::_format_upgrade_nudge",
    "design_reasoning.py::_print_aware_note",
    "emboss_generator.py::_qr_unavailable_message",
    "image_to_surface.py::generate_qr_data",
    "load_bearing_detector.py::_build_upgrade_recommendation",
    "plugins/design_tools.py::_DesignToolsPlugin.register.check_material_environment",
    "plugins/design_tools.py::_DesignToolsPlugin.register.get_post_processing_guide",
    "plugins/design_tools.py::_DesignToolsPlugin.register.recommend_design_material",
    "plugins/enterprise_tools.py::_encryption_not_installed",
    "plugins/material_inventory_tools.py::_MaterialInventoryToolsPlugin.register.check_material_sufficiency",
    "plugins/printability_tools.py::_PrintabilityToolsPlugin.register.auto_orient_model",
    "plugins/queue_tools.py::submit_job",
    "plugins/slicer_tools.py::_SlicerToolsPlugin.register.get_slicer_profile_tool",
    "plugins/slicer_tools.py::_fill_plate_placement",
    "plugins/slicer_tools.py::_spots_clause",
    "plugins/tier_diagnostic_tools.py::_build_response",
    "plugins/utility_tools.py::_UtilityToolsPlugin.register.get_started",
    "server.py::_pro_api_call",
    "server.py::_watch_capacity_error",
    "server.py::get_material_properties",
    "server.py::register_printer",
    "server.py::troubleshoot_printer",
    "skill_manifest.py::SkillManifest",
    "slicer_profiles.py::slicer_profile_to_dict",
})


def _key(node: ast.AST) -> object:
    return node.value if isinstance(node, ast.Constant) else None


def _pitches_and_asks(source: str) -> tuple[dict[str, int], set[str]]:
    """``({function: first pitching line}, {functions that ask or relay})``
    for one module.  A function asks when it, or one it is nested in, calls
    ``plan_offer``; it relays when it reads a pitch field of a refusal it
    was handed.  A docstring naming the pricing page is not a pitch."""
    tree = ast.parse(source)
    pitches: dict[str, int] = {}
    asks: set[str] = set()

    def visit(node: ast.AST, stack: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, (*stack, child.name))
                continue
            name = ".".join(stack)
            if stack:
                if isinstance(child, ast.Call):
                    func = child.func
                    called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                    if called == "plan_offer":
                        asks.add(name)
                    elif called in _PITCH_CALLS:
                        pitches.setdefault(name, child.lineno)
                    elif called == "get" and child.args and _key(child.args[0]) in _PITCH_KEYS:
                        asks.add(name)
                elif isinstance(child, ast.Subscript) and _key(child.slice) in _PITCH_KEYS:
                    if isinstance(child.ctx, ast.Store):
                        pitches.setdefault(name, child.lineno)
                    else:
                        asks.add(name)
                elif isinstance(child, ast.Dict):
                    if any(_key(k) in _PITCH_KEYS for k in child.keys if k is not None):
                        pitches.setdefault(name, child.lineno)
                elif (isinstance(child, ast.Constant) and isinstance(child.value, str) and _PRICING in child.value
                      and not _is_docstring(node, child)):
                    pitches.setdefault(name, child.lineno)
            visit(child, stack)

    visit(tree, ())
    return pitches, asks


def _is_docstring(parent: ast.AST, node: ast.AST) -> bool:
    return isinstance(parent, ast.Expr) and parent.value is node


def _scan(root: Path = SRC) -> dict[str, int]:
    """Every ``path::function`` that pitches without asking, with its line."""
    found: dict[str, int] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in _HELPERS:
            continue
        pitches, asks = _pitches_and_asks(path.read_text(encoding="utf-8"))
        for name, line in pitches.items():
            parts = name.split(".")
            if not any(".".join(parts[:i]) in asks for i in range(1, len(parts) + 1)):
                found[f"{rel}::{name}"] = line
    return found


def test_every_pitch_asks_the_plan_check_or_is_listed():
    unasked = _scan()
    new = sorted(f"{key} (line {line})" for key, line in unasked.items() if key not in NOT_YET)
    assert not new, (
        "these pitch a plan without asking kiln.tiers_and_terms.plan_offer, so they sell it to "
        f"people who already pay for it and whose sign-in no longer shows it: {new}"
    )


def test_a_listed_function_that_asks_now_or_no_longer_pitches_is_stale():
    stale = sorted(NOT_YET - set(_scan()))
    assert not stale, f"delete these from NOT_YET (they ask the plan check now, or are gone): {stale}"


_DOOR = '''
from kiln.tiers_and_terms import plan_offer, upgrade_link, upgrade_nudge_block

def sells_by_itself():
    """See https://kiln3d.com/pricing in a docstring: not a pitch."""
    return {"error": "part of Kiln Business", "upgrade_url": upgrade_link("x")}

def asks_first():
    offer = plan_offer("business", "Every printer at once")
    reply = {"note": offer.words}
    if offer.sells:
        reply["upgrade_nudge"] = upgrade_nudge_block(tier="business")
    return reply

def hint_with_the_price():
    return {"upgrade_hint": "see https://kiln3d.com/pricing"}

def sets_a_pitch_field(reply, link):
    reply["upgrade_url"] = link
    return reply

def relays_the_refusal(refused):
    answer = {"ask": "Which printer?"}
    if refused.get("upgrade_hint"):
        answer["upgrade_url"] = upgrade_link("tool")
    return answer

def outer():
    offer = plan_offer("pro")
    def inner():
        return {"upgrade_url": "https://kiln3d.com/pricing"} if offer.sells else {}
    return inner()

class Door:
    def pitches(self):
        return {"upgrade_nudge": upgrade_nudge_block(tier="pro")}

def says_nothing_about_plans():
    """Mentions https://kiln3d.com/pricing only here."""
    return {"ok": True}
'''


def test_the_scan_finds_a_door_that_pitches_by_itself_and_passes_one_that_asks(tmp_path):
    (tmp_path / "door.py").write_text(_DOOR, encoding="utf-8")
    assert set(_scan(tmp_path)) == {
        "door.py::sells_by_itself",
        "door.py::hint_with_the_price",
        "door.py::sets_a_pitch_field",
        "door.py::Door.pitches",
    }


@pytest.mark.parametrize("helper", sorted(_HELPERS))
def test_the_helpers_that_define_the_pitch_are_the_only_files_left_out(helper):
    assert (SRC / helper).is_file(), f"{helper} moved; the scan would read the plan check as a door"
