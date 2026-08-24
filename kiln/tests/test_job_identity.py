"""Telling one print from the next, on backends that mostly refuse to help.

A printer-slot rule needs to know when the print that justified a hold has
ended.  No two backends answer that the same way, and one of them answers it
misleadingly: Bambu reports ``task_id`` and ``subtask_id``, and on a LAN
print Kiln itself publishes both as the literal ``"0"``.  Trusting that
field would give every job on the machine the same identity, which is worse
than having no identity at all -- a hold would survive every reprint forever
and no test would notice, because the field was populated the whole time.

So the ladder is: a native id when it is really unique, a derived
(label, start-instant) pair when it is not, and nothing when neither can be
had.  These tests pin all three rungs, and -- more importantly -- pin the
DIRECTION of error, because that is the part a future change is most likely
to quietly flip.  Uncertainty must resolve to "different print, release
what you were holding", never to "same print, keep holding".
"""

from __future__ import annotations

import pytest

from kiln.printers.base import JobProgress
from kiln.printers.job_identity import (
    JobIdentity,
    clean_native_id,
    resolve,
    same_job,
)

_NOW = 1_760_000_000.0  # fixed wall clock, so start estimates are exact


def _job(**kwargs) -> JobProgress:
    return JobProgress(**kwargs)


class TestNativeIdRung:
    """A real vendor id short-circuits everything below it."""

    def test_native_id_is_used_when_present(self):
        ident = resolve(_job(file_name="bracket.gcode", print_time_seconds=60), native_id="4171")
        assert ident == JobIdentity(native="4171")

    def test_native_id_is_read_off_the_job_when_not_passed(self):
        ident = resolve(_job(file_name="bracket.gcode", job_id="4171"), now=_NOW)
        assert ident is not None and ident.native == "4171"

    @pytest.mark.parametrize("sentinel", ["0", "", "  ", "-1", "none", "NULL", "n/a"])
    def test_placeholder_ids_are_not_identities(self, sentinel):
        """The Bambu incident: an id-shaped field that is "0" for every job."""
        assert clean_native_id(sentinel) is None
        ident = resolve(
            _job(file_name="bracket.gcode", print_time_seconds=60),
            native_id=sentinel,
            now=_NOW,
        )
        # Falls through to the derived rung rather than claiming an identity.
        assert ident is not None and ident.native is None and ident.label == "bracket"

    def test_booleans_are_not_ids(self):
        """``True`` stringifies to a non-sentinel and would otherwise pass."""
        assert clean_native_id(True) is None
        assert clean_native_id(False) is None

    def test_integer_ids_survive(self):
        assert clean_native_id(4171) == "4171"


class TestDerivedRung:
    """Label plus the instant the print appears to have begun."""

    def test_label_is_normalized(self):
        ident = resolve(
            _job(file_name="/sdcard/model/Bracket.gcode.3mf", print_time_seconds=120),
            now=_NOW,
        )
        assert ident is not None
        assert ident.label == "bracket"
        assert ident.started_at == _NOW - 120

    def test_start_is_recomputed_from_the_printers_own_elapsed(self):
        """The same instant is derived before and after a restart.

        This is the whole reason the anchor is ``now - elapsed`` off the
        PRINTER's counter rather than a stamp Kiln keeps in memory: a
        process-local stamp reads as "no job" after a bounce, which would
        make restarting the server a way to shrug off the hold.
        """
        early = resolve(_job(file_name="bracket.gcode", print_time_seconds=100), now=_NOW)
        # ...600s later, the printer has counted 600s more.
        later = resolve(
            _job(file_name="bracket.gcode", print_time_seconds=700), now=_NOW + 600,
        )
        assert same_job(early, later)

    def test_no_label_and_no_id_is_unresolvable(self):
        assert resolve(_job(completion=40.0, print_time_seconds=60), now=_NOW) is None

    def test_label_alone_still_identifies_a_job(self):
        """Bambu after a restart: no native id, no elapsed, only a name.

        Refusing to match here would make any rule built on this identity a
        no-op on the most common printer brand, in exactly the restart case
        the design exists to survive.  The looseness is bounded by the
        caller releasing on a terminal state.
        """
        ident = resolve(_job(file_name="bracket.gcode"), now=_NOW)
        assert ident is not None and ident.label == "bracket" and ident.started_at is None
        assert same_job(ident, ident)

    def test_label_match_holds_even_when_only_one_side_knows_the_start(self):
        anchored = resolve(_job(file_name="bracket.gcode", print_time_seconds=60), now=_NOW)
        bare = resolve(_job(file_name="bracket.gcode"), now=_NOW)
        assert same_job(anchored, bare)

    def test_a_different_file_is_still_a_different_job_without_anchors(self):
        assert not same_job(
            JobIdentity(label="bracket"), JobIdentity(label="gasket"),
        )


