"""Every door that slices takes what a slicing refusal asks for.

A refusal for an abrasive filament on a nozzle that is not wear-resistant
asks the caller to call again with ``soft_nozzle_confirmed=true`` when the
owner says to print anyway, and may ask for a filament-to-nozzle map
(``filament_map``).  Both words come from the engine, the same at every door,
so every tool that reaches the slicer must take both, or say them another
way through :func:`kiln.slicer.said_for`, naming a door that does.

The doors are found, not listed: every registered tool whose code reaches
:func:`kiln.slicer._slice_file`, walked the way the read-only check walks a
tool (``tests/_read_only_reach.py``), on the code a plain install has.  A new
door that slices is judged the day it is added.
"""

from __future__ import annotations

import ast
import inspect
import re
import sys
import types
from typing import Any

import pytest

from kiln import scheduler, slicer
from tests import _read_only_reach as scan

#: What each ask of the refusal is called at a door.
_ASKS = {"soft_nozzle_confirmed": slicer.SOFT_NOZZLE_CONFIRM, "filament_map": slicer.MAP_ADVICE}

#: Places a refusal is said where neither ask can be passed, each with the
#: wording it uses and the door its wording names
#: (:func:`kiln.slicer.said_for`): a queued plate of copies, said on the job.
_SAID_ELSEWHERE = {
    "queued plate": (scheduler.QUEUED_PLATE_CONFIRM, scheduler.QUEUED_PLATE_MAP),
}

#: How deep the walk follows calls from a tool.
_DEPTH = 12


def _reaches(fn: Any, target: Any, memo: dict[int, bool], depth: int = 0, stack: tuple[int, ...] = ()) -> bool:
    """Whether *fn* reaches *target* through public Kiln's own calls."""
    fn = scan._unwrap(fn)
    if not isinstance(fn, types.FunctionType):
        return False
    if fn is target:
        return True
    key = id(fn)
    if key in memo:
        return memo[key]
    if key in stack or depth >= _DEPTH:
        return False
    tree = scan._tree(fn)
    if tree is None:
        memo[key] = False
        return False
    tree = scan._taken_branches(fn, tree)
    names = scan._scope(fn, tree, True)
    local = scan._local_classes(fn, tree, names, scan._is_public)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for called in scan._call_targets(node, names, local, scan._is_public):
            called = scan._unwrap(called) if callable(called) else called
            if (
                isinstance(called, types.FunctionType)
                and scan._is_public(called)
                and _reaches(called, target, memo, depth + 1, (*stack, key))
            ):
                memo[key] = True
                return True
    memo[key] = False
    return False


@pytest.fixture(scope="module")
def slice_doors(no_kiln_pro_module) -> dict[str, Any]:
    import kiln.server as server

    server._ensure_internal_tool_plugins_registered()
    memo: dict[int, bool] = {}
    found = {}
    for name, tool in sorted(server.mcp._tool_manager._tools.items()):
        fn = getattr(tool, "fn", None)
        if fn is None or (getattr(fn, "__module__", "") or "").startswith("kiln_pro"):
            continue
        if _reaches(fn, slicer._slice_file, memo):
            found[name] = fn
    return found


@pytest.fixture(scope="module")
def no_kiln_pro_module():
    held = sys.modules.get("kiln_pro", ...)
    sys.modules["kiln_pro"] = None  # type: ignore[assignment]
    yield
    if held is ...:
        sys.modules.pop("kiln_pro", None)
    else:
        sys.modules["kiln_pro"] = held


def test_the_refusal_asks_for_exactly_these():
    assert re.findall(r"\b([a-z_]+)=true\b", slicer.SOFT_NOZZLE_CONFIRM) == ["soft_nozzle_confirmed"]
    assert "filament-to-nozzle map" in slicer.MAP_ADVICE


def test_the_walk_finds_the_slice_doors(slice_doors):
    # The doors the walk must see, so a walk that finds nothing is no pass.
    assert {"slice_model", "slice_and_print", "run_quick_print", "multi_material_print"} <= set(slice_doors)


def test_every_slice_door_takes_what_its_refusal_asks_for(slice_doors):
    lacking = []
    for name, fn in slice_doors.items():
        params = inspect.signature(fn).parameters
        missing = [ask for ask in _ASKS if ask not in params]
        if missing:
            lacking.append(f"{name}: {', '.join(missing)}")
    assert not lacking, (
        "a slicing door whose refusal asks for a parameter it does not take (take it, or say it "
        "through kiln.slicer.said_for naming a door that does):\n  " + "\n  ".join(lacking)
    )


@pytest.mark.parametrize("where", sorted(_SAID_ELSEWHERE))
def test_a_refusal_said_elsewhere_names_a_door_that_takes_both(slice_doors, where):
    confirm, map_with = _SAID_ELSEWHERE[where]
    named = {word for word in re.findall(r"\b[a-z]+_[a-z_]+\b", f"{confirm} {map_with}") if word in slice_doors}
    assert named, f"the {where}'s refusal names no slicing door"
    for door in named:
        assert all(ask in inspect.signature(slice_doors[door]).parameters for ask in _ASKS), door
    said = slicer.said_for(
        f"X is abrasive, so nothing was sliced; {slicer.MAP_ADVICE}it on nozzle 1; {slicer.SOFT_NOZZLE_CONFIRM}.",
        confirm=confirm, map_with=map_with,
    )
    assert "call again" not in said and slicer.MAP_ADVICE not in said, said
    assert said.count(". ") == 0, said


@pytest.mark.parametrize("command", ["slice", "generate-and-print"])
def test_the_terminal_commands_that_slice_take_both_flags(command):
    from kiln.cli.main import cli

    options = {opt for param in cli.commands[command].params for opt in getattr(param, "opts", [])}
    assert {"--soft-nozzle-confirmed", "--filament-map"} <= options
