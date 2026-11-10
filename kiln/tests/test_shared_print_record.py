"""A shared print result says how a print went, and nothing about the part.

Every door that shares a print result builds it through
``kiln.community_sync.shared_print_record``.  These tests send a print through
each door holding everything a caller could know about the part, and read what
would leave this computer.
"""

from __future__ import annotations

import json

import pytest

from kiln import community_autofire, community_outbox, community_sync

ABOUT_THE_PART = (
    "geometric_signature",
    "geometric_signature_v2",
    "settings",
    "settings_hash",
    "print_time_seconds",
    "file_hash",
    "file_name",
)

SIGNATURE = "0123456789abcdef"
SIGNATURE_V2 = "v2:0123456789abcdef"


@pytest.fixture
def queued(monkeypatch):
    """Every record a door hands to the queue, as ``(key, record)``."""
    seen: list[tuple[str, dict]] = []

    def _contribute(key, record, **_kwargs):
        seen.append((key, dict(record)))
        return {"queued": True}

    monkeypatch.setattr(community_outbox, "contribute", _contribute)
    return seen


def _nothing_about_the_part(record: dict) -> None:
    for name in ABOUT_THE_PART:
        assert name not in record, f"{name} would be shared"
    text = json.dumps(record)
    assert SIGNATURE not in text
    assert "layer_height" not in text


def test_the_record_keeps_only_how_it_went():
    record = community_sync.shared_print_record(
        {
            "geometric_signature": SIGNATURE,
            "geometric_signature_v2": SIGNATURE_V2,
            "settings": {"layer_height_mm": 0.2},
            "settings_hash": "abcd",
            "print_time_seconds": 3600,
            "printer_model": "bambu_a1",
            "material": "PETG",
            "nozzle_diameter_mm": 0.4,
            "outcome": "failed",
            "quality_grade": None,
            "failure_mode": "adhesion",
            "print_error": 50348044,
        }
    )
    assert record == {
        "printer_model": "bambu_a1",
        "material": "PETG",
        "nozzle_diameter_mm": 0.4,
        "outcome": "failed",
        "failure_mode": "adhesion",
        "print_error": 50348044,
    }


def test_the_one_door_queues_nothing_about_the_part(queued):
    result = community_outbox.contribute_print_outcome(
        outcome="completed",
        geometric_signature=SIGNATURE,
        geometric_signature_v2=SIGNATURE_V2,
        job_id="job-1",
        printer_model="Bambu A1",
        material="PLA",
        nozzle_diameter_mm=0.4,
        print_time_seconds=5400,
        extra={
            "settings": {"layer_height_mm": 0.2},
            "settings_hash": "abcd",
            "geometric_signature": SIGNATURE,
            "quality_grade": "B",
        },
    )
    assert result["contributed"] is True
    assert "signature" not in result
    (key, record), = queued
    assert key == "print:job-1"
    _nothing_about_the_part(record)
    assert record == {
        "printer_model": "bambu_a1",
        "material": "PLA",
        "nozzle_diameter_mm": 0.4,
        "outcome": "success",
        "quality_grade": "B",
    }


def test_a_print_with_no_model_file_still_shares_how_it_went(queued, monkeypatch):
    """The part's signature used to be required, so a print whose model
    Kiln could not find shared nothing at all."""

    def _never(*_a, **_k):
        raise AssertionError("the part was read for a shared print result")

    monkeypatch.setattr(community_autofire, "geometric_signatures_for", _never)
    result = community_autofire.auto_contribute_completion(
        outcome="completed",
        printer_file_name="bracket.gcode",
        job_id="job-2",
        printer_model="prusa_mk4",
        material="PETG",
        print_time_seconds=1200,
    )
    assert result["contributed"] is True
    (_key, record), = queued
    _nothing_about_the_part(record)
    assert record["outcome"] == "success"


def test_a_settled_later_print_shares_how_it_went(queued, monkeypatch):
    def _never(*_a, **_k):
        raise AssertionError("the part was read for a shared print result")

    monkeypatch.setattr(community_autofire, "geometric_signatures_for", _never)
    result = community_autofire.contribute_resolved_outcome(
        outcome="failed",
        printer_file_name="bracket.gcode",
        job_id="job-3",
        material="PLA",
        failure_mode="adhesion",
    )
    assert result["contributed"] is True
    (_key, record), = queued
    _nothing_about_the_part(record)
    assert record["failure_mode"] == "adhesion"


def test_a_print_nothing_names_is_not_shared(queued):
    result = community_outbox.contribute_print_outcome(outcome="completed", printer_model="x", material="PLA")
    assert result == {"contributed": False, "reason": "no_identity"}
    assert queued == []


def test_the_sender_strips_a_record_queued_by_an_older_release(monkeypatch):
    """A row already waiting in the queue from before this release carries
    the old fields; the sender is the last place they can be dropped."""
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(community_sync, "community_sharing_enabled", lambda: True)
    monkeypatch.setattr(community_sync, "network_sends_suppressed", lambda: False)
    monkeypatch.setattr(
        community_sync, "send_contribution", lambda kind, record, send_id=None: sent.append((kind, record)) or True
    )
    community_sync.sync_community_print(
        {
            "geometric_signature": SIGNATURE,
            "geometric_signature_v2": SIGNATURE_V2,
            "printer_model": "bambu_a1",
            "material": "PLA",
            "settings_hash": "abcd",
            "settings": {"layer_height_mm": 0.2},
            "outcome": "success",
            "print_time_seconds": 3600,
        }
    )
    (kind, record), = sent
    assert kind == "print"
    _nothing_about_the_part(record)
    assert record == {"printer_model": "bambu_a1", "material": "PLA", "outcome": "success"}


def test_asking_about_a_part_makes_no_request(monkeypatch):
    def _never(*_a, **_k):
        raise AssertionError("a part's signature was sent")

    monkeypatch.setattr(community_sync, "_ask_community_api", _never)
    monkeypatch.setattr(community_sync, "community_sharing_enabled", lambda: True)
    assert community_sync.fetch_community_insight_for_signature(SIGNATURE, SIGNATURE_V2) is None
