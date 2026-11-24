"""The adhesion risk a reply states agrees with the adhesion-force check
beside it.

``adhesion_risk`` is the one word a person reads as "how likely is this to
come off the bed".  A reply states it twice (the bed-contact block and the
brim decision), next to the adhesion-force check's own answer.  The word is
the worse of what the contact reads and what the force check expects, so it
never says ``low`` beside a check that expects the part to come off.

The checks that judge contact itself (the score, the brim rows, the
small-spot sentence, the "low bed contact" advice) still read the contact,
so a tall part on a full footprint is not told it stands on a small spot.
"""

from __future__ import annotations

import pytest

trimesh = pytest.importorskip("trimesh")

from kiln.printability import (  # noqa: E402
    AdhesionForceEstimate,
    BedAdhesionAnalysis,
    analyze_printability,
    recommend_adhesion,
)
from tests import _real_exports as rx  # noqa: E402


def _box(tmp_path, name: str, size: tuple[float, float, float]) -> str:
    part = trimesh.creation.box(extents=size)
    part.apply_translation((0.0, 0.0, size[2] / 2.0))
    path = tmp_path / name
    part.export(path)
    return str(path)


@pytest.fixture()
def pillar(tmp_path) -> str:
    """A 4 x 4 mm pillar 200 mm tall: all of its footprint on the bed, and
    far too tall for it."""
    return _box(tmp_path, "pillar.stl", (4.0, 4.0, 200.0))


@pytest.fixture()
def thin_wall(tmp_path) -> str:
    """A 150 x 2 mm wall 20 mm tall, standing on its edge."""
    return _box(tmp_path, "wall.stl", (150.0, 2.0, 20.0))


@pytest.fixture()
def column(tmp_path) -> str:
    """A 20 x 20 mm column 300 mm tall."""
    return _box(tmp_path, "column.stl", (20.0, 20.0, 300.0))


@pytest.fixture()
def squat(tmp_path) -> str:
    """A 40 x 40 mm block 10 mm tall: real contact, nothing to tip."""
    return _box(tmp_path, "squat.stl", (40.0, 40.0, 10.0))


def _call(name: str, **args):
    from kiln import server

    with rx.without_kiln_pro():
        return rx.call(server.mcp, name, args)


def _stated(report: dict) -> tuple[str, str, str]:
    """The two words a report states for the risk, and the force check's own."""
    return (
        report["bed_adhesion"]["adhesion_risk"],
        report["adhesion"]["adhesion_risk"],
        report["adhesion_force"]["risk_level"],
    )


# ---------------------------------------------------------------------------
# Through the registered tools
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("part", ["pillar", "thin_wall"])
def test_a_part_expected_to_come_off_the_bed_is_never_low_risk(part, request):
    reply = _call("analyze_printability", file_path=request.getfixturevalue(part))
    assert reply.get("success"), reply
    report = reply["report"]
    assert report["adhesion_force"]["will_detach"] is True, "control: the force check expects it off the bed"
    assert _stated(report) == ("high", "high", "likely_detach")
    assert report["adhesion"]["brim_width_mm"] == 8
    # Its contact is whole, and nothing in the reply says otherwise.
    assert report["bed_adhesion"]["contact_percentage"] == 100.0
    assert not [r for r in report["recommendations"] if "Low bed contact" in r], report["recommendations"]
    assert "stands on only" not in reply["message"], reply["message"]
    assert "contact_risk" not in report["bed_adhesion"]


def test_a_borderline_hold_reads_medium(column):
    reply = _call("analyze_printability", file_path=column, material="pp")
    assert _stated(reply["report"]) == ("medium", "medium", "marginal"), reply["report"]["adhesion_force"]


def test_a_squat_part_with_real_contact_still_reads_low(squat):
    reply = _call("analyze_printability", file_path=squat)
    assert _stated(reply["report"]) == ("low", "low", "secure")
    assert reply["report"]["adhesion"]["brim_width_mm"] == 0
    assert reply["report"]["adhesion_force"]["will_detach"] is False


