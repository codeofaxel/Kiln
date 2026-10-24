"""A printability report Kiln Pro tuned is served as its verdict, never its arithmetic.

On Kiln's servers kiln-pro runs in the same process as public Kiln, so a
report it tuned held the tuned numbers: each check's ``score_deduction``,
the thermal check's material factor, the adhesion estimate's forces and
ratios, the overhang angle and the wall floor it was read against.  Every
door that turned the report into a reply carried them, and a reader could
take a constant back out of one reply -- the adhesion force over the bed
contact in the same report is the material's bed strength.

A reply now keeps the verdict, the score and grade, every check's risk and
reasons, and what to do; a check Kiln Pro tuned leaves out the numbers it
was worked out from, which stay in the process (``kiln.printability._served``).
A report on public Kiln's own floor serves every measurement it did, as
before; the points each check took off and the thermal factor are never
served, at any tier (2026-10-23).

The tuning here is stood in for with numbers no real material has, so a
number that reaches a reply can only have come from the tuning.
"""

from __future__ import annotations

import copy
import math
import re
import sys
import types
from dataclasses import asdict
from typing import Any

import pytest

trimesh = pytest.importorskip("trimesh")

from kiln import printability as P  # noqa: E402

# ---------------------------------------------------------------------------
# A stand-in for Kiln Pro's tuning
# ---------------------------------------------------------------------------

#: Per-material physics no real material has.  The stress factor clears the
#: bar for the "amplifies thermal stress" sentence, so that sentence is said.
STRESS_FACTOR = 1.37
ADHESION_STRENGTH = 0.0613  # below the floor, so the push test is tuned too
SHRINKAGE_STRAIN = 0.0071
WALL_FLOOR_MM = 1.73
OVERHANG_LIMIT_DEG = 47.3

STAND_INS = (STRESS_FACTOR, ADHESION_STRENGTH, SHRINKAGE_STRAIN, WALL_FLOOR_MM, OVERHANG_LIMIT_DEG)

#: Every number a reply about a tuned report leaves out, by the block that
#: carried it ("" for the report itself).  Written out rather than read from
#: the code, so a cut that is undone fails its own case.
WORKED_OUT = (
    ("", "stop_deduction"),
    ("", "overhang_threshold_deg"),
    ("overhangs", "score_deduction"),
    ("thin_walls", "score_deduction"),
    ("thin_walls", "threshold_mm"),
    ("bridging", "score_deduction"),
    ("bed_adhesion", "score_deduction"),
    ("supports", "score_deduction"),
    ("warping", "score_deduction"),
    ("thermal_stress", "score_deduction"),
    ("thermal_stress", "material_stress_factor"),
    ("adhesion_force", "score_deduction"),
    ("adhesion_force", "adhesion_force_n"),
    ("adhesion_force", "peel_force_n"),
    ("adhesion_force", "force_ratio"),
    ("adhesion_force", "tipping_ratio"),
)


def _block_kiln_pro(monkeypatch) -> None:
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


def _install_tuning(monkeypatch) -> None:
    """A kiln-pro that tunes every check, with the stand-in numbers."""
    _block_kiln_pro(monkeypatch)
    overlay = types.ModuleType("kiln_pro.printability_overlay")
    overlay.lookup_material = lambda material: {
        "stress_factor": STRESS_FACTOR,
        "adhesion_strength": ADHESION_STRENGTH,
        "shrinkage_strain": SHRINKAGE_STRAIN,
    }
    overlay.resolve_wall_floor = lambda material, **_kw: WALL_FLOOR_MM
    overlay.enrich_printability_report = lambda report, *_a, **_kw: dict(report)

    class _ProFeatures:
        printability_overlay = overlay

        def is_available(self, feature: str) -> bool:
            return feature == "printability_overlay"

    bridge = types.ModuleType("kiln_pro.bridge")
    bridge.pro_features = _ProFeatures()
    package = types.ModuleType("kiln_pro")
    package.bridge = bridge
    monkeypatch.setitem(sys.modules, "kiln_pro", package)
    monkeypatch.setitem(sys.modules, "kiln_pro.bridge", bridge)
    monkeypatch.setitem(sys.modules, "kiln_pro.printability_overlay", overlay)

    judgment = {
        "warping": copy.deepcopy(P._WARPING_PUBLIC_DEFAULTS),
        "thermal_stress": copy.deepcopy(P._THERMAL_STRESS_PUBLIC_DEFAULTS),
        "adhesion_force": copy.deepcopy(P._ADHESION_FORCE_PUBLIC_DEFAULTS),
        "overhangs": {"default_limit_deg": 45.0, "material_limits_deg": {"abs": OVERHANG_LIMIT_DEG}},
    }
    import kiln.design_intelligence as di

    monkeypatch.setattr(
        di, "load_pro_overlay_or_empty",
        lambda kind: judgment if kind == "printability_judgment" else {},
    )


