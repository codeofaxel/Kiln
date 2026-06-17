"""Tests for kiln.terms -- terms of use acceptance tracking."""

from __future__ import annotations

import time
from unittest import mock

import pytest

from kiln.persistence import KilnDB
from kiln.terms import (
    _CURRENT_TERMS_VERSION,
    _SETTINGS_KEY_TIMESTAMP,
    _SETTINGS_KEY_VERSION,
    _SUMMARY_REVIEWED_FOR_VERSION,
    _TERMS_SUMMARY,
    get_accepted_version,
    is_current,
    prompt_acceptance,
    record_acceptance,
)


@pytest.fixture()
def db(tmp_path):
    """In-memory-like DB using a temp file."""
    return KilnDB(db_path=str(tmp_path / "test.db"))


# ---------------------------------------------------------------------------
# get_accepted_version
# ---------------------------------------------------------------------------


class TestGetAcceptedVersion:
    def test_returns_none_when_never_accepted(self, db):
        assert get_accepted_version(db=db) is None

    def test_returns_version_after_acceptance(self, db):
        db.set_setting(_SETTINGS_KEY_VERSION, "1.0")
        assert get_accepted_version(db=db) == "1.0"

    def test_returns_stale_version(self, db):
        db.set_setting(_SETTINGS_KEY_VERSION, "0.9")
        assert get_accepted_version(db=db) == "0.9"


# ---------------------------------------------------------------------------
# is_current
# ---------------------------------------------------------------------------


class TestIsCurrent:
    def test_false_when_never_accepted(self, db):
        assert is_current(db=db) is False

    def test_false_when_old_version(self, db):
        db.set_setting(_SETTINGS_KEY_VERSION, "0.1")
        assert is_current(db=db) is False

    def test_true_when_current_version(self, db):
        db.set_setting(_SETTINGS_KEY_VERSION, _CURRENT_TERMS_VERSION)
        assert is_current(db=db) is True


# ---------------------------------------------------------------------------
# record_acceptance
# ---------------------------------------------------------------------------


class TestRecordAcceptance:
    def test_stores_version_and_timestamp(self, db):
        before = time.time()
        record_acceptance(db=db)
        after = time.time()

        assert db.get_setting(_SETTINGS_KEY_VERSION) == _CURRENT_TERMS_VERSION
        ts = float(db.get_setting(_SETTINGS_KEY_TIMESTAMP))
        assert before <= ts <= after

    def test_is_current_after_acceptance(self, db):
        assert is_current(db=db) is False
        record_acceptance(db=db)
        assert is_current(db=db) is True

    def test_overwrite_old_version(self, db):
        db.set_setting(_SETTINGS_KEY_VERSION, "0.1")
        assert is_current(db=db) is False
        record_acceptance(db=db)
        assert is_current(db=db) is True


# ---------------------------------------------------------------------------
# prompt_acceptance
# ---------------------------------------------------------------------------


class TestPromptAcceptance:
    def test_returns_true_on_accept(self, db):
        with mock.patch("kiln.persistence.get_db", return_value=db):
            with mock.patch("click.confirm", return_value=True):
                with mock.patch("click.echo"):
                    assert prompt_acceptance() is True
        assert is_current(db=db) is True

    def test_returns_false_on_decline(self, db):
        with mock.patch("kiln.persistence.get_db", return_value=db):
            with mock.patch("click.confirm", return_value=False):
                with mock.patch("click.echo"):
                    assert prompt_acceptance() is False
        assert is_current(db=db) is False

    def test_does_not_record_on_decline(self, db):
        with mock.patch("kiln.persistence.get_db", return_value=db):
            with mock.patch("click.confirm", return_value=False):
                with mock.patch("click.echo"):
                    prompt_acceptance()
        assert get_accepted_version(db=db) is None


# ---------------------------------------------------------------------------
# Summary review marker -- forcing function so the copy can't silently go stale
# ---------------------------------------------------------------------------


class TestSummaryReviewMarker:
    def test_summary_reviewed_for_current_version(self):
        """The review marker must track the current terms version.

        Goes red whenever _CURRENT_TERMS_VERSION is bumped without
        re-reviewing _TERMS_SUMMARY and the other acceptance surfaces.
        Bumping the marker is the conscious "yes, I refreshed the copy" step.
        """
        assert _SUMMARY_REVIEWED_FOR_VERSION == _CURRENT_TERMS_VERSION, (
            "Terms version changed but the summary review marker was not bumped. "
            "Re-read _TERMS_SUMMARY and the web/MCP acceptance copy, update what "
            "materially changed, then set _SUMMARY_REVIEWED_FOR_VERSION to match."
        )

    def test_summary_covers_the_load_bearing_points(self):
        """The summary must carry the points that make it fair notice."""
        # Safety responsibility
        assert "safety" in _TERMS_SUMMARY.lower()
        # The tier / commercial rule (the headline v3.0 addition)
        assert "Business" in _TERMS_SUMMARY
        # Fee transparency
        assert "5%" in _TERMS_SUMMARY
        # Canonical links
        assert "https://kiln3d.com/terms" in _TERMS_SUMMARY
        assert "https://kiln3d.com/privacy" in _TERMS_SUMMARY
