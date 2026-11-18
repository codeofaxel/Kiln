"""The environment check never calls a part fine above a heat limit Kiln states.

Kiln states a highest service temperature for each material in two places.
Where they differ, the environment check holds a part to the lower one.
"""
from __future__ import annotations

import pytest

from kiln.design_intelligence import _get_kb, check_environment_compatibility


def _both_figures() -> list[tuple[str, float, float]]:
    kb = _get_kb()
    rows = []
    for key, env in kb.environment.items():
        if key.startswith("_"):
            continue
        first = (env.get("temperature_range") or {}).get("max_service_c")
        second = ((kb.materials.get(key) or {}).get("thermal") or {}).get("max_service_temp_c")
        if isinstance(first, (int, float)) and isinstance(second, (int, float)) and second > 0:
            rows.append((key, float(first), float(second)))
    return rows


@pytest.mark.parametrize("material, celsius", [("nylon", 70), ("pp", 80), ("pps", 150), ("peek", 145)])
def test_a_temperature_above_either_stated_limit_is_not_recommended(material, celsius):
    report = check_environment_compatibility(material, f"a bracket that sits at {celsius} C")
    assert report is not None
    assert report.overall_verdict != "recommended"
    assert any("outside service range" in w for w in report.warnings)


def test_every_material_is_held_to_its_lower_stated_limit():
    rows = _both_figures()
    assert len(rows) > 20
    for material, first, second in rows:
        lower = min(first, second)
        report = check_environment_compatibility(material, f"a part that sits at {lower + 1:.0f} C")
        assert report is not None, material
        assert report.overall_verdict != "recommended", (material, lower)
        assert report.per_category_ratings["temperature_range"]["max_service_c"] == lower, material


def test_a_temperature_inside_both_limits_is_still_fine():
    report = check_environment_compatibility("petg", "a part that sits at 40 C")
    assert report is not None
    assert not any("outside service range" in w for w in report.warnings)


@pytest.mark.parametrize("material, celsius", [("tpu", 60), ("tpu_95a", 60), ("tpu_85a", 55)])
def test_a_flexible_part_is_not_called_fine_where_it_softens(material, celsius):
    report = check_environment_compatibility(material, f"a part that sits at {celsius} C")
    assert report is not None
    assert report.overall_verdict != "recommended"
    assert any("outside service range" in w for w in report.warnings)