class TestSameJobDirectionOfError:
    """Uncertainty resolves loose (release), never sticky (keep holding)."""

    def test_none_is_never_the_same_job(self):
        assert not same_job(None, None)
        assert not same_job(None, JobIdentity(native="1"))
        assert not same_job(JobIdentity(native="1"), None)

    def test_matching_native_ids_are_the_same_job(self):
        assert same_job(JobIdentity(native="4171"), JobIdentity(native="4171"))

    def test_differing_native_ids_are_different_jobs(self):
        assert not same_job(JobIdentity(native="4171"), JobIdentity(native="4172"))

    def test_native_versus_derived_is_not_an_identification(self):
        """A backend that changed its mind about what it can tell us."""
        assert not same_job(
            JobIdentity(native="4171"),
            JobIdentity(label="bracket", started_at=_NOW),
        )

    def test_different_labels_are_different_jobs(self):
        assert not same_job(
            JobIdentity(label="bracket", started_at=_NOW),
            JobIdentity(label="gasket", started_at=_NOW),
        )

    def test_a_long_pause_does_not_look_like_a_new_print(self):
        """Firmware that stops counting during a pause drifts the estimate."""
        assert same_job(
            JobIdentity(label="bracket", started_at=_NOW),
            JobIdentity(label="bracket", started_at=_NOW + 600),
        )

    def test_a_reprint_of_the_same_file_is_a_different_job(self):
        assert not same_job(
            JobIdentity(label="bracket", started_at=_NOW),
            JobIdentity(label="bracket", started_at=_NOW + 7200),
        )

    def test_comparison_never_raises_on_hostile_input(self):
        broken = JobIdentity(label="bracket", started_at=float("nan"))
        assert not same_job(broken, JobIdentity(label="bracket", started_at=_NOW))


class TestPersistence:
    """The record has to outlive the process that wrote it."""

    def test_round_trip_native(self):
        ident = JobIdentity(native="4171")
        assert JobIdentity.from_dict(ident.to_dict()) == ident

    def test_round_trip_derived(self):
        ident = JobIdentity(label="bracket", started_at=_NOW)
        assert JobIdentity.from_dict(ident.to_dict()) == ident

    @pytest.mark.parametrize(
        "garbage", [None, "", 42, [], {}, {"native": None, "label": None}, {"label": 7}],
    )
    def test_unreadable_records_read_as_no_identity(self, garbage):
        """A truncated or hand-edited record must not raise into the caller."""
        assert JobIdentity.from_dict(garbage) is None

    def test_unparseable_start_degrades_rather_than_raising(self):
        ident = JobIdentity.from_dict({"label": "bracket", "started_at": "not-a-number"})
        assert ident is not None and ident.started_at is None


class TestAdaptersSurfaceWhatTheyActuallyHave:
    """The two claims that sent this design one way rather than another."""

    def test_prusalink_surfaces_its_real_job_id(self):
        """It was throwing away the same handle its own cancel endpoint takes."""
        from unittest.mock import patch

        from kiln.printers.prusalink import PrusaLinkAdapter

        adapter = PrusaLinkAdapter(host="http://prusa.local", api_key="k", retries=1)
        payload = {
            "job": {"id": 4171, "progress": 42.0, "time_printing": 600, "time_remaining": 900},
        }
        with patch.object(adapter, "_get_json", return_value=payload):
            job = adapter.get_job()

        assert job.job_id == "4171"
        assert resolve(job) == JobIdentity(native="4171")

    def test_bambu_lan_print_reports_no_usable_id(self):
        """task_id/subtask_id are "0" on LAN -- Kiln publishes them that way.

        If this ever starts returning "0" as an identity, every print on the
        machine becomes the same print and a hold outlives its job silently.
        """
        from kiln.printers.bambu import _first_real_job_id

        assert _first_real_job_id("0", "0") is None
        assert _first_real_job_id("0", "918273") == "918273"
        assert _first_real_job_id(None, None) is None
