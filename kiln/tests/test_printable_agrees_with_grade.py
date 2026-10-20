"""A printability report never says "grade F" and "printable" together.

The letter and the printable verdict were two separate lines, 60 and 50, so
a part scoring 50-59 came back "Printability score: 54/100 (grade F).
Printable." and design_session handed agents ``prints: true`` beside grade F.
No print gate reads the flag, but the agent does, and the agent is who offers
to print.  The verdict is now read off the same ladder as the letter.
"""

from __future__ import annotations

from kiln.printability import (
    BundlePrintabilityFindings,
    _apply_placement_check,
    _score_to_grade,
)

# A part well inside a bed, so placement adds no fault and the verdict is the
# score's alone.
_ON_THE_BED = {"x_min": 10.0, "x_max": 60.0, "y_min": 10.0, "y_max": 60.0, "z_min": 0.0, "z_max": 40.0}
_BED = (256.0, 256.0, 256.0)


def test_every_score_is_printable_exactly_when_it_grades_above_f():
    for score in range(101):
        _, grade, printable, faults = _apply_placement_check(score, [], dict(_ON_THE_BED), build_volume=_BED)
        assert faults == [], faults
        assert printable == (grade != "F"), f"score {score}: grade {grade}, printable {printable}"


def test_the_headphone_stand_score_is_not_printable():
    # 2026-10-19: ChatGPT's first headphone stand scored 54 (F) for overhangs
    # and bridging; the report still called it printable.
    _, grade, printable, _ = _apply_placement_check(54, [], dict(_ON_THE_BED), build_volume=_BED)
    assert (grade, printable) == ("F", False)


def test_a_bundle_with_no_verdict_reads_it_off_the_grade():
    for score in (49, 54, 59, 60, 74):
        findings = BundlePrintabilityFindings.from_bundle_findings(
            {"score": score, "grade": _score_to_grade(score)}
        )
        assert findings.printable == (_score_to_grade(score) != "F"), score
