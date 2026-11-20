"""A request the design limits do not cover is said as the range it is.

Kiln's design limits cover nozzles, layers and names within a range; a
request outside it is refused the same way on every ask, so it is said as
what it is: the range the limits cover and what Kiln does instead, never as
a passing miss ("right now", "check again").  Stood in here: an install
without kiln-pro whose server refuses a 2.5 mm nozzle.
"""

from __future__ import annotations

import re
import sys

import pytest

from kiln import material_limits

RANGE = "Kiln's design limits cover the nozzle size from 0.1 to 2 mm."


def _forget() -> None:
    from kiln import _pro_material_limits_bridge as bridge

    material_limits._answers.clear()
    material_limits._said.clear()
    bridge._service_down_until, bridge._service_down_miss = 0.0, None


@pytest.fixture(autouse=True)
def refused_outside_the_range(monkeypatch):
    import kiln.server as srv

    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    _forget()

    def fake(tool_name, _timeout=None, _asked_by_user=True, _feature=None, **kwargs):
        return {"success": False, "code": "INVALID_INPUT", "error": RANGE}

    monkeypatch.setattr(srv, "_pro_api_call", fake)
    yield
    _forget()


def _said_as_the_range(sentence: str) -> None:
    assert "0.1 to 2 mm" in sentence, sentence
    assert "right now" not in sentence and "said no" not in sentence, sentence
    assert not re.search(r"\bagain\b", sentence), sentence


def test_the_slicer_check_says_the_range_it_covers():
    floors = material_limits.sliced_floors(nozzle_mm=2.5)
    assert not floors.exact
    note = floors.note()
    _said_as_the_range(note)
    assert "general minimum wall" in note and "stops nothing" in note


def test_the_design_limits_say_the_range_they_cover():
    limits = material_limits.limits_for("PLA", nozzle_mm=2.5)
    assert limits.basic
    (line,) = [n for n in limits.notes() if "0.1 to 2 mm" in n]
    _said_as_the_range(line)
    assert "general figures" in line and "extra margin" in line


def test_any_other_refusal_is_still_said_as_a_refusal():
    from kiln.served_answer import Miss

    line = material_limits.miss_sentence(Miss("refused", "PLAN_LIMIT", "Not on this plan."))
    assert "said no" in line and "Not on this plan." in line
