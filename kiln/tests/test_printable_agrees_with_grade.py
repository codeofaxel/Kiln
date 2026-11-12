"""A printability report never says "grade F" and "printable" together.

The letter and the printable verdict were two separate lines, 60 and 50, so
a part scoring 50-59 came back "Printability score: 54/100 (grade F).
Printable." and design_session handed agents ``prints: true`` beside grade F.
No print gate reads the flag, but the agent does, and the agent is who offers
to print.  The verdict is now read off the same ladder as the letter.
"""

from __future__ import annotations

from kiln.printability import _apply_placement_check

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
    # A headphone stand scoring 54 (F) for overhangs and bridging must not
    # be called printable by the report.
    _, grade, printable, _ = _apply_placement_check(54, [], dict(_ON_THE_BED), build_volume=_BED)
    assert (grade, printable) == ("F", False)