# ---------------------------------------------------------------------------
# The part and the doors
# ---------------------------------------------------------------------------


def _stepped_tower(path: str) -> str:
    """A plate with a column on it: an abrupt change of section (thermal
    stress), a flat base (warping) and a tall part (the adhesion estimate)."""
    base = trimesh.creation.box(extents=(60, 60, 4))
    base.apply_translation((0, 0, 2))
    column = trimesh.creation.box(extents=(12, 12, 70))
    column.apply_translation((0, 0, 39))
    trimesh.util.concatenate([base, column]).export(path)
    return path


def _tools() -> dict[str, Any]:
    from kiln.plugins.design_tools import plugin as design_plugin
    from kiln.plugins.printability_tools import plugin as printability_plugin

    tools: dict[str, Any] = {}

    class _Mcp:
        def tool(self, *_a, **_kw):
            def keep(fn):
                tools[fn.__name__] = fn
                return fn

            return keep

    printability_plugin.register(_Mcp())
    design_plugin.register(_Mcp())
    return tools


def _run(tmp_path, *, tuned: bool) -> dict[str, Any]:
    """Every door's answer for the stepped tower in ABS, tuned or on the floor."""
    with pytest.MonkeyPatch.context() as mp:
        _install_tuning(mp) if tuned else _block_kiln_pro(mp)
        path = _stepped_tower(str(tmp_path / f"tower_{'tuned' if tuned else 'floor'}.stl"))
        tools = _tools()
        report = P.analyze_printability(path, material="abs")
        return {
            "report": report,
            "reply": tools["analyze_printability"](path, material="abs"),
            "warping": tools["analyze_warping_risk"](path, material="abs"),
        }


@pytest.fixture(scope="module")
def tuned(tmp_path_factory) -> dict[str, Any]:
    return _run(tmp_path_factory.mktemp("tuned"), tuned=True)


@pytest.fixture(scope="module")
def floor(tmp_path_factory) -> dict[str, Any]:
    return _run(tmp_path_factory.mktemp("floor"), tuned=False)


def _block(reply_report: dict[str, Any], block: str) -> dict[str, Any]:
    return reply_report if not block else reply_report[block]


