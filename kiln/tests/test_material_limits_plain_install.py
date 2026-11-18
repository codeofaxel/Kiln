"""A plastic's design limits with no answer from Kiln's servers.

On an install without kiln-pro every check reads its limits through
``kiln.material_limits.limits_for``.  When no answer comes (offline, signed
out, the servers silent, a refusal) every figure is the general one, each
says ``basis: general``, the reason is said once per cause in the shared
voice, and no plastic's own figure is used or named.

And the plastics' own figures are not in public Kiln: no material record
carries a design-limits block, and no bundled file or module holds a table of
them.
"""

from __future__ import annotations

import errno
import json
import re
import sys
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from kiln import generic_limits, material_limits

_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "kiln"


@pytest.fixture(autouse=True)
def _plain_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    material_limits.forget()
    yield
    material_limits.forget()


def _serve(monkeypatch, answer: Any) -> list[str]:
    import kiln.server as srv

    calls: list[str] = []

    def fake(tool_name, _timeout=None, _asked_by_user=True, **kwargs):
        calls.append(tool_name)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(srv, "_pro_api_call", fake)
    return calls


_MISSES = {
    "offline": urllib.error.URLError(OSError(errno.ENETUNREACH, "Network is unreachable")),
    "signed_out": {"success": False, "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "Sign in.", "why": "signed_out"},
    "unanswered": {"success": False, "code": "SERVER_UNREACHABLE", "error": "No answer.", "why": "unanswered"},
    "refused": {"success": False, "code": "INVALID_INPUT", "error": "That request cannot be read.", "why": "refused"},
}


@pytest.mark.parametrize("cause", sorted(_MISSES))
def test_a_miss_checks_against_the_general_figures_and_says_why(monkeypatch, cause):
    _serve(monkeypatch, _MISSES[cause])
    limits = material_limits.limits_for("ABS")
    assert limits.miss is not None and limits.miss.cause == cause
    assert limits.basic
    assert all(limits.basis[q] in ("general", "not_applicable") for q in material_limits.PER_PLASTIC)
    assert limits.design_limits() == {
        "min_wall_thickness_mm": generic_limits.WALL_MIN_MM,
        "max_unsupported_overhang_deg": generic_limits.OVERHANG_UNSUPPORTED_DEG,
        "max_bridge_length_mm": generic_limits.BRIDGE_MAX_MM,
        "min_hole_diameter_mm": generic_limits.HOLE_USABLE_MM,
        "min_pin_diameter_mm": generic_limits.PIN_MIN_MM,
        "thread_min_pitch_mm": generic_limits.thread_min_pitch_mm(),
    }
    notes = limits.notes()
    assert len(notes) == 1
    assert "general figures" in notes[0]
    assert "kiln-pro" not in notes[0].lower() and "kiln_pro" not in notes[0].lower()
    # Said once per cause in a process.
    assert material_limits.limits_for("ABS").notes() == []


def test_the_printability_threshold_and_a_slice_use_the_general_figure(monkeypatch):
    from kiln.printability import overhang_threshold_deg
    from kiln.slicer_supports import support_settings

    _serve(monkeypatch, _MISSES["offline"])
    assert overhang_threshold_deg("pla") == generic_limits.OVERHANG_UNSUPPORTED_DEG
    assert support_settings(overhang_threshold_deg("pla")) == support_settings(generic_limits.OVERHANG_UNSUPPORTED_DEG)


def test_the_brief_carries_the_general_figures(monkeypatch):
    from kiln.design_intelligence import get_design_constraints

    _serve(monkeypatch, _MISSES["signed_out"])
    rules = get_design_constraints("a desk organizer", material="abs").combined_rules
    assert rules["material_max_unsupported_overhang_deg"] == generic_limits.OVERHANG_UNSUPPORTED_DEG
    assert rules["material_max_bridge_length_mm"] == generic_limits.BRIDGE_MAX_MM
    assert "material_recommended_wall_thickness_mm" not in rules


def test_a_flexible_plastic_answered_generally_says_the_figures_are_for_rigid_ones(monkeypatch):
    _serve(monkeypatch, _MISSES["offline"])
    notes = material_limits.limits_for("tpu_85a").notes()
    assert any("flexible" in line for line in notes)


def test_a_static_safe_grade_answered_generally_says_its_own_wall_is_not_known(monkeypatch):
    _serve(monkeypatch, _MISSES["offline"])
    notes = material_limits.limits_for("petg_esd").notes()
    assert any("static-safe" in line for line in notes)


def test_the_material_lookups_show_the_general_block(monkeypatch):
    from kiln.design_intelligence import get_public_material_profile

    calls = _serve(monkeypatch, _MISSES["offline"])
    block = get_public_material_profile("abs").design_limits
    assert block["basis"] == "general"
    assert block["max_unsupported_overhang_deg"] == generic_limits.OVERHANG_UNSUPPORTED_DEG
    assert calls == [], "a material lookup never asks for a plastic's figures"


# ---------------------------------------------------------------------------
# the figures are not in public Kiln
# ---------------------------------------------------------------------------

_OLD_KEYS = (
    "max_unsupported_overhang_deg",
    "max_bridge_length_mm",
    "min_pin_diameter_mm",
    "min_hole_diameter_mm",
    "min_wall_thickness_mm",
    "thread_min_pitch_mm",
    "min_embossed_text_height_mm",
    "min_engraved_text_depth_mm",
    "min_wall_thickness_note",
)


def test_no_material_record_carries_design_limits():
    raw = json.loads((_PACKAGE / "data" / "design_knowledge" / "materials.json").read_text(encoding="utf-8"))
    records = {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict)}
    assert len(records) >= 38
    carrying = sorted(k for k, v in records.items() if "design_limits" in v)
    assert carrying == []


#: Keys a data file may carry under the same name for something else: a
#: functional requirement's own minimum wall (load bearing, outdoor use) is a
#: rule about the part, not a plastic's limit.
_SAME_NAME_ELSEWHERE = {("functional_requirements.json", "min_wall_thickness_mm")}


def test_no_bundled_data_file_holds_the_design_limit_keys():
    found = []
    for path in (_PACKAGE / "data").rglob("*.json"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for key in _OLD_KEYS:
            if (path.name, key) in _SAME_NAME_ELSEWHERE:
                continue
            if f'"{key}"' in text:
                found.append(f"{path.relative_to(_PACKAGE)}: {key}")
    assert found == []


def test_the_general_home_is_one_figure_per_quantity():
    source = (_PACKAGE / "generic_limits.py").read_text(encoding="utf-8")
    assert not re.search(r"\b(pla|petg|abs|asa|tpu|nylon)\b", source, re.IGNORECASE), (
        "the general home names a plastic"
    )
