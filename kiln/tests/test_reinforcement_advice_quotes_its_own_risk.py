"""Reinforcement advice quotes the threshold its risk was flagged against.

A structural risk is surfaced at the public threshold, and Kiln Pro's tuning
only decides whether a borderline one is a warning or a note.  The advice
for that risk ("redesign with a minimum ... mm dimension", "reduce the
height-to-base ratio below ...") quoted Kiln Pro's own tuned value instead,
which put the tuning in a sentence a paying caller reads and disagreed with
the risk it answered.  It now quotes the risk's own threshold.

Kiln Pro's tuning is stood in for with numbers no real setting has, so a
number in a sentence can only have come from the stand-in.  The height
setting it tunes is the TIPPING ratio (how tall a part may stand on its
base), read under that name.
"""

from __future__ import annotations

import math

import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")

TIPPING = 5.37        # stand-in for Kiln Pro's tipping ratio (public: 4.0)
MIN_SECTION = 3.61    # stand-in for Kiln Pro's minimum cross-section, mm² (public: 5.0)


def _tuned(monkeypatch, *, old_name_too: bool = True) -> None:
    import kiln.design_intelligence as di

    overlay = {"tipping_ratio": TIPPING, "min_cross_section_mm2": MIN_SECTION}
    if old_name_too:
        overlay["cantilever_risk_ratio"] = TIPPING
    monkeypatch.setattr(
        di, "load_pro_overlay_or_empty",
        lambda kind: dict(overlay) if kind == "structural_thresholds" else {},
    )


def _tall_post(tmp_path) -> str:
    """A 10 x 10 mm post 45 mm tall: 4.5 to 1, past the public 4.0, short of
    the stand-in 5.37."""
    post = trimesh.creation.box(extents=(10.0, 10.0, 45.0))
    post.apply_translation((0.0, 0.0, 22.5))
    path = tmp_path / "tall_post.stl"
    post.export(path)
    return str(path)


def _necked(tmp_path) -> str:
    """Two 20 mm blocks joined by a 2 x 2 mm neck: 4 mm² of section, under
    the public 5 mm² and over the stand-in 3.61."""
    low = trimesh.creation.box(extents=(20.0, 20.0, 10.0))
    low.apply_translation((0.0, 0.0, 5.0))
    neck = trimesh.creation.box(extents=(2.0, 2.0, 12.0))
    neck.apply_translation((0.0, 0.0, 16.0))
    high = trimesh.creation.box(extents=(20.0, 20.0, 10.0))
    high.apply_translation((0.0, 0.0, 27.0))
    part = trimesh.boolean.union([low, neck, high], engine="manifold")
    path = tmp_path / "necked.stl"
    part.export(path)
    return str(path)


def _advice(path: str) -> list[str]:
    from kiln.design_reasoning import recommend_reinforcements

    return [r.description for r in recommend_reinforcements(path)]


def _said(numbers: list[str], sentences: list[str]) -> list[str]:
    return [n for n in numbers if any(n in s for s in sentences)]


def test_the_base_advice_never_quotes_the_tuned_tipping_ratio(tmp_path, monkeypatch):
    _tuned(monkeypatch)
    advice = [s for s in _advice(_tall_post(tmp_path)) if "height-to-base" in s]
    assert advice, "control: the tall post is advised a wider base"
    assert _said([f"{TIPPING:.1f}:1"], advice) == [], advice
    assert any("4.0:1" in s for s in advice), advice  # the ratio the risk was flagged against


def test_the_neck_advice_never_quotes_the_tuned_section(tmp_path, monkeypatch):
    _tuned(monkeypatch)
    advice = [s for s in _advice(_necked(tmp_path)) if "minimum" in s]
    assert advice, "control: the neck is advised a minimum dimension"
    assert _said([f"{math.sqrt(MIN_SECTION):.1f} mm"], advice) == [], advice
    assert any(f"{math.sqrt(5.0):.1f} mm" in s for s in advice), advice


def test_kiln_pros_tipping_tuning_is_read_by_its_own_name(tmp_path, monkeypatch):
    """The tuning names what it tunes: a 4.5 to 1 post sits under the
    stand-in's tipping ratio, so Kiln Pro calls it a note, not a warning."""
    from kiln.design_reasoning import analyze_structural_risks

    _tuned(monkeypatch, old_name_too=False)
    tipping = [r for r in analyze_structural_risks(_tall_post(tmp_path))
               if r.metric_name == "height_to_base_ratio"]
    assert tipping, "control: the post is flagged at the public ratio"
    assert all(r.severity == "info" for r in tipping), [(r.severity, r.description) for r in tipping]


def test_with_no_tuning_the_public_ratio_still_warns(tmp_path, monkeypatch):
    import kiln.design_intelligence as di
    from kiln.design_reasoning import analyze_structural_risks

    monkeypatch.setattr(di, "load_pro_overlay_or_empty", lambda kind: {})
    tipping = [r for r in analyze_structural_risks(_tall_post(tmp_path))
               if r.metric_name == "height_to_base_ratio"]
    assert [r.severity for r in tipping] == ["warning"]
