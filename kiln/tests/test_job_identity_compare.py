"""Same print, a different print, or cannot tell -- three answers, not two.

``same_job`` answers yes or no and resolves every doubt to "no", because each
of its callers releases a hold on "no" and releasing is their safe move.  The
print watchdog's safe move runs the other way: it must not walk away from the
print it guards because a printer left a name out.  So it needs "different"
to mean proof, with doubt kept apart from it.

These tests pin :func:`kiln.printers.job_identity.compare`'s three answers,
and that ``same_job`` -- now written through it -- answers every pair exactly
as it did before, so engagement, hand-backs and outcome matching cannot drift
with it.
"""

from __future__ import annotations

import itertools

import pytest

from kiln.printers import job_identity as ji
from kiln.printers.base import JobProgress
from kiln.printers.job_identity import JobIdentity, same_job

_NOW = 1_760_000_000.0  # fixed wall clock, so start estimates are exact

#: The module's own tolerance, read rather than retyped.
_TOLERANCE = ji._START_TOLERANCE_S

# Read with a fallback so the file collects against the code before the
# three-way answer existed, and each test fails on its own claim there.
SAME = getattr(ji, "SAME", "same")
DIFFERENT = getattr(ji, "DIFFERENT", "different")
UNKNOWN = getattr(ji, "UNKNOWN", "unknown")


def _compare(a: JobIdentity | None, b: JobIdentity | None) -> str:
    compare = getattr(ji, "compare", None)
    assert compare is not None, "job_identity has no three-way comparison"
    return compare(a, b)


#: Every shape an identity arrives in, the hand-built and hostile ones too --
#: ``from_dict`` guards what it reads, but nothing stops a caller building a
#: ``JobIdentity`` directly.
_GRID: list[JobIdentity | None] = [
    None,
    JobIdentity(),
    JobIdentity(native="4171"),
    JobIdentity(native="4172"),
    JobIdentity(native="4171", label="bracket", started_at=_NOW),
    JobIdentity(native="", label="bracket"),
    JobIdentity(label="bracket"),
    JobIdentity(label="bracket", started_at=_NOW),
    JobIdentity(label="bracket", started_at=_NOW + 600),
    JobIdentity(label="bracket", started_at=_NOW + _TOLERANCE),
    JobIdentity(label="bracket", started_at=_NOW + 7200),
    JobIdentity(label="gasket"),
    JobIdentity(label="gasket", started_at=_NOW),
    JobIdentity(label="bracket", started_at=float("nan")),
    JobIdentity(label="bracket", started_at=float("inf")),
    JobIdentity(label="bracket", started_at="not-a-number"),  # type: ignore[arg-type]
]


