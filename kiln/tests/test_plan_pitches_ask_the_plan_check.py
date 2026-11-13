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
where the refusal already did.  A pitch written once at module level and
used in a function (``_PLAN_LINE = "... kiln3d.com/pricing"``, or one
imported from another Kiln module by name) is a pitch where it is used.

:data:`INFORMS` lists the functions that name the plans to everyone alike,
each with why that never sells a plan to someone who already pays for it.
A function that pitches and is on none of these paths fails, and so does a
listed one that no longer pitches or now asks.
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

#: Functions that name the plans the same way to every caller, and why that
#: is never a sale to someone who already pays for one.
INFORMS: dict[str, str] = {
    "agent_loop.py::_get_default_system_prompt": "The Terms text every agent reads, the same for everyone.",
    "cli/main.py::upgrade": (
        "Runs only where kiln-pro's own licence manager is installed; a plain install shows the "
        "plan through _echo_account_plan, which asks."
    ),
    "plugins/enterprise_tools.py::_usage_from_the_servers": (
        "Where the printer prices are, beside the servers' own figure for the account's plan when "
        "that figure did not come back: a price list for the plan already held, never a plan to buy."
    ),
    "plugins/printability_tools.py::_PrintabilityToolsPlugin.register.auto_orient_model": (
        "Carries kiln-pro's own note, written where Kiln's servers rank for the caller they "
        "resolved; a plain install has no note to carry."
    ),
    "plugins/utility_tools.py::_UtilityToolsPlugin.register.get_started": (
        "Orientation for the agent: it describes what each plan includes, and says a lapsed "
        "sign-in needs signing back in before anything else."
    ),
    "server.py::_pro_api_call": (
        "Reached only on a machine with no sign-in at all: a sign-in the server refused is "
        "answered before it with no pitch, and an unconfirmed plan still holds its bearer."
    ),
    "skill_manifest.py::SkillManifest": "The description of each plan, the same for everyone.",
    "skill_manifest.py::pro_tier_line": "The description of Kiln Pro, the same for everyone.",
}

def _key(node: ast.AST) -> object:
    return node.value if isinstance(node, ast.Constant) else None


def _pricing_constants(tree: ast.Module) -> set[str]:
    """The module-level names whose value holds the pricing page, directly
    or through another such name."""
    held: set[str] = set()
    grew = True
    while grew:
        grew = False
        for node in tree.body:
            if isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and node.value is not None and isinstance(node.target, ast.Name):
                names = [node.target.id]
            else:
                continue
            if set(names) <= held:
                continue
            if any(
                (isinstance(n, ast.Constant) and isinstance(n.value, str) and _PRICING in n.value)
                or (isinstance(n, ast.Name) and n.id in held)
                for n in ast.walk(node.value)
            ):
                held.update(names)
                grew = True
    return held