def _numbers(node: Any) -> list[float]:
    """Every number in *node*, the ones written in its sentences included."""
    found: list[float] = []
    if isinstance(node, bool):
        return found
    if isinstance(node, (int, float)):
        return [float(node)]
    if isinstance(node, str):
        return [float(n) for n in re.findall(r"\d+(?:\.\d+)?", node)]
    if isinstance(node, dict):
        for value in node.values():
            found += _numbers(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            found += _numbers(value)
    return found


def _same(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-3, abs_tol=1e-9)


# ---------------------------------------------------------------------------
# A tuned report's reply
# ---------------------------------------------------------------------------


def test_the_stand_in_really_tuned_the_report(tuned):
    """No control, no verdict: the in-process report holds every tuned number."""
    report = tuned["report"]
    assert report.thermal_stress.material_stress_factor == pytest.approx(STRESS_FACTOR)
    assert report.thin_walls.threshold_mm == pytest.approx(WALL_FLOOR_MM)
    assert report.thin_walls.threshold_basis == "material"
    assert report.overhang_threshold_deg == pytest.approx(OVERHANG_LIMIT_DEG)
    assert report.adhesion_force.adhesion_force_n == pytest.approx(
        report.bed_adhesion.contact_area_mm2 * ADHESION_STRENGTH, abs=1e-3
    )


@pytest.mark.parametrize(("block", "key"), WORKED_OUT, ids=[f"{b or 'report'}.{k}" for b, k in WORKED_OUT])
def test_a_tuned_reply_leaves_out_the_number(tuned, block, key):
    served = _block(tuned["reply"]["report"], block)
    assert key not in served, f"{block or 'report'}.{key} reached the reply: {served.get(key)!r}"
    # ...and the number is still there in the process that worked it out.
    held = getattr(tuned["report"], block) if block else tuned["report"]
    assert getattr(held, key) is not None or key == "tipping_ratio"


def test_a_tuned_reply_keeps_the_verdict(tuned):
    report, reply = tuned["report"], tuned["reply"]
    served = reply["report"]
    assert reply["verdict"] == P.verdict_of(report)[0]
    assert reply["message"].startswith(P.verdict_of(report)[1])
    for key in ("printable", "score", "grade", "recommendations"):
        assert served[key] == getattr(report, key), key
    for block in ("warping", "thermal_stress", "adhesion_force"):
        assert served[block]["risk_level"] == getattr(report, block).risk_level, block
        assert served[block]["recommendations"] == getattr(report, block).recommendations, block
    assert served["adhesion_force"]["will_detach"] == report.adhesion_force.will_detach
    assert served["thin_walls"]["threshold_basis"] == "material"
    assert served["adhesion"] == report.adhesion.to_dict(), "what to do about the bed stays"
    assert "tuned" not in served


def test_no_number_in_a_tuned_reply_hands_back_a_constant(tuned):
    """Neither a stand-in itself, nor the bed contact times the strength."""
    report = tuned["report"]
    derived = (report.bed_adhesion.contact_area_mm2 * ADHESION_STRENGTH,)
    for door in ("reply", "warping"):
        for number in _numbers(tuned[door]):
            for constant in STAND_INS + derived:
                assert not _same(number, constant), f"{door} says {number}, the tuning's {constant}"


def test_no_field_differs_from_the_floor_by_a_constant(tuned, floor):
    """The free-vs-Pro difference of one field: a number both replies carry
    never moves from the floor by the tuning's own ratio."""
    ratios = (
        STRESS_FACTOR / P._DEFAULT_STRESS_FACTOR,
        ADHESION_STRENGTH / P._DEFAULT_ADHESION_STRENGTH,
        SHRINKAGE_STRAIN / P._DEFAULT_SHRINKAGE_STRAIN,
    )

    def leaves(node: Any, path: str = "") -> dict[str, float]:
        out: dict[str, float] = {}
        if isinstance(node, dict):
            for key, value in node.items():
                out.update(leaves(value, f"{path}.{key}"))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                out.update(leaves(value, f"{path}[{index}]"))
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            out[path] = float(node)
        return out

    paid, free = leaves(tuned["reply"]), leaves(floor["reply"])
    for path in paid.keys() & free.keys():
        if free[path] and paid[path] != free[path]:
            for ratio in ratios:
                assert not _same(paid[path] / free[path], ratio), f"{path}: {free[path]} -> {paid[path]}"


def test_no_sentence_says_the_material_factor(tuned):
    """The "amplifies thermal stress" sentence is said, without the factor."""
    said = [s for s in tuned["reply"]["report"]["recommendations"] if "amplifies thermal stress" in s]
    assert said, "the stand-in factor clears the bar, so the sentence must be said"
    for sentence in said:
        assert "factor" not in sentence and "{" not in sentence, sentence
        assert f"{STRESS_FACTOR:.1f}" not in sentence, sentence


def test_the_warping_door_serves_its_block_without_the_deduction(tuned):
    """analyze_warping_risk serves the warping block on its own."""
    reply = tuned["warping"]
    assert reply["success"] is True
    assert reply["risk_level"] == tuned["report"].warping.risk_level
    assert "score_deduction" not in reply
    assert "tuned" not in reply


# ---------------------------------------------------------------------------
# The floor's reply, as before
# ---------------------------------------------------------------------------


def _without_tuned(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _without_tuned(v) for k, v in node.items() if k != "tuned"}
    if isinstance(node, list):
        return [_without_tuned(v) for v in node]
    return node


#: The score's own arithmetic, which no reply carries at any tier
#: (``kiln.printability._SCORE_ARITHMETIC``): the points each check took
#: off and the thermal check's factor.  Nothing reads them from a reply.
SCORE_ARITHMETIC = frozenset({"score_deduction", "stop_deduction", "material_stress_factor"})


def _keys_at_any_depth(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(key)
            found |= _keys_at_any_depth(value)
    elif isinstance(node, list):
        for value in node:
            found |= _keys_at_any_depth(value)
    return found


def test_the_floor_is_served_whole_but_for_the_scores_arithmetic(floor):
    """A report public Kiln worked out alone serves every measurement it
    ever did, since the measuring is public Kiln's own; what it never serves,
    on any tier, is how the score was added up."""
    report = floor["report"]
    assert not getattr(report, "tuned", False)
    expected = _without_arithmetic(_without_tuned(asdict(report)))
    assert report.to_dict() == expected
    served = floor["reply"]["report"]
    for block, key in WORKED_OUT:
        if key in SCORE_ARITHMETIC:
            assert key not in _block(served, block), f"{block or 'report'}.{key} reached the floor's reply"
        else:
            assert key in _block(served, block), f"{block or 'report'}.{key} left the floor's reply"
    assert not (_keys_at_any_depth(floor["reply"]) & SCORE_ARITHMETIC)
    assert not (_keys_at_any_depth(floor["warping"]) & SCORE_ARITHMETIC)
    # The verdict, the score and the reasons are all still there.
    assert served["score"] == report.score and served["grade"] == report.grade
    assert floor["warping"]["risk_level"] == report.warping.risk_level


def _without_arithmetic(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _without_arithmetic(v) for k, v in node.items() if k not in SCORE_ARITHMETIC}
    if isinstance(node, list):
        return [_without_arithmetic(v) for v in node]
    return node