def test_the_adhesion_door_states_the_same_word(pillar, squat):
    tall = _call("recommend_adhesion_settings", model_path=pillar)["recommendation"]
    assert (tall["adhesion_risk"], tall["brim_width_mm"]) == ("high", 8), tall
    low = _call("recommend_adhesion_settings", model_path=squat)["recommendation"]
    assert (low["adhesion_risk"], low["brim_width_mm"]) == ("low", 0), low


def test_a_failure_diagnosis_names_the_height_not_the_contact(pillar, squat):
    reply = _call("diagnose_print_failure_live", model_path=pillar, material="PLA")
    assert reply.get("success"), reply
    diagnosis = reply["diagnosis"]
    assert diagnosis["failure_category"] == "adhesion", diagnosis
    causes = " ".join(diagnosis["probable_causes"])
    assert "tall for the area it stands on" in causes, causes
    assert "Insufficient bed contact" not in causes, causes
    assert diagnosis["slicer_overrides"]["brim_width"] == "8"
    assert reply["model_analysis"]["adhesion"]["brim_width_mm"] == 8
    calm = _call("diagnose_print_failure_live", model_path=squat, material="PLA")["diagnosis"]
    assert calm["failure_category"] != "adhesion", calm


# ---------------------------------------------------------------------------
# The reading itself
# ---------------------------------------------------------------------------


def _bed(risk: str, pct: float) -> BedAdhesionAnalysis:
    return BedAdhesionAnalysis(contact_area_mm2=100.0, contact_percentage=pct, adhesion_risk=risk)


def _force(level: str) -> AdhesionForceEstimate:
    return AdhesionForceEstimate(
        adhesion_force_n=1.0, peel_force_n=1.0, force_ratio=1.0, will_detach=level == "likely_detach",
        risk_level=level, score_deduction=0, recommendations=[],
    )


@pytest.mark.parametrize(("band", "level", "said"), [
    ("low", "secure", "low"),
    ("low", "marginal", "medium"),
    ("low", "likely_detach", "high"),
    ("medium", "secure", "medium"),
    ("medium", "likely_detach", "high"),
    ("high", "secure", "high"),
    ("high", "marginal", "high"),
])
def test_the_word_is_the_worse_of_the_contact_and_the_force_check(band, level, said):
    from kiln.printability import adhesion_risk_of, contact_band

    bed = _bed(band, {"low": 100.0, "medium": 20.0, "high": 6.0}[band])
    assert adhesion_risk_of(bed, _force(level)) == said
    assert recommend_adhesion(bed, adhesion_force=_force(level)).adhesion_risk == said
    # A block built by hand has no band of its own kept beside the word.
    assert contact_band(bed) == band


def test_with_no_force_check_the_word_is_the_contact_band():
    from kiln.printability import adhesion_risk_of

    assert adhesion_risk_of(_bed("medium", 20.0)) == "medium"
    assert recommend_adhesion(_bed("low", 100.0)).adhesion_risk == "low"


def test_the_contact_checks_still_read_the_contact(pillar):
    """In the process: the pillar's contact costs its score nothing and its
    brim comes from the force check, not from a contact row."""
    from kiln.printability import contact_band

    with rx.without_kiln_pro():
        report = analyze_printability(pillar)
    assert report.bed_adhesion.adhesion_risk == "high"
    assert contact_band(report.bed_adhesion) == "low"
    assert report.bed_adhesion.score_deduction == 0
    assert "adhesion-force check" in report.adhesion.rationale


def test_a_reading_taken_from_elsewhere_is_settled_to_one_word(pillar):
    """A report that takes its blocks from another reading of the same part
    still states one word, never lower than its own force check."""
    from kiln.printability import _take_servers_reading

    with rx.without_kiln_pro():
        report = analyze_printability(pillar)
    served = report.to_dict()
    served["bed_adhesion"]["adhesion_risk"] = "low"
    served["adhesion"]["adhesion_risk"] = "low"
    _take_servers_reading(report, served)
    assert report.adhesion_force.risk_level == "likely_detach"
    assert (report.bed_adhesion.adhesion_risk, report.adhesion.adhesion_risk) == ("high", "high")