def _imported_pricing_constants(tree: ast.Module, held_by_module: dict[str, set[str]]) -> set[str]:
    """The names this module imports, by name, from a Kiln module that holds
    the pricing page in them."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and not node.level and (node.module or "").startswith("kiln"):
            held = held_by_module.get(node.module.removeprefix("kiln").lstrip("."), set())
            found.update(alias.asname or alias.name for alias in node.names if alias.name in held)
    return found


def _pitches_and_asks(source: str, constants: frozenset[str] = frozenset()) -> tuple[dict[str, int], set[str]]:
    """``({function: first pitching line}, {functions that ask or relay})``
    for one module.  A function asks when it, or one it is nested in, calls
    ``plan_offer``; it relays when it reads a pitch field (by key or by
    attribute) of a refusal or answer it was handed.  A docstring naming the
    pricing page is not a pitch; reading one of *constants* (names that hold
    it) is."""
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
                elif (
                    isinstance(child, ast.Constant) and isinstance(child.value, str) and _PRICING in child.value
                    and not _is_docstring(node, child)
                ) or (isinstance(child, ast.Name) and child.id in constants and isinstance(child.ctx, ast.Load)):
                    pitches.setdefault(name, child.lineno)
                elif isinstance(child, ast.Attribute) and child.attr in _PITCH_KEYS and isinstance(child.ctx, ast.Load):
                    asks.add(name)
            visit(child, stack)

    visit(tree, ())
    return pitches, asks


def _is_docstring(parent: ast.AST, node: ast.AST) -> bool:
    return isinstance(parent, ast.Expr) and parent.value is node


def _scan(root: Path = SRC) -> dict[str, int]:
    """Every ``path::function`` that pitches without asking, with its line."""
    sources = {path.relative_to(root).as_posix(): path.read_text(encoding="utf-8") for path in sorted(root.rglob("*.py"))}
    trees = {rel: ast.parse(text) for rel, text in sources.items()}
    held_by_module = {
        rel[: -len(".py")].replace("/", ".").removesuffix("__init__").rstrip("."): _pricing_constants(tree)
        for rel, tree in trees.items()
    }
    found: dict[str, int] = {}
    for rel, text in sources.items():
        if rel in _HELPERS:
            continue
        here = rel[: -len(".py")].replace("/", ".").removesuffix("__init__").rstrip(".")
        constants = frozenset(held_by_module[here] | _imported_pricing_constants(trees[rel], held_by_module))
        pitches, asks = _pitches_and_asks(text, constants)
        for name, line in pitches.items():
            parts = name.split(".")
            if not any(".".join(parts[:i]) in asks for i in range(1, len(parts) + 1)):
                found[f"{rel}::{name}"] = line
    return found


def test_every_pitch_asks_the_plan_check_or_is_listed():
    unasked = _scan()
    new = sorted(f"{key} (line {line})" for key, line in unasked.items() if key not in INFORMS)
    assert not new, (
        "these pitch a plan without asking kiln.tiers_and_terms.plan_offer, so they sell it to "
        f"people who already pay for it and whose sign-in no longer shows it: {new}"
    )


def test_a_listed_function_that_asks_now_or_no_longer_pitches_is_stale():
    stale = sorted(set(INFORMS) - set(_scan()))
    assert not stale, f"delete these lines (they ask the plan check now, or are gone): {stale}"


def test_every_informing_function_says_why():
    assert all(why.strip() for why in INFORMS.values())


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

_PLAN_LINE = "Kiln Business does that: https://kiln3d.com/pricing"
_LONGER_LINE = _PLAN_LINE + " (several printers)"

def pitches_through_a_constant():
    return {"note": _LONGER_LINE}

def asks_before_the_constant():
    return {"note": plan_offer("business", "Several printers").tail(_PLAN_LINE)}

def relays_an_answers_field(guide):
    return {"answer": guide.text, "upgrade_hint": guide.upgrade_hint}

def says_nothing_about_plans():
    """Mentions https://kiln3d.com/pricing only here."""
    return {"ok": True}
'''


def test_the_scan_finds_a_door_that_pitches_by_itself_and_passes_one_that_asks(tmp_path):
    (tmp_path / "door.py").write_text(_DOOR, encoding="utf-8")
    (tmp_path / "lines.py").write_text('SHOP = "Kiln Pro: https://kiln3d.com/pricing"\n', encoding="utf-8")
    (tmp_path / "other.py").write_text(
        "from kiln.lines import SHOP as LINE\n\ndef borrows_the_line():\n    return LINE\n", encoding="utf-8"
    )
    assert set(_scan(tmp_path)) == {
        "door.py::sells_by_itself",
        "door.py::hint_with_the_price",
        "door.py::sets_a_pitch_field",
        "door.py::Door.pitches",
        "door.py::pitches_through_a_constant",
        "other.py::borrows_the_line",
    }


@pytest.mark.parametrize("helper", sorted(_HELPERS))
def test_the_helpers_that_define_the_pitch_are_the_only_files_left_out(helper):
    assert (SRC / helper).is_file(), f"{helper} moved; the scan would read the plan check as a door"