class TestDifferentMeansProof:
    def test_two_native_ids_are_compared_on_the_id(self):
        assert _compare(JobIdentity(native="4171"), JobIdentity(native="4171")) == SAME
        assert _compare(JobIdentity(native="4171"), JobIdentity(native="4172")) == DIFFERENT

    def test_two_different_labels_are_different_prints(self):
        assert _compare(JobIdentity(label="bracket"), JobIdentity(label="gasket")) == DIFFERENT
        assert (
            _compare(
                JobIdentity(label="bracket", started_at=_NOW),
                JobIdentity(label="gasket", started_at=_NOW),
            )
            == DIFFERENT
        )

    def test_one_label_with_no_start_on_either_side_is_the_same_print(self):
        assert _compare(JobIdentity(label="bracket"), JobIdentity(label="bracket")) == SAME

    def test_one_label_with_a_start_on_only_one_side_is_the_same_print(self):
        """Bambu's start is a Kiln stopwatch, absent for prints Kiln did not start."""
        anchored = JobIdentity(label="bracket", started_at=_NOW)
        bare = JobIdentity(label="bracket")
        assert _compare(anchored, bare) == SAME
        assert _compare(bare, anchored) == SAME

    def test_one_label_with_both_starts_is_judged_on_the_tolerance(self):
        early = JobIdentity(label="bracket", started_at=_NOW)
        at_the_edge = JobIdentity(label="bracket", started_at=_NOW + _TOLERANCE)
        past_it = JobIdentity(label="bracket", started_at=_NOW + _TOLERANCE + 1)
        assert _compare(early, at_the_edge) == SAME
        assert _compare(early, past_it) == DIFFERENT

    def test_a_native_id_against_a_label_is_not_evidence_either_way(self):
        """A backend that changed its mind about what it can tell us."""
        native = JobIdentity(native="4171")
        label = JobIdentity(label="bracket", started_at=_NOW)
        assert _compare(native, label) == UNKNOWN
        assert _compare(label, native) == UNKNOWN

    @pytest.mark.parametrize("missing", [None, JobIdentity()], ids=["none", "unusable"])
    def test_a_missing_identity_is_never_evidence(self, missing):
        for other in (None, JobIdentity(), JobIdentity(native="4171"), JobIdentity(label="bracket")):
            assert _compare(missing, other) == UNKNOWN
            assert _compare(other, missing) == UNKNOWN

    @pytest.mark.parametrize("broken", [float("nan"), float("inf")], ids=["nan", "inf"])
    def test_a_start_that_is_not_a_finite_number_is_not_evidence(self, broken):
        """Fails the tolerance test, and must not therefore read as a reprint."""
        hostile = JobIdentity(label="bracket", started_at=broken)
        assert _compare(hostile, JobIdentity(label="bracket", started_at=_NOW)) == UNKNOWN

    def test_the_answer_does_not_depend_on_which_side_is_asked_first(self):
        lopsided = [
            (a, b)
            for a, b in itertools.product(_GRID, repeat=2)
            if _compare(a, b) != _compare(b, a)
        ]
        assert lopsided == []


class TestTheNameOnItsOwn:
    """A job started from a vendor's cloud app carries an id; one Kiln started
    over the LAN does not.  The name is the only axis the two share."""

    def test_resolve_label_keeps_the_name_of_a_job_that_also_has_an_id(self):
        resolve_label = getattr(ji, "resolve_label", None)
        assert resolve_label is not None, "job_identity cannot resolve a job's name on its own"
        job = JobProgress(
            file_name="/sdcard/model/Bracket.gcode.3mf", print_time_seconds=120, job_id="918273"
        )

        assert ji.resolve(job, now=_NOW) == JobIdentity(native="918273")  # the ladder is unchanged
        assert resolve_label(job, now=_NOW) == JobIdentity(label="bracket", started_at=_NOW - 120)

    def test_resolve_label_of_an_unnamed_job_is_nothing(self):
        resolve_label = getattr(ji, "resolve_label", None)
        assert resolve_label is not None, "job_identity cannot resolve a job's name on its own"
        assert resolve_label(JobProgress(job_id="918273", print_time_seconds=60), now=_NOW) is None


def _same_job_before(a: JobIdentity | None, b: JobIdentity | None) -> bool:
    """``same_job`` exactly as it answered before it was written through ``compare``.

    Frozen here on purpose, as the reference the current one must match on
    every pair.  It reads the module's tolerance, so only the logic is frozen.
    """
    try:
        if a is None or b is None or not a.is_usable or not b.is_usable:
            return False
        if a.native is not None or b.native is not None:
            return a.native is not None and a.native == b.native
        if a.label != b.label:
            return False
        if a.started_at is None or b.started_at is None:
            return True
        return abs(a.started_at - b.started_at) <= ji._START_TOLERANCE_S
    except Exception:  # noqa: BLE001 — the reference raised nothing either
        return False


class TestSameJobIsUnchanged:
    """Engagement, hand-backs and outcome matching all release on ``False``."""

    def test_every_pair_answers_exactly_as_before(self):
        pairs = list(itertools.product(_GRID, repeat=2))
        # A grid on which the reference only ever said one thing would prove nothing.
        assert {_same_job_before(a, b) for a, b in pairs} == {True, False}

        changed = [(a, b, same_job(a, b)) for a, b in pairs if same_job(a, b) != _same_job_before(a, b)]
        assert changed == []
