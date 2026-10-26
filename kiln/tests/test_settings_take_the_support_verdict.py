"""The slicer settings take supports from the one support verdict.

The settings step turned supports on only when it found a cantilever, and
otherwise said "No overhangs requiring support detected." without checking
any overhang.  A 4 mm ledge is too short to read as a cantilever, yet the
support verdict every other door gives (the report, the pre-print check,
the slice) says it needs supports, so the settings said one thing and the
slice did another.
"""

from __future__ import annotations

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")


def _box(lo, hi):
    b = trimesh.creation.box(extents=np.subtract(hi, lo))
    b.apply_translation(np.add(lo, hi) / 2.0)
    return b


@pytest.mark.parametrize("reach", [4.0, 8.0], ids=["a_ledge", "a_cantilever"])
def test_supports_are_the_verdicts_call(reach, tmp_path):
    from kiln.design_reasoning import infer_print_settings
    from kiln.printability import judge_supports, support_verdict

    path = tmp_path / "ledge.stl"
    trimesh.util.concatenate([_box((0, 0, 0), (20, 20, 20)), _box((20, 0, 17), (20 + reach, 20, 20))]).export(path)
    reason = support_verdict(judge_supports(str(path)))
    assert reason  # the fixture needs supports by the one verdict

    settings = infer_print_settings(str(path))
    assert settings.support_enabled is True
    assert settings.support_reason == f"Prints with supports ({reason}).", settings.support_reason


def test_a_part_with_nothing_to_hold_up_says_the_check_ran(tmp_path):
    from kiln.design_reasoning import infer_print_settings

    path = tmp_path / "block.stl"
    _box((0, 0, 0), (20, 20, 10)).export(path)
    settings = infer_print_settings(str(path))
    assert settings.support_enabled is False
    assert settings.support_reason == "Kiln's support check finds no overhang or bridge that needs support."
