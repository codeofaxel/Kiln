"""The co-print matrix obeys its own structural rules.

``co_print_compatibility`` answers "can these two materials be fused in one
part, and how well does the interface hold?".  Three invariants hold across
the authored data, and each one has already failed in a way nobody noticed:

  1. SYMMETRY.  Interface adhesion is a property of a boundary, so A-to-B and
     B-to-A are the same physical question.  Seven pairs were one-directional
     on 2026-07-25: asking ``abs`` about ``tpu`` answered "moderate" while
     asking ``tpu`` about ``abs`` answered nothing at all.  A lookup keyed on
     the wrong side of the pair returned silence, which reads as "no data"
     when the answer was sitting in the file.

  2. ``compatible`` IS DERIVED, never independently authored.  Every one of
     the authored cells obeys {excellent, good, moderate} -> True and
     {poor, none} -> False.  Storing it separately is a chance for the two to
     disagree, so this pins them together.

  3. NO DANGLING TARGETS.  A pair may only reference a material that has a
     row of its own, or the reverse lookup is impossible by construction.

The matrix is deliberately SPARSE — not every material pair is a question
anyone asks — so these are consistency rules, NOT a coverage requirement.
Coverage is tracked separately in test_intelligence_parity.py.
"""

from __future__ import annotations

import json
from pathlib import Path

DATA = (Path(__file__).parent.parent / "src" / "kiln" / "data"
        / "design_knowledge" / "multi_material_pairing.json")

_COMPATIBLE_AT = {"excellent", "good", "moderate"}
_INCOMPATIBLE_AT = {"poor", "none"}


def _matrix() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))["co_print_compatibility"]


def test_every_pair_is_recorded_in_both_directions():
    matrix = _matrix()
    one_way = []
    for a, row in matrix.items():
        for b in row:
            if b in matrix and a not in matrix[b]:
                one_way.append(f"{a}+{b} is recorded on {a} but not on {b}")
    assert not one_way, (
        "interface adhesion is a property of a boundary, so these pairs answer "
        "from one side and stay silent from the other:\n  " + "\n  ".join(one_way)
    )


def test_both_directions_agree():
    matrix = _matrix()
    disagree = []
    for a, row in matrix.items():
        for b, entry in row.items():
            back = matrix.get(b, {}).get(a)
            if back is not None and back != entry:
                disagree.append(f"{a}+{b} says {entry} but {b}+{a} says {back}")
    assert not disagree, (
        "the same interface is described two different ways:\n  " + "\n  ".join(disagree)
    )


def test_compatible_is_derived_from_adhesion():
    matrix = _matrix()
    drift = []
    for a, row in matrix.items():
        for b, entry in row.items():
            adhesion = entry["interface_adhesion"]
            if adhesion in _COMPATIBLE_AT:
                want = True
            elif adhesion in _INCOMPATIBLE_AT:
                want = False
            else:
                drift.append(f"{a}+{b}: unknown adhesion value {adhesion!r}")
                continue
            if entry["compatible"] is not want:
                drift.append(
                    f"{a}+{b}: compatible={entry['compatible']} but adhesion "
                    f"{adhesion!r} derives {want}"
                )
    assert not drift, (
        "compatible must follow from interface_adhesion, never be authored "
        "against it:\n  " + "\n  ".join(drift)
    )


def test_no_pair_targets_a_material_without_a_row():
    matrix = _matrix()
    dangling = sorted({
        b for a, row in matrix.items() for b in row if b not in matrix
    })
    assert not dangling, (
        f"these materials are referenced as co-print partners but have no row "
        f"of their own, so the reverse lookup cannot work: {dangling}"
    )
